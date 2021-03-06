#! /usr/bin/env python3
# -*- coding: utf-8 -*-

# Copyright 2019 Kyoto University (Hirofumi Inaguma)
#  Apache 2.0  (http://www.apache.org/licenses/LICENSE-2.0)

"""Monotonic chunkwise atteniton (MoChA)."""

# [reference]
# https://github.com/j-min/MoChA-pytorch/blob/94b54a7fa13e4ac6dc255b509dd0febc8c0a0ee6/attention.py

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

from neural_sp.models.modules.causal_conv import CausalConv1d

NEG_INF = float(np.finfo(np.float32).min)


class Energy(nn.Module):
    def __init__(self, kdim, qdim, adim, init_r=None, conv1d=False, conv_kernel_size=5):
        """Energy function.

        Args:
            kdim (int): dimension of key
            qdim (int): dimension of quary
            adim (int): dimension of attention space
            init_r (int): initial value for offset r
            conv1d (bool): use 1D causal convolution for energy calculation
            conv_kernel_size (int): kernel size for 1D convolution

        """
        super().__init__()

        assert conv_kernel_size % 2 == 1, "Kernel size should be odd for 'same' conv."
        self.key = None
        self.mask = None

        self.w_key = nn.Linear(kdim, adim)
        self.w_query = nn.Linear(qdim, adim, bias=False)
        self.v = nn.Linear(adim, 1, bias=False)
        if init_r is not None:
            # for alpha
            self.r = nn.Parameter(torch.Tensor([init_r]))
            self.v = nn.utils.weight_norm(self.v, name='weight', dim=0)
            # initialization
            self.v.weight_g.data = torch.Tensor([1 / adim]).sqrt()
        else:
            # for beta
            self.r = None

        self.conv1d = None
        if conv1d:
            self.conv1d = CausalConv1d(in_channels=kdim,
                                       out_channels=kdim,
                                       kernel_size=conv_kernel_size,
                                       stride=1)
            # padding=(conv_kernel_size - 1) // 2

    def reset(self):
        self.key = None
        self.mask = None

    def forward(self, key, query, mask, aw_prev=None, cache=True):
        """Compute energy.

        Args:
            key (FloatTensor): `[B, klen, kdim]`
            query (FloatTensor): `[B, 1, qdim]`
            mask (ByteTensor): `[B, qmax, klen]`
            aw_prev (FloatTensor): `[B, klen, 1]`
            cache (bool): cache key and mask
        Return:
            energy (FloatTensor): `[B, value_dim]`

        """
        bs, klen, kdim = key.size()

        # Pre-computation of encoder-side features for computing scores
        if self.key is None or not cache:
            # 1d conv
            if self.conv1d is not None:
                key = torch.relu(self.conv1d(key))
            self.key = self.w_key(key)
            self.mask = mask

        # energy = torch.tanh(self.key + self.w_query(query))
        energy = torch.relu(self.key + self.w_query(query))
        energy = self.v(energy).squeeze(-1)  # `[B, klen]`
        if self.r is not None:
            energy = energy + self.r
        if self.mask is not None:
            energy = energy.masked_fill_(self.mask == 0, NEG_INF)
        return energy


class MoChA(nn.Module):
    def __init__(self, kdim, qdim, adim, chunk_size, adaptive=False, conv1d=False,
                 init_r=-4, noise_std=1.0, eps=1e-6, sharpening_factor=1.0):
        """Monotonic chunk-wise attention.

            "Monotonic Chunkwise Attention" (ICLR 2018)
            https://openreview.net/forum?id=Hko85plCW

            if chunk_size == 1, this is equivalent to Hard monotonic attention
                "Online and Linear-Time Attention by Enforcing Monotonic Alignment" (ICML 2017)
                 http://arxiv.org/abs/1704.00784

        Args:
            kdim (int): dimension of key
            qdim (int): dimension of query
            adim: (int) dimension of the attention layer
            chunk_size (int): size of chunk
            adaptive (bool): adaptive MoChA
            conv1d (bool): apply 1d convolution for energy calculation
            init_r (int): initial value for parameter 'r' used for monotonic attention
            noise_std (float): standard deviation for input noise
            eps (float):
            sharpening_factor (flaot): sharping factor for beta calculation

        """
        super(MoChA, self).__init__()

        self.chunk_size = chunk_size
        self.adaptive = adaptive
        self.unconstrained = False
        if chunk_size >= 100:
            self.unconstrained = True
        self.n_heads = 1
        # regularization
        self.noise_std = noise_std
        self.eps = eps
        self.sharpening_factor = sharpening_factor

        self.monotonic_energy = Energy(kdim, qdim, adim, init_r, conv1d=conv1d)
        self.chunk_energy = Energy(kdim, qdim, adim) if chunk_size > 1 else None

        self.chunk_len_energy = None
        if adaptive:
            assert chunk_size > 1
            self.chunk_len_energy = Energy(kdim, qdim, adim)

    def reset(self):
        self.monotonic_energy.reset()
        if self.chunk_size > 1:
            self.chunk_energy.reset()
            if self.adaptive:
                self.chunk_len_energy.reset()

    def forward(self, key, value, query, mask=None, aw_prev=None,
                mode='hard', cache=True, trigger_point=None):
        """Soft monotonic attention during training.

        Args:
            key (FloatTensor): `[B, klen, kdim]`
            value (FloatTensor): `[B, klen, value_dim]`
            query (FloatTensor): `[B, 1, qdim]`
            mask (ByteTensor): `[B, qmax, klen]`
            aw_prev (FloatTensor): `[B, klen, 1]`
            mode (str): recursive/parallel/hard
            cache (bool): cache key and mask
            trigger_point (IntTensor): `[B]`
        Return:
            cv (FloatTensor): `[B, 1, value_dim]`
            alpha (FloatTensor): `[B, klen, 1]`

        """
        bs, klen = key.size()[:2]

        if aw_prev is None:
            # aw_prev = [1, 0, 0 ... 0]
            aw_prev = key.new_zeros(bs, klen, 1)
            aw_prev[:, 0:1] = key.new_ones(bs, 1, 1)

        # Compute monotonic energy
        e_mono = self.monotonic_energy(key, query, mask, cache=cache)

        if mode == 'recursive':  # training
            p_choose_i = torch.sigmoid(add_gaussian_noise(e_mono, self.noise_std))  # `[B, klen]`
            # Compute [1, 1 - p_choose_i[0], 1 - p_choose_i[1], ..., 1 - p_choose_i[-2]]
            shifted_1mp_choose_i = torch.cat([key.new_ones(bs, 1),
                                              1 - p_choose_i[:, :-1]], dim=1)
            # Compute attention distribution recursively as
            # q[j] = (1 - p_choose_i[j]) * q[j - 1] + aw_prev[j]
            # alpha[j] = p_choose_i[j] * q[j]
            q = key.new_zeros(bs, klen + 1)
            for j in range(klen):
                q[:, j + 1] = shifted_1mp_choose_i[:, j].clone() * q[:, j].clone() + aw_prev[:, j, 0].clone()
            alpha = p_choose_i * q[:, 1:]

        elif mode == 'parallel':  # training
            p_choose_i = torch.sigmoid(add_gaussian_noise(e_mono, self.noise_std))  # `[B, klen]`
            # safe_cumprod computes cumprod in logspace with numeric checks
            cumprod_1mp_choose_i = safe_cumprod(1 - p_choose_i, eps=self.eps)
            # Compute recurrence relation solution
            alpha = p_choose_i * cumprod_1mp_choose_i * torch.cumsum(
                aw_prev.squeeze(2) / torch.clamp(cumprod_1mp_choose_i, min=self.eps, max=1.0), dim=1)

            # Mask the right part from the trigger point
            if trigger_point is not None:
                for b in range(bs):
                    alpha[b, trigger_point[b] + 1:] = 0
                    # TODO(hirofumi): add tolerance parameter

        elif mode == 'hard':  # inference
            # Attend when monotonic energy is above threshold (Sigmoid > 0.5)
            emit_probs = torch.sigmoid(e_mono)
            p_choose_i = (emit_probs >= 0.5).float()
            # Remove any probabilities before the index chosen at the last time step
            p_choose_i *= torch.cumsum(aw_prev.squeeze(2), dim=1)  # `[B, klen]`
            # Now, use exclusive cumprod to remove probabilities after the first
            # chosen index, like so:
            # p_choose_i                        = [0, 0, 0, 1, 1, 0, 1, 1]
            # 1 - p_choose_i                    = [1, 1, 1, 0, 0, 1, 0, 0]
            # exclusive_cumprod(1 - p_choose_i) = [1, 1, 1, 1, 0, 0, 0, 0]
            # alpha: product of above           = [0, 0, 0, 1, 0, 0, 0, 0]
            alpha = p_choose_i * exclusive_cumprod(1 - p_choose_i)
        else:
            raise ValueError("mode must be 'recursive', 'parallel', or 'hard'.")

        # Compute chunk energy
        beta = None
        if self.chunk_size > 1:
            e_chunk = self.chunk_energy(key, query, mask, cache=cache)
            if self.adaptive:
                e_chunk_len = self.chunk_len_energy(key, query, mask, cache=cache)
                if self.unconstrained:
                    chunk_len_dist = torch.exp(e_chunk_len)
                else:
                    chunk_len_dist = self.chunk_size * torch.sigmoid(e_chunk_len)
                # avoid zero length
                chunk_len_dist = chunk_len_dist.int() + 1
                beta = efficient_adaptive_chunkwise_attention(
                    alpha, e_chunk, chunk_len_dist, self.sharpening_factor)
            else:
                beta = efficient_chunkwise_attention(
                    alpha, e_chunk, self.chunk_size, self.sharpening_factor)

        # Compute context vector
        if self.chunk_size > 1:
            cv = torch.bmm(beta.unsqueeze(1), value)
            beta = beta.unsqueeze(2)
        else:
            cv = torch.bmm(alpha.unsqueeze(1), value)

        return cv, alpha.unsqueeze(2)


def add_gaussian_noise(xs, std):
    """Additive gaussian nosie to encourage discreteness."""
    noise = xs.new_zeros(xs.size()).normal_(std=std)
    return xs + noise


def safe_cumprod(x, eps):
    """Numerically stable cumulative product by cumulative sum in log-space."""
    return torch.exp(exclusive_cumsum(torch.log(torch.clamp(x, min=eps, max=1.0))))


def exclusive_cumsum(x):
    """Exclusive cumulative summation [a, b, c] => [0, a, a + b]."""
    return torch.cumsum(torch.cat([x.new_zeros(x.size(0), 1), x[:, :-1]], dim=1), dim=1)


def exclusive_cumprod(x):
    """Exclusive cumulative product [a, b, c] => [1, a, a * b]."""
    return torch.cumprod(torch.cat([x.new_ones(x.size(0), 1), x[:, :-1]], dim=1), dim=1)


def moving_sum(x, back, forward):
    """Compute the moving sum of x over a chunk_size with the provided bounds.

    Args:
        x (FloatTensor): `[B, klen]`
        back (int):
        forward (int):

    Returns:
        x_sum (FloatTensor): `[B, klen]`
    """
    # Moving sum is computed as a carefully-padded 1D convolution with ones
    x_padded = F.pad(x, pad=[back, forward])
    # Add a "channel" dimension
    x_padded = x_padded.unsqueeze(1)  # `[B, 1, back + T + forward]`
    # Construct filters
    filters = x.new_ones(1, 1, back + forward + 1)
    x_sum = F.conv1d(x_padded, filters)
    # Remove channel dimension
    return x_sum.squeeze(1)


def efficient_chunkwise_attention(alpha, e, chunk_size, sharpening_factor=1.):
    """Compute chunkwise attention distribution efficiently by clipping logits.

    Args:
        alpha (FloatTensor): `[B, klen]`
        e (FloatTensor): `[B, klen]`
        chunk_size (int): size of chunk
    Return
        beta (FloatTensor): `[B, klen]`

    """
    # Shift logits to avoid overflow
    e -= torch.max(e, dim=1, keepdim=True)[0]
    # Limit the range for numerical stability
    softmax_exp = torch.clamp(torch.exp(e), min=1e-5)
    # Compute chunkwise softmax denominators
    softmax_denominators = moving_sum(softmax_exp,
                                      back=chunk_size - 1, forward=0)
    # Compute \beta_{i, :}. emit_probs are \alpha_{i, :}.
    beta = softmax_exp * moving_sum(alpha * sharpening_factor / softmax_denominators,
                                    back=0, forward=chunk_size - 1)
    return beta


def efficient_adaptive_chunkwise_attention(alpha, e, chunk_len_dist, sharpening_factor=1.):
    """Compute adaptive chunkwise attention distribution efficiently by clipping logits.

    Args:
        alpha (FloatTensor): `[B, klen]`
        e (FloatTensor): `[B, klen]`
        chunk_len_dist (IntTensor): `[B, klen]`
    Return
        beta (FloatTensor): `[B, klen]`

    """
    # Shift logits to avoid overflow
    e -= torch.max(e, dim=1, keepdim=True)[0]
    # Limit the range for numerical stability
    softmax_exp = torch.clamp(torch.exp(e), min=1e-5)
    # Compute chunkwise softmax denominators
    boundary = torch.argmax(alpha, dim=1)
    bs = alpha.size(0)
    softmax_denominators = [moving_sum(
        softmax_exp[b:b + 1],
        back=chunk_len_dist[b, boundary[b]] - 1, forward=0)
        for b in range(bs)]
    # Compute \beta_{i, :}. emit_probs are \alpha_{i, :}.
    beta = [softmax_exp[b:b + 1] * moving_sum(
        alpha[b:b + 1] * sharpening_factor / softmax_denominators[b],
        back=0, forward=chunk_len_dist[b, boundary[b]] - 1)
        for b in range(bs)]
    beta = torch.cat(beta, dim=0)
    return beta
