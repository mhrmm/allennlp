from __future__ import unicode_literals, print_function, division
from io import open
import unicodedata
import string
import re
import random

import torch
import torch.nn as nn
from torch.autograd import Variable
from torch import optim
import torch.nn.functional as F

import codecs
import numpy as np
import h5py

import sys

import time
import math
#import matplotlib.pyplot as plt
#import matplotlib.ticker as ticker

from chunking.eval import validateRandomSubset, evaluate, evaluateRandomly
from chunking.data import variablesFromPair, target_variable_from_sentences
from chunking.elmo import ElmoEmbedder
from chunking.elmo import elmo_bilm, variablesFromPairElmo, elmo_variable_from_sentences


use_cuda = torch.cuda.is_available()


__all__ = ['EncoderRNNElmo', 'AttnDecoderRNN', 'trainItersElmo']


SOS_token = 0
EOS_token = 1


class EncoderRNNElmo(nn.Module):
    def __init__(self, hidden_size, device, n_layers=1):
        super(EncoderRNNElmo, self).__init__()
        self.n_layers = n_layers
        self.hidden_size = hidden_size
        special_tokens = ['sos', 'eos', '[[[', ']]]', '<unk>']     
        self.embedding = ElmoEmbedder(elmo_bilm, special_tokens, device)
        self.gru = nn.GRU(hidden_size, hidden_size)

    def forward(self, input, token_index, hidden):
        embedded = self.embedding(input, token_index).view(1, 1, -1)
        output = embedded
        for i in range(self.n_layers):
            output, hidden = self.gru(output, hidden)
        return output, hidden

    def initHidden(self):
        result = Variable(torch.zeros(1, 1, self.hidden_size))
        if use_cuda:
            return result.cuda()
        else:
            return result
        
    def trainableParameters(self):
        return filter(lambda p: p.requires_grad, self.parameters())



class AttnDecoderRNN(nn.Module):
    def __init__(self, hidden_size, output_size, max_length, n_layers=1, dropout_p=0.1):
        super(AttnDecoderRNN, self).__init__()
        self.hidden_size = hidden_size
        self.output_size = output_size
        self.n_layers = n_layers
        self.dropout_p = dropout_p
        self.max_length = max_length

        self.embedding = nn.Embedding(self.output_size, self.hidden_size)
        self.attn = nn.Linear(self.hidden_size * 2, self.max_length)
        self.attn_combine = nn.Linear(self.hidden_size * 2, self.hidden_size)
        self.dropout = nn.Dropout(self.dropout_p)
        self.gru = nn.GRU(self.hidden_size, self.hidden_size)
        self.out = nn.Linear(self.hidden_size, self.output_size)

    def forward(self, input, hidden, encoder_outputs):
        embedded = self.embedding(input).view(1, 1, -1)
        embedded = self.dropout(embedded)

        attn_weights = F.softmax(
            self.attn(torch.cat((embedded[0], hidden[0]), 1)), dim=1)
        attn_applied = torch.bmm(attn_weights.unsqueeze(0),
                                 encoder_outputs.unsqueeze(0))

        output = torch.cat((embedded[0], attn_applied[0]), 1)
        output = self.attn_combine(output).unsqueeze(0)

        for i in range(self.n_layers):
            output = F.relu(output)
            output, hidden = self.gru(output, hidden)

        output = F.log_softmax(self.out(output[0]), dim=1)
        return output, hidden, attn_weights

    def initHidden(self):
        result = Variable(torch.zeros(1, 1, self.hidden_size))
        if use_cuda:
            return result.cuda()
        else:
            return result

teacher_forcing_ratio = 0.5
 
def train(input_variable, target_variable, encoder, decoder, encoder_optimizer, decoder_optimizer, criterion, max_length):
    encoder_hidden = encoder.initHidden()

    encoder_optimizer.zero_grad()
    decoder_optimizer.zero_grad()
    
    target_length = target_variable.size()[0]

    encoder_outputs = Variable(torch.zeros(max_length, encoder.hidden_size))
    encoder_outputs = encoder_outputs.cuda() if use_cuda else encoder_outputs

    loss = 0

    input_length = input_variable.size()[1]
    for ei in range(input_length):
        encoder_output, encoder_hidden = encoder(
            input_variable, ei, encoder_hidden)
        encoder_outputs[ei] = encoder_output[0][0]

    decoder_input = Variable(torch.LongTensor([[SOS_token]]))
    decoder_input = decoder_input.cuda() if use_cuda else decoder_input

    decoder_hidden = encoder_hidden

    use_teacher_forcing = True if random.random() < teacher_forcing_ratio else False

    if use_teacher_forcing:
        # Teacher forcing: Feed the target as the next input
        for di in range(target_length):
            decoder_output, decoder_hidden, decoder_attention = decoder(
                decoder_input, decoder_hidden, encoder_outputs)
            loss += criterion(decoder_output, target_variable[di])
            decoder_input = target_variable[di]  # Teacher forcing

    else:
        # Without teacher forcing: use its own predictions as the next input
        for di in range(target_length):
            decoder_output, decoder_hidden, decoder_attention = decoder(
                decoder_input, decoder_hidden, encoder_outputs)
            topv, topi = decoder_output.data.topk(1)
            ni = topi[0][0]

            decoder_input = Variable(torch.LongTensor([[ni]]))
            decoder_input = decoder_input.cuda() if use_cuda else decoder_input

            loss += criterion(decoder_output, target_variable[di])
            if ni == EOS_token:
                break

    loss.backward()

    encoder_optimizer.step()
    decoder_optimizer.step()

    return loss.data[0] / target_length




def asMinutes(s):
    m = math.floor(s / 60)
    s -= m * 60
    return '%dm %ds' % (m, s)

def timeSince(since, percent):
    now = time.time()
    s = now - since
    es = s / (percent)
    rs = es - s
    return '%s (- %s)' % (asMinutes(s), asMinutes(rs))


def trainItersElmo(encoder, decoder, output_lang, n_iters, sent_pairs, sent_pairs_dev, max_length, print_every=1000, plot_every=100, save_every=10000, learning_rate=0.01):
    start = time.time()
    plot_losses = []
    print_loss_total = 0  # Reset every print_every
    plot_loss_total = 0  # Reset every plot_every

    encoder_optimizer = optim.SGD(encoder.trainableParameters(), lr=learning_rate)
    decoder_optimizer = optim.SGD(decoder.parameters(), lr=learning_rate)
    criterion = nn.NLLLoss()

    for iter in range(1, n_iters + 1):

        training_pair = variablesFromPairElmo(random.choice(sent_pairs), output_lang)
        input_variable = training_pair[0]
        target_variable = training_pair[1]

        loss = train(input_variable, target_variable, encoder,
                     decoder, encoder_optimizer, decoder_optimizer, criterion, max_length)
        print_loss_total += loss
        plot_loss_total += loss
        
        if iter % save_every == 0:
            torch.save(encoder, 'encoder2.{}.pt'.format(iter))
            torch.save(decoder, 'decoder2.{}.pt'.format(iter))

        if iter % print_every == 0:
            print('train accuracy: {}'.format(validateRandomSubset(encoder, decoder, output_lang, sent_pairs, max_length, 100)))
            print('dev accuracy: {}'.format(validateRandomSubset(encoder, decoder, output_lang, sent_pairs_dev, max_length, 100)))
            evaluateRandomly(encoder, decoder, output_lang, sent_pairs, max_length, 3)
            print_loss_avg = print_loss_total / print_every
            print_loss_total = 0
            print('%s (%d %d%%) %.4f' % (timeSince(start, iter / n_iters),
                                         iter, iter / n_iters * 100, print_loss_avg))

        if iter % plot_every == 0:
            plot_loss_avg = plot_loss_total / plot_every
            plot_losses.append(plot_loss_avg)
            plot_loss_total = 0





def trainItersElmoExperimental(encoder, decoder, output_lang, n_iters, minibatch_size, sent_pairs, sent_pairs_dev, max_length, print_every=1000, plot_every=100, save_every=10000, learning_rate=0.01):
    start = time.time()
    plot_losses = []
    print_loss_total = 0  # Reset every print_every
    plot_loss_total = 0  # Reset every plot_every

    encoder_optimizer = optim.SGD(encoder.trainableParameters(), lr=learning_rate)
    decoder_optimizer = optim.SGD(decoder.parameters(), lr=learning_rate)
    criterion = nn.NLLLoss()


    for iter in range(1, n_iters + 1):
        print('starting epoch {}'.format(iter))
        
        training_pairs = [sent_pairs[k] for k in np.random.choice(range(len(sent_pairs)), size=minibatch_size)]
        input_vars = elmo_variable_from_sentences([pair[0] for pair in training_pairs])
        target_vars = target_variable_from_sentences(output_lang, [pair[1] for pair in training_pairs])

        for j in range(minibatch_size):
            input_variable = torch.unsqueeze(input_vars[j], 0)
            target_variable = torch.unsqueeze(target_vars[j], 1)
            loss = train(input_variable, target_variable, encoder,
                     decoder, encoder_optimizer, decoder_optimizer, criterion, max_length)
            print_loss_total += loss
            plot_loss_total += loss
        
        if iter % save_every == 0:
            torch.save(encoder, 'encoder2.{}.pt'.format(iter))
            torch.save(decoder, 'decoder2.{}.pt'.format(iter))

        if iter % print_every == 0:
            print('train accuracy: {}'.format(validateRandomSubset(encoder, decoder, output_lang, sent_pairs, max_length, 100)))
            print('dev accuracy: {}'.format(validateRandomSubset(encoder, decoder, output_lang, sent_pairs_dev, max_length, 100)))
            evaluateRandomly(encoder, decoder, output_lang, sent_pairs, max_length, 3)
            print_loss_avg = print_loss_total / print_every
            print_loss_total = 0
            print('%s (%d %d%%) %.4f' % (timeSince(start, iter / n_iters),
                                         iter, iter / n_iters * 100, print_loss_avg))

        if iter % plot_every == 0:
            plot_loss_avg = plot_loss_total / plot_every
            plot_losses.append(plot_loss_avg)
            plot_loss_total = 0


