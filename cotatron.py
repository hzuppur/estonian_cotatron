import os
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import DataLoader
from torch.utils.tensorboard import SummaryWriter
import pytorch_lightning as pl
import random
import numpy as np
from omegaconf import OmegaConf

from modules import TextEncoder, TTSDecoder, Audio2Mel, SpeakerEncoder, SpkClassifier
from datasets import TextMelDataset, text_mel_collate
from datasets.text import Language


class Cotatron(pl.LightningModule):
    def __init__(self, hparams):
        super().__init__()
        self.hparams = hparams  # used for pl
        hp_global = OmegaConf.load(hparams.config[0])
        hp_cota = OmegaConf.load(hparams.config[1])
        hp = OmegaConf.merge(hp_global, hp_cota)
        self.hp = hp

        self.symbols = Language(hp.data.lang, hp.data.text_cleaners).get_symbols()
        self.symbols = ['"{}"'.format(symbol) for symbol in self.symbols]
        self.encoder = TextEncoder(hp.chn.encoder, hp.ker.encoder, hp.depth.encoder, len(self.symbols))
        self.speaker = SpeakerEncoder(hp)
        self.classifier = SpkClassifier(hp)
        self.decoder = TTSDecoder(hp)

        self.audio2mel = Audio2Mel(hp.audio.filter_length, hp.audio.hop_length, hp.audio.win_length,
            hp.audio.sampling_rate, hp.audio.n_mel_channels, hp.audio.mel_fmin, hp.audio.mel_fmax)

        self.is_val_first = True

    def forward(self, text, mel_target, speakers, input_lengths, output_lengths, max_input_len,
                prenet_dropout=0.5, no_mask=False, tfrate=True):
        text_encoding = self.encoder(text, input_lengths)  # [B, T, chn.encoder]
        speaker_emb = self.speaker(mel_target, output_lengths)  # [B, chn.speaker]
        speaker_emb_rep = speaker_emb.unsqueeze(1).expand(-1, text_encoding.size(1), -1)  # [B, T, chn.speaker]
        decoder_input = torch.cat((text_encoding, speaker_emb_rep), dim=2)  # [B, T, (chn.encoder + chn.speaker)]
        mel_pred, mel_postnet, alignment = \
            self.decoder(mel_target, decoder_input, input_lengths, output_lengths, max_input_len,
                         prenet_dropout, no_mask, tfrate)
        return speaker_emb, mel_pred, mel_postnet, alignment

    def inference(self, text, mel_target):
        device = text.device
        in_len = torch.LongTensor([text.size(1)]).to(device)
        out_len = torch.LongTensor([mel_target.size(2)]).to(device)

        text_encoding = self.encoder.inference(text)
        speaker_emb = self.speaker.inference(mel_target)
        speaker_emb_rep = speaker_emb.unsqueeze(1).expand(-1, text_encoding.size(1), -1)
        decoder_input = torch.cat((text_encoding, speaker_emb_rep), dim=2)
        _, mel_postnet, alignment = \
            self.decoder(mel_target, decoder_input, in_len, out_len, in_len,
                         prenet_dropout=0.0, no_mask=True, tfrate=False)
        return mel_postnet, alignment

    def training_step(self, batch, batch_idx):
        text, mel_target, speakers, input_lengths, output_lengths, max_input_len = batch
        speaker_emb, mel_pred, mel_postnet, _ = \
            self.forward(text, mel_target, speakers, input_lengths, output_lengths, max_input_len)
        speaker_prob = self.classifier(speaker_emb)
        classifier_loss = F.nll_loss(speaker_prob, speakers)

        loss_rec = F.mse_loss(mel_pred, mel_target) + F.mse_loss(mel_postnet, mel_target)
        self.logger.log_loss(loss_rec, mode='train', step=self.global_step, name='reconstruction')
        self.logger.log_loss(classifier_loss, mode='train', step=self.global_step, name='classifier')

        return {'loss': loss_rec + classifier_loss}

    def validation_step(self, batch, batch_idx):
        text, mel_target, speakers, input_lengths, output_lengths, max_input_len = batch
        speaker_emb, mel_pred, mel_postnet, alignment = \
            self.forward(text, mel_target, speakers, input_lengths, output_lengths, max_input_len,
                         prenet_dropout=0.0, tfrate=False)
        speaker_prob = self.classifier(speaker_emb)
        classifier_loss = F.nll_loss(speaker_prob, speakers)

        loss_rec = F.mse_loss(mel_pred, mel_target) + F.mse_loss(mel_postnet, mel_target)

        if self.is_val_first: # plot alignment, character embedding
            self.is_val_first = False
            self.logger.log_figures(mel_pred, mel_postnet, mel_target, alignment, self.global_step)
            self.logger.log_embedding(self.symbols, self.encoder.embedding.weight, self.global_step)

        return {'loss_rec': loss_rec, 'classifier_loss': classifier_loss}

    def validation_end(self, outputs):
        loss_rec = torch.stack([x['loss_rec'] for x in outputs]).mean()
        classifier_loss = torch.stack([x['classifier_loss'] for x in outputs]).mean()
        self.logger.log_loss(loss_rec, mode='val', step=self.global_step, name='reconstruction')
        self.logger.log_loss(classifier_loss, mode='val', step=self.global_step, name='classifier')
        self.is_val_first = True

        return {'val_loss': loss_rec + classifier_loss}

    def configure_optimizers(self):
        return torch.optim.Adam(
            self.parameters(),
            lr=self.hp.train.adam.lr,
            weight_decay=self.hp.train.adam.weight_decay,
        )

    def lr_lambda(self, step):
        progress = (step - self.hp.train.decay.start) / (self.hp.train.decay.end - self.hp.train.decay.start)
        return self.hp.train.decay.rate ** np.clip(progress, 0.0, 1.0)

    def optimizer_step(self, epoch_nb, batch_nb, optimizer, optimizer_idx, second_order_closure):
        lr_scale = self.lr_lambda(self.global_step)
        for pg in optimizer.param_groups:
            pg['lr'] = lr_scale * self.hp.train.adam.lr

        optimizer.step()
        optimizer.zero_grad()

        self.logger.log_learning_rate(lr_scale * self.hp.train.adam.lr, self.global_step)

    def train_dataloader(self):
        trainset = TextMelDataset(self.hp, self.hp.data.train_dir, self.hp.data.train_meta, train=True, norm=True)
        return DataLoader(trainset, batch_size=self.hp.train.batch_size, shuffle=True,
                        num_workers=self.hp.train.num_workers,
                        collate_fn=text_mel_collate, pin_memory=True, drop_last=True)

    def val_dataloader(self):
        valset = TextMelDataset(self.hp, self.hp.data.val_dir, self.hp.data.val_meta, train=False, norm=True)
        return DataLoader(valset, batch_size=self.hp.train.batch_size, shuffle=False,
                        num_workers=self.hp.train.num_workers,
                        collate_fn=text_mel_collate, pin_memory=False, drop_last=False)
