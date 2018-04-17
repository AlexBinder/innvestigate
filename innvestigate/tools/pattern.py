# Begin: Python 2/3 compatibility header small
# Get Python 3 functionality:
from __future__ import\
    absolute_import, print_function, division, unicode_literals
from future.utils import raise_with_traceback, raise_from
# catch exception with: except Exception as e
from builtins import range, map, zip, filter
from io import open
import six
# End: Python 2/3 compatability header small


###############################################################################
###############################################################################
###############################################################################


import keras.backend as K
import keras.layers
import keras.models
import keras.optimizers
import keras.utils
import numpy as np


from .. import layers as ilayers
from .. import utils as iutils
from ..utils import keras as kutils
from ..utils.keras import checks as kchecks
from ..utils.keras import graph as kgraph


__all__ = [
    "BasePattern",
    "PatternComputer",
]


###############################################################################
###############################################################################
###############################################################################


def _get_active_neuron_io(layer, active_node_indices,
                          return_i=True, return_o=True,
                          do_activation_search=False):

    def contains_activation(layer):
        return (kchecks.contains_activation(layer) and
                not kchecks.contains_activation(layer, "linear"))

    def get_Xs(node_index):
        return iutils.to_list(layer.get_input_at(node_index))

    def get_Ys(node_index):
        ret = iutils.to_list(layer.get_output_at(node_index))

        if(do_activation_search is not False and
           not contains_activation(layer)):
            # walk along execution graph until we find an activation function
            # if current layer has not.
            execution_list = do_activation_search

            # First find current node.
            layer_i = None
            for i, node in enumerate(execution_list):
                if layer is node[0]:
                    layer_i = i
                    break

            assert layer_i is not None
            assert len(ret) == 1
            input_to_next_layer = ret[0]

            found = False
            for i in range(layer_i+1, len(execution_list)):
                l, Xs, Ys = execution_list[i]
                if input_to_next_layer in Xs:
                    if not isinstance(
                            l,
                            kchecks.get_activation_search_safe_layers()):
                        break
                    if contains_activation(l):
                        found = Ys
                        break
                    assert len(Ys) == 1
                    input_to_next_layer = Ys[0]

            if found is not False:
                ret = Ys

        return ret

    tmp = [kgraph.get_layer_neuronwise_io(layer, Xs=get_Xs(i), Ys=get_Ys(i),
                                          return_i=return_i, return_o=return_o)
           for i in active_node_indices]

    if len(tmp) == 1:
        return tmp[0]
    else:
        raise NotImplementedError("This code seems not to handle several Ys.")
        # Layer is applied several times in model.
        # Concatenate the io of the applications.
        concatenate = keras.layers.Concatenate(axis=0)

        if return_i and return_o:
            return (concatenate([x[0] for x in tmp]),
                    concatenate([x[1] for x in tmp]))
        else:
            return concatenate([x[0] for x in tmp])


###############################################################################
###############################################################################
###############################################################################


class BasePattern(object):

    def __init__(self,
                 model,
                 layer,
                 model_tensors=None,
                 execution_list=None):
        self.model = model
        self.layer = layer
        # All the tensors used by the model.
        # Allows to filter nodes in layers that do not
        # belong to this model.
        self.model_tensors = model_tensors
        self.execution_list = execution_list
        self._active_node_indices = self._get_active_node_indices()

    def _get_active_node_indices(self):
        n_nodes = kgraph.get_layer_inbound_count(self.layer)
        if self.model_tensors is None:
            return list(range(n_nodes))
        else:
            ret = []
            for i in range(n_nodes):
                output_tensors = iutils.to_list(self.layer.get_output_at(i))
                # Check if output is used in the model.
                if all([tmp in self.model_tensors
                        for tmp in output_tensors]):
                    ret.append(i)
            return ret

    def has_pattern(self):
        return kchecks.contains_kernel(self.layer)

    def stats_from_batch(self):
        raise NotImplementedError()

    def compute_pattern(self):
        raise NotImplementedError()


class DummyPattern(BasePattern):

    def get_stats_from_batch(self):
        Xs, Ys = _get_active_neuron_io(self.layer,
                                       self._active_node_indices)
        self.mean_x = ilayers.RunningMeans()

        count = ilayers.CountNonZero(axis=0)(Ys[0])
        sum_x = ilayers.Dot()([ilayers.Transpose()(Xs[0]), Ys[0]])

        mean_x, count_x = self.mean_x([sum_x, count])

        # Return dummy output to have connected graph!
        return ilayers.Sum(axis=None)(count_x)

    def compute_pattern(self):
        return self.mean_x.get_weights()[0]


class LinearPattern(BasePattern):

    def _get_neuron_mask(self):
        Ys = _get_active_neuron_io(self.layer,
                                   self._active_node_indices,
                                   return_i=False, return_o=True)

        return ilayers.OnesLike()(Ys[0])

    def get_stats_from_batch(self):
        # todo: reuse_sybmolic tensors and apply once to ALL input tensors
        layer = kgraph.copy_layer_wo_activation(self.layer,
                                                keep_bias=False,
                                                reuse_symbolic_tensors=False)
        Xs, Ys = _get_active_neuron_io(layer, self._active_node_indices)
        if len(Ys) != 1:
            raise ValueError("Assume that kernel layer have only one output.")
        X, Y = Xs[0], Ys[0]

        self.mean_x = ilayers.RunningMeans()
        self.mean_y = ilayers.RunningMeans()
        self.mean_xy = ilayers.RunningMeans()

        # Compute mask and active neuron counts.
        mask = ilayers.AsFloatX()(self._get_neuron_mask())
        Y_masked = keras.layers.multiply([Y, mask])
        count = ilayers.CountNonZero(axis=0)(mask)
        count_all = ilayers.Sum(axis=0)(ilayers.OnesLike()(mask))

        # Get means ...
        def norm(x, count):
            return ilayers.SafeDivide(factor=1)([x, count])

        # ... along active neurons.
        mean_x = norm(ilayers.Dot()([ilayers.Transpose()(X), mask]), count)
        mean_xy = norm(ilayers.Dot()([ilayers.Transpose()(X), Y_masked]),
                       count)

        _, a = self.mean_x([mean_x, count])
        _, b = self.mean_xy([mean_xy, count])

        # ... along all neurons.
        mean_y = norm(ilayers.Sum(axis=0)(Y), count_all)
        _, c = self.mean_y([mean_y, count_all])

        # Create a dummy output to have a connected graph.
        # Needs to have the shape (mb_size, 1)
        dummy = keras.layers.Average()([a, b, c])
        return ilayers.Sum(axis=None)(dummy)

    def compute_pattern(self):

        def safe_divide(a, b):
            return a / (b + (b == 0))

        W = kgraph.get_kernel(self.layer)
        W2D = W.reshape((-1, W.shape[-1]))

        mean_x = self.mean_x.get_weights()[0]
        mean_y = self.mean_y.get_weights()[0]
        mean_xy = self.mean_xy.get_weights()[0]

        ExEy = mean_x * mean_y
        cov_xy = mean_xy - ExEy

        w_cov_xy = np.diag(np.dot(W2D.T, cov_xy))
        A = safe_divide(cov_xy, w_cov_xy[None, :])

        # update length
        if False:
            norm = np.diag(np.dot(W2D.T, A))
            A = safe_divide(A, norm)

        # check pattern
        if False:
            tmp = np.diag(np.dot(W2D.T, A))
            print("pattern_check", W.shape, tmp.min(), tmp.max())

        return A.reshape(W.shape)


class ReluPositivePattern(LinearPattern):

    def _get_neuron_mask(self):
        Ys = _get_active_neuron_io(self.layer,
                                   self._active_node_indices,
                                   return_i=False, return_o=True,
                                   do_activation_search=self.execution_list)
        return ilayers.GreaterThanZero()(Ys[0])


class ReluNegativePattern(LinearPattern):

    def _get_neuron_mask(self):
        Ys = _get_active_neuron_io(self.layer,
                                   self._active_node_indices,
                                   return_i=False, return_o=True,
                                   do_activation_search=self.execution_list)
        return ilayers.LessThanZero()(Ys[0])


def get_pattern_class(pattern_type):
    return {
        "dummy": DummyPattern,

        "linear": LinearPattern,
        "relu": ReluPositivePattern,
        "relu.positive": ReluPositivePattern,
        "relu.negative": ReluNegativePattern,
    }.get(pattern_type, pattern_type)


###############################################################################
###############################################################################
###############################################################################


class PatternComputer(object):
    """Pattern computer.

    Computes a pattern for each layer with a kernel of a given model.

    :param model: A Keras model.
    :param pattern_type: A string or a tuple of strings. Valid types are
      'linear', 'relu', 'relu.positive', 'relu.negative'.
    :param compute_layers_in_parallel: Not supported yet.
      Compute all patterns at once.
      Otherwise computer layer after layer.
    :param gpus: Not supported yet. Gpus to use.
    """

    def __init__(self, model,
                 pattern_type="linear",
                 # todo: this options seems to be buggy,
                 # if it sequential tensorflow still pushes all models to gpus
                 compute_layers_in_parallel=True,
                 gpus=None):
        self.model = model
        pattern_types = iutils.to_list(pattern_type)
        self.pattern_types = {k: get_pattern_class(k)
                              for k in pattern_types}
        self.compute_layers_in_parallel = compute_layers_in_parallel
        self.gpus = gpus

        if self.compute_layers_in_parallel is False:
            raise NotImplementedError("Not supported.")

        # create pattern instances and collect keras outputs
        self._work_sequence = []
        self._pattern_instances = {k: [] for k in self.pattern_types}
        computer_outputs = []
        # Broadcaster has shape (mb, 1)
        # Todod: does not work for tensors
        reduce_axes = list(range(len(K.int_shape(model.inputs[0]))))[1:]
        dummy_broadcaster = ilayers.Sum(axis=reduce_axes,
                                        keepdims=True)(model.inputs[0])

        def broadcast(x):
            return ilayers.Broadcast()([dummy_broadcaster, x])

        layers, execution_list, _ = kgraph.trace_model_execution(model)
        model_tensors = set()
        for _, input_tensors, output_tensors in execution_list:
            for t in input_tensors+output_tensors:
                model_tensors.add(t)

        for layer_id, layer in enumerate(layers):
            # This does not work with containers!
            # They should be replaced by trace_model_execution.
            if kchecks.is_container(layer):
                raise Exception("Container in container is not suppored!")
            for pattern_type, clazz in six.iteritems(self.pattern_types):
                pinstance = clazz(model, layer,
                                  model_tensors=model_tensors,
                                  execution_list=execution_list)
                if pinstance.has_pattern() is False:
                    continue
                self._pattern_instances[pattern_type].append(pinstance)
                dummy_output = pinstance.get_stats_from_batch()
                # Broadcast dummy_output to right shape.
                computer_outputs += iutils.to_list(broadcast(dummy_output))

        # initialize the keras outputs
        self._n_computer_outputs = len(computer_outputs)
        if self.compute_layers_in_parallel is True:
            self._computers = [
                keras.models.Model(inputs=model.inputs,
                                   outputs=computer_outputs)
            ]
        else:
            self._computers = [
                keras.models.Model(inputs=model.inputs,
                                   outputs=computer_output)
                for computer_output in computer_outputs
            ]

        # distribute computation on more gpus
        if self.gpus is not None and self.gpus > 1:
            raise NotImplementedError("Not supported yet.")
            self._computers = [keras.utils.multi_gpu_model(tmp, gpus=self.gpus)
                               for tmp in self._computers]
        # todo: model compiling?
        pass

    def compute(self, X, batch_size=32, verbose=0):
        """
        Compute and return the patterns for the model and the data `X`.

        :param X: Data to compute patterns.
        :param batch_size: Batch size to use.
        :param verbose: As for keras model.fit.
        """
        generator = iutils.BatchSequence(X, batch_size)
        return self.compute_generator(generator, verbose=verbose)

    def compute_generator(self, generator, **kwargs):
        """
        Compute and return the patterns for the model and the data `X`.

        :param generator: Data to compute patterns.
        :param kwargs: Same as for keras model.fit_generator.
        """
        if not hasattr(self, "_computers"):
            raise Exception("One shot computer. Already used.")

        # We don't do gradient updates.
        class NoOptimizer(keras.optimizers.Optimizer):
            def get_updates(self, *args, **kwargs):
                return []
        optimizer = NoOptimizer()
        # We only go over the training data once.
        if "epochs" in kwargs and kwargs["epochs"] != 1:
            raise ValueError("Pattern are computed with "
                             "a closed form solution. "
                             "Only need to do one epoch.")
        kwargs["epochs"] = 1

        if self.compute_layers_in_parallel is True:
            n_dummy_outputs = self._n_computer_outputs
        else:
            n_dummy_outputs = 1

        # Augment the input with dummy targets.
        def get_dummy_targets(Xs):
            n, dtype = Xs[0].shape[0], Xs[0].dtype
            dummy = np.ones(shape=(n, 1), dtype=dtype)
            return [dummy for _ in range(n_dummy_outputs)]

        if isinstance(generator, keras.utils.Sequence):
            generator = iutils.TargetAugmentedSequence(generator,
                                                       get_dummy_targets)
        else:
            base_generator = generator

            def generator(*args, **kwargs):
                for Xs in base_generator(*args, **kwargs):
                    Xs = iutils.to_list(Xs)
                    yield Xs, get_dummy_targets(Xs)

        # Compile models.
        for computer in self._computers:
            computer.compile(optimizer=optimizer, loss=lambda x, y: x)

        # Compute pattern statistics.
        for computer in self._computers:

            computer.fit_generator(generator, **kwargs)

        # retrieve the actual patterns
        pis = self._pattern_instances
        patterns = {ptype: [tmp.compute_pattern() for tmp in pis[ptype]]
                    for ptype in self.pattern_types}

        # free memory
        del self._computers
        del self._work_sequence
        del self._pattern_instances

        if len(self.pattern_types) == 1:
            return patterns[list(self.pattern_types.keys())[0]]
        else:
            return patterns
