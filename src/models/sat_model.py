"""This module contains the Show, Attend, and Tell model. 
This model consists of encoder and decoder stages. The encoder is a 
convolutional neural netowrk, and the decodedr is an LSTM model
where the annotation vectors generated by the encoder is weighted
by an attention model.

By the end of this project, this module should contain the following

 - Encoder Module
 - Decoder Module
 - Bayesian Decoder Module

"""
from base64 import encode
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torchvision.models as models
import typing
from typing import Optional
from .attention import SATAttention


class SATEncoder(nn.Module):
    """Show, Attend, and Tell encoder. For this project, we will use EfficientNet with ImageNet weights

    This part of the model is fairly simple, we take the convolutional outputs of the feature extraction
    network, then resize it to the required size for the decoder. We guaruntee the size of the output using
    an AdaptiveAveragePool. Given the small size of the dataset, it is prudent here to use a pretrained model,
    and remove the linear layers.
    """

    def __init__(
        self,
        encoded_size:int=7,
        pretrained: bool = True,
        freeze: bool = True,
        unfreeze_last: int = 0,
    ) -> typing.NoReturn:
        super(SATEncoder, self).__init__()
        features = models.resnet152(pretrained=pretrained)

        # remove classifier at the top of the model
        features = nn.Sequential(*(list(features.children())[:-2]))
        # freeze model parameters
        if freeze:
            for param in features.parameters():
                param.requires_grad = False

        # only useful if model is already frozen. Unfreezes the last n layers
        if unfreeze_last > 0:
            for param in features.features[unfreeze_last].parameters():
                param.requires_grad = True
        self.features = features
        self.sizing = nn.AdaptiveAvgPool2d((encoded_size, encoded_size))

    def forward(self, x: torch.Tensor):
        """Implements the forward pass of the encoder
        In the paper, the convolutional feature extractor produced :math:`L` vectors,
        each of whic is a D-Dimensional representation corresponding to the image.
        .. math::
            a\,=\,\{a_1, \dots, a_L\}, a_i ∈ \\mathbb{R}^D

        In practice the convolutional network returns a 3-tensor for each entry in the batch.
        In the decoder, we'll convert this 3 tensor in to a set of annotation vectors as
        described in the paper.
        Args:
            x (torch.Tensor) : The input image of shape (batch_size, channels, width, height).
        Returns:
            torch.Tensor : encoded image tensor of shape (batch_size, 1280, imag_size//32, image_size//32)
        """
        x = self.features(x)  # (batch_size, 1280, image_size//32, image_size//32 )
        x = self.sizing(x) # (batch_size, 1280, encoded_size, encoded_size)
        return x.permute(0, 2, 3, 1)  # pass encoded values and additional arguments to next layer


class SATDecoder(nn.Module):
    """Show, Attend, and Tell Decoder. For this we use an LSTM model to process the features

    This part of the model requires some additional work. According to the Show, Attend, and Tell paper,
    the decoder is composed of multiple parts: an MLP for initializing :math:`h_0`, an MLP for initializing :math:`c_0`,
    an attention model, and a LSTM. To implement these components, we use a single Linear layer to represent the MLPs
    encoding the initial hidden and memory state, an attention model following the specifications of the paper,
    and a LSTM Cell.
    """

    def __init__(
        self,
        embedding_size: int,
        vocabulary_size: int,
        max_caption_size: int,
        hidden_size: int,
        attention_size:int ,
        encoder_size: int = 1280,
        device: str = "cpu",
        dropout_rate:float = 0.5
    ) -> typing.NoReturn:
        super().__init__()

        # constants
        self.vocab_size = vocabulary_size
        self._device = device
        self._max_cap_size = max_caption_size

        # Adding attention mechanism
        self.attention = SATAttention(encoder_size, hidden_size, attention_size)

        # MLPs for initializing states
        self.fh = nn.Linear(encoder_size, hidden_size)  # Hidden State Initializer
        self.fc = nn.Linear(encoder_size, hidden_size)  # Memory Cell intializer

        # Gating Sigmoid
        self.f_beta = nn.Linear(hidden_size, encoder_size)
        self.sigmoid = nn.Sigmoid()

        # MLP for getting vocabulary scores
        self.deep_output = nn.Linear(hidden_size, vocabulary_size)
        # Embedding Layer
        self.embedding = nn.Embedding(vocabulary_size, embedding_size)

        # LSTM
        self.recurrent = nn.LSTMCell(embedding_size + encoder_size, hidden_size, bias=True)
        # Dropout Regularization
        self.dropout = nn.Dropout(dropout_rate)
        # Teacher Forcing Rate
        self._teacher_forcing_rate = 1
        self.initialize_weights()

    def initialize_weights(self):
        self.embedding.weight.data.uniform_(-0.1,0.1)
        self.deep_output.weight.data.uniform_(-0.1,0.1)
        self.deep_output.bias.data.fill_(0)

    def update_scheduled_sampling_rate(self, convergence_rate: float) -> typing.NoReturn:
        """Updates the scheduled sampling rate linearly"""
        self._teacher_forcing_rate -= convergence_rate  # linear ramp clipped at zero
        if self._teacher_forcing_rate <= 0:
            self._teacher_forcing_rate = 0
            return

    def initialize_hidden_states(self, encoded: torch.Tensor) -> tuple:
        mean = encoded.mean(dim=1)  # row wise mean to get the average a_i vector
        h = F.relu(self.fh(mean))
        c = F.relu(self.fc(mean))
        return h, c

    def forward(
        self,
        x: torch.Tensor,
        captions: torch.Tensor = None,
        lengths: torch.Tensor = None,
        scheduled_sampling: bool = False,
    ) -> tuple:
        """Implements the forward  step of the decoder network.

        The LSTM hidden and memory weights are initialized using an MLP operating
        on the mean of the annotation vectors.

        The LSTMCell performs the following

        .. math::

            i_t &= \\sigma ( W_i Ey_{t-1} + U_i h_{t-1} + Z_i \hat{z} + b_i )

            f_t &= \\sigma ( W_f Ey_{t-1} + U_f h_{t-1} Z_f \hat{z}_t + b_f )

            c_t &= f_t c_{t-1} + i_t \\tanh ( W_c Ey_{t-1} + U_c h_{t-1} + Z_c \hat{z}_t + b_c )

            o_t &= \\sigma ( W_o Ey_{t-1} + U_o h_{t-1} + Z_0 \\hat{z}_t + b_o )

            h_t &= o_t \\tanh (c _t)

        where the input, forget, memory, output and hidden state of the LSTM are represented by the
        preceding equations, respectively. W, U, and Z are weights matrices, and the E matrices are the
        embedding matrices. The :math:`\\hat{z}` vectors are the context vectors generated by the product of
        the annotation and the :math:`\\alpha` weights generated by the attention model.

        First, the encoder outputs are embedded into the learned embedding. This is represented in the
        :math:`Ey_{t-1}` variables. The hidden and memory states are intialized by passing the mean
        of the annotations vector to MLPs. These actions are represented in the following equations.

        .. math::
            h_0 = f_{init_h}\\left( \\frac{1}{L}\\sum_{i=1}^{L} a_i \\right)

            c_0 = f_{init_c}\\left( \\frac{1}{L}\\sum_{i=1}^{L} a_i \\right)

        Then for every token in the embedded sequence, the next state of the LSTM is determined by the equations
        describing the LSTM. In the paper, the data is batched into captions of the same length. It's easier to
        just load them randomly and stop processing on the <pad> tokens. Additionaly, we implement scheduled sampling
        which is an extension of the Teacher Forcing algorithm. The salient point here is that the model is fed the ground
        truth from the previous time step at a decreasing rate as the model converges. For simplciity, we implement
        a linear ramp for the rate of teacher forcing.

        Args:
            x (torch.Tensor): A encoded image of shape (batch_size, 1280, image_size//32, image_size//32)
            captions (torch.Tensor): The ground truth captions
            lengths (list): The lengths of the captions
        Returns:
            tuple:
        """
        batch_size = x.size(0)
        encoded_size = x.size(-1)
        vocab_size = self.vocab_size

        # Reshape encoded image into a set of annotation vectors.
        # we can compress the image into a vector and treat encoded_size as the number of annotation vectors
        x = x.view(batch_size, -1, encoded_size)

        # The LSTM expects tensors in (batch_ size, sequence length, number of sequences)
        # x = x.permute(0, 2, 1)

        # initialize hidden states
        h, c = self.initialize_hidden_states(x)

        if scheduled_sampling:
            # embed the ground truth for teacher forcing
            embedded_captions = self.embedding(captions)  # (batch_size, caption_length, embedding dim)

        # our predictions will be the size of the largest encoding (batch_size, largest_encoding, vocab_size)
        # each entry of this tensor will have a score for each batch entry, position in encoding, and vocabulary word candidate
        predictions = torch.zeros(batch_size, self._max_cap_size, vocab_size).to(self._device)  # predictions set to <pad>
        prev_words = torch.zeros((batch_size,)).long().to(self._device)
        αs = torch.zeros(
            batch_size, self._max_cap_size, x.size(1)
        )  # attention generated weights stored for Doubly Stochastic Regularization
        for i in range(self._max_cap_size):
            # For each token, determine if we apply teacher forcing
            if scheduled_sampling and np.random.uniform(0, 1) < self._teacher_forcing_rate:
                # In teacher forcing we know which captions have a specified length, so we can reduce wasteful
                # computation by only applying the model on valid captions
                if i > max(lengths[0]):
                    break  # no more captions left at requested size
                zhat, α = self.attention(x, h)
                # gate
                gate = self.sigmoid(self.f_beta(h))
                zhat = gate * zhat
                # get the next hidden state and memory state of the lstm
                h, c = self.recurrent(
                    # conditioning the LSTM on the previous state's ground truth.
                    # On i=0 this is just the start token
                    torch.cat([embedded_captions[:, i, :], zhat], dim=1),
                    # truncated hidden and memory states
                    (h, c),
                )
                scores = self.deep_output(self.dropout(h))  # assign a score to potential vocabulary candidtates
                predictions[:, i, :] = scores  # append predictions for the i-th token
                prev_words = torch.argmax(scores, dim=1)
                αs[:, i] = α  # store attention weights for doubly stochastic regularization
            else:
                # No teacher forcing done here. We just do the standard LSTM calculations
                zhat, α = self.attention(x, h)  # apply attention
                embedded = self.embedding(prev_words)  # condition on zero
                # Gate
                gate = self.sigmoid(self.f_beta(h))
                zhat = gate * zhat
                h, c = self.recurrent(
                    # Conditioning on previous predicted scores
                    torch.cat([embedded, zhat], dim=1),
                    (h, c),
                )
                scores = self.deep_output(self.dropout(h)) # assign a score to potential vocabulary candidtates
                prev_words = torch.argmax(scores, dim=1)
                predictions[:, i, :] = scores  # append predictions for the i-th token
                αs[:, i, :] = α  # store attention weights for doubly stochastic regularization
        return predictions, αs


class BayesianSATDecoder(nn.Module):
    def __init__(self) -> None:
        super().__init__()
        raise NotImplementedError

    def forward(self, x):
        raise NotImplementedError
