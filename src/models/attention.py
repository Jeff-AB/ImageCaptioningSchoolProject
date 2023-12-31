"""
This module contains the attention modules used in this project. At the end of the project,
This module should contain the following

- Single Headed Visual Attention
- Multi Headed Visual Attention
- Bayesian Single Headed Visual Attention
- Bayesian Multi Headed Visual Attention
"""

from optparse import Option
from turtle import forward
import torch
import torch.nn as nn
import torch.nn.functional as F
from typing import NoReturn, Optional
import math
import numpy as np


eps = 1e-20


class SATAttention(nn.Module):
    def __init__(self, encoder_size: int, hidden_size: int, attention_size: int) -> None:
        super().__init__()
        self.feature_shaper = nn.Linear(encoder_size, attention_size)
        self.hidden_state_shaper = nn.Linear(hidden_size, attention_size)
        self.attention_model = nn.Linear(
            attention_size, 1
        )  # attention for each annotation vector in aᵢ for i = 1 ... L
        self.feature_vector_size = encoder_size

    def forward(self, feature_vectors, hidden_state):

        # Shape vectors so I can add them together
        fv_shaped = self.feature_shaper(feature_vectors)
        hidden_state_shaped = self.hidden_state_shaper(hidden_state).unsqueeze(1)

        # Compute e in the paper
        e = self.attention_model(F.relu(fv_shaped + hidden_state_shaped)).squeeze(2)

        # alpha = softmax(e)
        alpha = F.softmax(e, dim=1)

        # z = sum alpha_i a_i
        zhat = (feature_vectors * alpha.unsqueeze(2)).sum(dim=1)

        # Return values
        return (zhat, alpha)


############################################
##
## Meshed Memory Transformer
##
############################################


class Attention(nn.Module):
    """Implements Scaled Dot Product Attention"""

    def __init__(self, out_size: int, key_size: int, value_size: int, num_heads: int) -> NoReturn:
        """Initializer function for scaled dot product attention
        Args:
            vocab_size (int):  the number of words in the model's vocabulary
            key_size (int): Key dimension
            value_size (int): size of feature array
            num_heads (int): The number of heads in attention
        """
        super().__init__()
        self.vocab_size = out_size
        self.key_size = key_size
        self.value_size = value_size
        self.num_heads = num_heads
        self.softmax = nn.Softmax(dim=-1)
        self.scale = 1 / math.sqrt(key_size)

        # Layers to reshape inputs and generate multiheaded subspaces
        # Linear layers represent the flattened attention heads
        self.keygen = nn.Linear(out_size, num_heads * key_size)
        self.querygen = nn.Linear(out_size, num_heads * key_size)
        self.valuegen = nn.Linear(out_size, num_heads * value_size)
        self.output = nn.Linear(num_heads * value_size, out_size)

        # Xavier Uniform yields good initialization
        nn.init.xavier_uniform_(self.keygen.weight)
        nn.init.xavier_uniform_(self.querygen.weight)
        nn.init.xavier_uniform_(self.output.weight)
        nn.init.xavier_normal_(self.valuegen.weight)

        # set bias to zero
        self.keygen.bias.data.fill_(0)
        self.querygen.bias.data.fill_(0)
        self.valuegen.bias.data.fill_(0)

    def preprocess_inputs(self, keys: torch.Tensor, queries: torch.Tensor, values: torch.Tensor) -> tuple:
        """_summary_

        Args:
            queries (torch.Tensor): _description_
            keys (torch.Tensor): _description_
            values (torch.Tensor): _description_

        Returns:
            tuple: _description_
        """
        num_queries = queries.size(1)
        num_keys = keys.size(1)
        batch_size = keys.size(0)

        # Flattened keys queries, and values
        queries = self.querygen(queries)
        keys = self.keygen(keys)
        values = self.valuegen(values)

        # Unflatten keys, queries, and values
        # shape should be (batch_size, heads, *, *)
        queries = queries.view(batch_size, num_queries, self.num_heads, self.key_size)
        queries = queries.permute(0, 2, 1, 3)

        keys = keys.view(batch_size, num_keys, self.num_heads, self.key_size)
        keys = keys.permute(0, 2, 3, 1)

        values = values.view(batch_size, num_keys, self.num_heads, self.value_size)
        values = values.permute(0, 2, 1, 3)

        return queries, keys, values

    def process_masks_and_weights(
        self,
        attention: torch.Tensor,
        num_keys: int,
        attention_mask: Optional[torch.Tensor] = None,
        attention_weights: Optional[torch.Tensor] = None,
    ) -> torch.Tensor:
        """Previous layers can generate masks and weights for self and cross attention

        Args:
            attention_mask (Optional[torch.Tensor]) : binary mask to block contributions from some attention locations
            attention_weights (Optional[torch.Tensor]) : weights to attentuate regions of attention
        Returns:
            (torch.Tensor) :
        """
        if attention_weights is not None:
            # element wise product with binary array
            attention *= attention_mask
        if attention_mask is not None:
            attention = attention.masked_fill(attention_mask, -np.inf)
        return attention

    def forward(
        self,
        queries: torch.Tensor,
        keys: torch.Tensor,
        values: torch.Tensor,
        attention_mask: Optional[torch.Tensor] = None,
        attention_weights: Optional[torch.Tensor] = None,
    ) -> torch.Tensor:
        """_summary_

        Args:
            queries (torch.Tensor): _description_
            keys (torch.Tensor): _description_
            values (torch.Tensor): _description_
            attention_mask (Optional[torch.Tensor], optional): _description_. Defaults to None.
            attention_weights (Optional[torch.Tensor], optional): _description_. Defaults to None.

        Returns:
            torch.Tensor: _description_
        """
        num_queries = queries.size(1)
        num_keys = keys.size(1)
        batch_size = keys.size(0)
        queries, keys, values = self.preprocess_inputs(queries=queries, keys=keys, values=values)
        attention = torch.matmul(queries, keys) / self.scale

        # Pass in information from previous layers
        attention = self.process_masks_and_weights(attention, num_keys, attention_mask, attention_weights)

        # complete attention computation
        attention = torch.softmax(attention, dim=-1)
        output = torch.matmul(attention, values)

        # reshape
        output = output.permute(0, 2, 1, 3).contiguous().view(batch_size, num_queries, self.num_heads * self.value_size)
        output = self.output(output)
        return output


class AttentionWithMemory(Attention):
    def __init__(self, out_size: int, key_size: int, value_size: int, num_heads: int, num_mem_slots: int) -> NoReturn:
        """_summary_

        Args:
            out_size (int): output size of model
            key_size (int): size of key matrices
            value_size (int): size of value matrices
            num_heads (int): number of heads
            num_mem_slots (int): number of memory slots
        """
        super().__init__(out_size, key_size, value_size, num_heads)
        self.mem_keys = nn.Parameter(torch.FloatTensor(1, num_mem_slots, num_heads * key_size))
        self.mem_values = nn.Parameter(torch.FloatTensor(1, num_mem_slots, num_heads * value_size))
        self.num_mem_slots = num_mem_slots

        # initialize parameter weights
        nn.init.normal_(self.mem_keys, 0, 1 / self.scale)
        nn.init.normal_(self.mem_values, 0, 1 / self.num_mem_slots)

    def preprocess_inputs(self, queries: torch.Tensor, keys: torch.Tensor, values: torch.Tensor) -> tuple:
        """_summary_

        Args:
            queries (torch.Tensor): _description_
            keys (torch.Tensor): _description_
            values (torch.Tensor): _description_

        Returns:
            tuple: _description_
        """
        num_queries = queries.size(1)
        num_keys = keys.size(1)
        batch_size = keys.size(0)

        # Reshape memory
        mem_key = np.sqrt(self.key_size) * self.mem_keys.expand(
            batch_size, self.num_mem_slots, self.num_heads * self.key_size
        )
        mem_val = np.sqrt(self.num_mem_slots) * self.mem_values.expand(
            batch_size, self.num_mem_slots, self.num_heads * self.value_size
        )

        # Flattened keys queries, and values
        queries = self.querygen(queries)

        keys = self.keygen(keys)
        keys = torch.cat([keys, mem_key], dim=1)

        values = self.valuegen(values)
        values = torch.cat([values, mem_val], dim=1)

        # Unflatten keys, queries, and values
        # shape should be (batch_size, heads, *, *)
        queries = queries.view(batch_size, num_queries, self.num_heads, self.key_size)
        queries = queries.permute(0, 2, 1, 3)

        keys = keys.view(batch_size, num_keys + self.num_mem_slots, self.num_heads, self.key_size)
        keys = keys.permute(0, 2, 3, 1)

        values = values.view(batch_size, num_keys + self.num_mem_slots, self.num_heads, self.value_size)
        values = values.permute(0, 2, 1, 3)

        return queries, keys, values

    def process_masks_and_weights(
        self,
        attention: torch.Tensor,
        num_keys: int,
        attention_mask: Optional[torch.Tensor] = None,
        attention_weights: Optional[torch.Tensor] = None,
    ) -> torch.Tensor:
        """Apply masks to current layer

        Args:
            attention (torch.Tensor): _description_
            attention_mask (Optional[torch.Tensor], optional): _description_. Defaults to None.
            attention_weights (Optional[torch.Tensor], optional): _description_. Defaults to None.

        Returns:
            torch.Tensor: _description_
        """
        if attention_weights is not None:
            # element wise product with binary array
            attention = torch.cat(
                [attention[:, :, :, : self.key_size] * attention_weights, attention[:, :, :, self.key_size :]], -1
            )
        if attention_mask is not None:
            attention[:, :, :, :num_keys] = attention[:, :, :, :num_keys].masked_fill(attention_mask, -np.inf)
        return attention


class BayesianAttention(Attention):
    def __init__(self, k: float, *args, **kwargs) -> None:
        """This class implements soft probabilistic attention by imposing a reparametrized
        Weibull distribution on the weights of the attention and inducing a prior from the keys.
        We accomplish this by building on top of the standard Scaled Dot Product Attention.

        For scaled dot product attention, the attention weights are defined by

        .. math::
            W_{ij} = \\frac{\\exp{\\left( \\Phi_{ij} \\right)}}{\\sum_{j'=1}^{n} \\exp{\\left( \\Phi_{ij'}\\right)} }

        The Weibull Distribution is defined by the following:

        .. math::
            Pr(S | k, \\lambda) =

        Args:
            k (float): k is the shape parameter of the Weibull Distribution
        """
        super().__init__(*args, **kwargs)
        key_head = kwargs["key_size"] * kwargs["num_heads"]
        self.kl = 0  # we keep track of kl divergence over time

        # Contextual Prior
        self.prior_layer1 = nn.Linear(kwargs["key_size"], kwargs["out_size"])
        self.relu = nn.LeakyReLU()
        self.prior_layer2 = nn.Linear(kwargs["out_size"], 1)

        # Weibull Setup
        self.alpha_gamma = torch.tensor(torch.Tensor(1))  # we learn alpha gamma across training
        self.beta_gamma = torch.tensor(1).type(torch.float32)
        self.k_weibull = torch.tensor(k).type(torch.float32)
        # Initializations
        nn.init.xavier_uniform_(self.prior_layer1.weight)
        nn.init.xavier_uniform_(self.prior_layer2.weight)

    def compute_prior(self, keys, attention_mask):
        # Compute Contextual Prior
        dot_gamma = self.prior_layer2(self.relu(self.prior_layer1(keys.permute(0,1,3,2)))).permute(0,1,3,2)
        if attention_mask is not None:
            dot_gamma = dot_gamma.masked_fill(attention_mask, -np.inf)
        self.prior_att_weights = F.softmax(dot_gamma, dim=-1)
        self.alpha_gamma = self.prior_att_weights * self.beta_gamma
        
    def stochastic_attention(self, attention: torch.Tensor, keys:torch.Tensor):
        # Compute Weibull Likelihood
        logprobs = torch.log(F.softmax(attention, dim=-1) + eps)

        unif = torch.rand_like(logprobs)
        attention = F.softmax(
            logprobs - torch.lgamma(1 + 1.0 / self.k_weibull + 1.0 / self.k_weibull * torch.log(-torch.log(1.0 - unif + eps) + eps)),
            dim=-1,
        )
        # Compute KL divergence for training
        
        kl = -(
            self.alpha_gamma * (logprobs - torch.lgamma(1 + 1.0 / self.k_weibull))
            - np.euler_gamma * self.alpha_gamma / self.k_weibull
            - self.beta_gamma
            * torch.exp(logprobs - torch.lgamma(1 + 1.0 / self.k_weibull) + torch.lgamma(1 + 1.0 / self.k_weibull))
            + self.alpha_gamma * torch.log(self.beta_gamma + eps)
            - torch.lgamma(self.alpha_gamma + eps)
        )
        self.kl = kl.mean()
        return attention

    def forward(
        self,
        queries: torch.Tensor,
        keys: torch.Tensor,
        values: torch.Tensor,
        attention_mask: Optional[torch.Tensor] = None,
        attention_weights: Optional[torch.Tensor] = None,
    ) -> torch.Tensor:
        """_summary_

        Args:
            queries (torch.Tensor): _description_
            keys (torch.Tensor): _description_
            values (torch.Tensor): _description_
            attention_mask (Optional[torch.Tensor], optional): _description_. Defaults to None.
            attention_weights (Optional[torch.Tensor], optional): _description_. Defaults to None.

        Returns:
            torch.Tensor: _description_
        """
        num_queries = queries.size(1)
        num_keys = keys.size(1)
        batch_size = keys.size(0)
        queries, keys, values = self.preprocess_inputs(queries=queries, keys=keys, values=values)

        attention = torch.matmul(queries, keys) / self.scale  # alignment matrix

        # Pass in information from previous layers
        attention = self.process_masks_and_weights(attention, num_keys, attention_mask, attention_weights)
        if self.training:
            self.compute_prior(keys, attention_mask)
            attention = self.stochastic_attention(attention, keys)
        else:
            # deterministic attention computation
            attention = torch.softmax(attention, dim=-1)
        output = torch.matmul(attention, values)

        # reshape
        output = output.permute(0, 2, 1, 3).contiguous().view(batch_size, num_queries, self.num_heads * self.value_size)
        output = self.output(output)
        return output


class BayesianAttentionWithMemory(BayesianAttention, AttentionWithMemory):
    def __init__(self, *args, **kwargs):
        """

        Args:
            k (float): k is the shape parameter of the Weibull Distribution
        """
        super().__init__(*args, **kwargs)

    def compute_prior(self, keys, attention_mask, num_keys:int):
            # Compute Contextual Prior
        dot_gamma = self.prior_layer2(self.relu(self.prior_layer1(keys.permute(0,1,3,2)))).permute(0,1,3,2)
        if attention_mask is not None:
            dot_gamma[:, :, :, :num_keys] = dot_gamma[:, :, :, :num_keys].masked_fill(attention_mask, -np.inf)
        self.prior_att_weights = F.softmax(dot_gamma, dim=-1)
        self.alpha_gamma = self.prior_att_weights * self.beta_gamma

    def forward(
        self,
        queries: torch.Tensor,
        keys: torch.Tensor,
        values: torch.Tensor,
        attention_mask: Optional[torch.Tensor] = None,
        attention_weights: Optional[torch.Tensor] = None,
    ) -> torch.Tensor:
        """_summary_

        Args:
            queries (torch.Tensor): _description_
            keys (torch.Tensor): _description_
            values (torch.Tensor): _description_
            attention_mask (Optional[torch.Tensor], optional): _description_. Defaults to None.
            attention_weights (Optional[torch.Tensor], optional): _description_. Defaults to None.

        Returns:
            torch.Tensor: _description_
        """
        num_queries = queries.size(1)
        num_keys = keys.size(1)
        batch_size = keys.size(0)
        queries, keys, values = self.preprocess_inputs(queries=queries, keys=keys, values=values)
        attention = torch.matmul(queries, keys) / self.scale

        # Pass in information from previous layers
        attention = self.process_masks_and_weights(attention, num_keys, attention_mask, attention_weights)
        if self.training:
            self.compute_prior(keys, attention_mask, num_keys)
            attention = self.stochastic_attention(attention, keys)
        else:
            # deterministic attention computation
            attention = torch.softmax(attention, dim=-1)
        output = torch.matmul(attention, values)

        # reshape
        output = output.permute(0, 2, 1, 3).contiguous().view(batch_size, num_queries, self.num_heads * self.value_size)
        output = self.output(output)
        return output

class AttentionLayer(nn.Module):
    def __init__(
        self,
        out_size: int,
        key_size: int,
        value_size: int,
        num_heads: int,
        dropout: float = 0.5,
        num_memory_slots: Optional[int] = None,
        bayesian: bool = False,
        k:Optional[float] = None,
    ) -> NoReturn:
        """Wrapper around the attention module to add the other components in the paper

        The Meshed Memory paper includes residual connections between each attention module. This class implements that and also
        incorporates dropout regularization which will absolutely be needed given the data size.

        Args:
            out_size (int): intermediate dimension between attention layers
            key_size (int): dimensionality of keys
            value_size (int): dimensionality of values
            num_heads (int): number of attention heads
            dropout (float, optional): dropout rate. Defaults to 0.5.
            num_memory_slots (Optional[int], optional): number of memory slots to use. Defaults to None.
        """
        super().__init__()
        self.bayesian = bayesian
        if bayesian:
            if num_memory_slots is not None:
                self.attention = BayesianAttentionWithMemory(
                    k=k,out_size=out_size, key_size=key_size, value_size=value_size, num_heads=num_heads, num_mem_slots=num_memory_slots
                )
            else:
                self.attention = BayesianAttention(
                    k=k, out_size=out_size, key_size=key_size, value_size=value_size, num_heads=num_heads
                )
        else:
            if num_memory_slots is not None:
                self.attention = AttentionWithMemory(out_size, key_size, value_size, num_heads, num_memory_slots)
            else:
                self.attention = Attention(out_size, key_size, value_size, num_heads)
        self.dropout = nn.Dropout(dropout)
        self.norm = nn.LayerNorm(out_size)

    def forward(
        self,
        queries: torch.Tensor,
        keys: torch.Tensor,
        values: torch.Tensor,
        attention_mask: Optional[torch.Tensor] = None,
        attention_weights: Optional[torch.Tensor] = None,
    ) -> torch.Tensor:
        """_summary_

        Args:
            queries (torch.Tensor): _description_
            keys (torch.Tensor): _description_
            values (torch.Tensor): _description_
            attention_mask (Optional[torch.Tensor], optional): _description_. Defaults to None.
            attention_weights (Optional[torch.Tensor], optional): _description_. Defaults to None.

        Returns:
            torch.Tensor: _description_
        """
        output = self.attention(queries, keys, values, attention_mask, attention_weights)
        if self.bayesian:
            self.kl = self.attention.kl
        output = self.dropout(output)
        output = self.norm(output + queries)
        return output
