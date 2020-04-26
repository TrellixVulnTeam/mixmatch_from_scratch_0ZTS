import time
import datetime
import random
import numpy as np
from torch.nn import CrossEntropyLoss
import torch.nn.functional as F

import torch
import torch.nn as nn

from utils import *

import pdb


class Trainer():
    def __init__(
            self, 
            model=None, optimizer=None, device=None, scheduler=None,
            train_loader=None, val_loader=None, unsup_loader=None,
            cfg=None, num_labels=None
        ):
        self.model = model
        self.optimizer = optimizer
        self.device = device
        self.scheduler = scheduler
        self.train_loader = train_loader
        self.val_loader = val_loader
        self.unsup_loader = unsup_loader
        self.cfg = cfg
        self.num_labels = num_labels

    def seed_torch(self, seed):
        random.seed(seed)
        np.random.seed(seed)
        torch.manual_seed(seed)
        torch.cuda.manual_seed(seed)
        torch.cuda.manual_seed_all(seed) # if you are using multi-GPU.
        torch.backends.cudnn.benchmark = False
        torch.backends.cudnn.deterministic = True

    # TSA
    def get_tsa_thresh(schedule, current, rampup_length, start, end):
        training_progress = torch.tensor(float(current) / float(rampup_length))
        if schedule == 'linear_schedule':
            threshold = training_progress
        elif schedule == 'exp_schedule':
            scale = 5
            threshold = torch.exp((training_progress - 1) * scale)
        elif schedule == 'log_schedule':
            scale = 5
            threshold = 1 - torch.exp((-training_progress) * scale)
        output = threshold * (end - start) + start
        return output.to(_get_device())


    def linear_rampup(self, current):
        rampup_length = self.cfg.epochs

        if rampup_length == 0:
            return 1.0
        else:
            current = np.clip(current / rampup_length, 0.0, 1.0)
            return float(current)

    def semi_loss(self, outputs_x, targets_x, outputs_u, targets_u, current_epoch):
        probs_u = torch.softmax(outputs_u, dim=1)

        Lx = -torch.mean(torch.sum(F.log_softmax(outputs_x, dim=1) * targets_x, dim=1))
        Lu = torch.mean((probs_u - targets_u)**2)

        return Lx, Lu, self.cfg.lambda_u * self.linear_rampup(current_epoch)

    def train_uda(self, epoch):
        t0 = time.time()

        model = self.model
        optimizer = self.optimizer
        device = self.device
        scheduler = self.scheduler
        train_loader = self.train_loader
        unsup_loader = self.unsup_loader
        cfg = self.cfg

        labeled_train_iter = iter(train_loader)

        total_train_loss = 0
        total_sup_loss = 0
        total_unsup_loss = 0

        model.train()

        for step, batch in enumerate(unsup_loader):

            model.zero_grad() 

            #batch
            try:
                input_ids, input_mask, segment_ids, label_ids, num_tokens = labeled_train_iter.next()
            except:
                labeled_train_iter = iter(train_loader)
                input_ids, input_mask, segment_ids, label_ids, num_tokens = labeled_train_iter.next()

            ori_input_ids, ori_segment_ids, ori_input_mask, \
            aug_input_ids, aug_segment_ids, aug_input_mask  = batch

            input_ids = torch.cat((input_ids, aug_input_ids), dim=0)
            segment_ids = torch.cat((segment_ids, aug_segment_ids), dim=0)
            input_mask = torch.cat((input_mask, aug_input_mask), dim=0)

            #logits
            logits = model(input_ids=input_ids, attention_mask=input_mask)

            #sup loss
            sup_criterion = nn.CrossEntropyLoss(reduction='none')
            current = float(epoch) + float(step/len(unsup_loader))
            rampup_length = float(cfg.epochs)

            sup_size = label_ids.shape[0]            
            sup_loss = sup_criterion(logits[:sup_size], label_ids)  # shape : train_batch_size
            if cfg.tsa:
                tsa_thresh = get_tsa_thresh(cfg.tsa, current, rampup_length, start=1./logits.shape[-1], end=1)
                larger_than_threshold = torch.exp(-sup_loss) > tsa_thresh   # prob = exp(log_prob), prob > tsa_threshold
                # larger_than_threshold = torch.sum(  F.softmax(pred[:sup_size]) * torch.eye(num_labels)[sup_label_ids]  , dim=-1) > tsa_threshold
                loss_mask = torch.ones_like(label_ids, dtype=torch.float32) * (1 - larger_than_threshold.type(torch.float32))
                sup_loss = torch.sum(sup_loss * loss_mask, dim=-1) / torch.max(torch.sum(loss_mask, dim=-1), torch_device_one())
            else:
                sup_loss = torch.mean(sup_loss)

            total_train_loss += sup_loss.item()

            loss.backward()

            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)

            optimizer.step()

            scheduler.step()

        # Calculate the average loss over all of the batches.
        avg_train_loss = total_train_loss / len(unsup_loader)   
        
        # Measure how long this epoch took.
        training_time = format_time(time.time() - t0)

        print("")
        print("  Average training loss: {0:.2f}".format(avg_train_loss))
        print("  Training epcoh took: {:}".format(training_time))

        return avg_train_loss, training_time

    def train_mixmatch(self, epoch):
        t0 = time.time()

        model = self.model
        optimizer = self.optimizer
        device = self.device
        scheduler = self.scheduler
        train_loader = self.train_loader
        unsup_loader = self.unsup_loader
        cfg = self.cfg

        labeled_train_iter = iter(train_loader)

        total_train_loss = 0
        total_sup_loss = 0
        total_unsup_loss = 0

        model.train()

        for step, batch in enumerate(unsup_loader):
            try:
                sup_ids, sup_mask, sup_seg, sup_labels, sup_num_tokens = labeled_train_iter.next()
            except:
                labeled_train_iter = iter(train_loader)
                sup_ids, sup_mask, sup_seg, sup_labels, sup_num_tokens = labeled_train_iter.next()

            ori_ids, ori_mask, ori_seg, aug_ids, aug_mask, aug_seg = batch

            batch_size = sup_ids.size(0)

            model.zero_grad()
            
            #convert label_ids to hot vector
            sup_labels = torch.zeros(batch_size, self.num_labels).scatter_(1, sup_labels.view(-1,1), 1).cuda()

            # compute guessed labels of unlabeled samples:
            with torch.no_grad():
                outputs_u = model(input_ids=ori_ids, attention_mask=ori_mask)
                outputs_u2 = model(input_ids=aug_ids, attention_mask=aug_mask)
                p = (torch.softmax(outputs_u, dim=1) + torch.softmax(outputs_u2, dim=1)) / 2
                pt = p**(1/cfg.T)
                targets_u = pt / pt.sum(dim=1, keepdim=True)
                targets_u = targets_u.detach()

            unsup_labels = torch.cat([targets_u, targets_u], dim=0)

            all_ids = torch.cat([sup_ids, ori_ids, aug_ids], dim=0).to(device)
            all_mask = torch.cat([sup_mask, ori_mask, aug_mask], dim=0).to(device)

            all_logits = model(input_ids=all_ids, attention_mask=all_mask)

            Lx, Lu, w = self.semi_loss(
                all_logits[:batch_size],
                sup_labels,
                all_logits[batch_size:],
                unsup_labels,
                epoch + step/len(unsup_loader)
            )

            loss = Lx + w * Lu
            #loss = Lx

            total_train_loss += loss.item()
            total_sup_loss += Lx.item()
            total_unsup_loss += Lu.item()

            # Perform a backward pass to calculate the gradients.
            loss.backward()

            # Clip the norm of the gradients to 1.0.
            # This is to help prevent the "exploding gradients" problem.
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)

            optimizer.step()

            # Update the learning rate.
            scheduler.step()

        # Calculate the average loss over all of the batches.
        avg_train_loss = total_train_loss / len(unsup_loader)
        avg_sup_loss = total_sup_loss / len(unsup_loader)
        avg_unsup_loss = total_unsup_loss / len(unsup_loader)
        
        # Measure how long this epoch took.
        training_time = format_time(time.time() - t0)

        print("")
        print("  Average training loss: {0:.2f}".format(avg_train_loss))
        print("  Average sup loss: {0:.2f}".format(avg_sup_loss))
        print("  Average unsup loss: {0:.2f}".format(avg_unsup_loss))
        print("  Training epcoh took: {:}".format(training_time))

        return avg_train_loss, training_time


    def train(self):
        # Measure how long the training epoch takes.
        t0 = time.time()

        model = self.model
        optimizer = self.optimizer
        device = self.device
        scheduler = self.scheduler
        train_loader = self.train_loader
        cfg = self.cfg

        total_train_loss = 0
        model.train()

        # For each batch of training data...
        for step, batch in enumerate(train_loader):

            # Progress update every 40 batches.
            if step % 40 == 0 and not step == 0:
                # Calculate elapsed time in minutes.
                elapsed = format_time(time.time() - t0)
            
                # Report progress.
                print('  Batch {:>5,}  of  {:>5,}.    Elapsed: {:}.'.format(step, len(train_loader), elapsed))

            b_input_ids, b_input_mask, b_segment_ids, b_labels, b_num_tokens = batch
            
            batch_size = b_input_ids.size(0)

            model.zero_grad()        

            # convert label_ids to hot vector
            label_ids = torch.zeros(batch_size, self.num_labels).scatter_(1, b_labels.view(-1,1), 1).cuda()

            sup_l = np.random.beta(cfg.alpha, cfg.alpha)
            sup_l = max(sup_l, 1-sup_l)
            sup_idx = torch.randperm(batch_size)

            c_input_ids = b_input_ids.clone()

            if cfg.mixup == 'word':
                for i in range(0, batch_size):
                    j = sup_idx[i]
                    i_count = int(b_num_tokens[i])
                    j_count = int(b_num_tokens[j])

                    if i_count < j_count:
                        small = i
                        big = j
                        small_count = i_count
                        big_count = j_count
                        small_ids = b_input_ids
                        big_ids = c_input_ids
                    elif i_count > j_count:
                        small = j
                        big = i
                        small_count = j_count
                        big_count = i_count
                        small_ids = c_input_ids
                        big_ids = b_input_ids

                    if i_count != j_count:
                        first = small_ids[small][0:small_count-1]
                        second = torch.tensor([1] * (big_count - small_count))
                        third = big_ids[big][big_count-1:128]
                        combined = torch.cat((first, second, third), 0)
                        small_ids[small] = combined
                        if i_count < j_count:
                            b_input_mask[i] = b_input_mask[j]
            
            #for i in range(0, batch_size):
            #    new_mask = b_input_mask[i]
            #    new_ids = b_input_ids[i]
            #    old_ids = c_input_ids[i]
            #    pdb.set_trace()

            b_input_ids = b_input_ids.to(device)
            b_input_mask = b_input_mask.to(device)
            b_segment_ids = b_segment_ids.to(device)
            b_num_tokens = b_num_tokens.to(device)

            sup_logits = model(
                input_ids=b_input_ids,
                c_input_ids=c_input_ids,
                attention_mask=b_input_mask,
                mixup=cfg.mixup,
                shuffle_idx=sup_idx,
                l=sup_l
            )

            if cfg.mixup:
                sup_label_a, sup_label_b = label_ids, label_ids[sup_idx]
                label_ids = sup_l * sup_label_a + (1 - sup_l) * sup_label_b

            loss = -torch.sum(F.log_softmax(sup_logits, dim=1) * label_ids, dim=1)
            loss = torch.mean(loss)

            total_train_loss += loss.item()

            # Perform a backward pass to calculate the gradients.
            loss.backward()

            # Clip the norm of the gradients to 1.0.
            # This is to help prevent the "exploding gradients" problem.
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)

            optimizer.step()

            # Update the learning rate.
            scheduler.step()

        # Calculate the average loss over all of the batches.
        avg_train_loss = total_train_loss / len(train_loader)   
        
        # Measure how long this epoch took.
        training_time = format_time(time.time() - t0)

        print("")
        print("  Average training loss: {0:.2f}".format(avg_train_loss))
        print("  Training epcoh took: {:}".format(training_time))

        return avg_train_loss, training_time
    

    def validate(self):
        t0 = time.time()

        model = self.model
        device = self.device
        val_loader = self.val_loader
        cfg = self.cfg

        # Put the model in evaluation mode--the dropout layers behave differently
        # during evaluation.
        model.eval()

        # Tracking variables 
        total_eval_accuracy = 0
        total_eval_loss = 0
        nb_eval_steps = 0
        total_prec1 = 0
        total_prec5 = 0

        # Evaluate data for one epoch
        for batch in val_loader:
                
            b_input_ids, b_input_mask, b_segment_ids, b_labels, b_num_tokens = batch
            batch_size = b_input_ids.size(0)

            b_input_ids = b_input_ids.to(device)
            b_input_mask = b_input_mask.to(device)
            b_segment_ids = b_segment_ids.to(device)
            b_labels = b_labels.to(device)


            with torch.no_grad():        
                logits = model(
                    input_ids=b_input_ids,
                    attention_mask=b_input_mask
                )
                    
                loss_fct = CrossEntropyLoss()
                loss = loss_fct(logits, b_labels)
            # Accumulate the validation loss.
            total_eval_loss += loss.item()

            # Calculate the accuracy for this batch of test sentences, and
            # accumulate it over all batches.

            if self.num_labels == 2:
                logits = logits.detach().cpu().numpy()
                b_labels = b_labels.to('cpu').numpy()
                total_prec1 += bin_accuracy(logits, b_labels)
            else:
                prec1, prec5 = multi_accuracy(logits, b_labels, topk=(1,5))
                total_prec1 += prec1
                total_prec5 += prec5

        avg_prec1 = total_prec1 / len(val_loader)
        avg_prec5 = total_prec5 / len(val_loader)

        avg_val_loss = total_eval_loss / len(val_loader)
        
        # Report the final accuracy for this validation run.
        print("  Top 1 Accuracy: {0:.4f}".format(avg_prec1))
        print("  Top 5 Accuracy: {0:.4f}".format(avg_prec5))

        # Measure how long the validation run took.
        validation_time = format_time(time.time() - t0)

        print("  Validation Loss: {0:.2f}".format(avg_val_loss))
        print("  Validation took: {:}".format(validation_time))


        return avg_prec1, avg_prec5, avg_val_loss, validation_time

    def iterate(self, epochs):
        cfg = self.cfg

        # Set the seed value all over the place to make this reproducible.        
        self.seed_torch(cfg.seed)

        # We'll store a number of quantities such as training and validation loss, 
        # validation accuracy, and timings.
        training_stats = []

        # Measure the total training time for the whole run.
        total_t0 = time.time()

        best_val_acc = 0
        best_epoch = 0
        best_train_loss = None
        best_val_loss = None

        for epoch_i in range(0, epochs):
            model = self.model
            device = self.device
            val_loader = self.val_loader
    
            # ========================================
            #               Training
            # ========================================
    
            # Perform one full pass over the training set.

            print("")
            print('======== Epoch {:} / {:} ========'.format(epoch_i + 1, epochs))
            print('Training...')

            if self.cfg.uda:
                avg_train_loss, training_time = self.train_uda(epoch_i)
            if self.cfg.mixmatch:
                avg_train_loss, training_time = self.train_mixmatch(epoch_i)
            else:
                avg_train_loss, training_time = self.train()
    
        
            # ========================================
            #               Validation
            # ========================================
            # After the completion of each training epoch, measure our performance on
            # our validation set.

            print("")
            print("Running Validation...")

        
            avg_prec1, avg_prec5, avg_val_loss, validation_time = self.validate()

    
            # update best val accuracy
            if avg_prec1 > best_val_acc:
                best_val_acc = avg_prec1
                best_epoch = epoch_i
                best_train_loss = avg_train_loss
                best_val_loss = avg_val_loss


            # Record all statistics from this epoch.
            training_stats.append(
                {
                    'epoch': epoch_i + 1,
                    'Training Loss': avg_train_loss * 100,
                    'Valid. Loss': avg_val_loss * 100,
                    'Valid. Accur_top1.': avg_prec1,
                    'Valid. Accur_top5.': avg_prec5,
                    'Training Time': training_time,
                    'Validation Time': validation_time
                }
            )

        print("")
        print("Training complete!")

        print("Total training took {:} (h:mm:ss)".format(format_time(time.time()-total_t0)))
        print("Best Epoch: {}".format(best_epoch))
        print("Best Training Loss: {}".format(best_train_loss))
        print("Best Val Loss: {}".format(best_val_loss))
        print("Best Validation Accuracy: {0:.4f}".format(best_val_acc))
