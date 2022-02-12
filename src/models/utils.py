"""Provides utilities for model training"""
import torch
from torch.utils.tensorboard import SummaryWriter
import torch.nn as nn
import typing
from tqdm import tqdm
from time import time


def save_model_dict(model: nn.Module, path: str) -> typing.NoReturn:
    """Wrapper function for saving model state
    Args:
        model (nn.Module): The model to save
        path (str): The path to the checkpoint location
    """
    torch.save(model.state_dict(), path)


def load_model_dict(model: nn.Module, path: str) -> nn.Module:
    """Wrapper function for loading the model state
    Args:
        model (nn.Module): The model to save
        path (str): The path to the checkpoint location
    """
    model.load_state_dict(torch.load(path))
    return model
