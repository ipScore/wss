# Lint as: python2, python3
# Copyright 2020 Google LLC

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     https://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""Training script for consistency-constrained training."""

from absl import flags
import six
#import tensorflow.compat.v1 as tf
import tensorflow as tf
from tensorflow.contrib import slim as contrib_slim
from tensorflow.contrib import tfprof as contrib_tfprof
from third_party.deeplab import common
from third_party.deeplab.core import feature_extractor
from third_party.deeplab.utils import train_utils
from third_party.slim.deployment import model_deploy
# Custom import
from core import data_generator
from core import model
from core import preprocess_utils
from core import train_utils_core

slim = contrib_slim
FLAGS = flags.FLAGS

## DeepLab options
# Settings for multi-GPUs/multi-replicas training.

flags.DEFINE_integer('num_clones', 16, 'Number of clones to deploy.')

flags.DEFINE_boolean('clone_on_cpu', False, 'Use CPUs to deploy clones.')

flags.DEFINE_integer('num_replicas', 1, 'Number of worker replicas.')

flags.DEFINE_integer('startup_delay_steps', 15,
                     'Number of training steps between replicas startup.')

flags.DEFINE_integer(
    'num_ps_tasks', 0,
    'The number of parameter servers. If the value is 0, then '
    'the parameters are handled locally by the worker.')

flags.DEFINE_string('master', '', 'BNS name of the tensorflow server')

flags.DEFINE_integer('task', 0, 'The task ID.')

# Settings for logging.

flags.DEFINE_string('train_logdir', None,
                    'Where the checkpoint and logs are stored.')

flags.DEFINE_integer('log_steps', 10,
                     'Display logging information at every log_steps.')

flags.DEFINE_integer('save_interval_secs', 1200,
                     'How often, in seconds, we save the model to disk.')

flags.DEFINE_integer('save_summaries_secs', 180,
                     'How often, in seconds, we compute the summaries.')

flags.DEFINE_boolean(
    'save_summaries_images', True,
    'Save sample inputs, labels, and semantic predictions as '
    'images to summary.')

# Settings for profiling.

flags.DEFINE_string('profile_logdir', None,
                    'Where the profile files are stored.')

# Settings for training strategy.

flags.DEFINE_enum('optimizer', 'momentum', ['momentum', 'adam'],
                  'Which optimizer to use.')


# Momentum optimizer flags

flags.DEFINE_enum('learning_policy', 'poly', ['poly', 'step'],
                  'Learning rate policy for training.')

# Use 0.007 when training on PASCAL augmented training set, train_aug. When
# fine-tuning on PASCAL trainval set, use learning rate=0.0001.
flags.DEFINE_float('base_learning_rate', 0.007,
                   'The base learning rate for model training.')

flags.DEFINE_float('decay_steps', 0.0,
                   'Decay steps for polynomial learning rate schedule.')

flags.DEFINE_float('end_learning_rate', 0.0,
                   'End learning rate for polynomial learning rate schedule.')

flags.DEFINE_float('learning_rate_decay_factor', 0.1,
                   'The rate to decay the base learning rate.')

flags.DEFINE_integer('learning_rate_decay_step', 2000,
                     'Decay the base learning rate at a fixed step.')

flags.DEFINE_float('learning_power', 0.9,
                   'The power value used in the poly learning policy.')

flags.DEFINE_integer('training_number_of_steps', 30000,
                     'The number of steps used for training')

flags.DEFINE_float('momentum', 0.9, 'The momentum value to use')

# Adam optimizer flags
flags.DEFINE_float('adam_learning_rate', 0.001,
                   'Learning rate for the adam optimizer.')
flags.DEFINE_float('adam_epsilon', 1e-08, 'Adam optimizer epsilon.')

# When fine_tune_batch_norm=True, use at least batch size larger than 12
# (batch size more than 16 is better). Otherwise, one could use smaller batch
# size and set fine_tune_batch_norm=False.
flags.DEFINE_integer('train_batch_size', 64,
                     'The number of images in each batch during training.')

# For weight_decay, use 0.00004 for MobileNet-V2 or Xcpetion model variants.
# Use 0.0001 for ResNet model variants.
flags.DEFINE_float('weight_decay', 0.00004,
                   'The value of the weight decay for training.')

flags.DEFINE_list('train_crop_size', '513,513',
                  'Image crop size [height, width] during training.')

flags.DEFINE_float(
    'last_layer_gradient_multiplier', 1.0,
    'The gradient multiplier for last layers, which is used to '
    'boost the gradient of last layers if the value > 1.')

flags.DEFINE_boolean('upsample_logits', True,
                     'Upsample logits during training.')

# Hyper-parameters for NAS training strategy.

flags.DEFINE_float(
    'drop_path_keep_prob', 1.0,
    'Probability to keep each path in the NAS cell when training.')

# Settings for fine-tuning the network.

flags.DEFINE_string('tf_initial_checkpoint', None,
                    'The initial checkpoint in tensorflow format.')

# Set to False if one does not want to re-use the trained classifier weights.
flags.DEFINE_boolean('initialize_last_layer', False,
                     'Initialize the last layer.')

flags.DEFINE_boolean('last_layers_contain_logits_only', True,
                     'Only consider logits as last layers or not.')

flags.DEFINE_integer('slow_start_step', 0,
                     'Training model with small learning rate for few steps.')

flags.DEFINE_float('slow_start_learning_rate', 1e-4,
                   'Learning rate employed during slow start.')

# Set to True if one wants to fine-tune the batch norm parameters in DeepLabv3.
# Set to False and use small batch size to save GPU memory.
flags.DEFINE_boolean('fine_tune_batch_norm', True,
                     'Fine tune the batch norm parameters or not.')

flags.DEFINE_float('min_scale_factor', 0.5,
                   'Mininum scale factor for data augmentation.')

flags.DEFINE_float('max_scale_factor', 2.,
                   'Maximum scale factor for data augmentation.')

flags.DEFINE_float('scale_factor_step_size', 0.25,
                   'Scale factor step size for data augmentation.')

# For `xception_65`, use atrous_rates = [12, 24, 36] if output_stride = 8, or
# rates = [6, 12, 18] if output_stride = 16. For `mobilenet_v2`, use None. Note
# one could use different atrous_rates/output_stride during training/evaluation.
flags.DEFINE_multi_integer('atrous_rates', None,
                           'Atrous rates for atrous spatial pyramid pooling.')

flags.DEFINE_integer('output_stride', 16,
                     'The ratio of input to output spatial resolution.')

# Hard example mining related flags.
flags.DEFINE_integer(
    'hard_example_mining_step', 0,
    'The training step in which exact hard example mining kicks off. Note we '
    'gradually reduce the mining percent to the specified '
    'top_k_percent_pixels. For example, if hard_example_mining_step=100K and '
    'top_k_percent_pixels=0.25, then mining percent will gradually reduce from '
    '100% to 25% until 100K steps after which we only mine top 25% pixels.')

flags.DEFINE_float(
    'top_k_percent_pixels', 1.0,
    'The top k percent pixels (in terms of the loss values) used to compute '
    'loss during training. This is useful for hard pixel mining.')

# Quantization setting.
flags.DEFINE_integer(
    'quantize_delay_step', -1,
    'Steps to start quantized training. If < 0, will not quantize model.')

# Dataset settings.
flags.DEFINE_string('dataset', 'pascal_voc_seg',
                    'Name of the segmentation dataset.')

flags.DEFINE_string('train_split', 'train',
                    'Which split of the dataset to be used for training (seg)')

flags.DEFINE_string('dataset_dir', None, 'Where the dataset reside.')

## Pseudo_seg options.
flags.DEFINE_boolean('weakly', False, 'Using image-level labeled data or not')

flags.DEFINE_string('train_split_cls', 'train_aug',
                    'Which split of the dataset to be used for training (cls)')

# Pseudo label settings.
flags.DEFINE_boolean('soft_pseudo_label', True, 'Use soft pseudo label or not')

flags.DEFINE_float('pseudo_label_threshold', 0.0,
                   'Confidence threshold to filter pseudo labels')

flags.DEFINE_float('unlabeled_weight', 1.0,
                   'Weight of the unlabeled consistency loss')

# Attention settings.
flags.DEFINE_list('att_strides', '15,16', 'Hypercolumn layer strides.')

flags.DEFINE_integer('attention_dim', 128,
                     'Key and query dimension of self-attention module')

flags.DEFINE_boolean('use_attention', True,
                     'Use self-attention for weak augmented branch or not')

flags.DEFINE_boolean('att_v2', True,
                     'Use self-attention v2 or not.')

# Ensemble settings.
flags.DEFINE_enum('pseudo_src', 'avg', ['att', 'avg'],
                  'Pseudo label source, self-attention or average.')

flags.DEFINE_float(
    'temperature', 0.5,
    'Temperature for pseudo label sharpen, only valid when using soft label')

flags.DEFINE_boolean(
    'logit_norm', True,
    'Use logit norm to change the flatness or not')

flags.DEFINE_boolean(
    'cls_with_cls', True,
    'Using samples_cls or samples_seg to train the classifier. Only valid in wss mode.')

# Others
flags.DEFINE_integer('seed', 0, 'Random seed')


def _build_pseudo_seg(iterator_seg, iterator, outputs_to_num_classes,
                      ignore_label, batch_size=8):
  """Builds a clone of PseudoSeg.

  Args:
    iterator_seg: An iterator of type tf.data.Iterator for images and labels.
    (seg)
    iterator: An iterator of type tf.data. Iterator for images and labels.
    outputs_to_num_classes: A map from output type to the number of classes. For
      example, for the task of semantic segmentation with 21 semantic classes,
      we would have outputs_to_num_classes['semantic'] = 21.
    ignore_label: Ignore label.
    batch_size: Training batch size for each clone.
  """
  samples_cls = iterator.get_next()
  samples_cls[common.IMAGE] = tf.identity(
      samples_cls[common.IMAGE], name='weak')
  samples_cls['strong'] = tf.identity(
      samples_cls['strong'], name='strong')
  samples_cls[common.LABEL] = tf.identity(
      samples_cls[common.LABEL], name='unlabeled')

  samples_seg = iterator_seg.get_next()
  samples_seg[common.IMAGE] = tf.identity(
      samples_seg[common.IMAGE], name=common.IMAGE + '_seg')
  samples_seg[common.LABEL] = tf.identity(
      samples_seg[common.LABEL], name=common.LABEL + '_seg')

  model_options = common.ModelOptions(
      outputs_to_num_classes=outputs_to_num_classes,
      crop_size=[int(sz) for sz in FLAGS.train_crop_size],
      atrous_rates=FLAGS.atrous_rates,
      output_stride=FLAGS.output_stride)

  if FLAGS.att_v2:
    cam_func = train_utils_core.compute_cam_v2
  else:
    cam_func = train_utils_core.compute_cam

  ### Cls/unlabeled data
  ## 1) If we have image-level label, we train the classifier here
  if FLAGS.weakly:
    if FLAGS.cls_with_cls:
      curr_samples = samples_cls
    else:
      curr_samples = samples_seg

    _, end_points_cls = feature_extractor.extract_features(
        curr_samples[common.IMAGE],
        output_stride=model_options.output_stride,
        multi_grid=model_options.multi_grid,
        model_variant=model_options.model_variant,
        depth_multiplier=model_options.depth_multiplier,
        divisible_by=model_options.divisible_by,
        weight_decay=FLAGS.weight_decay,
        reuse=tf.AUTO_REUSE,
        is_training=True,
        preprocessed_images_dtype=model_options.preprocessed_images_dtype,
        fine_tune_batch_norm=FLAGS.fine_tune_batch_norm,
        global_pool=True,
        num_classes=outputs_to_num_classes[common.OUTPUT_TYPE] - 1)

    # ResNet beta version has an additional suffix in FLAGS.model_variant, but
    # it shares the same variable names with original version. Add a special
    # handling here for beta version ResNet.
    logits_cls = end_points_cls['{}/logits'.format(FLAGS.model_variant).replace(
        '_beta', '')]
    logits_cls = tf.reshape(
        logits_cls, [-1, outputs_to_num_classes[common.OUTPUT_TYPE] - 1])
    # Seems that people usually use multi-label soft margin loss in PyTorch
    loss_cls = tf.nn.sigmoid_cross_entropy_with_logits(
        labels=curr_samples['cls_label'],
        logits=logits_cls)
    loss_cls = tf.reduce_mean(loss_cls)
    loss_cls = tf.identity(loss_cls, name='loss_cls')
    tf.compat.v1.losses.add_loss(loss_cls)

  ## 2) Consistency
  with tf.name_scope('cls_weak'):
    outputs_to_scales_to_logits, _ = model.multi_scale_logits(
        samples_cls[common.IMAGE],
        model_options=model_options,
        image_pyramid=FLAGS.image_pyramid,
        weight_decay=FLAGS.weight_decay,
        is_training=True,
        fine_tune_batch_norm=FLAGS.fine_tune_batch_norm,
        nas_training_hyper_parameters={
            'drop_path_keep_prob': FLAGS.drop_path_keep_prob,
            'total_training_steps': FLAGS.training_number_of_steps,
        },
        output_end_points=True)
    logits_weak = outputs_to_scales_to_logits[common.OUTPUT_TYPE][
        model.MERGED_LOGITS_SCOPE]

  prob_weak = tf.nn.softmax(logits_weak, axis=-1)
  logits_weak = tf.identity(logits_weak, name='logits_weak')
  # Monitor max score
  max_prob_weak = tf.reduce_max(prob_weak, axis=-1)
  max_prob_weak = tf.identity(max_prob_weak, name='max_prob_weak')

  valid_mask_pad = samples_cls['valid']
  valid_mask_pad = tf.compat.v1.image.resize_nearest_neighbor(
      valid_mask_pad,
      preprocess_utils.resolve_shape(logits_weak, 4)[1:3])
  valid_mask_pad = tf.cast(valid_mask_pad, tf.float32)

  if FLAGS.use_attention:
    # Using inference mode to generate Grad-CAM
    with tf.name_scope('cls_data_cls_inference'):
      _, end_points_cls = feature_extractor.extract_features(
          samples_cls[common.IMAGE],
          output_stride=model_options.output_stride,
          multi_grid=model_options.multi_grid,
          model_variant=model_options.model_variant,
          depth_multiplier=model_options.depth_multiplier,
          divisible_by=model_options.divisible_by,
          weight_decay=FLAGS.weight_decay,
          reuse=tf.AUTO_REUSE,
          is_training=False,
          preprocessed_images_dtype=model_options.preprocessed_images_dtype,
          fine_tune_batch_norm=FLAGS.fine_tune_batch_norm,
          global_pool=True,
          num_classes=outputs_to_num_classes[common.OUTPUT_TYPE] - 1)

    logits_cls = end_points_cls['{}/logits'.format(FLAGS.model_variant).replace(
        '_beta', '')]
    logits_cls = tf.reshape(
        logits_cls, [-1, outputs_to_num_classes[common.OUTPUT_TYPE] - 1])

    # We can only get ground truth image-level label in weakly+semi setting
    if FLAGS.weakly:
      image_level_label = samples_cls['cls_label']
    else:
      prob_cls = tf.sigmoid(logits_cls)
      # TODO(ylzou): Might use a variable threshold for different classes
      pred_cls = tf.greater_equal(prob_cls, 0.5)
      image_level_label = tf.stop_gradient(tf.cast(pred_cls, tf.float32))

    cam_weak, att_cam_weak = cam_func(
        end_points_cls,
        logits_cls,
        image_level_label,
        num_class=outputs_to_num_classes[common.OUTPUT_TYPE],
        use_attention=True,
        attention_dim=FLAGS.attention_dim,
        strides=[int(st) for st in FLAGS.att_strides],
        is_training=True,
        valid_mask=valid_mask_pad,
        net=FLAGS.model_variant.replace('_beta', ''))
    att_logits_weak = att_cam_weak
    # Upsample att-cam
    att_logits_weak = tf.compat.v1.image.resize_bilinear(
        att_logits_weak, preprocess_utils.resolve_shape(logits_weak, 4)[1:3],
        align_corners=True)
    # Monitor vanilla cam
    cam_weak = tf.compat.v1.image.resize_bilinear(
        cam_weak, preprocess_utils.resolve_shape(logits_weak, 4)[1:3],
        align_corners=True)
    cam_weak = tf.identity(cam_weak, name='cam_weak')

    att_prob_weak = tf.nn.softmax(att_logits_weak, axis=-1)
    att_logits_weak = tf.identity(att_logits_weak, name='att_logits_weak')
    # Monitor max score
    max_att_prob_weak = tf.reduce_max(att_prob_weak, axis=-1)
    max_att_prob_weak = tf.identity(max_att_prob_weak, name='max_att_prob_weak')

    # Ensemble
    if FLAGS.pseudo_src == 'att':
      prob_weak = att_prob_weak
    else:
      if FLAGS.logit_norm:
        v = tf.concat([logits_weak, att_logits_weak], axis=0)
        all_logits_weak = v * tf.rsqrt(tf.reduce_mean(tf.square(v)) + 1e-8)
        scaled_logits_weak = all_logits_weak[:batch_size]
        prob_weak = tf.nn.softmax(scaled_logits_weak, axis=-1)
        scaled_att_logits_weak = all_logits_weak[batch_size:]
        att_prob_weak = tf.nn.softmax(scaled_att_logits_weak, axis=-1)
      prob_weak = (prob_weak + att_prob_weak) / 2.

    # Monitor max score
    max_prob_avg = tf.reduce_max(prob_weak, axis=-1)
    max_prob_avg = tf.identity(max_prob_avg, name='max_prob_avg')

  # Temperature
  if FLAGS.soft_pseudo_label and FLAGS.temperature != 1.0:
    prob_weak = tf.pow(prob_weak, 1. / FLAGS.temperature)
    prob_weak /= tf.reduce_sum(prob_weak, axis=-1, keepdims=True)
    # Monitor max score
    max_prob_avg_t = tf.reduce_max(prob_weak, axis=-1)
    max_prob_avg_t = tf.identity(max_prob_avg_t, name='max_prob_avg_t')
    # Monitor merged logits
    prob_weak = tf.identity(prob_weak, name='merged_logits')

  with tf.name_scope('cls_strong'):
    outputs_to_scales_to_logits, _ = model.multi_scale_logits(
        samples_cls['strong'],
        model_options=model_options,
        image_pyramid=FLAGS.image_pyramid,
        weight_decay=FLAGS.weight_decay,
        is_training=True,
        fine_tune_batch_norm=FLAGS.fine_tune_batch_norm,
        nas_training_hyper_parameters={
            'drop_path_keep_prob': FLAGS.drop_path_keep_prob,
            'total_training_steps': FLAGS.training_number_of_steps,
        },
        output_end_points=True)
    logits_strong = outputs_to_scales_to_logits[common.OUTPUT_TYPE][
        model.MERGED_LOGITS_SCOPE]
  logits_strong = tf.identity(logits_strong, name='logits_strong')

  if FLAGS.pseudo_label_threshold > 0:
    confidence_weak = tf.expand_dims(tf.reduce_max(prob_weak, axis=-1), axis=-1)
    valid_mask_score = tf.greater_equal(confidence_weak,
                                        FLAGS.pseudo_label_threshold)
    valid_mask_score = tf.cast(valid_mask_score, tf.float32)
    valid_mask = valid_mask_score * valid_mask_pad
  else:
    valid_mask_score = None
    valid_mask = valid_mask_pad
  # Save for visualization
  valid_mask = tf.identity(valid_mask, name='valid_mask')

  logits_strong = tf.reshape(logits_strong,
                             [-1, outputs_to_num_classes[common.OUTPUT_TYPE]])

  if not FLAGS.soft_pseudo_label:
    pseudo_label = tf.argmax(prob_weak, axis=-1)
    pseudo_label = tf.reshape(pseudo_label, [-1])
    pseudo_label = tf.stop_gradient(pseudo_label)
    loss_consistency = tf.compat.v1.nn.sparse_softmax_cross_entropy_with_logits(
        labels=pseudo_label, logits=logits_strong,
        name='consistency_losses')
    loss_consistency = loss_consistency * tf.reshape(valid_mask, [-1])
    pred_pseudo = pseudo_label
  else:
    pseudo_label = prob_weak
    pseudo_label = tf.reshape(
        pseudo_label, [-1, outputs_to_num_classes[common.OUTPUT_TYPE]])
    pseudo_label = tf.stop_gradient(pseudo_label)
    loss_consistency = tf.compat.v1.nn.softmax_cross_entropy_with_logits_v2(
        labels=pseudo_label, logits=logits_strong,
        name='consistency_losses')
    loss_consistency = loss_consistency * tf.reshape(valid_mask, [-1])
    pred_pseudo = tf.argmax(pseudo_label, axis=-1)

  # NOTE: When average, we divide by the number of pixels excluding padding
  loss_consistency = tf.reduce_sum(loss_consistency)
  loss_consistency = train_utils._div_maybe_zero(
      loss_consistency,
      tf.reduce_sum(valid_mask_pad))
  loss_consistency *= FLAGS.unlabeled_weight
  loss_consistency = tf.identity(loss_consistency, 'loss_consistency')
  tf.compat.v1.losses.add_loss(loss_consistency)

  ## 3) Monitor prediction quality
  temp_label = tf.compat.v1.image.resize_nearest_neighbor(
      samples_cls[common.LABEL],
      preprocess_utils.resolve_shape(logits_weak, 4)[1:3])
  temp_label = tf.reshape(temp_label, [-1])

  # Get #pixel of each class, so that we can re-weight them for pixel acc.
  dump = tf.concat(
      [tf.range(outputs_to_num_classes[common.OUTPUT_TYPE]), temp_label],
      axis=-1)
  _, _, count = tf.unique_with_counts(dump)
  num_pixel_list = count - 1
  # Exclude the ignore region
  num_pixel_list = num_pixel_list[:outputs_to_num_classes[common.OUTPUT_TYPE]]
  num_pixel_list = tf.cast(num_pixel_list, tf.float32)
  inverse_ratio = train_utils._div_maybe_zero(1, num_pixel_list)
  inverse_ratio = inverse_ratio / tf.reduce_sum(inverse_ratio)

  # Since tf.metrics.mean_per_class_accuracy does not support weighted average
  # for each class directly, we here convert it to pixel-wise weighted mask to
  # compute weighted average pixel accuracy.
  weight_mask = tf.einsum(
      '...y,y->...',
      tf.one_hot(
          temp_label,
          outputs_to_num_classes[common.OUTPUT_TYPE],
          dtype=tf.float32), inverse_ratio)
  temp_valid = tf.not_equal(temp_label, ignore_label)
  if valid_mask_score is not None:
    temp_valid_confident = tf.cast(temp_valid, tf.float32) * tf.reshape(
        valid_mask_score, [-1])
    temp_valid_confident = tf.cast(temp_valid_confident, tf.bool)
  else:
    temp_valid_confident = temp_valid

  temp_label_confident = tf.boolean_mask(temp_label, temp_valid_confident)
  temp_label_valid = tf.boolean_mask(temp_label, temp_valid)
  weight_mask_confident = tf.boolean_mask(weight_mask, temp_valid_confident)
  weight_mask_valid = tf.boolean_mask(weight_mask, temp_valid)

  if FLAGS.pseudo_label_threshold > 0:
    acc_pseudo, acc_pseudo_op = tf.metrics.mean_per_class_accuracy(
        temp_label_confident,
        tf.boolean_mask(pred_pseudo, temp_valid_confident),
        outputs_to_num_classes[common.OUTPUT_TYPE],
        weights=weight_mask_confident)
    with tf.control_dependencies([acc_pseudo_op]):
      acc_pseudo = tf.identity(acc_pseudo, name='acc_pseudo')

  pred_weak = tf.cast(tf.argmax(prob_weak, axis=-1), tf.int32)
  pred_weak = tf.reshape(pred_weak, [-1])
  acc_weak, acc_weak_op = tf.metrics.mean_per_class_accuracy(
      temp_label_valid,
      tf.boolean_mask(pred_weak, temp_valid),
      outputs_to_num_classes[common.OUTPUT_TYPE],
      weights=weight_mask_valid)
  with tf.control_dependencies([acc_weak_op]):
    acc_weak = tf.identity(acc_weak, name='acc_weak')

  pred_strong = tf.cast(tf.argmax(logits_strong, axis=-1), tf.int32)
  pred_strong = tf.reshape(pred_strong, [-1])
  # For all pixels
  acc_strong, acc_strong_op = tf.metrics.mean_per_class_accuracy(
      temp_label_valid,
      tf.boolean_mask(pred_strong, temp_valid),
      outputs_to_num_classes[common.OUTPUT_TYPE],
      weights=weight_mask_valid)
  with tf.control_dependencies([acc_strong_op]):
    acc_strong = tf.identity(acc_strong, name='acc_strong')

  # For confident pixels
  if FLAGS.pseudo_label_threshold > 0:
    acc_strong_confident, acc_strong_confident_op = tf.metrics.mean_per_class_accuracy(
        temp_label_confident,
        tf.boolean_mask(pred_strong, temp_valid_confident),
        outputs_to_num_classes[common.OUTPUT_TYPE],
        weights=weight_mask_confident)
    with tf.control_dependencies([acc_strong_confident_op]):
      acc_strong_confident = tf.identity(
          acc_strong_confident, name='acc_strong_confident')

    valid_ratio = tf.reduce_sum(valid_mask) / tf.reduce_sum(valid_mask_pad)
    valid_ratio = tf.identity(valid_ratio, name='valid_ratio')

  ### Pixel-level data
  ## 1) Segmentation
  outputs_to_scales_to_logits = model.multi_scale_logits(
      samples_seg[common.IMAGE],
      model_options=model_options,
      image_pyramid=FLAGS.image_pyramid,
      weight_decay=FLAGS.weight_decay,
      is_training=True,
      fine_tune_batch_norm=FLAGS.fine_tune_batch_norm,
      nas_training_hyper_parameters={
          'drop_path_keep_prob': FLAGS.drop_path_keep_prob,
          'total_training_steps': FLAGS.training_number_of_steps,
      })

  # Add name to graph node so we can add to summary.
  output_type_dict = outputs_to_scales_to_logits[common.OUTPUT_TYPE]
  output_type_dict[model.MERGED_LOGITS_SCOPE] = tf.identity(
      output_type_dict[model.MERGED_LOGITS_SCOPE],
      name=common.OUTPUT_TYPE + '_seg')

  for output, num_classes in six.iteritems(outputs_to_num_classes):
    train_utils.add_softmax_cross_entropy_loss_for_each_scale(
        outputs_to_scales_to_logits[output],
        samples_seg[common.LABEL],
        num_classes,
        ignore_label,
        loss_weight=model_options.label_weights,
        upsample_logits=FLAGS.upsample_logits,
        hard_example_mining_step=FLAGS.hard_example_mining_step,
        top_k_percent_pixels=FLAGS.top_k_percent_pixels,
        scope=output)

  ## 2) Train self-attention module
  if FLAGS.use_attention:
    valid_mask_pad = samples_seg['valid']
    valid_mask_pad = tf.compat.v1.image.resize_nearest_neighbor(
        valid_mask_pad,
        preprocess_utils.resolve_shape(logits_weak, 4)[1:3])
    valid_mask_pad = tf.cast(valid_mask_pad, tf.float32)

    with tf.name_scope('seg_data_cls'):
      _, end_points_cls = feature_extractor.extract_features(
          samples_seg[common.IMAGE],
          output_stride=model_options.output_stride,
          multi_grid=model_options.multi_grid,
          model_variant=model_options.model_variant,
          depth_multiplier=model_options.depth_multiplier,
          divisible_by=model_options.divisible_by,
          weight_decay=FLAGS.weight_decay,
          reuse=tf.AUTO_REUSE,
          is_training=True,
          preprocessed_images_dtype=model_options.preprocessed_images_dtype,
          fine_tune_batch_norm=FLAGS.fine_tune_batch_norm,
          global_pool=True,
          num_classes=outputs_to_num_classes[common.OUTPUT_TYPE] - 1)

    logits_cls = end_points_cls['{}/logits'.format(FLAGS.model_variant).replace(
        '_beta', '')]
    logits_cls = tf.reshape(
        logits_cls, [-1, outputs_to_num_classes[common.OUTPUT_TYPE] - 1])

    _, att_cam_labeled = cam_func(
        end_points_cls,
        logits_cls,
        samples_seg['cls_label'],
        num_class=outputs_to_num_classes[common.OUTPUT_TYPE],
        use_attention=True,
        attention_dim=FLAGS.attention_dim,
        strides=[int(st) for st in FLAGS.att_strides],
        is_training=True,
        valid_mask=valid_mask_pad,
        net=FLAGS.model_variant.replace('_beta', ''))
    att_logits_labeled = att_cam_labeled

    # Loss
    train_utils.add_softmax_cross_entropy_loss_for_each_scale(
        {'self-attention_logits': att_logits_labeled},
        samples_seg[common.LABEL],
        outputs_to_num_classes[common.OUTPUT_TYPE],
        ignore_label,
        loss_weight=model_options.label_weights,
        upsample_logits=FLAGS.upsample_logits,
        hard_example_mining_step=FLAGS.hard_example_mining_step,
        top_k_percent_pixels=FLAGS.top_k_percent_pixels,
        scope='self-attention')

    att_logits_labeled = tf.identity(
        att_logits_labeled, name='att_logits_labeled')

    ## 3) If no image-level label, convert pixel-level label to train classifier
    if not FLAGS.weakly:
      # Seems that people usually use multi-label soft margin loss in PyTorch
      loss_cls = tf.nn.sigmoid_cross_entropy_with_logits(
          labels=samples_seg['cls_label'],
          logits=logits_cls)
      loss_cls = tf.reduce_mean(loss_cls)
      loss_cls = tf.identity(loss_cls, name='loss_cls')
      tf.compat.v1.losses.add_loss(loss_cls)

  ## 4) Sanity check. Monitor pixel accuracy
  logits_seg = outputs_to_scales_to_logits[common.OUTPUT_TYPE][
      model.MERGED_LOGITS_SCOPE]
  temp_label = tf.compat.v1.image.resize_nearest_neighbor(
      samples_seg[common.LABEL],
      preprocess_utils.resolve_shape(logits_seg, 4)[1:3])
  temp_label = tf.reshape(temp_label, [-1])

  dump = tf.concat(
      [tf.range(outputs_to_num_classes[common.OUTPUT_TYPE]), temp_label],
      axis=-1)
  _, _, count = tf.unique_with_counts(dump)
  num_pixel_list = count - 1
  # Exclude the ignore region
  num_pixel_list = num_pixel_list[:outputs_to_num_classes[common.OUTPUT_TYPE]]
  num_pixel_list = tf.cast(num_pixel_list, tf.float32)
  inverse_ratio = train_utils._div_maybe_zero(1, num_pixel_list)
  inverse_ratio = inverse_ratio / tf.reduce_sum(inverse_ratio)

  # Create weight mask to balance each class
  weight_mask = tf.einsum(
      '...y,y->...',
      tf.one_hot(
          temp_label,
          outputs_to_num_classes[common.OUTPUT_TYPE],
          dtype=tf.float32), inverse_ratio)
  temp_valid = tf.not_equal(temp_label, ignore_label)
  temp_label_valid = tf.boolean_mask(temp_label, temp_valid)
  weight_mask_valid = tf.boolean_mask(weight_mask, temp_valid)

  pred_seg = tf.argmax(logits_seg, axis=-1)
  pred_seg = tf.reshape(pred_seg, [-1])
  acc_seg, acc_seg_op = tf.metrics.mean_per_class_accuracy(
      temp_label_valid,
      tf.boolean_mask(pred_seg, temp_valid),
      outputs_to_num_classes[common.OUTPUT_TYPE],
      weights=weight_mask_valid)
  with tf.control_dependencies([acc_seg_op]):
    acc_seg = tf.identity(acc_seg, name='acc_seg')


def main(unused_argv):
  tf.logging.set_verbosity(tf.logging.INFO)
  tf.set_random_seed(FLAGS.seed)
  # Set up deployment (i.e., multi-GPUs and/or multi-replicas).
  config = model_deploy.DeploymentConfig(
      num_clones=FLAGS.num_clones,
      clone_on_cpu=FLAGS.clone_on_cpu,
      replica_id=FLAGS.task,
      num_replicas=FLAGS.num_replicas,
      num_ps_tasks=FLAGS.num_ps_tasks)

  # Split the batch across GPUs.
  assert FLAGS.train_batch_size % config.num_clones == 0, (
      'Training batch size not divisble by number of clones (GPUs).')
  clone_batch_size = FLAGS.train_batch_size // config.num_clones

  tf.gfile.MakeDirs(FLAGS.train_logdir)
  tf.logging.info('Training segmentation and self-attention on %s set',
                  FLAGS.train_split)
  if FLAGS.weakly:
    tf.logging.info('Training classification on %s set', FLAGS.train_split_cls)
  else:
    tf.logging.info('Training classification on %s set', FLAGS.train_split)
  tf.logging.info('Enforcing consistency constraint on %s set',
                  FLAGS.train_split_cls)

  with tf.Graph().as_default() as graph:
    with tf.device(config.inputs_device()):
      dataset = data_generator.Dataset(
          dataset_name=FLAGS.dataset,
          split_name=FLAGS.train_split,
          dataset_dir=FLAGS.dataset_dir,
          batch_size=clone_batch_size,
          crop_size=[int(sz) for sz in FLAGS.train_crop_size],
          min_resize_value=FLAGS.min_resize_value,
          max_resize_value=FLAGS.max_resize_value,
          resize_factor=FLAGS.resize_factor,
          min_scale_factor=FLAGS.min_scale_factor,
          max_scale_factor=FLAGS.max_scale_factor,
          scale_factor_step_size=FLAGS.scale_factor_step_size,
          model_variant=FLAGS.model_variant,
          num_readers=4,
          is_training=True,
          should_shuffle=True,
          should_repeat=True,
          output_valid=True,
          with_cls=True,
          cls_only=False)

      dataset_cls = data_generator.Dataset(
          dataset_name=FLAGS.dataset,
          split_name=FLAGS.train_split_cls,
          dataset_dir=FLAGS.dataset_dir,
          batch_size=clone_batch_size,
          crop_size=[int(sz) for sz in FLAGS.train_crop_size],
          min_resize_value=FLAGS.min_resize_value,
          max_resize_value=FLAGS.max_resize_value,
          resize_factor=FLAGS.resize_factor,
          min_scale_factor=FLAGS.min_scale_factor,
          max_scale_factor=FLAGS.max_scale_factor,
          scale_factor_step_size=FLAGS.scale_factor_step_size,
          model_variant=FLAGS.model_variant,
          num_readers=4,
          is_training=True,
          should_shuffle=True,
          should_repeat=True,
          with_cls=FLAGS.weakly,
          cls_only=False,
          strong_weak=True)

    # Create the global step on the device storing the variables.
    with tf.device(config.variables_device()):
      global_step = tf.train.get_or_create_global_step()

      # Define the model and create clones.
      model_fn = _build_pseudo_seg
      model_args = (dataset.get_one_shot_iterator(),
                    dataset_cls.get_one_shot_iterator(), {
                        common.OUTPUT_TYPE: dataset.num_of_classes
                    }, dataset.ignore_label, clone_batch_size)
      clones = model_deploy.create_clones(config, model_fn, args=model_args)

      # Gather update_ops from the first clone. These contain, for example,
      # the updates for the batch_norm variables created by model_fn.
      first_clone_scope = config.clone_scope(0)
      update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS, first_clone_scope)

    # Gather initial summaries.
    summaries = set(tf.get_collection(tf.GraphKeys.SUMMARIES))

    # Add summaries for model variables.
    for model_var in tf.model_variables():
      summaries.add(tf.summary.histogram(model_var.op.name, model_var))

    if FLAGS.use_attention:
      summary = graph.get_tensor_by_name(
          ('%s/%s:0' % (first_clone_scope, 'max_prob_weak')).strip('/'))
      summaries.add(tf.summary.histogram('max_prob_weak', summary))

      summary = graph.get_tensor_by_name(
          ('%s/%s:0' % (first_clone_scope, 'max_att_prob_weak')).strip('/'))
      summaries.add(tf.summary.histogram('max_att_prob_weak', summary))

      summary = graph.get_tensor_by_name(
          ('%s/%s:0' % (first_clone_scope, 'max_prob_avg')).strip('/'))
      summaries.add(tf.summary.histogram('max_prob_avg', summary))

      if FLAGS.soft_pseudo_label and FLAGS.temperature != 1.0:
        summary = graph.get_tensor_by_name(
            ('%s/%s:0' % (first_clone_scope, 'max_prob_avg_t')).strip('/'))
        summaries.add(tf.summary.histogram('max_prob_avg_t', summary))

    # Add summaries for images, labels, semantic predictions
    # Visualize seg image and predictions
    if FLAGS.save_summaries_images:
      summary_image = graph.get_tensor_by_name(
          ('%s/%s:0' % (first_clone_scope, common.IMAGE+'_seg')).strip('/'))
      summaries.add(
          tf.summary.image('samples/%s' % common.IMAGE+'_seg', summary_image))

      first_clone_label = graph.get_tensor_by_name(
          ('%s/%s:0' % (first_clone_scope, common.LABEL+'_seg')).strip('/'))
      # Scale up summary image pixel values for better visualization.
      pixel_scaling = max(1, 255 // dataset.num_of_classes)
      summary_label = tf.cast(first_clone_label * pixel_scaling, tf.uint8)
      summaries.add(
          tf.summary.image('samples/%s' % common.LABEL+'_seg', summary_label))

      first_clone_output = graph.get_tensor_by_name(
          ('%s/%s:0' %
           (first_clone_scope, common.OUTPUT_TYPE + '_seg')).strip('/'))
      predictions = tf.expand_dims(tf.argmax(first_clone_output, 3), -1)
      summary_predictions = tf.cast(predictions * pixel_scaling, tf.uint8)
      summaries.add(
          tf.summary.image(
              'samples/%s' % common.OUTPUT_TYPE+'_seg', summary_predictions))

      # For unlabeled image
      summary_image = graph.get_tensor_by_name(
          ('%s/%s:0' % (first_clone_scope, 'valid_mask')).strip('/'))
      summaries.add(
          tf.summary.image('sanity_check/valid_mask', summary_image))

      summary_image = graph.get_tensor_by_name(
          ('%s/%s:0' % (first_clone_scope, 'weak')).strip('/'))
      summaries.add(
          tf.summary.image('unlabeled/weak', summary_image))

      summary_image = graph.get_tensor_by_name(
          ('%s/%s:0' % (first_clone_scope, 'strong')).strip('/'))
      summaries.add(
          tf.summary.image('unlabeled/strong', summary_image))

      first_clone_label = graph.get_tensor_by_name(
          ('%s/%s:0' % (first_clone_scope, 'unlabeled')).strip('/'))
      pixel_scaling = max(1, 255 // dataset.num_of_classes)
      summary_label = tf.cast(first_clone_label * pixel_scaling, tf.uint8)
      summaries.add(
          tf.summary.image('unlabeled/%s' % common.LABEL, summary_label))

      first_clone_output = graph.get_tensor_by_name(
          ('%s/%s:0' % (first_clone_scope, 'logits_weak')).strip('/'))
      predictions = tf.expand_dims(tf.argmax(first_clone_output, 3), -1)
      summary_predictions = tf.cast(predictions * pixel_scaling, tf.uint8)
      summaries.add(
          tf.summary.image(
              'unlabeled/logits_weak', summary_predictions))

      first_clone_output = graph.get_tensor_by_name(
          ('%s/%s:0' % (first_clone_scope, 'logits_strong')).strip('/'))
      predictions = tf.expand_dims(tf.argmax(first_clone_output, 3), -1)
      summary_predictions = tf.cast(predictions * pixel_scaling, tf.uint8)
      summaries.add(
          tf.summary.image(
              'unlabeled/logits_strong', summary_predictions))

      if FLAGS.use_attention:
        first_clone_output = graph.get_tensor_by_name(
            ('%s/%s:0' % (first_clone_scope, 'att_logits_weak')).strip('/'))
        predictions = tf.expand_dims(tf.argmax(first_clone_output, 3), -1)
        predictions = tf.compat.v1.image.resize_bilinear(
            predictions, [int(sz) for sz in FLAGS.train_crop_size],
            align_corners=True)
        summary_predictions = tf.cast(predictions * pixel_scaling, tf.uint8)
        summaries.add(
            tf.summary.image(
                'att/att_logits_weak', summary_predictions))

        first_clone_output = graph.get_tensor_by_name(
            ('%s/%s:0' % (first_clone_scope, 'cam_weak')).strip('/'))
        predictions = tf.expand_dims(tf.argmax(first_clone_output, 3), -1)
        predictions = tf.compat.v1.image.resize_bilinear(
            predictions, [int(sz) for sz in FLAGS.train_crop_size],
            align_corners=True)
        summary_predictions = tf.cast(predictions * pixel_scaling, tf.uint8)
        summaries.add(
            tf.summary.image(
                'att/cam_weak', summary_predictions))

        first_clone_output = graph.get_tensor_by_name(
            ('%s/%s:0' % (first_clone_scope, 'merged_logits')).strip('/'))
        predictions = tf.expand_dims(tf.argmax(first_clone_output, 3), -1)
        predictions = tf.compat.v1.image.resize_bilinear(
            predictions, [int(sz) for sz in FLAGS.train_crop_size],
            align_corners=True)
        summary_predictions = tf.cast(predictions * pixel_scaling, tf.uint8)
        summaries.add(
            tf.summary.image(
                'att/merged_logits', summary_predictions))

        first_clone_output = graph.get_tensor_by_name(
            ('%s/%s:0' % (first_clone_scope, 'att_logits_labeled')).strip('/'))
        predictions = tf.expand_dims(tf.argmax(first_clone_output, 3), -1)
        predictions = tf.compat.v1.image.resize_bilinear(
            predictions, [int(sz) for sz in FLAGS.train_crop_size],
            align_corners=True)
        summary_predictions = tf.cast(predictions * pixel_scaling, tf.uint8)
        summaries.add(
            tf.summary.image(
                'att/att_logits_labeled', summary_predictions))

    # Add summaries for losses.
    for loss in tf.get_collection(tf.GraphKeys.LOSSES, first_clone_scope):
      summaries.add(tf.summary.scalar('losses/%s' % loss.op.name, loss))

    # Monitor pseudo label quality
    summary = graph.get_tensor_by_name(
        ('%s/%s:0' % (first_clone_scope, 'acc_seg')).strip('/'))
    summaries.add(tf.summary.scalar('sanity_check/acc_seg', summary))

    summary = graph.get_tensor_by_name(
        ('%s/%s:0' % (first_clone_scope, 'acc_weak')).strip('/'))
    summaries.add(tf.summary.scalar('sanity_check/acc_weak', summary))

    summary = graph.get_tensor_by_name(
        ('%s/%s:0' % (first_clone_scope, 'acc_strong')).strip('/'))
    summaries.add(tf.summary.scalar('sanity_check/acc_strong', summary))

    if FLAGS.pseudo_label_threshold > 0:
      summary = graph.get_tensor_by_name(
          ('%s/%s:0' % (first_clone_scope, 'acc_pseudo')).strip('/'))
      summaries.add(tf.summary.scalar('sanity_check/acc_pseudo', summary))

      summary = graph.get_tensor_by_name(
          ('%s/%s:0' % (first_clone_scope, 'acc_strong_confident')).strip('/'))
      summaries.add(
          tf.summary.scalar('sanity_check/acc_strong_confident', summary))

      summary = graph.get_tensor_by_name(
          ('%s/%s:0' % (first_clone_scope, 'valid_ratio')).strip('/'))
      summaries.add(tf.summary.scalar('sanity_check/valid_ratio', summary))

    # Build the optimizer based on the device specification.
    with tf.device(config.optimizer_device()):
      learning_rate = train_utils.get_model_learning_rate(
          FLAGS.learning_policy,
          FLAGS.base_learning_rate,
          FLAGS.learning_rate_decay_step,
          FLAGS.learning_rate_decay_factor,
          FLAGS.training_number_of_steps,
          FLAGS.learning_power,
          FLAGS.slow_start_step,
          FLAGS.slow_start_learning_rate,
          decay_steps=FLAGS.decay_steps,
          end_learning_rate=FLAGS.end_learning_rate)

      summaries.add(tf.summary.scalar('learning_rate', learning_rate))

      if FLAGS.optimizer == 'momentum':
        optimizer = tf.train.MomentumOptimizer(learning_rate, FLAGS.momentum)
      elif FLAGS.optimizer == 'adam':
        optimizer = tf.train.AdamOptimizer(
            learning_rate=FLAGS.adam_learning_rate, epsilon=FLAGS.adam_epsilon)
      else:
        raise ValueError('Unknown optimizer')

    startup_delay_steps = FLAGS.task * FLAGS.startup_delay_steps
    with tf.device(config.variables_device()):
      total_loss, grads_and_vars = model_deploy.optimize_clones(
          clones, optimizer)
      total_loss = tf.check_numerics(total_loss, 'Loss is inf or nan.')
      summaries.add(tf.summary.scalar('total_loss', total_loss))

      # Modify the gradients for biases and last layer variables.
      last_layers = model.get_extra_layer_scopes(
          FLAGS.last_layers_contain_logits_only)
      grad_mult = train_utils.get_model_gradient_multipliers(
          last_layers, FLAGS.last_layer_gradient_multiplier)
      if grad_mult:
        grads_and_vars = slim.learning.multiply_gradients(
            grads_and_vars, grad_mult)
      # NOTE: Neither last cls nor last seg layer loads pre-trained weights
      last_layers += [
          '{}/logits'.format(FLAGS.model_variant).replace('_beta', '')
      ]

      # Create gradient update op.
      grad_updates = optimizer.apply_gradients(
          grads_and_vars, global_step=global_step)
      update_ops.append(grad_updates)
      update_op = tf.group(*update_ops)
      with tf.control_dependencies([update_op]):
        train_tensor = tf.identity(total_loss, name='train_op')

    # Add the summaries from the first clone. These contain the summaries
    # created by model_fn and either optimize_clones() or _gather_clone_loss().
    summaries |= set(
        tf.get_collection(tf.GraphKeys.SUMMARIES, first_clone_scope))

    # Merge all summaries together.
    summary_op = tf.summary.merge(list(summaries))

    # Soft placement allows placing on CPU ops without GPU implementation.
    session_config = tf.ConfigProto(
        allow_soft_placement=True, log_device_placement=False)
    session_config.gpu_options.allow_growth = True

    # Start the training.
    profile_dir = FLAGS.profile_logdir
    if profile_dir is not None:
      tf.gfile.MakeDirs(profile_dir)

    with contrib_tfprof.ProfileContext(
        enabled=profile_dir is not None, profile_dir=profile_dir):
      init_fn = None
      if FLAGS.tf_initial_checkpoint:
        init_fn = train_utils.get_model_init_fn(
            FLAGS.train_logdir,
            FLAGS.tf_initial_checkpoint,
            FLAGS.initialize_last_layer,
            last_layers,
            ignore_missing_vars=True)

      slim.learning.train(
          train_tensor,
          logdir=FLAGS.train_logdir,
          log_every_n_steps=FLAGS.log_steps,
          master=FLAGS.master,
          number_of_steps=FLAGS.training_number_of_steps,
          is_chief=(FLAGS.task == 0),
          session_config=session_config,
          startup_delay_steps=startup_delay_steps,
          init_fn=init_fn,
          summary_op=summary_op,
          save_summaries_secs=FLAGS.save_summaries_secs,
          save_interval_secs=FLAGS.save_interval_secs)


if __name__ == '__main__':
  flags.mark_flag_as_required('train_logdir')
  flags.mark_flag_as_required('dataset_dir')
  tf.app.run()
