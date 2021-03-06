# Copyright 2015 Conchylicultor. All Rights Reserved.
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
# ==============================================================================

"""
Model to generate new songs

"""

import numpy as np  # To generate random numbers
import tensorflow as tf

from deepmusic.musicdata import Batch
from deepmusic.keyboardcell import KeyboardCell
import deepmusic.songstruct as music


class Model:
    """
    Base class which manage the different models and experimentation.
    """

    class TargetWeightsPolicy:
        """ Structure to represent the different policy for choosing the target weights
        This is used to scale the contribution of each timestep to the global loss
        """
        NONE = 'none'  # All weights equals (=1.0) (default behavior)
        LINEAR = 'linear'  # The first outputs are less penalized than the last ones
        STEP = 'step'  # We start penalizing only after x steps (enco/deco behavior)

        def __init__(self, args):
            """
            Args:
                args: parameters of the model
            """
            self.args = args

        def get_weight(self, i):
            """ Return the target weight for the given step i using the chosen policy
            """
            if not self.args.target_weights or self.args.target_weights == Model.TargetWeightsPolicy.NONE:
                return 1.0
            elif self.args.target_weights == Model.TargetWeightsPolicy.LINEAR:
                return i / (self.args.sample_length - 1)  # Gradually increment the loss weight
            elif self.args.target_weights == Model.TargetWeightsPolicy.STEP:
                raise NotImplementedError('Step target weight policy not implemented yet, please consider another policy')
            else:
                raise ValueError('Unknown chosen target weight policy: {}'.format(self.args.target_weights))

        @staticmethod
        def get_policies():
            """ Return the list of the different modes
            Useful when parsing the command lines arguments
            """
            return [
                Model.TargetWeightsPolicy.NONE,
                Model.TargetWeightsPolicy.LINEAR,
                Model.TargetWeightsPolicy.STEP
            ]

    class ScheduledSamplingPolicy:
        """ Container for the schedule sampling policy
        See http://arxiv.org/abs/1506.03099 for more details
        """
        NONE = 'none'  # No scheduled sampling (always take the given input)
        ALWAYS = 'always'  # Always samples from the predicted output
        LINEAR = 'linear'  # Gradually increase the sampling rate

        def __init__(self, args):
            self.sampling_policy_fct = None

            assert args.scheduled_sampling
            assert len(args.scheduled_sampling) > 0

            policy = args.scheduled_sampling[0]
            if policy == Model.ScheduledSamplingPolicy.NONE:
                self.sampling_policy_fct = lambda step: 1.0
            elif policy == Model.ScheduledSamplingPolicy.ALWAYS:
                self.sampling_policy_fct = lambda step: 0.0
            elif policy == Model.ScheduledSamplingPolicy.LINEAR:
                if len(args.scheduled_sampling) != 5:
                    raise ValueError('Not the right arguments for the sampling linear policy ({} instead of 4)'.format(len(args.scheduled_sampling)-1))

                start_step = int(args.scheduled_sampling[1])
                end_step = int(args.scheduled_sampling[2])
                start_value = float(args.scheduled_sampling[3])
                end_value = float(args.scheduled_sampling[4])

                if (start_step >= end_step or
                   not (0.0 <= start_value <= 1.0) or
                   not (0.0 <= end_value <= 1.0)):
                    raise ValueError('Some schedule sampling parameters incorrect.')

                # TODO: Add default values (as optional arguments)

                def linear_policy(step):
                    if step < start_step:
                        threshold = start_value
                    elif start_step <= step < end_step:
                        slope = (start_value-end_value)/(start_step-end_step)  # < 0 (because end_step>start_step and start_value>end_value)
                        threshold = slope*(step-start_step) + start_value
                    elif end_step <= step:
                        threshold = end_value
                    else:
                        raise RuntimeError('Invalid value for the sampling policy')  # Parameters have not been correctly defined!
                    assert 0.0 <= threshold <= 1.0
                    return threshold

                self.sampling_policy_fct = linear_policy
            else:
                raise ValueError('Unknown chosen schedule sampling policy: {}'.format(policy))

        def get_prev_threshold(self, glob_step, i=0):
            """ Return the previous sampling probability for the current step.
            If above, the RNN should use the previous step instead of the given input.
            Args:
                glob_step (int): the global iteration step for the training
                i (int): the timestep of the RNN (TODO: implement incrementive slope (progression like -\|), remove the '=0')
            """
            return self.sampling_policy_fct(glob_step)

    class LearningRatePolicy:
        """ Contains the different policies for the learning rate decay
        """
        CST = 'cst'  # Fixed learning rate over all steps (default behavior)
        STEP = 'step'  # We divide the learning rate every x iterations
        EXPONENTIAL = 'exponential'  #

        @staticmethod
        def get_policies():
            """ Return the list of the different modes
            Useful when parsing the command lines arguments
            """
            return [
                Model.LearningRatePolicy.CST,
                Model.LearningRatePolicy.STEP,
                Model.LearningRatePolicy.EXPONENTIAL
            ]

        def __init__(self, args):
            """
            Args:
                args: parameters of the model
            """
            self.learning_rate_fct = None

            assert args.learning_rate
            assert len(args.learning_rate) > 0

            policy = args.learning_rate[0]

            if policy == Model.LearningRatePolicy.CST:
                if not len(args.learning_rate) == 2:
                    raise ValueError('Learning rate cst policy should be on the form: {} lr_value'.format(Model.LearningRatePolicy.CST))
                self.learning_rate_init = float(args.learning_rate[1])
                self.learning_rate_fct = self._lr_cst

            elif policy == Model.LearningRatePolicy.STEP:
                if not len(args.learning_rate) == 3:
                    raise ValueError('Learning rate step policy should be on the form: {} lr_init decay_period'.format(Model.LearningRatePolicy.STEP))
                self.learning_rate_init = float(args.learning_rate[1])
                self.decay_period = int(args.learning_rate[2])
                self.learning_rate_fct = self._lr_step

            elif policy == Model.LearningRatePolicy.EXPONENTIAL:
                raise NotImplementedError('Exponential learning rate policy not implemented yet, please consider another policy')

            else:
                raise ValueError('Unknown chosen learning rate policy: {}'.format(policy))

        def _lr_cst(self, glob_step):
            """ Just a constant learning rate
            """
            return self.learning_rate_init

        def _lr_step(self, glob_step):
            """ Every decay period, the learning rate is divided by 2
            """
            return self.learning_rate_init / 2**(glob_step//self.decay_period)

        def get_learning_rate(self, glob_step):
            """ Return the learning rate associated at the current training step
            Args:
                glob_step (int): Number of iterations since the beginning of training
            Return:
                float: the learning rate at the given step
            """
            return self.learning_rate_fct(glob_step)

    class EncoderNetwork:
        """ From the previous keyboard configuration, prepare the state for the next one
        """
        @staticmethod
        def get_cell(prev_keyboard, prev_state):
            prev_state_enco, prev_state_deco = prev_state

            # TODO
            next_state_enco = prev_state_enco

            return next_state_enco

    class DecoderNetwork:
        """ Predict a keyboard configuration at step t
        """
        def __init__(self, args):
            """
            Args:
                args: parameters of the model
            """
            self.args = args

            # Projection on the keyboard
            with tf.variable_scope('weights_note_projection'):
                self.W = tf.get_variable(
                    'weights',
                    [self.args.hidden_size, music.NB_NOTES],
                    initializer=tf.truncated_normal_initializer()  # TODO: Tune value (fct of hidden size)
                )
                self.b = tf.get_variable(
                    'bias',
                    [music.NB_NOTES],
                    initializer=tf.constant_initializer()
                )

            # TODO: DELETE
            with tf.variable_scope('weights_to_delete'):
                self.W_to_delete = tf.get_variable(
                    'weights',
                    [music.NB_NOTES, self.args.hidden_size],
                    initializer=tf.truncated_normal_initializer()
                )

        def _project_note(self, X):
            with tf.name_scope('note_projection'):
                return tf.matmul(X, self.W) + self.b  # [batch_size, NB_NOTE]

        def get_cell(self, prev_keyboard, prev_state_enco):

            # TODO
            hidden_state = tf.matmul(prev_keyboard, self.W_to_delete)
            next_keyboard = self._project_note(hidden_state)
            next_state_deco = prev_state_enco  # Return the last state (Useful ?)

            return next_keyboard, next_state_deco

    def __init__(self, args):
        """
        Args:
            args: parameters of the model
        """
        print('Model creation...')

        self.args = args  # Keep track of the parameters of the model

        # Placeholders
        self.inputs = None
        self.targets = None
        self.use_prev = None  # Boolean tensor which say at Graph evaluation time if we use the input placeholder or the previous output.
        self.current_learning_rate = None  # Allow to have a dynamic learning rate

        # Main operators
        self.opt_op = None  # Optimizer
        self.outputs = None  # Outputs of the network
        self.final_state = None  # When testing, we feed this value as initial state ?

        # Other options
        self.target_weights_policy = None
        self.schedule_policy = None
        self.learning_rate_policy = None

        # Construct the graphs
        self._build_network()

    def _build_network(self):
        """ Create the computational graph
        """

        # Placeholders (Use tf.SparseTensor with training=False instead) (TODO: Try restoring dynamic batch_size)
        with tf.name_scope('placeholder_inputs'):
            self.inputs = [
                tf.placeholder(
                    tf.float32,  # -1.0/1.0 ? Probably better for the sigmoid
                    [self.args.batch_size, music.NB_NOTES],
                    name='input')
                for _ in range(self.args.sample_length)
                ]
        with tf.name_scope('placeholder_targets'):
            self.targets = [
                tf.placeholder(
                    tf.float32,  # 0/1
                    [self.args.batch_size, music.NB_NOTES],
                    name='target')
                for _ in range(self.args.sample_length)
                ]
        with tf.name_scope('placeholder_use_prev'):
            self.use_prev = [
                tf.placeholder(
                    tf.bool,
                    [],
                    name='use_prev')
                for _ in range(self.args.sample_length)  # The first value will never be used (always takes self.input for the first step)
                ]

        # Define the network

        def loop_rnn(prev, i):
            """ Loop function used to connect one output of the rnn to the next input.
            The previous input and returned value have to be from the same shape.
            This is useful to use the same network for both training and testing.
            Args:
                prev: the previous predicted keyboard configuration at step i-1
                i: the current step id (Warning: start at 1, 0 is ignored)
            Return:
                tf.Tensor: the input at the step i
            """
            def activate_and_scale(X):
                """ Predict the output from prev and scale the result on [-1, 1]
                Use sigmoid activation
                Args:
                    X (tf.Tensor): the input
                Return:
                    tf.Ops: the activate_and_scale operator
                """
                # TODO: Use tanh instead ? tanh=2*sigm(2*x)-1
                with tf.name_scope('activate_and_scale'):
                    return tf.sub(tf.mul(2.0, tf.nn.sigmoid(X)), 1.0)  # x_{i} = 2*sigmoid(y_{i-1}) - 1

            next_input = activate_and_scale(prev)

            # On training, we force the correct input, on testing, we use the previous output as next input
            return tf.cond(self.use_prev[i], lambda: next_input, lambda: self.inputs[i])

        # TODO: Try attention decoder
        self.outputs, self.final_state = tf.nn.seq2seq.rnn_decoder(
            decoder_inputs=self.inputs,
            initial_state=None,  # The initial state is defined inside KeyboardCell
            cell=KeyboardCell(self.args),
            loop_function=loop_rnn
        )

        # For training only
        if not self.args.test:
            # Finally, we define the loss function

            # The network will predict a mix a wrong and right notes. For the loss function, we would like to
            # penalize note which are wrong. Eventually, the penalty should be less if the network predict the same
            # note but not in the right pitch (ex: C4 instead of C5), with a decay the further the prediction
            # is (D5 and D1 more penalized than D4 and D3 if the target is D2)

            # For now, by using sigmoid_cross_entropy_with_logits, the task is formulated as a NB_NOTES binary
            # classification problems

            self.schedule_policy = Model.ScheduledSamplingPolicy(self.args)
            self.target_weights_policy = Model.TargetWeightsPolicy(self.args)
            self.learning_rate_policy = Model.LearningRatePolicy(self.args)  # Load the chosen policies

            # TODO: If train on different length, check that the loss is proportional to the length or average ???
            loss_fct = tf.nn.seq2seq.sequence_loss(
                self.outputs,
                self.targets,
                [tf.constant(self.target_weights_policy.get_weight(i), shape=self.targets[0].get_shape()) for i in range(len(self.targets))],  # Weights
                softmax_loss_function=tf.nn.sigmoid_cross_entropy_with_logits,
                average_across_timesteps=False,  # I think it's best for variables length sequences (specially with the target weights=0), isn't it (it implies also that short sequences are less penalized than long ones) ? (TODO: For variables length sequences, be careful about the target weights)
                average_across_batch=False  # Penalize by sample (should allows dynamic batch size) Warning: need to tune the learning rate
            )
            tf.scalar_summary('training_loss', loss_fct)  # Keep track of the cost

            self.current_learning_rate = tf.placeholder(tf.float32, [])

            # Initialize the optimizer
            opt = tf.train.AdamOptimizer(
                learning_rate=self.current_learning_rate,
                beta1=0.9,
                beta2=0.999,
                epsilon=1e-08
            )

            # TODO: Also keep track of magnitudes (how much is updated)
            self.opt_op = opt.minimize(loss_fct)

    def step(self, batch, train_set=True, glob_step=-1, ret_output=False):
        """ Forward/training step operation.
        Does not perform run on itself but just return the operators to do so. Those have then to be run by the
        main program.
        If the output operator is returned, it will always be the last one on the list
        Args:
            batch (Batch): Input data on testing mode, input and target on output mode
            train_set (Bool): indicate if the batch come from the test/train set (not used when generating)
            glob_step (int): indicate the global step for the schedule sampling
            ret_output (Bool): for the training mode, if true,
        Return:
            Tuple[ops], dict: The list of the operators to run (training_step or outputs) with the associated feed dictionary
        """
        # TODO: Could optimize feeding between train/test/generating (compress code)

        # Feed the dictionary
        feed_dict = {}
        ops = ()  # For small length, it seems (from my investigations) that tuples are faster than list for merging

        # Feed placeholders and choose the ops
        if not self.args.test:  # Training
            if train_set:
                assert glob_step >= 0
                feed_dict[self.current_learning_rate] = self.learning_rate_policy.get_learning_rate(glob_step)

            for i in range(self.args.sample_length):
                feed_dict[self.inputs[i]] = batch.inputs[i]
                feed_dict[self.targets[i]] = batch.targets[i]
                #if np.random.rand() >= self.schedule_policy.get_prev_threshold(glob_step)*self.target_weights_policy.get_weight(i):  # Regular Schedule sample (TODO: Try sampling with the weigths or a mix of weights/sampling)
                if np.random.rand() >= self.schedule_policy.get_prev_threshold(glob_step):  # Weight the threshold by the target weights (don't schedule sample if weight=0)
                    feed_dict[self.use_prev[i]] = True
                else:
                    feed_dict[self.use_prev[i]] = False

            if train_set:
                ops += (self.opt_op,)
            if ret_output:
                ops += (self.outputs,)
        else:  # Generating (batch_size == 1)
            # TODO: What to put for initialisation state (empty ? random ?) ?
            # TODO: Modify use_prev
            for i in range(self.args.sample_length):
                if i < len(batch.inputs):
                    feed_dict[self.inputs[i]] = batch.inputs[i]
                    feed_dict[self.use_prev[i]] = False
                else:  # Even not used, we still need to feed a placeholder
                    feed_dict[self.inputs[i]] = batch.inputs[0]  # Could be anything but we need it to be from the right shape
                    feed_dict[self.use_prev[i]] = True  # When we don't have an input, we use the previous output instead

            ops += (self.outputs,)

        # Return one pass operator
        return ops, feed_dict
