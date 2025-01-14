"""A Quantizer defines the way of transforming a full precision input to a
quantized output and the pseudo-gradient method used for the backwards pass.

Quantizers can either be used through quantizer arguments that are supported
for Larq layers, such as `input_quantizer` and `kernel_quantizer`; or they
can be used similar to activations, i.e. either through an `Activation` layer,
or through the `activation` argument supported by all forward layers:

```python
import tensorflow as tf
import larq as lq
...
x = lq.layers.QuantDense(64, activation=None)(x)
x = lq.layers.QuantDense(64, input_quantizer="ste_sign")(x)
```

is equivalent to:

```python
x = lq.layers.QuantDense(64)(x)
x = tf.keras.layers.Activation("ste_sign")(x)
x = lq.layers.QuantDense(64)(x)
```

as well as:

```python
x = lq.layers.QuantDense(64, activation="ste_sign")(x)
x = lq.layers.QuantDense(64)(x)
```

We highly recommend using the first of these formulations: for the
other two formulations, intermediate layers - like batch normalization or
average pooling - and shortcut connections may result in non-binary input
to the convolutions.

Quantizers can either be referenced by string or called directly.
The following usages are equivalent:

```python
lq.layers.QuantDense(64, kernel_quantizer="ste_sign")
```
```python
lq.layers.QuantDense(64, kernel_quantizer=lq.quantizers.SteSign(clip_value=1.0))
```
"""
from typing import Callable, Union
import os
import tensorflow as tf

from larq import context, math
from larq import metrics as lq_metrics
from larq import utils
from larq import layers

__all__ = [
    "ApproxSign",
    "DoReFa",
    "DoReFaQuantizer",
    "MagnitudeAwareSign",
    "NoOp",
    "NoOpQuantizer",
    "Quantizer",
    "SteHeaviside",
    "SteSign",
    "SteTern",
    "SwishSign",
    "LAB",
    "Niblack",
    "Sauvola"
]


def _clipped_gradient(x, dy, clip_value):
    """Calculate `clipped_gradent * dy`."""

    if clip_value is None:
        return dy

    zeros = tf.zeros_like(dy)
    mask = tf.math.less_equal(tf.math.abs(x), clip_value)
    return tf.where(mask, dy, zeros)


def ste_sign(x: tf.Tensor, clip_value: float = 1.0) -> tf.Tensor:
    @tf.custom_gradient
    def _call(x):
        def grad(dy):
            return _clipped_gradient(x, dy, clip_value)

        return math.sign(x), grad

    return _call(x)


def _scaled_sign(x):  # pragma: no cover
    return 1.3 * ste_sign(x)


@tf.custom_gradient
def approx_sign(x: tf.Tensor) -> tf.Tensor:
    def grad(dy):
        abs_x = tf.math.abs(x)
        zeros = tf.zeros_like(dy)
        mask = tf.math.less_equal(abs_x, 1.0)
        return tf.where(mask, (1 - abs_x) * 2 * dy, zeros)

    return math.sign(x), grad


def swish_sign(x: tf.Tensor, beta: float = 5.0) -> tf.Tensor:
    @tf.custom_gradient
    def _call(x):
        def grad(dy):
            b_x = beta * x
            return dy * beta * (2 - b_x * tf.tanh(b_x * 0.5)) / (1 + tf.cosh(b_x))

        return math.sign(x), grad

    return _call(x)


def ste_tern(
    x: tf.Tensor,
    threshold_value: float = 0.05,
    ternary_weight_networks: bool = False,
    clip_value: float = 1.0,
) -> tf.Tensor:
    @tf.custom_gradient
    def _call(x):
        if ternary_weight_networks:
            threshold = 0.7 * tf.reduce_sum(tf.abs(x)) / tf.cast(tf.size(x), x.dtype)
        else:
            threshold = threshold_value

        def grad(dy):
            return _clipped_gradient(x, dy, clip_value)

        return tf.sign(tf.sign(x + threshold) + tf.sign(x - threshold)), grad

    return _call(x)


def ste_heaviside(x: tf.Tensor, clip_value: float = 1.0) -> tf.Tensor:
    @tf.custom_gradient
    def _call(x):
        def grad(dy):
            return _clipped_gradient(x, dy, clip_value)

        return math.heaviside(x), grad

    return _call(x)

class Quantizer(tf.keras.layers.Layer):
    """Common base class for defining quantizers.

    # Attributes
        precision: An integer defining the precision of the output. This value will be
            used by `lq.models.summary()` for improved logging.
    """

    precision = None

    def compute_output_shape(self, input_shape):
        return input_shape


class _BaseQuantizer(Quantizer):
    """Private base class for defining quantizers with Larq metrics."""

    def __init__(self, *args, metrics=None, **kwargs):
        self._custom_metrics = metrics
        super().__init__(*args, **kwargs)

    def build(self, input_shape):
        if self._custom_metrics and "flip_ratio" in self._custom_metrics:
            self.flip_ratio = lq_metrics.FlipRatio(name=f"flip_ratio/{self.name}")
            self.flip_ratio.build(input_shape)
        super().build(input_shape)

    def call(self, inputs):
        if hasattr(self, "flip_ratio"):
            self.add_metric(self.flip_ratio(inputs))
        return inputs

    @property
    def non_trainable_weights(self):
        return []


@utils.register_keras_custom_object
class NoOp(_BaseQuantizer):
    r"""Instantiates a serializable no-op quantizer.

    \\[
    q(x) = x
    \\]

    !!! warning
        This quantizer will not change the input variable. It is only intended to mark
        variables with a desired precision that will be recognized by optimizers like
        `Bop` and add training metrics to track variable changes.

    !!! example
        ```python
        layer = lq.layers.QuantDense(
            16, kernel_quantizer=lq.quantizers.NoOp(precision=1),
        )
        layer.build((32,))
        assert layer.kernel.precision == 1
        ```

    # Arguments
        precision: Set the desired precision of the variable. This can be used to tag
        metrics: An array of metrics to add to the layer. If `None` the metrics set in
            `larq.context.metrics_scope` are used. Currently only the `flip_ratio`
            metric is available.
    """
    precision = None

    def __init__(self, precision: int, **kwargs):
        self.precision = precision
        super().__init__(**kwargs)

    def get_config(self):
        return {**super().get_config(), "precision": self.precision}


# `NoOp` used to be called `NoOpQuantizer`; this alias is for
# backwards-compatibility.
NoOpQuantizer = NoOp


@utils.register_alias("ste_sign")
@utils.register_keras_custom_object
class SteSign(_BaseQuantizer):
    r"""Instantiates a serializable binary quantizer.

    \\[
    q(x) = \begin{cases}
      -1 & x < 0 \\\
      1 & x \geq 0
    \end{cases}
    \\]

    The gradient is estimated using the Straight-Through Estimator
    (essentially the binarization is replaced by a clipped identity on the
    backward pass).
    \\[\frac{\partial q(x)}{\partial x} = \begin{cases}
      1 & \left|x\right| \leq \texttt{clip_value} \\\
      0 & \left|x\right| > \texttt{clip_value}
    \end{cases}\\]

    ```plot-activation
    quantizers.SteSign
    ```

    # Arguments
        clip_value: Threshold for clipping gradients. If `None` gradients are not
            clipped.
        metrics: An array of metrics to add to the layer. If `None` the metrics set in
            `larq.context.metrics_scope` are used. Currently only the `flip_ratio`
            metric is available.

    # References
        - [Binarized Neural Networks: Training Deep Neural Networks with Weights and
            Activations Constrained to +1 or -1](https://arxiv.org/abs/1602.02830)
    """
    precision = 1

    def __init__(self, clip_value: float = 1.0, **kwargs):
        self.clip_value = clip_value
        super().__init__(name="ste_sign"+str(tf.keras.backend.get_uid("ste_sign")), **kwargs)

    def call(self, inputs):
        outputs = ste_sign(inputs, clip_value=self.clip_value)
        return super().call(outputs)

    def get_config(self):
        return {**super().get_config(), "clip_value": self.clip_value}


@utils.register_alias("approx_sign")
@utils.register_keras_custom_object
class ApproxSign(_BaseQuantizer):
    r"""Instantiates a serializable binary quantizer.
    \\[
    q(x) = \begin{cases}
      -1 & x < 0 \\\
      1 & x \geq 0
    \end{cases}
    \\]

    The gradient is estimated using the ApproxSign method.
    \\[\frac{\partial q(x)}{\partial x} = \begin{cases}
      (2 - 2 \left|x\right|) & \left|x\right| \leq 1 \\\
      0 & \left|x\right| > 1
    \end{cases}
    \\]

    ```plot-activation
    quantizers.ApproxSign
    ```

    # Arguments
        metrics: An array of metrics to add to the layer. If `None` the metrics set in
            `larq.context.metrics_scope` are used. Currently only the `flip_ratio`
            metric is available.

    # References
        - [Bi-Real Net: Enhancing the Performance of 1-bit CNNs With Improved
            Representational Capability and Advanced Training
            Algorithm](https://arxiv.org/abs/1808.00278)
    """
    precision = 1

    def call(self, inputs):
        outputs = approx_sign(inputs)
        return super().call(outputs)


@utils.register_alias("ste_heaviside")
@utils.register_keras_custom_object
class SteHeaviside(_BaseQuantizer):
    r"""
    Instantiates a binarization quantizer with output values 0 and 1.
    \\[
    q(x) = \begin{cases}
    +1 & x > 0 \\\
    0 & x \leq 0
    \end{cases}
    \\]

    The gradient is estimated using the Straight-Through Estimator
    (essentially the binarization is replaced by a clipped identity on the
    backward pass).

    \\[\frac{\partial q(x)}{\partial x} = \begin{cases}
    1 & \left|x\right| \leq 1 \\\
    0 & \left|x\right| > 1
    \end{cases}\\]

    ```plot-activation
    quantizers.SteHeaviside
    ```

    # Arguments
        clip_value: Threshold for clipping gradients. If `None` gradients are not
            clipped.
        metrics: An array of metrics to add to the layer. If `None` the metrics set in
            `larq.context.metrics_scope` are used. Currently only the `flip_ratio`
            metric is available.

    # Returns
        AND Binarization function
    """
    precision = 1

    def __init__(self, clip_value: float = 1.0, **kwargs):
        self.clip_value = clip_value
        super().__init__(**kwargs)

    def call(self, inputs):
        outputs = ste_heaviside(inputs, clip_value=self.clip_value)
        return super().call(outputs)

    def get_config(self):
        return {**super().get_config(), "clip_value": self.clip_value}


@utils.register_alias("swish_sign")
@utils.register_keras_custom_object
class SwishSign(_BaseQuantizer):
    r"""Sign binarization function.

    \\[
    q(x) = \begin{cases}
      -1 & x < 0 \\\
      1 & x \geq 0
    \end{cases}
    \\]

    The gradient is estimated using the SignSwish method.

    \\[
    \frac{\partial q_{\beta}(x)}{\partial x} = \frac{\beta\left\\{2-\beta x \tanh \left(\frac{\beta x}{2}\right)\right\\}}{1+\cosh (\beta x)}
    \\]

    ```plot-activation
    quantizers.SwishSign
    ```
    # Arguments
        beta: Larger values result in a closer approximation to the derivative of the
            sign.
        metrics: An array of metrics to add to the layer. If `None` the metrics set in
            `larq.context.metrics_scope` are used. Currently only the `flip_ratio`
            metric is available.

    # Returns
        SwishSign quantization function

    # References
        - [BNN+: Improved Binary Network Training](https://arxiv.org/abs/1812.11800)
    """
    precision = 1

    def __init__(self, beta: float = 5.0, **kwargs):
        self.beta = beta
        super().__init__(**kwargs)

    def call(self, inputs):
        outputs = swish_sign(inputs, beta=self.beta)
        return super().call(outputs)

    def get_config(self):
        return {**super().get_config(), "beta": self.beta}

@utils.register_alias("LAB")
@utils.register_keras_custom_object
class LAB(_BaseQuantizer):
    r"""Custom LAB binarization function as proposed in the paper: 
    "LAB: Learnable Activation Binarizer for Binary Neural Networks"
    
    args:
        beta (float):   Value of beta if beta is desired static. If no value is supplied, 
                        beta will be initialized to 1 and learned.
        name (str):     Custom name in the graph.
    """
    precision = 1

    def __init__(self, beta=None, name="LAB", **kwargs):
        super().__init__(name=name+str(tf.keras.backend.get_uid(name)), **kwargs)
        uid = "soft_argmax_beta"+str(tf.keras.backend.get_uid("soft_argmax_beta"))
        self.soft_argmax_beta = beta if beta else tf.Variable(1.0, name=uid)

    def build(self, input_shape):
        self.conv = layers.QuantDepthwiseConv2D(kernel_size=3, 
                                                strides=1, 
                                                padding='same', 
                                                depth_multiplier=2,
                                                depthwise_quantizer=NoOp(precision=1))
        self.n, self.h, self.w, self.c = input_shape
        

    def call(self, inputs):
        @tf.custom_gradient
        def soft_argmax(x):
            out_no_grad = tf.argmax(x, axis=3)  
            out_no_grad = tf.where(out_no_grad==0, tf.constant([-1.0]), tf.constant([1.0]))

            @tf.function
            def argmax_soft(x):
                out = tf.nn.softmax(x, axis=3)[:,:,:,1,:]
                return tf.math.subtract(tf.math.multiply(out,2),1)

            def grad(dy):
                gradient = tf.gradients(argmax_soft(x), x)[0] 
                gradient = gradient * tf.expand_dims(dy, axis=3)
                return gradient
            return out_no_grad, grad


        x = self.conv(inputs) 
        x = tf.reshape(x, [-1, self.h, self.w, 2, self.c])
        x = x * self.soft_argmax_beta
        outputs = soft_argmax(x)

        return super().call(outputs)

    def get_config(self):
        return {**super().get_config(), "soft_argmax_beta": self.soft_argmax_beta.numpy()}


@utils.register_alias("Niblack")
@utils.register_keras_custom_object
class Niblack(_BaseQuantizer):
    r"""
    """
    precision = 1

    def __init__(self, name="niblack", **kwargs):
        super().__init__(name=name+str(tf.keras.backend.get_uid(name)), **kwargs)

    def build(self, input_shape):
        self.b, self.h, self.w, self.c = input_shape
        self.n=3 
        self.k=-0.2
        self.mean = tf.keras.layers.AveragePooling2D(self.n, strides=1, padding="same")

        self.sign = SteSign()

    def call(self, inputs):
        
        epsilon = 1e-9
        mn = self.mean(inputs)
        std = tf.math.sqrt(self.mean(tf.math.square(tf.math.abs(inputs - mn)))+epsilon)
        
        # Calculate the threshold value 
        th = mn + self.k * std
        outputs = self.sign(inputs - th)
        return super().call(outputs)

    def get_config(self):
        return {**super().get_config()}
    
       
@utils.register_alias("Sauvola")
@utils.register_keras_custom_object
class Sauvola(_BaseQuantizer):
    r"""
    """
    precision = 1

    def __init__(self, name="sauvola", **kwargs):
        super().__init__(name=name+str(tf.keras.backend.get_uid(name)), **kwargs)

    def build(self, input_shape):
        self.b, self.h, self.w, self.c = input_shape
        self.n=3 
        self.k=0.5
        self.mean = tf.keras.layers.AveragePooling2D(self.n, strides=1, padding="same")

        self.sign = SteSign()

    def call(self, inputs):
        
        epsilon = 1e-9
        mn = self.mean(inputs)
        std = tf.math.sqrt(self.mean(tf.math.square(tf.math.abs(inputs - mn)))+epsilon)
        self.R = tf.math.reduce_max(tf.math.abs(std))
        
        # Calculate the threshold value 
        th = mn * (1.0 + self.k * ((std/(self.R+epsilon)) - 1.0))
        outputs = self.sign(inputs - th)
        return super().call(outputs)

    def get_config(self):
        return {**super().get_config()}
            
    
@utils.register_alias("magnitude_aware_sign")
@utils.register_keras_custom_object
class MagnitudeAwareSign(_BaseQuantizer):
    r"""Instantiates a serializable magnitude-aware sign quantizer for Bi-Real Net.

    A scaled sign function computed according to Section 3.3 in
    [Zechun Liu et al](https://arxiv.org/abs/1808.00278).

    ```plot-activation
    quantizers._scaled_sign
    ```

    # Arguments
        clip_value: Threshold for clipping gradients. If `None` gradients are not
            clipped.
        metrics: An array of metrics to add to the layer. If `None` the metrics set in
            `larq.context.metrics_scope` are used. Currently only the `flip_ratio`
            metric is available.

    # References
        - [Bi-Real Net: Enhancing the Performance of 1-bit CNNs With Improved
        Representational Capability and Advanced Training
        Algorithm](https://arxiv.org/abs/1808.00278)

    """
    precision = 1

    def __init__(self, clip_value: float = 1.0, **kwargs):
        self.clip_value = clip_value
        super().__init__(**kwargs)

    def call(self, inputs):
        scale_factor = tf.stop_gradient(
            tf.reduce_mean(tf.abs(inputs), axis=list(range(len(inputs.shape) - 1)))
        )

        outputs = scale_factor * ste_sign(inputs, clip_value=self.clip_value)
        return super().call(outputs)

    def get_config(self):
        return {**super().get_config(), "clip_value": self.clip_value}


@utils.register_alias("ste_tern")
@utils.register_keras_custom_object
class SteTern(_BaseQuantizer):
    r"""Instantiates a serializable ternarization quantizer.

    \\[
    q(x) = \begin{cases}
    +1 & x > \Delta \\\
    0 & |x| < \Delta \\\
     -1 & x < - \Delta
    \end{cases}
    \\]

    where \\(\Delta\\) is defined as the threshold and can be passed as an argument,
    or can be calculated as per the Ternary Weight Networks original paper, such that

    \\[
    \Delta = \frac{0.7}{n} \sum_{i=1}^{n} |W_i|
    \\]
    where we assume that \\(W_i\\) is generated from a normal distribution.

    The gradient is estimated using the Straight-Through Estimator
    (essentially the Ternarization is replaced by a clipped identity on the
    backward pass).
    \\[\frac{\partial q(x)}{\partial x} = \begin{cases}
    1 & \left|x\right| \leq \texttt{clip_value} \\\
    0 & \left|x\right| > \texttt{clip_value}
    \end{cases}\\]

    ```plot-activation
    quantizers.SteTern
    ```

    # Arguments
        threshold_value: The value for the threshold, \\(\Delta\\).
        ternary_weight_networks: Boolean of whether to use the
            Ternary Weight Networks threshold calculation.
        clip_value: Threshold for clipping gradients. If `None` gradients are not
            clipped.
        metrics: An array of metrics to add to the layer. If `None` the metrics set in
            `larq.context.metrics_scope` are used. Currently only the `flip_ratio`
            metric is available.

    # References
        - [Ternary Weight Networks](https://arxiv.org/abs/1605.04711)
    """

    precision = 2

    def __init__(
        self,
        threshold_value: float = 0.05,
        ternary_weight_networks: bool = False,
        clip_value: float = 1.0,
        **kwargs,
    ):
        self.threshold_value = threshold_value
        self.ternary_weight_networks = ternary_weight_networks
        self.clip_value = clip_value
        super().__init__(**kwargs)

    def call(self, inputs):
        outputs = ste_tern(
            inputs,
            threshold_value=self.threshold_value,
            ternary_weight_networks=self.ternary_weight_networks,
            clip_value=self.clip_value,
        )
        return super().call(outputs)

    def get_config(self):
        return {
            **super().get_config(),
            "threshold_value": self.threshold_value,
            "ternary_weight_networks": self.ternary_weight_networks,
            "clip_value": self.clip_value,
        }


@utils.register_alias("dorefa_quantizer")
@utils.register_keras_custom_object
class DoReFa(_BaseQuantizer):
    r"""Instantiates a serializable k_bit quantizer as in the DoReFa paper.

    \\[
    q(x) = \begin{cases}
    0 & x < \frac{1}{2n} \\\
    \frac{i}{n} & \frac{2i-1}{2n} < x < \frac{2i+1}{2n} \text{ for } i \in \\{1,n-1\\}\\\
     1 & \frac{2n-1}{2n} < x
    \end{cases}
    \\]

    where \\(n = 2^{\text{k_bit}} - 1\\). The number of bits, k_bit, needs to be passed
    as an argument.
    The gradient is estimated using the Straight-Through Estimator
    (essentially the binarization is replaced by a clipped identity on the
    backward pass).
    \\[\frac{\partial q(x)}{\partial x} = \begin{cases}
    1 &  0 \leq x \leq 1 \\\
    0 & \text{else}
    \end{cases}\\]

    The behavior for quantizing weights should be different in comparison to
    the quantization of activations:
    instead of limiting input operands (or in this case: weights) using a hard
    limiter, a tangens hyperbolicus is applied to achieve a softer limiting
    with a gradient, which is continuously differentiable itself.

    \\[
    w_{lim}(w) = \tanh(w)
    \\]

    Furthermore, the weights of each layer are normed, such that the weight with
    the largest magnitude gets the largest or smallest (depending on its sign)
    quantizable value. That way, the full quantizable numeric range is utilized.

    \\[
    w_{norm}(w) = \frac{w}{\max(|w|)}
    \\]

    The formulas can be found in the paper in section 2.3. Please note, that
    the paper refers to weights being quantized on a numeric range of [-1, 1], while
    activations are quantized on the numeric range [0, 1]. This implementation
    uses the same ranges as specified in the paper.

    The activation quantizer defines the function quantizek() from the paper with
    the correct numeric range of [0, 1]. The weight quantization mode adds
    pre- and post-processing for numeric range adaptions, soft limiting and
    norming. The full quantization function including the adaption of numeric ranges is

    \\[
    q(w) = 2 \, quantize_{k}(\frac{w_{norm}\left(w_{lim}\left(w\right)\right)}{2} + \frac{1}{2}) - 1
    \\]

    !!! warning
        The weight mode works for weights on the range [-1, 1], which matches the
        default setting of `constraints.weight_clip`. Do not use this quantizer
        with a different constraint `clip_value` than the default one.

    __`mode == "activations"`__
    ```plot-activation
    quantizers.DoReFa
    ```

    __`mode == "weights"`__
    ```plot-activation
    quantizers.DoReFa(mode='weights')
    ```

    # Arguments
        k_bit: number of bits for the quantization.
        mode: `"activations"` for clipping inputs on [0, 1] range or `"weights"` for
            soft-clipping and norming weights on [-1, 1] range before applying
            quantization.
        metrics: An array of metrics to add to the layer. If `None` the metrics set in
            `larq.context.metrics_scope` are used. Currently only the `flip_ratio`
            metric is available.

    # Returns
        Quantization function

    # Raises
        ValueError for bad value of `mode`.

    # References
        - [DoReFa-Net: Training Low Bitwidth Convolutional Neural Networks with Low
            Bitwidth Gradients](https://arxiv.org/abs/1606.06160)
    """
    precision = None

    def __init__(self, k_bit: int = 2, mode: str = "activations", **kwargs):
        self.precision = k_bit

        if mode not in ("activations", "weights"):
            raise ValueError(
                f"Invalid DoReFa quantizer mode {mode}. "
                "Valid values are 'activations' and 'weights'."
            )
        self.mode = mode

        super().__init__(**kwargs)

    def weight_preprocess(self, inputs):
        # Limit inputs to [-1, 1] range
        limited = tf.math.tanh(inputs)

        # Divider for max-value norm.
        dividend = tf.math.reduce_max(tf.math.abs(limited))

        # Need to stop the gradient here. Otherwise, for the maximum element,
        # which gives the dividend, normed is limited/limited (for this one
        # maximum digit). The derivative of y = x/x, dy/dx is just zero, when
        # one does the simplification y = x/x = 1. But TF does NOT do this
        # simplification when computing the gradient for the
        # normed = limited/dividend operation. As a result, this gradient
        # becomes complicated, because during the computation, "dividend" is
        # not just a constant, but depends on "limited" instead. Here,
        # tf.stop_gradient is used to mark "dividend" as a constant explicitly.
        dividend = tf.stop_gradient(dividend)

        # Norm and then scale from value range [-1,1] to [0,1] (the range
        # expected by the core quantization operation).
        # If the dividend used for the norm operation is 0, all elements of
        # the weight tensor are 0 and divide_no_nan returns 0 for all weights.
        # So if all elements of the weight tensor are zero, nothing is normed.
        return tf.math.divide_no_nan(limited, 2.0 * dividend) + 0.5

    def call(self, inputs):
        # Depending on quantizer mode (activation or weight) just clip inputs
        # on [0, 1] range or use weight preprocessing method.
        if self.mode == "activations":
            inputs = tf.clip_by_value(inputs, 0.0, 1.0)
        elif self.mode == "weights":
            inputs = self.weight_preprocess(inputs)
        else:
            raise ValueError(
                f"Invalid DoReFa quantizer mode {self.mode}. "
                "Valid values are 'activations' and 'weights'."
            )

        @tf.custom_gradient
        def _k_bit_with_identity_grad(x):
            n = 2 ** self.precision - 1
            return tf.round(x * n) / n, lambda dy: dy

        outputs = _k_bit_with_identity_grad(inputs)

        # Scale weights from [0, 1] quantization range back to [-1,1] range
        if self.mode == "weights":
            outputs = 2.0 * outputs - 1.0

        return super().call(outputs)

    def get_config(self):
        return {**super().get_config(), "k_bit": self.precision, "mode": self.mode}


# `DoReFa` used to be called `DoReFaQuantizer`; this alias is for
# backwards-compatibility.
DoReFaQuantizer = DoReFa


QuantizerType = Union[Quantizer, Callable[[tf.Tensor], tf.Tensor]]


def serialize(quantizer: tf.keras.layers.Layer):
    return tf.keras.utils.serialize_keras_object(quantizer)


def deserialize(name, custom_objects=None):
    return tf.keras.utils.deserialize_keras_object(
        name,
        module_objects=globals(),
        custom_objects=custom_objects,
        printable_module_name="quantization function",
    )


def get(identifier):
    if identifier is None:
        return None
    if isinstance(identifier, dict):
        return deserialize(identifier)
    if isinstance(identifier, str):
        return deserialize(str(identifier))
    if callable(identifier):
        return identifier
    raise ValueError(
        f"Could not interpret quantization function identifier: {identifier}"
    )


def get_kernel_quantizer(identifier):
    """Returns a quantizer from identifier and adds default kernel quantizer metrics.

    # Arguments
        identifier: Function or string

    # Returns
        `Quantizer` or `None`
    """
    quantizer = get(identifier)
    if isinstance(quantizer, _BaseQuantizer) and not quantizer._custom_metrics:
        quantizer._custom_metrics = list(context.get_training_metrics())
    return quantizer
