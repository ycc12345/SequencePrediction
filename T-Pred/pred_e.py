from __future__ import print_function
import os
import sys
import time
import datetime
import random
import argparse
import tensorflow as tf
import numpy as np
import utils
import read_data
import model_config
import logging

os.environ['CUDA_VISIBLE_DEVICES'] = '0'
FORMAT = "%(asctime)s - [line:%(lineno)s - %(funcName)10s() ] %(message)s"

'''remember to change the vocab size '''

event_file = './T-pred-Dataset/CIKM16_event.txt'
time_file = './T-pred-Dataset/CIKM16_time.txt'

logging.basicConfig(filename='log/{}-{}-{}.log'.format('Pred_e', 'CIKM16', str(datetime.datetime.now())),
            level=logging.INFO, format=FORMAT)

handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter(FORMAT))
logging.getLogger().addHandler(handler)
logging.info('Start {}'.format('RECSYS15'))


for k, v in locals().items():
    logging.info('{}  {}'.format(k, v))

def parse_time():
    return time.strftime("%Y.%m.%d-%H:%M:%S", time.localtime())


def print_in_file(sstr):
    sys.stdout.write(str(sstr) + '\n')
    sys.stdout.flush()
    os.fsync(sys.stdout)


def make_noise(shape):
    return tf.random_normal(shape)


class T_Pred(object):
    def __init__(self, config, cell_type, event_file, time_file, is_training):
        self.alpha = 1.0
        self.cell_type = cell_type
        self.event_file = event_file
        self.time_file = time_file
        self.num_layers = config.num_layers
        self.hidden_size = config.hidden_size
        self.g_size = config.g_size
        self.filter_output_dim = config.filter_output_dim
        self.filter_size = config.filter_size
        self.batch_size = config.batch_size
        self.num_steps = config.num_steps
        self.n_g = config.num_gen
        self.is_training = is_training
        self.keep_prob = config.keep_prob
        self.res_rate = config.res_rate
        self.length = 1 # config.length
        self.vocab_size = 122911 # config.vocab_size
        self.learning_rate = config.learning_rate
        self.lr = config.learning_rate
        self.LAMBDA = config.LAMBDA
        self.gamma = config.gamma
        self.train_data, self.valid_data, self.test_data = read_data.data_split(
            event_file, time_file, shuffle=False)

        self.sample_t = tf.placeholder(tf.float32, [self.batch_size, self.num_steps + self.length])
        self.target_t = tf.placeholder(tf.float32, [self.batch_size, self.length])
        self.inputs_t = tf.placeholder(tf.float32, [self.batch_size, self.num_steps])
        self.targets_e = tf.placeholder(tf.int64, [self.batch_size, self.length])
        self.input_e = tf.placeholder(tf.int64, [self.batch_size, self.num_steps])
        self.build()

    def encoder_e(self, cell_type, inputs):
        with tf.variable_scope('Generator'):
            outputs_e = utils.build_encoder_graph_gru(
                inputs,
                self.hidden_size,
                self.num_layers,
                self.batch_size,
                self.num_steps,
                self.keep_prob,
                self.is_training,
                "Encoder_e" + cell_type)
            hidden_re = [tf.expand_dims(output_e, 1) for output_e in outputs_e]
            hidden_re = tf.concat(hidden_re, 1)
            return hidden_re

    def g_event(self, hidden_r, name=''):
        """
        The generative model for time and event
        mode:
        1. use the concatenated hidden representation for each time step
        2. use the unfolded hidden representation separately for each time step
        """

        with tf.variable_scope("Generator_E" + name):
            outputs = utils.build_rnn_graph_decoder1(
                hidden_r,
                self.num_layers,
                self.g_size,
                self.batch_size,
                self.length,
                "G_E.RNN")
            output = tf.reshape(tf.concat(outputs, 1), [-1, self.g_size])
            output = utils.linear('G_E.Output', self.g_size, self.vocab_size, output)
            logits = tf.reshape(output, [self.batch_size, self.length, self.vocab_size])
            return logits

    def params_with_name(self, name):
        variables = tf.get_collection(tf.GraphKeys.GLOBAL_VARIABLES)
        return [v for v in variables if name in v.name]

    def params_all(self):
        variables = tf.get_collection(tf.GraphKeys.GLOBAL_VARIABLES)
        return variables

    def loss(self, pred_e, real_e):

        gen_e_cost = tf.contrib.seq2seq.sequence_loss(pred_e, real_e, weights=tf.ones([self.batch_size, self.length]),
                                                      name="SeqLoss")
        '''The separate training of Generator and Discriminator'''
        gen_params = self.params_all()

        '''Use the Adam Optimizer to update the variables'''
        gen_train_op = tf.train.AdamOptimizer(learning_rate=1e-4, beta1=0.5, beta2=0.9).minimize(gen_e_cost, var_list=gen_params)

        '''Use the RMSProp Optimizer to update the variables'''
        # gen_train_op = tf.train.RMSPropOptimizer(learning_rate=self.lr).minimize(gen_e_cost, var_list=gen_params)

        return gen_train_op, gen_e_cost

    def build(self):
        """
        build the model
        define the loss function
        define the optimization method
        """
        logging.info('VOCAB_SIZE {}, MAX_LEN {}, LENGTH {}'.format(self.vocab_size, self.num_steps, self.length))
        embeddings = tf.get_variable(
            "embedding", [self.vocab_size, self.hidden_size], dtype=tf.float32)
        inputs_e = tf.nn.embedding_lookup(embeddings, self.input_e)

        hidden_re = self.encoder_e(self.cell_type, inputs_e)

        pred_e = self.g_event(tf.reshape(hidden_re, [self.batch_size, -1]))
        # self.pred_e = self.g_event(output_re)
        # use the extracted feature from input events and timestamps to generate the time sequence

        gen_train_op, gen_e_cost = self.loss(
            pred_e,
            self.targets_e)

        self.g_train_op = gen_train_op
        self.gen_e_cost = gen_e_cost
        self.pred_e = pred_e

        logging.info('SHAPE OF Pred_e {} targets_e {}'.format(pred_e.get_shape(), self.targets_e.get_shape()))

        # Hit@k, MRR@k & Recall@k
        MRR10 = 0.0
        Recall= 0.
        rate_sum = 0

        hit_matrix = tf.math.in_top_k(
            tf.reshape(pred_e, [self.batch_size*self.length, -1]),
            tf.reshape(self.targets_e, [self.batch_size*self.length]),
            k=10
        )
        self.hit_count = tf.math.count_nonzero(hit_matrix)

        # MRR@k
        self.batch_precision, self.batch_precision_op = tf.metrics.average_precision_at_k(
            labels=self.targets_e, predictions=pred_e, k=10, name='precision_k')
        # Isolate the variables stored behind the scenes by the metric operation
        self.running_precision_vars = tf.get_collection(tf.GraphKeys.LOCAL_VARIABLES, scope="precision_k")
        # Define initializer to initialize/reset running variables
        self.running_precision_vars_initializer = tf.variables_initializer(var_list=self.running_precision_vars)

        # Recall@k
        self.batch_recall, self.batch_recall_op = tf.metrics.recall_at_k(
            labels=self.targets_e, predictions=pred_e, k=10, name='recall_k')
        # Isolate the variables stored behind the scenes by the metric operation
        self.running_recall_vars = tf.get_collection(tf.GraphKeys.LOCAL_VARIABLES, scope="recall_k")
        # Define initializer to initialize/reset running variables
        self.running_recall_vars_initializer = tf.variables_initializer(var_list=self.running_recall_vars)

        self.saver = tf.train.Saver(max_to_keep=None)

    def train(self, sess, args):
        self.logdir = args.logdir + parse_time()
        while os.path.exists(self.logdir):
            time.sleep(random.randint(1, 5))
            self.logdir = args.logdir + parse_time()
        os.makedirs(self.logdir)

        if not os.path.exists('%s/logs' % self.logdir):
            os.makedirs('%s/logs' % self.logdir)

        if args.weights is not None:
            self.saver.restore(sess, args.weights)

        self.lr = self.learning_rate

        for epoch in range(args.iters):
            '''training'''
            sess.run([self.running_precision_vars_initializer, self.running_recall_vars_initializer])

            if epoch > 0 and epoch % (args.iters // 5) == 0:
                self.lr = self.lr * 2. / 3

            # re initialize the metric variables of metric.precision and metric.recall,
            # to calculate these metric for each epoch
            batch_precision, batch_recall = 0.0, 0.0

            sum_iter = 0.0

            i_e, t_e, i_t, t_t = read_data.data_iterator(self.train_data,self.num_steps, self.length)

            sample_t = read_data.generate_sample_t(self.batch_size, i_t, t_t)

            i = 0
            hit_sum = 0.0
            batch_num = len(list(read_data.generate_batch(self.batch_size, i_e, t_e, i_t, t_t)))
            logging.info("Total Batch Number {}".format(batch_num))

            for e_x, e_y, t_x, t_y in read_data.generate_batch(self.batch_size, i_e, t_e, i_t, t_t):

                feed_dict = {
                    self.input_e: e_x,
                    self.inputs_t: np.maximum(np.log(t_x), 0),
                    self.target_t: t_y,
                    self.targets_e: e_y,
                    self.sample_t: np.maximum(np.log(sample_t), 0)}

                _, gen_e_cost, hit_count, batch_precision, batch_recall, = sess.run([
                    self.g_train_op,
                    self.gen_e_cost,
                    self.hit_count,
                    self.batch_precision_op,
                    self.batch_recall_op],
                    feed_dict=feed_dict)
                sum_iter = sum_iter + 1
                hit_sum += hit_count
                # if self.cell_type == 'T_LSTMCell':
                #     sess.run(self.clip_op)

                if i % (batch_num // 10) == 0:
                    logging.info('[epoch: {}, {}] hit10: {}, gen_e_loss: {}, precision: {}, recall: {}'.format(
                        epoch, float(i) / batch_num, hit_sum / (sum_iter*self.batch_size*self.length),
                        gen_e_cost, batch_precision, batch_recall))
                i += 1

            '''evaluation'''

            # re initialize the metric variables of metric.precision and metric.recall,
            # to calculate these metric for each epoch

            i_e, t_e, i_t, t_t = read_data.data_iterator(
                self.valid_data,
                self.num_steps,
                self.length)

            sample_t = read_data.generate_sample_t(
                self.batch_size,
                i_t,
                t_t)

            sum_iter = 0.0
            hit_sum = 0.0
            i = 0

            self.lr = self.learning_rate
            batch_num = len(list(read_data.generate_batch(self.batch_size,i_e,t_e,i_t,t_t)))
            logging.info('Total Batch Number For Evaluation {}'.format(batch_num))

            for e_x, e_y, t_x, t_y in read_data.generate_batch(self.batch_size,i_e,t_e,i_t,t_t):

                feed_dict = {
                    self.input_e: e_x,
                    self.inputs_t: np.maximum(np.log(t_x), 0),
                    self.target_t: t_y,
                    self.targets_e: e_y,
                    self.sample_t: np.maximum(np.log(sample_t), 0)}

                gen_e_cost, hit_count, batch_precision, batch_recall = sess.run([
                    self.gen_e_cost,
                    self.hit_count,
                    self.batch_precision,
                    self.batch_recall],
                    feed_dict=feed_dict)
                sum_iter = sum_iter + 1
                hit_sum += hit_count
                i += 1

                if i % (batch_num // 10) == 0:
                    logging.info('{}, gen_e_cost: {}, hit10: {}, precision: {}, recall: {}'.format(
                        float(i) / batch_num,
                        gen_e_cost,
                        hit_sum / (sum_iter*self.batch_size*self.length),
                        batch_precision,
                        batch_recall
                        ))
        self.save_model(sess, self.logdir, args.iters)

    def eval(self, sess, args):
        if not os.path.exists(args.logdir + '/output'):
            os.makedirs(args.logdir + '/output')

        # if args.eval_only:
        #     self.test_data = read_data.load_test_dataset(self.dataset_file)

        if args.weights is not None:
            self.saver.restore(sess, args.weights)
            print_in_file("Saved")

        lr = self.learning_rate

        batch_size = 100

        input_event_data, target_event_data, input_time_data, target_time_data = read_data.data_iterator(
            self.test_data,
            self.num_steps,
            self.length)

        sample_t = read_data.generate_sample_t(
            batch_size,
            input_time_data,
            target_time_data)

        f = open(os.path.join(args.logdir, "output_e.txt"), 'w+')
        i = 0

        for e_x, e_y, t_x, t_y in read_data.generate_batch(
                self.batch_size,
                input_event_data,
                target_event_data,
                input_time_data,
                target_time_data):

            feed_dict = {
                self.input_e: e_x,
                self.inputs_t: np.maximum(np.log(t_x), 0),
                # self.target_t : t_y_list[i],
                # self.targets_e : e_y_list[i],
                self.sample_t: np.maximum(np.log(sample_t), 0)}

            pred_e = sess.run(self.pred_e, feed_dict=feed_dict)

            _, pred_e_index = tf.nn.top_k(pred_e, 1, name=None)
            f.write('pred_e: ' + '\t'.join([str(v) for v in tf.reshape(tf.squeeze(pred_e_index), [-1]).eval()]))
            f.write('\n')
            f.write('targ_e: ' + '\t'.join([str(v) for v in np.array(e_y[i]).flatten()]))
            f.write('\n')

    def save_model(self, sess, logdir, counter):
        ckpt_file = '%s/model-%d.ckpt' % (logdir, counter)
        logging.info('Checkpoint:{}'.format(ckpt_file))
        self.saver.save(sess, ckpt_file)


def get_config(config_mode):
    """Get model config."""

    if config_mode == "small":
        config = model_config.SmallConfig()
    elif config_mode == "medium":
        config = model_config.MediumConfig()
    elif config_mode == "large":
        config = model_config.LargeConfig()
    elif config_mode == "test":
        config = model_config.TestConfig()
    else:
        raise ValueError("Invalid model: %s", config_mode)
    return config


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', default='small', type=str)
    parser.add_argument('--is_training', default=True, type=bool)
    parser.add_argument('--weights', default=None, type=str)
    parser.add_argument('--gpu', default=0, type=int)
    parser.add_argument('--eval_only', default=False, action='store_true')
    parser.add_argument('--logdir', default='log/log_kick', type=str)
    parser.add_argument('--iters', default=100, type=int)
    parser.add_argument('--cell_type', default='T_GRUCell', type=str)
    args = parser.parse_args()

    assert args.logdir[-1] != '/'
    model_config = get_config(args.mode)
    is_training = args.is_training
    cell_type = args.cell_type
    model = T_Pred(model_config, cell_type, event_file, time_file, is_training)

    config = tf.ConfigProto()
    config.gpu_options.allow_growth = True
    with tf.Session(config=config) as sess:
        sess.run(tf.group(tf.global_variables_initializer(), tf.local_variables_initializer()))
        if not args.eval_only:
            model.train(sess, args)
        model.eval(sess, args)
    logging.info('Logging End!')


if __name__ == '__main__':
    main()
