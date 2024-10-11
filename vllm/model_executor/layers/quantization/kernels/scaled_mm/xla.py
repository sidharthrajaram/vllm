from typing import Optional, Tuple

import torch
import torch_xla.experimental.xla_quantized_matmul  # noqa: F401

from vllm.model_executor.layers.quantization.utils import replace_parameter
from vllm.model_executor.layers.quantization.utils.w8a8_utils import (
    convert_to_channelwise)
from vllm.platforms import current_platform

from .ScaledMMLinearKernel import (ScaledMMLinearKernel,
                                   ScaledMMLinearLayerConfig)


class XLAScaledMMLinearKernel(ScaledMMLinearKernel):

    @classmethod
    def get_min_capability(cls) -> Optional[int]:
        return None

    @classmethod
    def can_implement(
            cls, c: ScaledMMLinearLayerConfig) -> Tuple[bool, Optional[str]]:

        if not current_platform.is_tpu():
            return False, "ScaledMMXLA requires running on TPU."

        if c.is_static_input_scheme:
            return False, "ScaledMMXLA requires dynamic activation scales."

        if not c.input_symmetric:
            return False, "ScaledMMXLA requires symmetric activation scales."

        if not c.is_channelwise:
            return False, "ScaledMMXLA requires channelwise weight scales"

        return True, None

    def process_weights_after_loading(self, layer: torch.nn.Module) -> None:
        # WEIGHT
        # Cutlass kernels need transposed weight.
        weight = getattr(layer, self.w_q_name)

        # [out, in] (different than cutlass_scaled_mm)
        replace_parameter(layer, self.w_q_name,
                          torch.nn.Parameter(weight.data, requires_grad=False))

        # WEIGHT SCALE
        # XLA kernels support only per-tensor and per-channel.
        # If we have a fused module (QKV, MLP) with per tensor scales (thus N
        # scales being passed to the kernel), convert to the per-channel case.
        is_fused_module = len(layer.logical_widths) > 1
        weight_scale = getattr(layer, self.w_s_name)
        if is_fused_module and not self.config.is_channelwise:
            weight_scale = convert_to_channelwise(weight_scale,
                                                  layer.logical_widths)

        # [out_channel,] (different than cutlass_scaled_mm)
        weight_scale = weight_scale.squeeze(-1).to(torch.bfloat16)
        replace_parameter(
            layer, self.w_s_name,
            torch.nn.Parameter(weight_scale.data, requires_grad=False))

    def apply_weights(self,
                      layer: torch.nn.Module,
                      x: torch.Tensor,
                      bias: Optional[torch.Tensor] = None) -> torch.Tensor:
        w_q, w_s, i_s, i_zp, i_azp_adj = self._get_weight_params(layer)
        assert i_s is None
        assert i_zp is None
        assert i_azp_adj is None

        return torch.ops.xla.quantized_matmul(x,
                                              w_q,
                                              w_s,
                                              quantize_activation=True)