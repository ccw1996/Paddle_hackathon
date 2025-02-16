# Copyright (c) 2022 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging

from paddle.nn import Layer
from paddle.jit import to_static, not_to_static
from paddle.distributed.utils import get_logger
from paddle.fluid.framework import Operator, Parameter, _non_static_mode

from .utils import to_list


class ProxyLayer(Layer):
    """
    ProxyLayer implements all logic for converting dygraph model into
    static Program IR. Meanwhile, it provides conviential interfaces for
    auto parallel to visit feed/fetch/loss/metric variables.
    """

    def __init__(self, layer, loss_func, metrics):
        super(ProxyLayer, self).__init__()
        # NOTE: All verify logics are finished in Engine.Prepare
        self.inner_layer = layer
        self.loss_func = loss_func
        self.metrics = metrics
        # train / eval / predict
        self.mode = None

        # generated program vars
        self.input_vars = []
        self.label_vars = []
        self.output_vars = []
        self.loss_vars = []
        self.metric_vars = []

    def _train(self, inputs, labels):
        """
        Train process of inner_layer with forward/loss/metric logic.
        """
        # step 1. save feed variables of Program
        self.input_vars = inputs
        self.label_vars = labels

        # step 2. call inner_layer.forward
        self.output_vars = self.inner_layer(*inputs)

        # step 3. calculate loss if needed
        new_inputs = self._prepare(self.output_vars, labels)
        self.loss_vars = self.call_loss(new_inputs)

        # step 4. calculate metrics if needed
        self.metric_vars = self.call_metrics(new_inputs)

    def _eval(self, inputs, labels):
        """
        Evaluate process of inner_layer with forward/loss/metric logic.
        """
        # TODO(dev): we can reuse codes with self._train after making
        # sure if they can.

        # step 1. save feed variables of Program
        self.input_vars = inputs
        self.label_vars = labels

        # step 2. call inner_layer.forward
        self.output_vars = self.inner_layer(*inputs)

        # step 3. calculate loss if needed
        new_inputs = self._prepare(self.output_vars, labels)
        self.loss_vars = self.call_loss(new_inputs)

        # step 4. calculate metrics if needed
        self.metric_vars = self.call_metrics(new_inputs)

    def _predict(self, inputs):
        """
        Predict process of inner_layer with forward logic.
        """
        # step 1. save feed variables of Program
        self.input_vars = inputs

        # step 2. call inner_layer.forward
        self.output_vars = self.inner_layer(*inputs)

    @not_to_static
    def _prepare(self, outputs, labels):
        """
        Concat outputs and labels as a single list

        NOTE(dev): We use @not_to_static to avoid AST Analysis.
        """
        return to_list(outputs) + to_list(labels)

    def call_loss(self, inputs):
        """
        Apply Loss Function on outputs and labels.

        Args:
            inputs: List[Variable]

        Returns: List[Variable]
        """
        res = []
        if self.loss_func is not None:
            res = self.loss_func(*inputs)
        return res

    def call_metrics(self, inputs):
        """
        Apply Metrics Function on outputs and labels.

        Args:
            inputs: List[Variable]

        Returns: List[Variable]
        """
        outs = []
        for metric in self.metrics:
            outs.extend(metric.compute(*inputs))

        return outs

    def set_mode(self, mode):
        self.mode = mode
        self.training = mode == 'train'


class BuildInfo:

    def __init__(self, mode=None, state=False):
        self.mode = mode
        self.state = state

    def has_cache(self, mode):
        return self.mode == mode and self.state is True


class ProgramHelper(object):
    """
    A Helper class for Engine to provides different Program IR according specified 'mode'.
    """

    def __init__(self, layer, loss_func, metrics, inputs_spec, labels_spec):
        # original model config information
        # TODO(Aurelius84): Implenet append_backward and optimizer in ProxyLayer
        # after distribute engine satisify basic condition.
        self.proxy_layer = ProxyLayer(layer, loss_func, metrics)
        self.inputs_spec = inputs_spec
        self.labels_spec = labels_spec

        self.build_info = BuildInfo()
        self._logger = get_logger(logging.INFO)

    def build_program(self, mode):
        """
        Convert dygraph model into static Program IR.
        """
        assert mode in ['train', 'eval', 'predict']
        # skip if we has already built program.
        if self.build_info.has_cache(mode):
            self._logger.info(
                "Already build program with mode = %s, use cached program." %
                mode)
            return

        self._logger.info("start to build program for mode = %s." % mode)
        self.proxy_layer.mode = mode
        input_spec = [self.inputs_spec, self.labels_spec
                      ] if mode != 'predict' else [self.inputs_spec]
        static_func = to_static(self.static_func(), input_spec=input_spec)

        func_name = '_' + mode
        setattr(self.proxy_layer, func_name, static_func)

        # NOTE(dev): Because @to_static is a Lazy mechanism, so we explicitly call this to trigger
        # generating Program IR immediately.
        getattr(self.proxy_layer, func_name).concrete_program

    def _build_startup_program(self):
        """
        Create and Sync parameters into startup program.
        """
        for param in self.concrete_program.parameters:
            Parameter(name=param.name,
                      desc=param,
                      type=param.type,
                      shape=param.shape,
                      dtype=param.dtype,
                      stop_gradient=param.stop_gradient,
                      block=self.startup_program.global_block())

    def static_func(self):
        """
        Return target mode function.
        """
        assert self.proxy_layer.mode in [
            'train', 'eval', 'predict'
        ], "Please call build_program(mode) firstly."
        func_name = '_' + self.proxy_layer.mode
        return getattr(self.proxy_layer, func_name)

    @property
    def concrete_program(self):
        return self.static_func().concrete_program

    @property
    def main_program(self):
        return self.concrete_program.main_program

    @property
    def startup_program(self):
        return self.concrete_program.startup_program

    @property
    def input_vars(self):
        return to_list(self.proxy_layer.input_vars)

    @property
    def output_vars(self):
        return to_list(self.proxy_layer.output_vars)

    @property
    def label_vars(self):
        return to_list(self.proxy_layer.label_vars)

    @property
    def loss_vars(self):
        return to_list(self.proxy_layer.loss_vars)

    @property
    def metric_vars(self):
        return to_list(self.proxy_layer.metric_vars)
