#! /usr/bin/env python3
# -*- coding: utf-8 -*-

# Copyright 2018 Kyoto University (Hirofumi Inaguma)
#  Apache 2.0  (http://www.apache.org/licenses/LICENSE-2.0)

"""Train the ASR model."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import argparse
import copy
import cProfile
import editdistance
import logging
import numpy as np
import os
from setproctitle import setproctitle
import shutil
import time
import torch
from tqdm import tqdm

from neural_sp.bin.args_asr import parse
from neural_sp.bin.train_utils import load_checkpoint
from neural_sp.bin.train_utils import load_config
from neural_sp.bin.train_utils import save_checkpoint
from neural_sp.bin.train_utils import save_config
from neural_sp.bin.train_utils import set_logger
from neural_sp.bin.train_utils import set_save_path
from neural_sp.datasets.asr import Dataset
from neural_sp.evaluators.character import eval_char
from neural_sp.evaluators.phone import eval_phone
from neural_sp.evaluators.ppl import eval_ppl
from neural_sp.evaluators.word import eval_word
from neural_sp.evaluators.wordpiece import eval_wordpiece
from neural_sp.models.data_parallel import CustomDataParallel
from neural_sp.models.lm.build import build_lm
from neural_sp.models.seq2seq.acoustic_model import AcousticModel
from neural_sp.models.seq2seq.speech2text import Speech2Text
from neural_sp.trainers.lr_scheduler import LRScheduler
from neural_sp.trainers.model_name import set_asr_model_name
from neural_sp.trainers.optimizer import set_optimizer
from neural_sp.trainers.reporter import Reporter
from neural_sp.utils import mkdir_join

torch.manual_seed(1)
torch.cuda.manual_seed_all(1)

logger = logging.getLogger(__name__)


def main():

    args = parse()
    args_init = copy.deepcopy(args)
    args_teacher = copy.deepcopy(args)

    # Load a conf file
    if args.resume:
        conf = load_config(os.path.join(os.path.dirname(args.resume), 'conf.yml'))
        for k, v in conf.items():
            if k != 'resume':
                setattr(args, k, v)
    recog_params = vars(args)

    # Compute subsampling factor
    subsample_factor = 1
    subsample_factor_sub1 = 1
    subsample_factor_sub2 = 1
    subsample = [int(s) for s in args.subsample.split('_')]
    if args.conv_poolings and 'conv' in args.enc_type:
        for p in args.conv_poolings.split('_'):
            subsample_factor *= int(p.split(',')[0].replace('(', ''))
    else:
        subsample_factor = np.prod(subsample)
    if args.train_set_sub1:
        if args.conv_poolings and 'conv' in args.enc_type:
            subsample_factor_sub1 = subsample_factor * np.prod(subsample[:args.enc_n_layers_sub1 - 1])
        else:
            subsample_factor_sub1 = subsample_factor
    if args.train_set_sub2:
        if args.conv_poolings and 'conv' in args.enc_type:
            subsample_factor_sub2 = subsample_factor * np.prod(subsample[:args.enc_n_layers_sub2 - 1])
        else:
            subsample_factor_sub2 = subsample_factor

    # Set save path
    if args.resume:
        save_path = os.path.dirname(args.resume)
        dir_name = os.path.basename(save_path)
    else:
        dir_name = set_asr_model_name(args, subsample_factor)
        save_path = mkdir_join(args.model_save_dir, '_'.join(
            os.path.basename(args.train_set).split('.')[:-1]), dir_name)
        save_path = set_save_path(save_path)  # avoid overwriting

    # Set logger
    set_logger(os.path.join(save_path, 'train.log'), stdout=args.stdout)

    # for multi-GPUs
    if args.n_gpus > 1:
        logger.info("Batch size is automatically reduced from %d to %d" %
                    (args.batch_size, args.batch_size // 2))
        args.batch_size //= 2

    if args.resume:
        transformer = 'transformer' in conf['enc_type'] or conf['dec_type'] == 'transformer'
        if transformer and conf['optimizer'] != 'noam':
            logger.warning('Noam Optimizer is not set for Transformer.')
    else:
        transformer = 'transformer' in args.enc_type or args.dec_type == 'transformer'
        if transformer and args.optimizer != 'noam':
            logger.warning('Noam Optimizer is not set for Transformer.')

    # Load dataset
    train_set = Dataset(corpus=args.corpus,
                        tsv_path=args.train_set,
                        tsv_path_sub1=args.train_set_sub1,
                        tsv_path_sub2=args.train_set_sub2,
                        dict_path=args.dict,
                        dict_path_sub1=args.dict_sub1,
                        dict_path_sub2=args.dict_sub2,
                        nlsyms=args.nlsyms,
                        unit=args.unit,
                        unit_sub1=args.unit_sub1,
                        unit_sub2=args.unit_sub2,
                        wp_model=args.wp_model,
                        wp_model_sub1=args.wp_model_sub1,
                        wp_model_sub2=args.wp_model_sub2,
                        batch_size=args.batch_size * args.n_gpus,
                        n_epochs=args.n_epochs,
                        min_n_frames=args.min_n_frames,
                        max_n_frames=args.max_n_frames,
                        shuffle_bucket=args.shuffle_bucket,
                        sort_by='input',
                        short2long=args.sort_short2long,
                        sort_stop_epoch=args.sort_stop_epoch,
                        dynamic_batching=args.dynamic_batching,
                        ctc=args.ctc_weight > 0,
                        ctc_sub1=args.ctc_weight_sub1 > 0,
                        ctc_sub2=args.ctc_weight_sub2 > 0,
                        subsample_factor=subsample_factor,
                        subsample_factor_sub1=subsample_factor_sub1,
                        subsample_factor_sub2=subsample_factor_sub2,
                        discourse_aware=args.discourse_aware)
    dev_set = Dataset(corpus=args.corpus,
                      tsv_path=args.dev_set,
                      tsv_path_sub1=args.dev_set_sub1,
                      tsv_path_sub2=args.dev_set_sub2,
                      dict_path=args.dict,
                      dict_path_sub1=args.dict_sub1,
                      dict_path_sub2=args.dict_sub2,
                      nlsyms=args.nlsyms,
                      unit=args.unit,
                      unit_sub1=args.unit_sub1,
                      unit_sub2=args.unit_sub2,
                      wp_model=args.wp_model,
                      wp_model_sub1=args.wp_model_sub1,
                      wp_model_sub2=args.wp_model_sub2,
                      batch_size=args.batch_size * args.n_gpus,
                      min_n_frames=args.min_n_frames,
                      max_n_frames=args.max_n_frames,
                      ctc=args.ctc_weight > 0,
                      ctc_sub1=args.ctc_weight_sub1 > 0,
                      ctc_sub2=args.ctc_weight_sub2 > 0,
                      subsample_factor=subsample_factor,
                      subsample_factor_sub1=subsample_factor_sub1,
                      subsample_factor_sub2=subsample_factor_sub2,
                      discourse_aware=args.discourse_aware)
    eval_sets = [Dataset(corpus=args.corpus,
                         tsv_path=s,
                         dict_path=args.dict,
                         nlsyms=args.nlsyms,
                         unit=args.unit,
                         wp_model=args.wp_model,
                         batch_size=1,
                         discourse_aware=args.discourse_aware,
                         is_test=True) for s in args.eval_sets]

    args.vocab = train_set.vocab
    args.vocab_sub1 = train_set.vocab_sub1
    args.vocab_sub2 = train_set.vocab_sub2
    args.input_dim = train_set.input_dim

    # Load a LM conf file for LM fusion & LM initialization
    if not args.resume and (args.lm_fusion or args.lm_init):
        if args.lm_fusion:
            lm_conf = load_config(os.path.join(os.path.dirname(args.lm_fusion), 'conf.yml'))
        elif args.lm_init:
            lm_conf = load_config(os.path.join(os.path.dirname(args.lm_init), 'conf.yml'))
        args.lm_conf = argparse.Namespace()
        for k, v in lm_conf.items():
            setattr(args.lm_conf, k, v)
        assert args.unit == args.lm_conf.unit
        assert args.vocab == args.lm_conf.vocab

    # Model setting
    if args.am_pretrain_type:
        model = AcousticModel(args, save_path)
    else:
        model = Speech2Text(args, save_path)

    if args.resume:
        # Set optimizer
        epoch = int(args.resume.split('-')[-1])
        optimizer = set_optimizer(model, 'sgd' if epoch > conf['convert_to_sgd_epoch'] else conf['optimizer'],
                                  conf['lr'], conf['weight_decay'])

        # Wrap optimizer by learning rate scheduler
        optimizer = LRScheduler(optimizer, conf['lr'],
                                decay_type=conf['lr_decay_type'],
                                decay_start_epoch=conf['lr_decay_start_epoch'],
                                decay_rate=conf['lr_decay_rate'],
                                decay_patient_n_epochs=conf['lr_decay_patient_n_epochs'],
                                early_stop_patient_n_epochs=conf['early_stop_patient_n_epochs'],
                                warmup_start_lr=conf['warmup_start_lr'],
                                warmup_n_steps=conf['warmup_n_steps'],
                                model_size=conf['transformer_d_model'],
                                factor=conf['lr_factor'],
                                noam=transformer)

        # Restore the last saved model
        load_checkpoint(model, args.resume, optimizer)

        # Resume between convert_to_sgd_epoch -1 and convert_to_sgd_epoch
        if epoch == conf['convert_to_sgd_epoch']:
            optimizer.convert_to_sgd(model, args.lr, conf['weight_decay'],
                                     decay_type='always', decay_rate=0.5)
    else:
        # Save the conf file as a yaml file
        save_config(vars(args), os.path.join(save_path, 'conf.yml'))
        if args.lm_fusion:
            save_config(args.lm_conf, os.path.join(save_path, 'conf_lm.yml'))

        # Save the nlsyms, dictionar, and wp_model
        if args.nlsyms:
            shutil.copy(args.nlsyms, os.path.join(save_path, 'nlsyms.txt'))
        for sub in ['', '_sub1', '_sub2']:
            if getattr(args, 'dict' + sub):
                shutil.copy(getattr(args, 'dict' + sub), os.path.join(save_path, 'dict' + sub + '.txt'))
            if getattr(args, 'unit' + sub) == 'wp':
                shutil.copy(getattr(args, 'wp_model' + sub), os.path.join(save_path, 'wp' + sub + '.model'))

        for k, v in sorted(vars(args).items(), key=lambda x: x[0]):
            logger.info('%s: %s' % (k, str(v)))

        # Count total parameters
        for n in sorted(list(model.num_params_dict.keys())):
            n_params = model.num_params_dict[n]
            logger.info("%s %d" % (n, n_params))
        logger.info("Total %.2f M parameters" % (model.total_parameters / 1000000))
        logger.info(model)

        # Initialize with pre-trained model's parameters
        if args.asr_init:
            # Load the ASR model
            assert os.path.isfile(args.asr_init), 'There is no checkpoint.'
            conf_init = load_config(os.path.join(os.path.dirname(args.asr_init), 'conf.yml'))
            for k, v in conf_init.items():
                setattr(args_init, k, v)
            model_init = Speech2Text(args_init)
            load_checkpoint(model_init, args.asr_init)

            # Overwrite parameters
            # only_enc = (args.enc_n_layers != args_init.enc_n_layers) or (
            #     args.unit != args_init.unit) or args_init.ctc_weight == 1
            param_dict = dict(model_init.named_parameters())
            for n, p in model.named_parameters():
                if n in param_dict.keys() and p.size() == param_dict[n].size():
                    # if only_enc and 'enc' not in n:
                    #     continue
                    p.data = param_dict[n].data
                    logger.info('Overwrite %s' % n)

        # Set optimizer
        optimizer = set_optimizer(model, args.optimizer, args.lr, args.weight_decay)

        # Wrap optimizer by learning rate scheduler
        optimizer = LRScheduler(optimizer, args.lr,
                                decay_type=args.lr_decay_type,
                                decay_start_epoch=args.lr_decay_start_epoch,
                                decay_rate=args.lr_decay_rate,
                                decay_patient_n_epochs=args.lr_decay_patient_n_epochs,
                                early_stop_patient_n_epochs=args.early_stop_patient_n_epochs,
                                warmup_start_lr=args.warmup_start_lr,
                                warmup_n_steps=args.warmup_n_steps,
                                model_size=args.transformer_d_model,
                                factor=args.lr_factor,
                                noam=transformer)

    # Load the teacher ASR model
    teacher = None
    if args.teacher:
        assert os.path.isfile(args.teacher), 'There is no checkpoint.'
        conf_teacher = load_config(os.path.join(os.path.dirname(args.teacher), 'conf.yml'))
        for k, v in conf_teacher.items():
            setattr(args_teacher, k, v)
        # Setting for knowledge distillation
        args_teacher.ss_prob = 0
        args.lsm_prob = 0
        teacher = Speech2Text(args_teacher)
        load_checkpoint(teacher, args.teacher)

    # Load the teacher LM
    teacher_lm = None
    if args.teacher_lm:
        assert os.path.isfile(args.teacher_lm), 'There is no checkpoint.'
        conf_lm = load_config(os.path.join(os.path.dirname(args.teacher_lm), 'conf.yml'))
        args_lm = argparse.Namespace()
        for k, v in conf_lm.items():
            setattr(args_lm, k, v)
        teacher_lm = build_lm(args_lm)
        load_checkpoint(teacher_lm, args.teacher_lm)

    # GPU setting
    if args.n_gpus >= 1:
        torch.backends.cudnn.benchmark = True
        model = CustomDataParallel(model, device_ids=list(range(0, args.n_gpus)))
        model.cuda()
        if teacher is not None:
            teacher.cuda()
        if teacher_lm is not None:
            teacher_lm.cuda()

    # Set process name
    logger.info('PID: %s' % os.getpid())
    logger.info('USERNAME: %s' % os.uname()[1])
    setproctitle(args.job_name if args.job_name else dir_name)

    # Set reporter
    reporter = Reporter(save_path)

    if args.mtl_per_batch:
        # NOTE: from easier to harder tasks
        tasks = []
        if 1 - args.bwd_weight - args.ctc_weight - args.sub1_weight - args.sub2_weight > 0:
            tasks += ['ys']
        if args.bwd_weight > 0:
            tasks = ['ys.bwd'] + tasks
        if args.ctc_weight > 0:
            tasks = ['ys.ctc'] + tasks
        if args.mbr_weight > 0:
            tasks = ['ys.mbr'] + tasks
        for sub in ['sub1', 'sub2']:
            if getattr(args, 'train_set_' + sub):
                if getattr(args, sub + '_weight') - getattr(args, 'ctc_weight_' + sub) > 0:
                    tasks = ['ys_' + sub] + tasks
                if getattr(args, 'ctc_weight_' + sub) > 0:
                    tasks = ['ys_' + sub + '.ctc'] + tasks
    else:
        tasks = ['all']

    start_time_train = time.time()
    start_time_epoch = time.time()
    start_time_step = time.time()
    pbar_epoch = tqdm(total=len(train_set))
    accum_n_steps = 0
    n_steps = 0
    while True:
        # Compute loss in the training set
        batch_train, is_new_epoch = train_set.next()
        accum_n_steps += 1

        # Change mini-batch depending on task
        for task in tasks:
            loss, observation = model(batch_train, task,
                                      teacher=teacher, teacher_lm=teacher_lm)
            reporter.add(observation)
            loss.backward()
            loss.detach()  # Trancate the graph
            if args.accum_grad_n_steps == 1 or accum_n_steps >= args.accum_grad_n_steps:
                if args.clip_grad_norm > 0:
                    total_norm = torch.nn.utils.clip_grad_norm_(
                        model.module.parameters(), args.clip_grad_norm)
                    reporter.add_tensorboard_scalar('total_norm', total_norm)
                optimizer.step()
                optimizer.zero_grad()
                accum_n_steps = 0
            loss_train = loss.item()
            del loss

        reporter.add_tensorboard_scalar('learning_rate', optimizer.lr)
        # NOTE: loss/acc/ppl are already added in the model
        reporter.step()
        pbar_epoch.update(len(batch_train['utt_ids']))
        n_steps += 1

        if n_steps % args.print_step == 0:
            # Compute loss in the dev set
            # batch_dev = dev_set.next()[0]
            batch_dev = dev_set.next(batch_size=1 if 'transducer' in args.dec_type else None)[0]
            # Change mini-batch depending on task
            for task in tasks:
                loss, observation = model(batch_dev, task, is_eval=True)
                reporter.add(observation, is_eval=True)
                loss_dev = loss.item()
                del loss
            # NOTE: this makes training slow
            # Compute WER/CER regardless of the output unit (greedy decoding)
            # best_hyps_id, _, _ = model.module.decode(
            #     batch_dev['xs'], recog_params, dev_set.idx2token[0], exclude_eos=True)
            # cer = 0.
            # ref_n_words, ref_n_chars = 0, 0
            # for b in range(len(batch_dev['xs'])):
            #     ref = batch_dev['text'][b]
            #     hyp = dev_set.idx2token[0](best_hyps_id[b])
            #     cer += editdistance.eval(hyp, ref)
            #     ref_n_words += len(ref.split())
            #     ref_n_chars += len(ref)
            # wer = cer / ref_n_words
            # cer /= ref_n_chars
            # reporter.add_tensorboard_scalar('dev/WER', wer)
            # reporter.add_tensorboard_scalar('dev/CER', cer)
            # logger.info('WER (dev)', wer)
            # logger.info('CER (dev)', cer)
            reporter.step(is_eval=True)

            duration_step = time.time() - start_time_step
            if args.input_type == 'speech':
                xlen = max(len(x) for x in batch_train['xs'])
                ylen = max(len(y) for y in batch_train['ys'])
            elif args.input_type == 'text':
                xlen = max(len(x) for x in batch_train['ys'])
                ylen = max(len(y) for y in batch_train['ys_sub1'])
            logger.info("step:%d(ep:%.2f) loss:%.3f(%.3f)/lr:%.7f/bs:%d/xlen:%d/ylen:%d (%.2f min)" %
                        (n_steps, optimizer.n_epochs + train_set.epoch_detail,
                         loss_train, loss_dev,
                         optimizer.lr, len(batch_train['utt_ids']),
                         xlen, ylen, duration_step / 60))
            start_time_step = time.time()

        # Save fugures of loss and accuracy
        if n_steps % (args.print_step * 10) == 0:
            reporter.snapshot()
            model.module.plot_attention()

        # Save checkpoint and evaluate model per epoch
        if is_new_epoch:
            duration_epoch = time.time() - start_time_epoch
            logger.info('========== EPOCH:%d (%.2f min) ==========' %
                        (optimizer.n_epochs + 1, duration_epoch / 60))

            if optimizer.n_epochs + 1 < args.eval_start_epoch:
                optimizer.epoch()  # lr decay
                reporter.epoch()  # plot

                # Save the model
                save_checkpoint(model, optimizer, save_path,
                                remove_old_checkpoints=not transformer)
            else:
                start_time_eval = time.time()
                # dev
                metric_dev = eval_epoch([model.module], dev_set, recog_params, args,
                                        optimizer.n_epochs + 1, logger)
                optimizer.epoch(metric_dev)  # lr decay
                reporter.epoch(metric_dev)  # plot

                if optimizer.is_best:
                    # Save the model
                    save_checkpoint(model, optimizer, save_path,
                                    remove_old_checkpoints=not transformer)

                    # test
                    for eval_set in eval_sets:
                        eval_epoch([model.module], eval_set, recog_params, args,
                                   optimizer.n_epochs, logger)

                    # start scheduled sampling
                    if args.ss_prob > 0:
                        model.module.scheduled_sampling_trigger()

                duration_eval = time.time() - start_time_eval
                logger.info('Evaluation time: %.2f min' % (duration_eval / 60))

                # Early stopping
                if optimizer.is_early_stop:
                    break

                # Convert to fine-tuning stage
                if optimizer.n_epochs == args.convert_to_sgd_epoch:
                    optimizer.convert_to_sgd(model, args.lr, args.weight_decay,
                                             decay_type='always', decay_rate=0.5)

            pbar_epoch = tqdm(total=len(train_set))

            if optimizer.n_epochs == args.n_epochs:
                break

            start_time_step = time.time()
            start_time_epoch = time.time()

    duration_train = time.time() - start_time_train
    logger.info('Total time: %.2f hour' % (duration_train / 3600))

    reporter.tf_writer.close()
    pbar_epoch.close()

    return save_path


def eval_epoch(models, dataset, recog_params, args, epoch, logger):
    if args.metric == 'edit_distance':
        if args.unit in ['word', 'word_char']:
            metric = eval_word(models, dataset, recog_params, epoch=epoch)[0]
            logger.info('WER (%s, ep:%d): %.2f %%' % (dataset.set, epoch, metric))
        elif args.unit == 'wp':
            metric, cer = eval_wordpiece(models, dataset, recog_params, epoch=epoch)
            logger.info('WER (%s, ep:%d): %.2f %%' % (dataset.set, epoch, metric))
            logger.info('CER (%s, ep:%d): %.2f %%' % (dataset.set, epoch, cer))
        elif 'char' in args.unit:
            metric, cer = eval_char(models, dataset, recog_params, epoch=epoch)
            logger.info('WER (%s, ep:%d): %.2f %%' % (dataset.set, epoch, metric))
            logger.info('CER (%s, ep:%d): %.2f %%' % (dataset.set, epoch, cer))
        elif 'phone' in args.unit:
            metric = eval_phone(models, dataset, recog_params, epoch=epoch)
            logger.info('PER (%s, ep:%d): %.2f %%' % (dataset.set, epoch, metric))
    elif args.metric == 'ppl':
        metric = eval_ppl(models, dataset, batch_size=args.batch_size)[0]
        logger.info('PPL (%s, ep:%d): %.2f' % (dataset.set, epoch, metric))
    elif args.metric == 'loss':
        metric = eval_ppl(models, dataset, batch_size=args.batch_size)[1]
        logger.info('Loss (%s, ep:%d): %.2f' % (dataset.set, epoch, metric))
    else:
        raise NotImplementedError(args.metric)
    return metric


if __name__ == '__main__':
    # Setting for profiling
    pr = cProfile.Profile()
    save_path = pr.runcall(main)
    pr.dump_stats(os.path.join(save_path, 'train.profile'))
