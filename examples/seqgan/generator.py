import tensorflow as tf
import texar as tx
from texar.losses.mle_losses import _mask_sequences
from utils import *


class Generator:
    def __init__(self, config, word2id, bos, eos, pad):
        initializer = tf.random_uniform_initializer(
            -config.init_scale, config.init_scale)
        with tf.variable_scope('generator', initializer=initializer):
            self.batch_size = config.batch_size
            self.max_seq_length = config.num_steps
            self.vocab_size = len(word2id)
            self.bos_id = bos
            self.eos_id = eos
            self.pad_id = pad
            self.reward_gamma = 0.9

            self.data_batch = tf.placeholder(dtype=tf.int32, name="data_batch",
                                             shape=[None, self.max_seq_length + 2])
            self.rewards = tf.placeholder(dtype=tf.float32, name='rewards',
                                          shape=[None, self.max_seq_length])
            self.expected_reward = tf.Variable(tf.zeros([self.max_seq_length]))

            self.embedder = tx.modules.WordEmbedder(
                vocab_size=self.vocab_size, hparams=config.emb)
            self.decoder = tx.modules.BasicRNNDecoder(
                vocab_size=self.vocab_size,
                hparams={"rnn_cell": config.cell,
                         "max_decoding_length_train": self.max_seq_length + 1,
                         "max_decoding_length_infer": self.max_seq_length})

            emb_inputs = self.embedder(self.data_batch[:, :-1])
            if config.keep_prob < 1:
                emb_inputs = tf.nn.dropout(
                    emb_inputs, tx.utils.switch_dropout(config.keep_prob))

            initial_state = self.decoder.zero_state(self.batch_size, tf.float32)
            self.outputs, final_state, seq_lengths = self.decoder(
                decoding_strategy="train_greedy",
                impute_finished=True,
                inputs=emb_inputs,
                sequence_length=[self.max_seq_length + 1] * self.batch_size,
                initial_state=initial_state)

            preds = tf.nn.softmax(self.outputs.logits)

            self.mle_loss = -tf.reduce_sum(
                tf.one_hot(tf.to_int32(tf.reshape(self.data_batch[:, 1:], [-1])), self.vocab_size, 1.0, 0.0) * tf.log(
                    tf.clip_by_value(tf.reshape(preds, [-1, self.vocab_size]), 1e-20, 1.0)
                )
            ) / ((self.max_seq_length + 1) * self.batch_size)

            # Use global_step to pass epoch, for lr decay
            self.global_step = tf.placeholder(tf.int32)
            self.train_op = tx.core.get_train_op(
                self.mle_loss, global_step=self.global_step, increment_global_step=False,
                hparams=config.opt)

            self.update_loss = -tf.reduce_sum(
                tf.reduce_sum(
                    tf.one_hot(tf.to_int32(tf.reshape(self.data_batch[:, 1:self.max_seq_length + 1], [-1])), self.vocab_size, 1.0, 0.0) * tf.log(
                        tf.clip_by_value(tf.reshape(preds[:, :self.max_seq_length, :], [-1, self.vocab_size]), 1e-20, 1.0)
                    ), 1) * tf.reshape(self.rewards, [-1])
            )

            self.update_step = tf.placeholder(tf.int32)
            self.update_op = tx.core.get_train_op(
                self.update_loss, global_step=self.update_step, increment_global_step=False,
                hparams=config.opt)

            # for generation
            self.generated_outputs, _, _ = self.decoder(
                decoding_strategy="infer_sample",
                start_tokens=[self.bos_id] * self.batch_size,
                end_token=self.eos_id,
                embedding=self.embedder,
                initial_state=initial_state)
