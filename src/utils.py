import torch
from torch.utils.tensorboard import SummaryWriter
import torch.nn as nn
import typing
from tqdm import tqdm
from time import time
import numpy as np
from torchmetrics import BLEUScore
from torchmetrics.text.rouge import ROUGEScore
from nltk.translate.meteor_score import meteor_score
from nltk.tokenize import word_tokenize
from functools import wraps
from pytorch_lightning.callbacks import Callback
import pytorch_lightning as pl
from pytorch_lightning.utilities.types import STEP_OUTPUT
from typing import Any, Optional, NoReturn



class NLPMetricAggregator(object):
    """Class for aggregating caption hypotheses and generating NLP metrics"""
    def __init__(self, inv_word_map:dict) -> None:
        self.inv_word_map = inv_word_map
        self.bleu1 = BLEUScore(1)
        self.bleu2 = BLEUScore(2)
        self.bleu3 = BLEUScore(3)
        self.bleu4 = BLEUScore(4)
        self.rouge = ROUGEScore()
        self.meteor_meter = AverageMeter()
        self.reset()
    
    def convert_tokens_to_string(self, caption:list) -> str:
        return " ".join( [self.inv_word_map[tok] for tok in caption if self.inv_word_map[tok] not in ["<pad>", "<start>"] ]).strip()
    
    def update(self, predicted: list, reference: list):
        """Store predictions and references"""
        predicted = self.convert_tokens_to_string(predicted)
        reference = [self.convert_tokens_to_string(ref) for ref in reference]
        # Update Meteor Meter
        self.meteor_meter.update(meteor_score([word_tokenize(r) for r in reference], word_tokenize(predicted)))
        self._predicted_captions.append(predicted)
        self._reference_captions.append(reference)
    
    def reset(self):
        self._predicted_captions = []
        self._reference_captions = []
        
    def generate_metric_summaries(self):
        """Retrieves NLP metrics
        Returns:
            (dict): A dictionary of NLP metrics generated from stored hypotheses and references
        """
        return {
            "bleu1": self.bleu1(self._predicted_captions, self._reference_captions),
            "bleu2": self.bleu2(self._predicted_captions, self._reference_captions),
            "bleu3": self.bleu3(self._predicted_captions, self._reference_captions),
            "bleu4": self.bleu4(self._predicted_captions, self._reference_captions),
            "rouge_fmeasure": self.rouge(self._predicted_captions, self._reference_captions)["rouge1_fmeasure"],
            "meteor": np.round(self.meteor_meter.get_average(), 4),
        }


class AverageMeter(object):
    """Simple class to compute a running average of some tracked value and can be printed"""

    def __init__(self, name: str = "mean") -> None:
        self._name = name
        self._sum = 0
        self._count = 0

    def get_average(self) -> float:
        if self._count == 0:
            return 0
        return self._sum / self._count

    def update(self, x: float) -> typing.NoReturn:
        self._sum += x
        self._count += 1

    def __str__(self) -> str:
        return f"{self._name}: {self.get_average():.4f}"

class EarlyStopping(object):
    def __init__(
        self,
        checkpoint_path: str,
        delta: float = 0.005,
        mode: str = "max",
        patience: int = 5,
        report_func: typing.Callable = print,
    ) -> None:
        self.patience = patience
        self.checkpoint = checkpoint_path
        self.mode = mode
        self.counter = 0
        self.delta = delta
        self.report = report_func
        self.stop = False
        self.best_metric = np.inf if mode == "min" else -np.inf

    def __call__(self, metric: float, state: dict):

        metric = abs(metric)

        if (self.mode == "min" and self.best_metric - self.delta <= metric) or (
            self.mode == "max" and self.best_metric + self.delta >= metric
        ):
            self.counter += 1
            self.report(f"Model did not improve {self.counter}/{self.patience}")
            if self.counter >= self.patience:
                self.stop = True
        else:
            self.best_metric = metric
            torch.save(state, self.checkpoint)
            self.counter = 0

class Flickr30KMetricsCallback(Callback):
    def __init__(self, inv_word_map:dict, caption_reference:dict, sequence_len:int = 30):
        self.tracker = NLPMetricAggregator(inv_word_map)
        self.tracker.reset()
        self.caption_refs = caption_reference
        self.seq_len = sequence_len
    
    def on_validation_batch_end(self, trainer: pl.Trainer, pl_module: pl.LightningModule, outputs: Optional[STEP_OUTPUT], batch: Any, batch_idx: int, dataloader_idx: int) -> None:
        _, _, img_ids = batch
        batch_predictions = outputs["val_batch_preds"]
        pred_caps = torch.argmax(batch_predictions, dim=-1) # get token predictions
        pred_caps = pred_caps.unsqueeze(0).view(-1, self.seq_len - 1)
        for pred, id in zip(pred_caps.tolist(), img_ids):
            ref_caps = self.caption_refs[id]
            self.tracker.update(pred, ref_caps)

    def on_validation_epoch_end(self, trainer: pl.Trainer, pl_module: pl.LightningModule) -> None:
        metrics = self.tracker.generate_metric_summaries()
        self.tracker.reset()
        for metric, value in metrics.items():
            pl_module.log(metric, value)

def calc_time(t: float) -> str:
    hours = int(t) // 3600
    minutes = int(t - hours * 3600) // 60
    seconds = int(t - hours * 3600 - minutes * 60)
    return f"{hours}h{minutes}m{seconds}s"


def topk_accuracy(yhat: torch.Tensor, y: torch.Tensor, k: int):
    batch_size = y.size(0)
    _, idx = yhat.topk(k, 1, True, True)
    correct = idx.eq(y.view(-1, 1).expand_as(idx))
    correct = correct.view(-1).float().sum()
    return correct.item() / batch_size * 100.0


def count_trainable_parameters(model):
    return sum(p.numel() for p in model.parameters() if p.requires_grad)

def send_text_message(message):
    raise NotImplementedError