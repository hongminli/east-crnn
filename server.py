#!/usr/bin/env python3

import os

import time
import datetime
import cv2
import numpy as np
import uuid
import json

import functools
import logging
import collections
from utils.config_utils import load_config

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Read configuration for width/height
cfg = load_config().cfg

@functools.lru_cache(maxsize=1)
def get_host_info():
    return {}



@functools.lru_cache(maxsize=100)
def get_crnn(checkpoint_path):
    import tensorflow as tf
    from models.crnn import crnn_model
    from utils import data_utils

    # Read configuration for width/height
    w, h = cfg.ARCH.INPUT_SIZE

    # Determine the number of classes.
    decoder = data_utils.TextFeatureIO().reader
    num_classes = len(decoder.char_dict) + 1

    g_2 = tf.Graph()
    with g_2.as_default():
        inputdata = tf.placeholder(dtype=tf.float32, shape=[1, h, w, 3], name='input')
        net = crnn_model.ShadowNet(phase='Test', hidden_nums=cfg.ARCH.HIDDEN_UNITS, layers_nums=cfg.ARCH.HIDDEN_LAYERS, num_classes=num_classes)
        with tf.variable_scope('shadow'):
            net_out = net.build_shadownet(inputdata=inputdata)

        decodes, _ = tf.nn.ctc_beam_search_decoder(inputs=net_out, sequence_length=cfg.ARCH.SEQ_LENGTH * np.ones(1), merge_repeated=False)
        decoder = data_utils.TextFeatureIO()

        saver = tf.train.Saver()
        sess = tf.Session(config=tf.ConfigProto(allow_soft_placement=True), graph=g_2)

        ckpt_state = tf.train.get_checkpoint_state(checkpoint_path)
        model_path = os.path.join(checkpoint_path, os.path.basename(ckpt_state.model_checkpoint_path))

        saver.restore(sess=sess, save_path=model_path)

        def predictor(img):
            img = cv2.resize(img, (w, h))
            img = np.expand_dims(img, axis=0).astype(np.float32)
            preds = sess.run(decodes, feed_dict={inputdata: img})
            preds = decoder.writer.sparse_tensor_to_str(preds[0])
            return preds

        return predictor

@functools.lru_cache(maxsize=100)
def get_predictor(checkpoint_path):
    logger.info('loading model')
    import tensorflow as tf
    from models.east import model
    from eval import resize_image, sort_poly, detect

    g_1 = tf.Graph()
    with g_1.as_default():
        input_images = tf.placeholder(tf.float32, shape=[None, None, None, 3], name='input_images')
        global_step = tf.get_variable('global_step', [], initializer=tf.constant_initializer(0), trainable=False)

        f_score, f_geometry = model.model(input_images, is_training=False)

        variable_averages = tf.train.ExponentialMovingAverage(0.997, global_step)
        saver = tf.train.Saver(variable_averages.variables_to_restore())

        sess = tf.Session(config=tf.ConfigProto(allow_soft_placement=True), graph=g_1)

        ckpt_state = tf.train.get_checkpoint_state(checkpoint_path)
        model_path = os.path.join(checkpoint_path, os.path.basename(ckpt_state.model_checkpoint_path))
        logger.info('Restore from {}'.format(model_path))
        saver.restore(sess, model_path)

        def predictor(img):
            """
            :return: {
                'text_lines': [
                    {
                        'score': ,
                        'x0': ,
                        'y0': ,
                        'x1': ,
                        ...
                        'y3': ,
                    }
                ],
                'rtparams': {  # runtime parameters
                    'image_size': ,
                    'working_size': ,
                },
                'timing': {
                    'net': ,
                    'restore': ,
                    'nms': ,
                    'cpuinfo': ,
                    'meminfo': ,
                    'uptime': ,
                }
            }
            """
            start_time = time.time()
            rtparams = collections.OrderedDict()
            rtparams['start_time'] = datetime.datetime.now().isoformat()
            rtparams['image_size'] = '{}x{}'.format(img.shape[1], img.shape[0])
            timer = collections.OrderedDict([
                ('net', 0),
                ('restore', 0),
                ('nms', 0)
            ])

            im_resized, (ratio_h, ratio_w) = resize_image(img)
            rtparams['working_size'] = '{}x{}'.format(
                im_resized.shape[1], im_resized.shape[0])
            start = time.time()
            score, geometry = sess.run(
                [f_score, f_geometry],
                feed_dict={input_images: [im_resized[:,:,::-1]]})
            timer['net'] = time.time() - start

            boxes, timer = detect(score_map=score, geo_map=geometry, timer=timer)
            logger.info('net {:.0f}ms, restore {:.0f}ms, nms {:.0f}ms'.format(
                timer['net']*1000, timer['restore']*1000, timer['nms']*1000))

            if boxes is not None:
                scores = boxes[:,8].reshape(-1)
                boxes = boxes[:, :8].reshape((-1, 4, 2))
                boxes[:, :, 0] /= ratio_w
                boxes[:, :, 1] /= ratio_h

            duration = time.time() - start_time
            timer['overall'] = duration
            logger.info('[timing] {}'.format(duration))

            text_lines = []
            if boxes is not None:
                text_lines = []
                for box, score in zip(boxes, scores):
                    box = sort_poly(box.astype(np.int32))
                    if np.linalg.norm(box[0] - box[1]) < 5 or np.linalg.norm(box[3]-box[0]) < 5:
                        continue
                    tl = collections.OrderedDict(zip(
                        ['x0', 'y0', 'x1', 'y1', 'x2', 'y2', 'x3', 'y3'],
                        map(float, box.flatten())))
                    tl['score'] = float(score)
                    text_lines.append(tl)
            ret = {
                'text_lines': text_lines,
                'rtparams': rtparams,
                'timing': timer,
            }
            ret.update(get_host_info())
            return ret


        return predictor


### the webserver
from flask import Flask, request, render_template
import argparse


class Config:
    SAVE_DIR = 'static/results'


config = Config()


app = Flask(__name__)

@app.route('/')
def index():
    return render_template('index.html', session_id='dummy_session_id')


def draw_illu(illu, rst):
    for t in rst['text_lines']:
        d = np.array([t['x0'], t['y0'], t['x1'], t['y1'], t['x2'],
                      t['y2'], t['x3'], t['y3']], dtype='int32')
        d = d.reshape(-1, 2)
        cv2.polylines(illu, [d], isClosed=True, color=(255, 255, 0))
    return illu


def save_result(img, rst):
    session_id = str(uuid.uuid1())
    dirpath = os.path.join(config.SAVE_DIR, session_id)
    os.makedirs(dirpath)

    # save input image
    output_path = os.path.join(dirpath, 'input.png')
    cv2.imwrite(output_path, img)

    # save illustration
    output_path = os.path.join(dirpath, 'output.png')
    cv2.imwrite(output_path, draw_illu(img.copy(), rst))

    # save json data
    output_path = os.path.join(dirpath, 'result.json')
    with open(output_path, 'w') as f:
        json.dump(rst, f)

    rst['session_id'] = session_id
    return rst

@app.route('/', methods=['POST'])
def index_post():
    global predictor
    import io
    bio = io.BytesIO()
    request.files['image'].save(bio)
    img = cv2.imdecode(np.frombuffer(bio.getvalue(), dtype='uint8'), 1)
    rst = get_predictor(cfg.PATH.EAST_MODEL_SAVE_DIR)(img)

    for line in rst["text_lines"]:
        xt = int(min(line["x0"], line["x2"])) - 1
        yt = int(min(line["y0"], line["y2"])) - 1
        xb = int(max(line["x1"], line["x3"])) + 1
        yb = int(max(line["y1"], line["y3"])) + 1
        cropped = img[yt:yb, xt:xb]
        line["text"] = get_crnn(cfg.PATH.CRNN_MODEL_SAVE_DIR)(cropped)[0]

    save_result(img, rst)
    return render_template('index.html', session_id=rst['session_id'])


def main():
    global checkpoint_path
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', default=8769, type=int)
    parser.add_argument('--debug', action='store_true')
    args = parser.parse_args()
    app.debug = args.debug
    app.run('0.0.0.0', args.port)

if __name__ == '__main__':
    main()

