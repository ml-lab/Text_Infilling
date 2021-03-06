"""
 Another transformer decoders using template as input. Attention is all you need.
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import tensorflow as tf
from tensorflow.python.framework import tensor_shape, dtypes
from tensorflow.python.util import nest

from texar.core import layers, attentions
from texar import context
from texar.module_base import ModuleBase
from texar.modules.networks.networks import FeedForwardNetwork
from texar.modules.decoders.transformer_decoders import TransformerDecoderOutput
from texar.modules.embedders import embedder_utils
from texar.modules.embedders import position_embedders
from texar.utils import beam_search
from texar.utils import utils
from texar.utils.shapes import shape_list


class TemplateTransformerDecoder(ModuleBase):
    """decoder for transformer: Attention is all you need
    """
    def __init__(self, embedding=None, vocab_size=None, hparams=None):
        ModuleBase.__init__(self, hparams)
        self._vocab_size = vocab_size
        self._embedding = None
        self.sampling_method = self._hparams.sampling_method
        with tf.variable_scope(self.variable_scope):
            if self._hparams.initializer:
                tf.get_variable_scope().set_initializer( \
                    layers.get_initializer(self._hparams.initializer))
            if self._hparams.position_embedder.name == 'sinusoids':
                self.position_embedder = \
                    position_embedders.SinusoidsSegmentalPositionEmbedder( \
                    self._hparams.position_embedder.hparams)

        if self._hparams.use_embedding:
            if embedding is None and vocab_size is None:
                raise ValueError("""If 'embedding' is not provided,
                    'vocab_size' must be specified.""")
            if isinstance(embedding, (tf.Tensor, tf.Variable)):
                self._embedding = embedding
            else:
                self._embedding = embedder_utils.get_embedding(
                    self._hparams.embedding, embedding, vocab_size,
                    variable_scope=self.variable_scope)
                self._embed_dim = shape_list(self._embedding)[-1]
                if self._hparams.zero_pad:
                    self._embedding = tf.concat( \
                        (tf.zeros(shape=[1, self._embed_dim]),\
                        self._embedding[1:, :]), 0)
            if self._vocab_size is None:
                self._vocab_size = self._embedding.get_shape().as_list()[0]
        self.output_layer = \
            self.build_output_layer(shape_list(self._embedding)[-1])
    @staticmethod
    def default_hparams():
        """default hyperrams for transformer deocder.
            sampling_method: argmax or sample. To choose the function transforming the logits to the sampled id in the next position when inferencing.
        """
        return {
            'sampling_method': 'argmax',
            'initializer': None,
            'multiply_embedding_mode': 'sqrt_depth',
            'position_embedder': None,
            'share_embed_and_transform': True,
            'transform_with_bias': True,
            "use_embedding": True,
            "name":"decoder",
            "num_heads":8,
            "num_blocks":6,
            "zero_pad": False,
            "bos_pad": False,
            "max_seq_length":10,
            "maximum_decode_length":10,
            "beam_width":1,
            'alpha':0,
            "embedding_dropout":0.1,
            'attention_dropout':0.1,
            'residual_dropout':0.1,
            "sinusoid":True,
            'poswise_feedforward':None,
            'num_units':512,
            'eos_idx': 2,
            'bos_idx': 1,
        }

    def prepare_tokens_to_embeds(self, tokens):
        """ a callable function to transform tokens into embeddings."""
        token_emb = tf.nn.embedding_lookup(self._embedding, tokens)
        return token_emb

    def _symbols_to_logits_fn(self, embedding_fn, max_length, segment_ids, offsets):
        channels = shape_list(self._embedding)[-1]
        timing_signal = self.position_embedder(max_length, channels, segment_ids, offsets)

        """ the function is normally called in dynamic decoding mode.
                the ids should be `next_id` with the shape [batch_size, 1]
            the returned logits is [batch_size, 1]
        """
        def _impl(ids, step, cache):
            ids = ids[:, -1:]
            decoder_self_attention_bias = (
                attentions.attention_bias_lower_triangle(
                    shape_list(ids)[1]))
            inputs = embedding_fn(ids)
            if self._hparams.multiply_embedding_mode == 'sqrt_depth':
                inputs *= self._embedding.shape.as_list()[-1]**0.5
            else:
                assert NotImplementedError
            inputs += timing_signal[:, step:step+1]

            outputs = self._self_attention_stack(
                inputs,
                template_input=cache['memory'],
                cache=cache,
                decoder_self_attention_bias=decoder_self_attention_bias,
            )
            logits = self.output_layer(outputs)
            logits = tf.squeeze(logits, axis=[1])

            return logits, cache

        return _impl
    #pylint:disable=arguments-differ
    def _build(self, decoder_input_pack, template_input_pack,
               encoder_decoder_attention_bias, args):
        """
            this function is called on training generally.
            Args:
                targets: [bath_size, target_length], generally begins with [bos] token
                template_input: [batch_size, source_length, channels]
                segment_ids: [batch_size, source_length], which segment this word belongs to
            outputs:
                logits: [batch_size, target_length, vocab_size]
                preds: [batch_size, target_length]
        """
        input = decoder_input_pack['text_ids'][:, :-1]
        decoder_self_attention_bias = (
            attentions.attention_bias_lower_triangle(
                shape_list(input)[1]))
        input_word_embeds = tf.nn.embedding_lookup(self._embedding, input)
        if self._hparams.multiply_embedding_mode == 'sqrt_depth':
            input_word_embeds = input_word_embeds * \
                (self._embedding.shape.as_list()[-1]**0.5)
        length = shape_list(input_word_embeds)[1]
        channels = shape_list(input_word_embeds)[2]
        input_pos_embeds = self.position_embedder(length, channels,
                                                  decoder_input_pack['segment_ids'][:, :-1],
                                                  decoder_input_pack['offsets'][:, :-1])
        inputs = input_word_embeds + input_pos_embeds

        template = template_input_pack['templates']
        template_word_embeds = tf.nn.embedding_lookup(self._embedding, template)
        template_length = shape_list(template)[1]
        template_pos_embeds = self.position_embedder(template_length, channels,
                                                     template_input_pack['segment_ids'],
                                                     template_input_pack['offsets'])
        template_inputs = template_word_embeds + template_pos_embeds
        self.decoder_output = self._self_attention_stack(
            inputs,
            template_inputs,
            decoder_self_attention_bias=decoder_self_attention_bias,
        )

        logits = self.output_layer(self.decoder_output)
        preds = tf.to_int32(tf.argmax(logits, axis=-1))

        if not self._built:
            self._add_internal_trainable_variables()
            self._built = True

        return logits, preds

    def dynamic_decode(self, template_input_pack, encoder_decoder_attention_bias,
                       segment_ids, offsets, bos_id, eos_id):
        """
            this function is called on in test mode, without the target input.
        """
        with tf.variable_scope(self.variable_scope, reuse=True):
            template = template_input_pack['templates']
            template_word_embeds = tf.nn.embedding_lookup(self._embedding, template)
            batch_size = tf.shape(template)[0]
            template_length = shape_list(template)[1]
            channels = shape_list(template_word_embeds)[2]
            template_pos_embeds = self.position_embedder(template_length, channels,
                                                         template_input_pack['segment_ids'],
                                                         template_input_pack['offsets'])
            template_inputs = template_word_embeds + template_pos_embeds

            # batch_size = tf.shape(template_inputs)[0]
            beam_width = self._hparams.beam_width
            maximum_decode_length = self.hparams.maximum_decode_length
            start_tokens = tf.cast(tf.fill([batch_size], bos_id), dtype=tf.int32)
            if beam_width <= 1:
                sampled_ids, log_probs = self.greedy_decode(
                    self.prepare_tokens_to_embeds,
                    start_tokens,
                    eos_id, #self._hparams.eos_idx,
                    decode_length=maximum_decode_length,
                    memory=template_inputs,
                    encoder_decoder_attention_bias=\
                        encoder_decoder_attention_bias,
                    segment_ids=segment_ids,
                    offsets=offsets,
                )
            else:
                sampled_ids, log_probs = self.beam_decode(
                    self.prepare_tokens_to_embeds,
                    start_tokens,
                    eos_id, #self._hparams.eos_idx,
                    beam_width=beam_width,
                    decode_length=maximum_decode_length,
                    memory=template_inputs,
                    encoder_decoder_attention_bias=\
                        encoder_decoder_attention_bias,
                    segment_ids=segment_ids,
                    offsets=offsets
                )
            predictions = {
                'sampled_ids': sampled_ids,
                'log_probs': log_probs
            }
        return predictions

    def _self_attention_stack(self,
                              inputs,
                              template_input,
                              decoder_self_attention_bias=None,
                              encoder_decoder_attention_bias=None,
                              cache=None):
        """
            stacked multihead attention module.
        """
        inputs = tf.layers.dropout(inputs,
                                   rate=self._hparams.embedding_dropout,
                                   training=context.global_mode_train())
        if cache is not None and 'encoder_decoder_attention_bias' in cache.keys():
            encoder_decoder_attention_bias = \
                cache['encoder_decoder_attention_bias']
        else:
            assert decoder_self_attention_bias is not None

        x = inputs
        for i in range(self._hparams.num_blocks):
            layer_name = 'layer_{}'.format(i)
            layer_cache = cache[layer_name] if cache is not None else None
            with tf.variable_scope(layer_name):
                with tf.variable_scope("self_attention"):
                    selfatt_output = attentions.multihead_attention(
                        queries=layers.layer_normalize(x),
                        memory=None,
                        memory_attention_bias=decoder_self_attention_bias,
                        num_units=self._hparams.num_units,
                        num_heads=self._hparams.num_heads,
                        dropout_rate=self._hparams.attention_dropout,
                        cache=layer_cache,
                        scope="multihead_attention",
                    )
                    x = x + tf.layers.dropout(
                        selfatt_output,
                        rate=self._hparams.residual_dropout,
                        training=context.global_mode_train()
                    )
                if template_input is not None:
                    with tf.variable_scope('encdec_attention'):
                        encdec_output = attentions.multihead_attention(
                            queries=layers.layer_normalize(x),
                            memory=template_input,
                            memory_attention_bias=encoder_decoder_attention_bias,
                            num_units=self._hparams.num_units,
                            num_heads=self._hparams.num_heads,
                            dropout_rate=self._hparams.attention_dropout,
                            scope="multihead_attention"
                        )
                        x = x + tf.layers.dropout(encdec_output, \
                            rate=self._hparams.residual_dropout, \
                            training=context.global_mode_train()
                        )
                poswise_network = FeedForwardNetwork( \
                    hparams=self._hparams['poswise_feedforward'])
                with tf.variable_scope(poswise_network.variable_scope):
                    sub_output = tf.layers.dropout(
                        poswise_network(layers.layer_normalize(x)),
                        rate=self._hparams.residual_dropout,
                        training=context.global_mode_train()
                    )
                    x = x + sub_output

        return layers.layer_normalize(x)

    def build_output_layer(self, num_units):
        if self._hparams.share_embed_and_transform:
            if self._hparams.transform_with_bias:
                with tf.variable_scope(self.variable_scope):
                    affine_bias = tf.get_variable('affine_bias',
                        [self._vocab_size])
            else:
                affine_bias = None
            def outputs_to_logits(outputs):
                shape = shape_list(outputs)
                outputs = tf.reshape(outputs, [-1, num_units])
                logits = tf.matmul(outputs, self._embedding, transpose_b=True)
                if affine_bias is not None:
                    logits += affine_bias
                logits = tf.reshape(logits, shape[:-1] + [self._vocab_size])
                return logits
            return outputs_to_logits
        else:
            layer = tf.layers.Dense(self._vocab_size, \
                use_bias=self._hparams.transform_with_bias)
            layer.build([None, num_units])
            return layer

    @property
    def output_size(self):
        """
        The output of the _build function, (logits, preds)
        logits: [batch_size, length, vocab_size]
        preds: [batch_size, length]
        """
        return TransformerDecoderOutput(
            output_logits=tensor_shape.TensorShape([None, None, self._vocab_size]),
            sample_id=tensor_shape.TensorShape([None, None])
            )

    def output_dtype(self):
        """
        The output dtype of the _build function, (float32, int32)
        """
        return TransformerDecoderOutput(
            output_logits=dtypes.float32, sample_id=dtypes.int32)

    def _init_cache(self, memory, encoder_decoder_attention_bias):
        cache = {'memory': memory}
        if encoder_decoder_attention_bias is not None:
            cache['encoder_decoder_attention_bias'] = \
                encoder_decoder_attention_bias
        batch_size = tf.shape(memory)[0]
        depth = memory.get_shape().as_list()[-1]
        for l in range(self._hparams.num_blocks):
            cache['layer_{}'.format(l)] = {
                'self_keys': tf.zeros([batch_size, 0, depth]),
                'self_values': tf.zeros([batch_size, 0, depth]),
                'memory_keys': tf.zeros([batch_size, 0, depth]),
                'memory_values': tf.zeros([batch_size, 0, depth]),
            }
        return cache

    def greedy_decode(self,
                      embedding_fn,
                      start_tokens,
                      EOS,
                      decode_length,
                      memory,
                      encoder_decoder_attention_bias,
                      segment_ids,
                      offsets):
        batch_size = tf.shape(start_tokens)[0]
        finished = tf.fill([batch_size], False)
        step = tf.constant(0)
        decoded_ids = tf.zeros([batch_size, 0], dtype=tf.int32)
        next_id = tf.expand_dims(start_tokens, 1)
        print('next id:{}'.format(next_id.shape))
        log_prob = tf.zeros([batch_size], dtype=tf.float32)

        cache = self._init_cache(memory, encoder_decoder_attention_bias)
        symbols_to_logits_fn = self._symbols_to_logits_fn(embedding_fn,
            max_length=decode_length+1, segment_ids=segment_ids,
            offsets=offsets)

        def _body(step, finished, next_id, decoded_ids, cache, log_prob):

            logits, cache = symbols_to_logits_fn(next_id, step, cache)
            log_probs = logits - \
                tf.reduce_logsumexp(logits, axis=-1, keep_dims=True)

            #TODO: by default, the output_type is tf.int64.
            # Can we adjust the default int type of texar to tf.int64?
            if self.sampling_method == 'argmax':
                next_id = tf.argmax(logits, -1, output_type=tf.int32)
            elif self.sampling_method == 'sample':
                next_id = tf.multinomial(logits, 1).squeeze(axis=1)
            finished |= tf.equal(next_id, EOS)
            log_prob_indices = tf.stack(
                [tf.range(tf.to_int32(batch_size)), next_id], axis=1)
            log_prob += tf.gather_nd(log_probs, log_prob_indices)

            next_id = tf.expand_dims(next_id, axis=1)
            #keep the shape as [batch_size, seq_len]

            decoded_ids = tf.concat([decoded_ids, next_id], axis=1)
            return step+1, finished, next_id, decoded_ids, cache, log_prob

        def is_not_finished(i, finished, *_):
            return (i < decode_length) & tf.logical_not(tf.reduce_all(finished))

        _, _, _, decoded_ids, _, log_prob = tf.while_loop(
            is_not_finished,
            _body,
            loop_vars=(step, finished, next_id, decoded_ids, cache, log_prob),
            shape_invariants=(
                tf.TensorShape([]),
                tf.TensorShape([None]),
                tf.TensorShape([None, None]),
                tf.TensorShape([None, None]),
                nest.map_structure(beam_search.get_state_shape_invariants, cache),
                tf.TensorShape([None]),
            ))

        outputs = tf.expand_dims(decoded_ids, 1)
        log_prob = tf.expand_dims(log_prob, 1)
        return (outputs, log_prob)

    def _expand_to_beam_width(self, tensor, beam_width):
        """
        :param tensor: [batch_size, max_len]
        :param beam_width:
        :return: [batch_size*beam_width, max_len]
        """
        batch_size = shape_list(tensor)[0]
        expanded = tf.tile(tf.expand_dims(tensor, axis=1), [1, beam_width, 1])
        return tf.reshape(expanded, [batch_size * beam_width, -1])

    def beam_decode(self,
                    embedding_fn,
                    start_tokens,
                    EOS,
                    memory,
                    encoder_decoder_attention_bias,
                    segment_ids,
                    offsets,
                    decode_length=256,
                    beam_width=5,):
        cache = self._init_cache(memory, encoder_decoder_attention_bias)
        symbols_to_logits_fn = self._symbols_to_logits_fn(embedding_fn,
            max_length=decode_length+1,
            segment_ids=self._expand_to_beam_width(segment_ids, beam_width),
            offsets=self._expand_to_beam_width(offsets, beam_width))
        outputs, log_probs = beam_search.beam_search(
            symbols_to_logits_fn,
            start_tokens,
            beam_width,
            decode_length,
            self._vocab_size,
            self._hparams.alpha,
            states=cache,
            eos_id=EOS)

        outputs = outputs[:, :, 1:]  # ignore <BOS>
        return (outputs, log_probs)
