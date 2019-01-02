import tensorflow as tf
import numpy as np
import texar as tx
import argparse
import os

parser = argparse.ArgumentParser(description='prepare data')
parser.add_argument('--dataset', type=str, default='grimm_prep',
                    help='dataset to prepare')
parser.add_argument('--data_path', type=str, default='./',
                    help="Directory containing coco. If not exists, "
                    "the directory will be created, and the data "
                    "will be downloaded.")
args = parser.parse_args()

FLAGS = tf.app.flags.FLAGS


def transform_input_with_is_missing_token(inputs, targets_present, mask_id):
    """
    Transforms the inputs to have missing tokens when it's masked out.  The
    mask is for the targets, so therefore, to determine if an input at time t is
    masked, we have to check if the target at time t - 1 is masked out.
    e.g.
    inputs = [a, b, c, d]
    targets = [b, c, d, e]
    targets_present = [1, 0, 1, 0]
    then,
    transformed_input = [a, b, <missing>, d]
    Args:
    inputs:  tf.int32 Tensor of shape [batch_size, sequence_length] with tokens
      up to, but not including, vocab_size.
    targets_present:  tf.bool Tensor of shape [batch_size, sequence_length] with
      True representing the presence of the word.
    Returns:
    transformed_input:  tf.int32 Tensor of shape [batch_size, sequence_length]
      which takes on value of inputs when the input is present and takes on
      value=vocab_size to indicate a missing token.
    """
    # To fill in if the input is missing.
    input_missing = tf.fill(tf.shape(inputs), mask_id)

    # The 0th input will always be present.
    zeroth_input_present = tf.zeros_like(inputs)[:, 0][:, tf.newaxis]
    print(zeroth_input_present.shape)
    print(targets_present.shape)

    # Input present mask.
    inputs_present = tf.concat(
        [zeroth_input_present, targets_present[:, :-1]], axis=1)

    transformed_input = tf.where(tf.equal(inputs_present, tf.ones_like(inputs)),
                                 inputs, input_missing)
    return transformed_input


def prepare_data(dataset):
    """Downloads the PTB or COCO dataset
    """
    url = {
        'grimm_prep': 'https://VegB.github.io/downloads/grimm_prep.tgz',
        'grimm_lm': 'https://VegB.github.io/downloads/grimm_lm.tgz',
        'nba_lm': 'https://VegB.github.io/downloads/nba_lm.tgz'
    }
    data_path = {
        'grimm_prep': 'grimm_prep_data/',
        'grimm_lm': 'grimm_lm_data/',
        'nba_lm': 'nba_lm_data/'
    }

    if not tf.gfile.Exists(data_path[dataset]):
        tx.data.maybe_download(url[dataset], args.data_path, extract=True)
        os.remove(url[dataset].split('/')[-1])


if __name__ == '__main__':
    prepare_data(args.dataset)