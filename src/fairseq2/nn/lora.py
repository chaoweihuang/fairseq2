# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

import math
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Optional, final

import torch
import torch.nn as nn
from torch import Tensor
from torch.nn import Dropout
from torch.nn.functional import embedding, linear
from torch.nn.parameter import Parameter

from fairseq2.nn import Embedding, Linear, Projection
from fairseq2.typing import DataType, Device


@dataclass
class LoRAConfig:
    r: int
    alpha: float
    dropout_p: float
    keys: List[str]


class LoRALayer(ABC):
    def __init__(self, config: LoRAConfig):
        self.r = config.r
        self.alpha = config.alpha
        self.scaling = self.alpha / self.r
        self.dropout_p = config.dropout_p

    @property
    @abstractmethod
    def wrapped_module(self) -> nn.Module:
        ...

    @abstractmethod
    def merge(self) -> None:
        ...

    @abstractmethod
    def unmerge(self) -> None:
        ...


@final
class LoRAEmbedding(Embedding, LoRALayer):
    weight: Parameter
    lora_A: Parameter
    lora_B: Parameter
    merged: bool

    def __init__(
        self,
        wrapped: Embedding,
        config: LoRAConfig,
        device: Optional[Device] = None,
        dtype: Optional[DataType] = None,
    ) -> None:
        """
        :param wrapped:
            The Embedding module to be wrapped.
        :param config:
            The LoRA config.
        """
        Embedding.__init__(
            self,
            wrapped.num_embeddings,
            wrapped.embedding_dim,
            wrapped.pad_idx,
        )
        LoRALayer.__init__(self, config)

        self.weight = Parameter(
            wrapped.weight, requires_grad=False  # type: ignore[arg-type]
        )

        self.lora_A = Parameter(
            torch.empty((self.r, self.num_embeddings), device=device, dtype=dtype)
        )

        self.lora_B = Parameter(
            torch.empty((self.embedding_dim, self.r), device=device, dtype=dtype)
        )

        self.merged = False

        self.reset_lora_parameters()

    def forward(self, x: Tensor) -> Tensor:
        if self.merged:
            return embedding(x, self.weight, self.pad_idx)
        else:
            return embedding(
                x,
                self.weight + (self.lora_B @ self.lora_A).T * self.scaling,
                self.pad_idx,
            )

    def reset_lora_parameters(self) -> None:
        """Reset the parameters and buffers of the module."""
        nn.init.zeros_(self.lora_A)
        nn.init.normal_(self.lora_B)

    @property
    def wrapped_module(self) -> nn.Module:
        return self

    def merge(self) -> None:
        if self.merged:
            return

        self.weight.data += (self.lora_B.data @ self.lora_A.data).T * self.scaling
        self.merged = True

    def unmerge(self) -> None:
        if not self.merged:
            return

        self.weight.data -= (self.lora_B.data @ self.lora_A.data).T * self.scaling
        self.merged = False


@final
class LoRALinear(Projection, LoRALayer):
    weight: Parameter
    bias: Optional[Parameter]
    lora_A: Parameter
    lora_B: Parameter
    dropout: Optional[Dropout]
    skip_init: bool
    merged: bool

    def __init__(
        self,
        wrapped: Projection,
        config: LoRAConfig,
        skip_init: bool = False,
        device: Optional[Device] = None,
        dtype: Optional[DataType] = None,
    ) -> None:
        """
        :param wrapped:
            The Linear module to be wrapped.
        :param config:
            The LoRA config.
        :param skip_init:
            If ``True``, the weights and bias will be left uninitialized, and
            :meth:`reset_lora_parameters` will become noop.
        """
        Projection.__init__(self, wrapped.input_dim, wrapped.output_dim)

        LoRALayer.__init__(self, config)

        self.weight = Parameter(
            wrapped.weight, requires_grad=False  # type: ignore[arg-type]
        )

        if wrapped.bias is not None:
            self.bias = Parameter(
                wrapped.bias, requires_grad=False  # type: ignore[arg-type]
            )
        else:
            self.register_module("bias", None)

        self.lora_A = Parameter(
            torch.empty((self.r, self.input_dim), device=device, dtype=dtype)
        )

        self.lora_B = Parameter(
            torch.empty((self.output_dim, self.r), device=device, dtype=dtype)
        )

        if self.dropout_p > 0.0:
            self.dropout = Dropout(self.dropout_p)

        else:
            self.register_module("dropout", None)

        self.merged = False

        self.skip_init = skip_init

        self.reset_lora_parameters()

    def forward(self, x: Tensor) -> Tensor:
        if self.merged:
            return linear(x, self.weight, self.bias)
        else:
            h1 = linear(x, self.weight, self.bias)

            if self.dropout is not None:
                h2 = linear(self.dropout(x), self.lora_B @ self.lora_A * self.scaling)
            else:
                h2 = linear(x, self.lora_B @ self.lora_A * self.scaling)

            return h1 + h2

    def reset_lora_parameters(self) -> None:
        """Reset the parameters and buffers of the module."""
        if not self.skip_init:
            nn.init.kaiming_uniform_(self.lora_A, a=math.sqrt(5))
            nn.init.zeros_(self.lora_B)

    @property
    def wrapped_module(self) -> nn.Module:
        return self

    def merge(self) -> None:
        if self.merged:
            return
        self.weight.data += self.lora_B.data @ self.lora_A.data * self.scaling

        self.merged = True

    def unmerge(self) -> None:
        if not self.merged:
            return
        else:
            self.weight.data -= self.lora_B.data @ self.lora_A.data * self.scaling

            self.merged = False


def wrap_lora(
    module: nn.Module, config: LoRAConfig, skip_init: bool = False
) -> nn.Module:
    """Iterate all submodules. If `config.keys` regex matches module name,
    wrap it with a LoRALayer based on module's type. Note that the wrapping
    also happens in-place."""
    for name, submodule in module.named_modules():
        if not _is_target_module(name, config.keys):
            continue

        submodule_path = name.split(".")
        parent = module.get_submodule(".".join(submodule_path[:-1]))
        submodule_name = submodule_path[-1]

        if isinstance(submodule, Projection):
            lora_layer = LoRALinear(
                wrapped=submodule,
                config=config,
                skip_init=skip_init,
                device=submodule.weight.data.device,  # type: ignore[arg-type]
                dtype=submodule.weight.data.dtype,  # type: ignore[arg-type]
            )
        else:
            raise ValueError(
                f"Cannot wrap the module '{name}' with LoRA as the module type '{type(submodule).__name__}' is not supported."
            )

        lora_layer.train(mode=submodule.training)
        setattr(parent, submodule_name, lora_layer)

    return module


def unwrap_lora(module: nn.Module, merge: bool = True) -> nn.Module:
    # Reverses the model to its original architecture by replacing all
    # `LoRALayers` back with their original wrapped modules.
    # By default, we perform `merge_lora` before unwrapping.
    # Note that the unwrapping also happends in-place.
    if merge:
        merge_lora(module)

    for name, submodule in module.named_modules():
        if not isinstance(submodule, LoRALayer):
            continue

        submodule_path = name.split(".")
        parent = module.get_submodule(".".join(submodule_path[:-1]))
        submodule_name = submodule_path[-1]

        if isinstance(submodule, LoRALinear):
            # TODO: currently there's no way to distinguish which type the
            # original module is (`Linear` or `TiedProjection` or
            # `QKVProjection`). Using `Linear` should be functionally
            # identical (output would be the same), but since it might be a
            # different projection layer, it might cause issues in
            # downstream operations.
            unwrapped_layer = Linear(
                submodule.input_dim,
                submodule.output_dim,
                bias=submodule.bias is not None,
                skip_init=True,
            )
            unwrapped_layer.weight = submodule.weight
            if submodule.bias is not None:
                unwrapped_layer.bias = submodule.bias
        else:
            raise ValueError(
                f"Cannot unwrap the module '{name}' as the module type '{type(submodule).__name__}' is not supported."
            )

        unwrapped_layer.train(mode=submodule.training)
        setattr(parent, submodule_name, unwrapped_layer)

    return module


def merge_lora(module: nn.Module) -> None:
    # Iterate through all `LoRALayer`s in `module`, and call their `merge`
    # method which is expected to merge LoRA A, B weights with the wrapped W.
    for submodule in module.modules():
        if isinstance(submodule, LoRALayer):
            submodule.merge()


def unmerge_lora(module: nn.Module) -> None:
    for submodule in module.modules():
        if isinstance(submodule, LoRALayer):
            submodule.unmerge()


def lora_state_dict(module: nn.Module) -> Dict[str, Any]:
    state_dict = module.state_dict()
    lora_states = {name: state for name, state in state_dict.items() if "lora_" in name}
    return lora_states


def freeze_non_lora(
    module: nn.Module, unfreeze_bias: Literal["none", "all", "lora_only"] = "none"
) -> None:
    # Set requires_grad to False for all parameters in the module except
    # lora layers
    for name, param in module.named_parameters():
        param.requires_grad = "lora_" in name

    if unfreeze_bias == "all":
        for name, param in module.named_parameters():
            if "bias" in name:
                param.requires_grad = True
    elif unfreeze_bias == "lora_only":
        for submodule in module.modules():
            if (
                isinstance(submodule, LoRALayer)
                and getattr(submodule, "bias", None) is not None
            ):
                submodule.bias.requires_grad = True


def _is_target_module(name: str, target_keys: List[str]) -> bool:
    # Check if the `name` matches any of the `target_keys``.
    return any(re.match(key, name) for key in target_keys) or any(
        name == key for key in target_keys
    )
