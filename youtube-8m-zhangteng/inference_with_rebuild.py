# Copyright 2016 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS-IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Binary for generating predictions over a set of videos."""

import os
import time

import numpy
import tensorflow as tf

from tensorflow import app
from tensorflow import flags
from tensorflow import gfile
from tensorflow import logging

import eval_util
import losses
import frame_level_models
import video_level_models
import readers
import utils
import numpy as np

FLAGS = flags.FLAGS

if __name__ == '__main__':
    flags.DEFINE_string("model_checkpoint_path", "",
                        "The file path to load the model from.")
    flags.DEFINE_string("output_dir", "",
                        "The file to save the predictions to.")
    flags.DEFINE_string(
        "input_data_pattern", "",
        "File glob defining the evaluation dataset in tensorflow.SequenceExample "
        "format. The SequenceExamples are expected to have an 'rgb' byte array "
        "sequence feature as well as a 'labels' int64 context feature.")

    # Model flags.
    flags.DEFINE_string("label_loss", "CrossEntropyLoss",
                        "Which loss function to use for training the model.")
    flags.DEFINE_bool(
        "frame_features", False,
        "If set, then --eval_data_pattern must be frame-level features. "
        "Otherwise, --eval_data_pattern must be aggregated video-level "
        "features. The model must also be set appropriately (i.e. to read 3D "
        "batches VS 4D batches.")
    flags.DEFINE_bool(
        "norm", True,
        "If set, then --input_data should be l2-normalized before follow-up processing. "
        "Otherwise, --input_data remain unchanged")
    flags.DEFINE_string(
        "model", "LogisticModel",
        "Which architecture to use for the model. Options include 'Logistic', "
        "'SingleMixtureMoe', and 'TwoLayerSigmoid'. See aggregated_models.py and "
        "frame_level_models.py for the model definitions.")
    flags.DEFINE_integer(
        "batch_size", 8192,
        "How many examples to process per batch.")
    flags.DEFINE_string("feature_names", "mean_rgb", "Name of the feature "
                        "to use for training.")
    flags.DEFINE_string("feature_sizes", "1024", "Length of the feature vectors.")
    flags.DEFINE_integer("file_size", 4096,
                         "Number of samples to be written into one tfrecord file.")

    # Other flags.
    flags.DEFINE_integer("num_readers", 1,
                         "How many threads to use for reading input files.")
    flags.DEFINE_integer("top_k", 20,
                         "How many predictions to output per video.")

def find_class_by_name(name, modules):
    """Searches the provided modules for the named class and returns it."""
    modules = [getattr(module, name, None) for module in modules]
    return next(a for a in modules if a)

def get_input_data_tensors(reader, data_pattern, batch_size, num_readers=1):
    """Creates the section of the graph which reads the input data.

    Args:
      reader: A class which parses the input data.
      data_pattern: A 'glob' style path to the data files.
      batch_size: How many examples to process at a time.
      num_readers: How many I/O threads to use.

    Returns:
      A tuple containing the features tensor, labels tensor, and optionally a
      tensor containing the number of frames per video. The exact dimensions
      depend on the reader being used.

    Raises:
      IOError: If no files matching the given pattern were found.
    """
    with tf.name_scope("input"):
        files = gfile.Glob(data_pattern)
        files.sort()
        if not files:
            raise IOError("Unable to find input files. data_pattern='" +
                          data_pattern + "'")
        logging.info("number of input files: " + str(len(files)))
        filename_queue = tf.train.string_input_producer(
            files, num_epochs=1, shuffle=False)
        examples_and_labels = [reader.prepare_reader(filename_queue)
                               for _ in range(num_readers)]

        video_id_batch, video_batch, unused_labels, num_frames_batch = (
            tf.train.batch_join(examples_and_labels,
                                batch_size=batch_size,
                                allow_smaller_final_batch=True,
                                enqueue_many=True))
        return video_id_batch, video_batch, unused_labels, num_frames_batch

def build_graph(reader,
                model,
                eval_data_pattern,
                label_loss_fn,
                batch_size=1024,
                num_readers=1):
    """Creates the Tensorflow graph for evaluation.

    Args:
      reader: The data file reader. It should inherit from BaseReader.
      model: The core model (e.g. logistic or neural net). It should inherit
             from BaseModel.
      eval_data_pattern: glob path to the evaluation data files.
      label_loss_fn: What kind of loss to apply to the model. It should inherit
                  from BaseLoss.
      batch_size: How many examples to process at a time.
      num_readers: How many threads to use for I/O operations.
    """

    global_step = tf.Variable(0, trainable=False, name="global_step")
    video_id_batch, model_input_raw, labels_batch, num_frames = get_input_data_tensors(  # pylint: disable=g-line-too-long
        reader,
        eval_data_pattern,
        batch_size=batch_size,
        num_readers=num_readers)
    tf.summary.histogram("model_input_raw", model_input_raw)

    feature_dim = len(model_input_raw.get_shape()) - 1

    # Normalize input features.
    if FLAGS.norm:
        model_input = tf.nn.l2_normalize(model_input_raw, feature_dim)
    else:
        model_input = model_input_raw

    with tf.name_scope("model"):
        result = model.create_model(model_input,
                                    num_frames=num_frames,
                                    vocab_size=reader.num_classes,
                                    labels=labels_batch,
                                    is_training=False)
        predictions = result["predictions"]
        tf.summary.histogram("model_activations", predictions)
        if "loss" in result.keys():
            label_loss = result["loss"]
        else:
            label_loss = label_loss_fn.calculate_loss(predictions, labels_batch)

    tf.add_to_collection("global_step", global_step)
    tf.add_to_collection("loss", label_loss)
    tf.add_to_collection("predictions", predictions)
    tf.add_to_collection("input_batch", model_input)
    tf.add_to_collection("video_id_batch", video_id_batch)
    tf.add_to_collection("num_frames", num_frames)
    tf.add_to_collection("labels", tf.cast(labels_batch, tf.float32))
    tf.add_to_collection("summary_op", tf.summary.merge_all())

def inference(video_id_batch, prediction_batch, label_batch, saver, out_file_location):
    global_step_val = -1
    with tf.Session() as sess:
        if FLAGS.model_checkpoint_path:
            checkpoint = FLAGS.model_checkpoint_path
        else:
            checkpoint = tf.train.latest_checkpoint(FLAGS.train_dir)
        if checkpoint:
            logging.info("Loading checkpoint for eval: " + checkpoint)
            # Restores from checkpoint
            saver.restore(sess, checkpoint)
            # Assuming model_checkpoint_path looks something like:
            # /my-favorite-path/yt8m_train/model.ckpt-0, extract global_step from it.
            global_step_val = checkpoint.split("/")[-1].split("-")[-1]
        else:
            logging.info("No checkpoint file found.")
            return global_step_val

        sess.run([tf.local_variables_initializer()])

        # Workaround for num_epochs issue.
        def set_up_init_ops(variables):
            init_op_list = []
            for variable in list(variables):
                if "train_input" in variable.name:
                    init_op_list.append(tf.assign(variable, 1))
                    variables.remove(variable)
            init_op_list.append(tf.variables_initializer(variables))
            return init_op_list

        sess.run(set_up_init_ops(tf.get_collection_ref(
            tf.GraphKeys.LOCAL_VARIABLES)))

        coord = tf.train.Coordinator()
        threads = tf.train.start_queue_runners(sess=sess, coord=coord)
        num_examples_processed = 0
        start_time = time.time()

        video_id = []
        video_label = []
        video_features = []
        filenum = 0

        directory = FLAGS.output_dir
        if not os.path.exists(directory):
            os.makedirs(directory)
        else:
            raise IOError("Output path exists! path='" + directory + "'")

        try:
            while not coord.should_stop():
                video_id_batch_val, video_label_batch_val, predictions = sess.run([video_id_batch, label_batch, prediction_batch])
                now = time.time()
                num_examples_processed += len(video_id_batch_val)

                video_id.append(video_id_batch_val)
                video_label.append(video_label_batch_val)
                video_features.append(predictions)

                if num_examples_processed>=FLAGS.file_size:
                    assert num_examples_processed==FLAGS.file_size, "num_examples_processed should be equal to file_size"
                    video_id = np.concatenate(video_id,axis=0)
                    video_label = np.concatenate(video_label,axis=0)
                    video_features = np.concatenate(video_features,axis=0)
                    write_to_record(video_id, video_label, video_features, filenum, num_examples_processed)
                    filenum += 1
                    video_id = []
                    video_label = []
                    video_features = []
                    num_examples_processed = 0

                logging.info("num examples processed: " + str(num_examples_processed) + " elapsed seconds: " + "{0:.2f}".format(now-start_time))


        except tf.errors.OutOfRangeError:
            logging.info('Done with inference. The output file was written to ' + out_file_location)
        finally:
            coord.request_stop()
            if num_examples_processed<FLAGS.file_size:
                video_id = np.concatenate(video_id,axis=0)
                video_label = np.concatenate(video_label,axis=0)
                video_features = np.concatenate(video_features,axis=0)
                write_to_record(video_id, video_label, video_features, filenum,num_examples_processed)

        coord.join(threads)
        sess.close()

def write_to_record(id_batch, label_batch, predictions, filenum, num_examples_processed):
    writer = tf.python_io.TFRecordWriter(FLAGS.output_dir + '/' + 'predictions-%03d.tfrecord' % filenum)
    for i in range(num_examples_processed):
        video_id = id_batch[i]
        label = np.nonzero(label_batch[i,:])[0]
        example = get_output_feature(video_id, label, [predictions[i,:]], ['predictions'])
        serialized = example.SerializeToString()
        writer.write(serialized)
    writer.close()

def get_output_feature(video_id, labels, features,feature_names):
    feature_maps = {'video_id': tf.train.Feature(bytes_list=tf.train.BytesList(value=[video_id])),
                    'labels': tf.train.Feature(int64_list=tf.train.Int64List(value=labels))}
    for feature_index in range(len(feature_names)):
        feature_maps[feature_names[feature_index]] = tf.train.Feature(
            float_list=tf.train.FloatList(value=features[feature_index]))
    example = tf.train.Example(features=tf.train.Features(feature=feature_maps))
    return example

def main(unused_argv):
    logging.set_verbosity(tf.logging.INFO)

    # convert feature_names and feature_sizes to lists of values
    feature_names, feature_sizes = utils.GetListOfFeatureNamesAndSizes(
        FLAGS.feature_names, FLAGS.feature_sizes)

    if FLAGS.frame_features:
        reader = readers.YT8MFrameFeatureReader(feature_names=feature_names,
                                                feature_sizes=feature_sizes)
    else:
        reader = readers.YT8MAggregatedFeatureReader(feature_names=feature_names,
                                                     feature_sizes=feature_sizes)

    if FLAGS.output_dir is "":
        raise ValueError("'output_dir' was not specified. "
                         "Unable to continue with inference.")

    if FLAGS.input_data_pattern is "":
        raise ValueError("'input_data_pattern' was not specified. "
                         "Unable to continue with inference.")


    model = find_class_by_name(FLAGS.model,
                               [frame_level_models, video_level_models])()
    label_loss_fn = find_class_by_name(FLAGS.label_loss, [losses])()

    if FLAGS.input_data_pattern is "":
        raise IOError("'input_data_pattern' was not specified. " +
                      "Nothing to evaluate.")

    build_graph(
        reader=reader,
        model=model,
        eval_data_pattern=FLAGS.input_data_pattern,
        label_loss_fn=label_loss_fn,
        num_readers=FLAGS.num_readers,
        batch_size=FLAGS.batch_size)

    logging.info("built evaluation graph")
    video_id_batch = tf.get_collection("video_id_batch")[0]
    prediction_batch = tf.get_collection("predictions")[0]
    label_batch = tf.get_collection("labels")[0]
    saver = tf.train.Saver(tf.global_variables())

    inference(video_id_batch, prediction_batch, label_batch, saver, FLAGS.output_dir)


if __name__ == "__main__":
    app.run()
