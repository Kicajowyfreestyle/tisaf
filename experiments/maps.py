import inspect
import os
import sys
import numpy as np

os.environ["CUDA_VISIBLE_DEVICES"] = "-1"
from models.maps import MapAE

currentdir = os.path.dirname(os.path.abspath(inspect.getfile(inspect.currentframe())))
parentdir = os.path.dirname(currentdir)
sys.path.insert(0, parentdir)

# add parent (root) to pythonpath
from dataset import scenarios
from models.planner import plan_loss, _plot, PlanningNetworkMP
from utils.utils import Environment
from dataset.scenarios import Task

from argparse import ArgumentParser

import tensorflow as tf
import tensorflow.contrib as tfc
from tqdm import tqdm

from dl_work.utils import ExperimentHandler, LoadFromFile

tf.enable_eager_execution()
tf.set_random_seed(444)

_tqdm = lambda t, s, i: tqdm(
    ncols=80,
    total=s,
    bar_format='%s epoch %d | {l_bar}{bar} | Remaining: {remaining}' % (t, i))


def _ds(title, ds, ds_size, i, batch_size):
    with _tqdm(title, ds_size, i) as pbar:
        for i, data in enumerate(ds):
            yield (i, data)
            pbar.update(batch_size)


def main(args):
    # 1. Get datasets
    train_ds, train_size = scenarios.planning_dataset(args.scenario_path)
    val_ds, val_size = scenarios.planning_dataset(args.scenario_path)

    #train_ds = train_ds \
    #    .batch(args.batch_size) \
    #    .prefetch(args.batch_size)

    val_ds = val_ds \
        .batch(args.batch_size) \
        .prefetch(args.batch_size)

    # 2. Define model
    model = MapAE()

    # 3. Optimization

    eta = tfc.eager.Variable(args.eta)
    eta_f = tf.train.exponential_decay(
        args.eta,
        tf.train.get_or_create_global_step(),
        int(float(train_size) / args.batch_size),
        args.train_beta)
    eta.assign(eta_f())
    optimizer = tf.train.AdamOptimizer(eta)
    #optimizer = tf.train.GradientDescentOptimizer(eta)
    l2_reg = tf.keras.regularizers.l2(1e-5)

    # 4. Restore, Log & Save
    experiment_handler = ExperimentHandler(args.working_path, args.out_name, args.log_interval, model, optimizer)
    experiment_handler.restore("./working_dir/map_net/checkpoints/best-283")

    #experiment_handler.restore("./results/I/checkpoints/last_n-36")

    # 5. Run everything
    train_step, val_step = 0, 0
    best_iou = 0.0
    iou = tf.keras.metrics.MeanIoU(2)
    for epoch in range(args.num_epochs):
        iou.reset_states()
        # workaround for tf problems with shuffling
        dataset_epoch = train_ds.shuffle(train_size)
        dataset_epoch = dataset_epoch.batch(args.batch_size).prefetch(args.batch_size)

        # 5.1. Training Loop
        experiment_handler.log_training()
        for i, data in _ds('Train', dataset_epoch, train_size, epoch, args.batch_size):
            # 5.1.1. Make inference of the model, calculate losses and record gradients
            with tf.GradientTape(persistent=True) as tape:
                target = data[3]
                output = model(data[3], training=True)
                loss = tf.losses.softmax_cross_entropy(target, output)
                reg_loss = tfc.layers.apply_regularization(l2_reg, model.trainable_variables)
                total_loss = loss + reg_loss

            # 5.1.2 Take gradients (if necessary apply regularization like clipping),
            grads = tape.gradient(total_loss, model.trainable_variables)
            optimizer.apply_gradients(zip(grads, model.trainable_variables),
                                      global_step=tf.train.get_or_create_global_step())

            output = tf.nn.softmax(output, -1)
            labels = tf.cast(target[:, :, :, :1] > 0.5, tf.float32)
            pred = tf.cast(output[:, :, :, :1] > 0.5, tf.float32)
            iou.update_state(labels, pred)

            # 5.1.4 Save logs for particular interval
            with tfc.summary.record_summaries_every_n_global_steps(args.log_interval, train_step):
                tfc.summary.image('images/input', target[:, :, :, :1], step=train_step)
                tfc.summary.image('images/output', pred, step=train_step)
                tfc.summary.image('images/raw_output', output[:, :, :, :1], step=train_step)
                tfc.summary.scalar('metrics/loss', total_loss, step=train_step)
                tfc.summary.scalar('metrics/model_loss', loss, step=train_step)
                tfc.summary.scalar('metrics/iou', iou.result(), step=train_step)
                tfc.summary.scalar('metrics/reg_loss', reg_loss, step=train_step)

            # 5.1.5 Update meta variables
            eta.assign(eta_f())
            train_step += 1

        # 5.1.6 Take statistics over epoch
        with tfc.summary.always_record_summaries():
            tfc.summary.scalar('epoch/iou', iou.result(), step=epoch)

        # 5.2. Validation Loop
        experiment_handler.log_validation()
        iou.reset_states()
        for i, data in _ds('Validation', val_ds, val_size, epoch, args.batch_size):
            # 5.2.1 Make inference of the model for validation and calculate losses
            target = data[3]
            output = model(data[3], training=True)
            loss = tf.losses.softmax_cross_entropy(target, output)

            output = tf.nn.softmax(output, -1)
            labels = tf.cast(target[:, :, :, :1] > 0.5, tf.float32)
            pred = tf.cast(output[:, :, :, :1] > 0.5, tf.float32)
            iou.update_state(labels, pred)

            # 5.2.3 Print logs for particular interval
            with tfc.summary.record_summaries_every_n_global_steps(args.log_interval, val_step):
                tfc.summary.image('images/input', target[:, :, :, :1], step=val_step)
                tfc.summary.image('images/output', pred, step=val_step)
                tfc.summary.image('images/raw_output', output[:, :, :, :1], step=val_step)
                tfc.summary.scalar('metrics/model_loss', loss, step=val_step)
                tfc.summary.scalar('metrics/iou', iou.result(), step=val_step)

            # 5.2.4 Update meta variables
            val_step += 1

        epoch_iou = iou.result()
        # 5.2.5 Take statistics over epoch
        with tfc.summary.always_record_summaries():
            tfc.summary.scalar('epoch/iou', epoch_iou , step=epoch)

        # 5.3 Save last and best
        if epoch_iou > best_iou:
            experiment_handler.save_best()
            best_accuracy = epoch_iou
        experiment_handler.save_last()

        experiment_handler.flush()


if __name__ == '__main__':
    parser = ArgumentParser()
    parser.add_argument('--config-file', action=LoadFromFile, type=open)
    parser.add_argument('--scenario-path', type=str)
    parser.add_argument('--working-path', type=str, default='./working_dir')
    parser.add_argument('--num-epochs', type=int)
    parser.add_argument('--batch-size', type=int)
    parser.add_argument('--log-interval', type=int, default=5)
    parser.add_argument('--out-name', type=str)
    parser.add_argument('--eta', type=float, default=5e-4)
    parser.add_argument('--train-beta', type=float, default=0.99)
    parser.add_argument('--augment', action='store_true', default=False)
    parser.add_argument('--width', type=int, default=640)
    parser.add_argument('--height', type=int, default=480)
    args, _ = parser.parse_known_args()
    main(args)
