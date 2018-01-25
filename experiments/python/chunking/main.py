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


use_cuda = torch.cuda.is_available()

__all__ = ['prepareData', 'WordVectors', 'EncoderRNN', 'AttnDecoderRNN', 'trainIters', 'validate', 'evaluate', 'NeuralChunker',
           'variableFromSentence']


SOS_token = 0
EOS_token = 1

class WordVectors:
    def __init__(self, special_tokens, elmo_vectors):
        self.special_tokens = special_tokens
        self.special_token_to_id = {tok: i for (i, tok) in enumerate(special_tokens)}
        self.dimension = len(special_tokens) + 1024
        self.elmo_vectors = {k: v for (k, v) in elmo_vectors}
        self.words = self.special_tokens + [tok for (tok, v) in elmo_vectors]
        self.index2word = {k: v for (k, v) in enumerate(self.words)}
        self.word2index = {v: k for (k, v) in enumerate(self.words)}
        self.n_words = len(self.words)

    def dumpAsTensor(self):
        veclist = [self.getWordVector(self.index2word[i]) for i in range(self.n_words)]
        return np.concatenate(veclist, axis=0).reshape(-1, self.dimension)
    
    def getWordVector(self, word):
        if word in self.special_token_to_id:
            return np.eye(self.dimension)[self.special_token_to_id[word]]
        else:
            return np.concatenate([np.zeros(len(self.special_tokens)), self.elmo_vectors[word]], axis=0)
    
class Lang:
    def __init__(self, name, wordVectors):
        self.name = name
        self.word2index = {v: k for (k, v) in enumerate(wordVectors.special_tokens)}
        self.index2word = {k: v for (k, v) in enumerate(wordVectors.special_tokens)}
        self.n_words = len(wordVectors.special_tokens)
        self.wordVectors = wordVectors
        self.dimension = wordVectors.dimension

    def addSentence(self, sentence):
        for word in sentence.split(' '):
            self.addWord(word)

    def addWord(self, word):
        if word not in self.word2index:
            self.word2index[word] = self.n_words
            self.index2word[self.n_words] = word
            self.n_words += 1


# Turn a Unicode string to plain ASCII, thanks to
# http://stackoverflow.com/a/518232/2809427
def unicodeToAscii(s):
    return ''.join(
        c for c in unicodedata.normalize('NFD', s)
        if unicodedata.category(c) != 'Mn'
    )

# Lowercase and trim characters
def normalizeString(s):
    s = unicodeToAscii(s.lower().strip())
    return s

def readLangs(lang1, lang2, wordVecs):
    print("Reading lines...")

    # Read the file and split into lines
    lines = open('data/%s-%s.txt' % (lang1, lang2), encoding='utf-8').\
        read().strip().split('\n')

    # Split every line into pairs and normalize
    pairs = [[s.strip() for s in l.split('\t')] for l in lines]

    # Reverse pairs, make Lang instances
    input_lang = Lang(lang1, wordVecs)
    output_lang = Lang(lang2, wordVecs)

    return input_lang, output_lang, pairs

def prepareData(lang1, lang2, wordVecs):
    input_lang, output_lang, pairs = readLangs(lang1, lang2, wordVecs)
    print("Read %s sentence pairs" % len(pairs))
    print("Counting words...")
    max_encoding_length = 1
    for pair in pairs:
        if len(pair[0].split(' ')) > max_encoding_length:
            max_encoding_length = len(pair[0].split(' '))
        input_lang.addSentence(pair[0])
        output_lang.addSentence(pair[1])
    print("Max encoding length: %s" % max_encoding_length)
    print("Counted words:")
    print(input_lang.name, input_lang.n_words)
    print(output_lang.name, output_lang.n_words)
    print(output_lang.index2word)
    return input_lang, output_lang, pairs, max_encoding_length + 5




# Maps the sentence tokens into their ids.
def indexesFromSentence(lang, sentence):
    return [lang.word2index[word] for word in sentence.split(' ')]

# Maps the sentence into a PyTorch vector (containing the token ids).
def variableFromSentence(lang, sentence):
    indexes = indexesFromSentence(lang, sentence)
    indexes.append(EOS_token)
    result = Variable(torch.LongTensor(indexes).view(-1, 1))
    if use_cuda:
        return result.cuda()
    else:
        return result

# Maps the source and target sentence into PyTorch vectors (of the token ids).
# This is the main function; the above are just helpers
def variablesFromPair(pair, input_lang, output_lang):
    input_variable = variableFromSentence(input_lang, pair[0])
    target_variable = variableFromSentence(output_lang, pair[1])
    return (input_variable, target_variable)

class EncoderRNN(nn.Module):
    def __init__(self, input_size, hidden_size, wordVecs, n_layers=1):
        super(EncoderRNN, self).__init__()
        self.n_layers = n_layers
        self.hidden_size = hidden_size
        self.embedding = nn.Embedding(input_size, hidden_size)
        self.embedding.weight.data.copy_(torch.from_numpy(wordVecs.dumpAsTensor()))
        self.embedding.weight.requires_grad = False
        self.gru = nn.GRU(hidden_size, hidden_size)

    def forward(self, input, hidden):
        embedded = self.embedding(input).view(1, 1, -1)
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

    input_length = input_variable.size()[0]
    target_length = target_variable.size()[0]

    encoder_outputs = Variable(torch.zeros(max_length, encoder.hidden_size))
    encoder_outputs = encoder_outputs.cuda() if use_cuda else encoder_outputs

    loss = 0

    for ei in range(input_length):
        encoder_output, encoder_hidden = encoder(
            input_variable[ei], encoder_hidden)
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



import time
import math
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
import numpy as np

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

def showPlot(points):
    plt.figure()
    fig, ax = plt.subplots()
    # this locator puts ticks at regular intervals
    loc = ticker.MultipleLocator(base=0.2)
    ax.yaxis.set_major_locator(loc)
    plt.plot(points)

def trainIters(encoder, decoder, input_lang, output_lang, n_iters, sent_pairs, sent_pairs_dev, max_length, print_every=1000, plot_every=100, save_every=10000, learning_rate=0.01):
    start = time.time()
    plot_losses = []
    print_loss_total = 0  # Reset every print_every
    plot_loss_total = 0  # Reset every plot_every
    parameters = filter(lambda p: p.requires_grad, encoder.parameters())
    #parameters = encoder.parameters()
    encoder_optimizer = optim.SGD(parameters, lr=learning_rate)
    decoder_optimizer = optim.SGD(decoder.parameters(), lr=learning_rate)
    training_pairs = [variablesFromPair(random.choice(sent_pairs), input_lang, output_lang)
                      for i in range(n_iters)]
    criterion = nn.NLLLoss()

    for iter in range(1, n_iters + 1):
        training_pair = training_pairs[iter - 1]
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
            print('train accuracy: {}'.format(validateRandomSubset(encoder, decoder, input_lang, output_lang, sent_pairs, max_length, 100)))
            print('dev accuracy: {}'.format(validateRandomSubset(encoder, decoder, input_lang, output_lang, sent_pairs_dev, max_length, 100)))
            evaluateRandomly(encoder, decoder, input_lang, output_lang, sent_pairs, max_length, 3)
            print_loss_avg = print_loss_total / print_every
            print_loss_total = 0
            print('%s (%d %d%%) %.4f' % (timeSince(start, iter / n_iters),
                                         iter, iter / n_iters * 100, print_loss_avg))

        if iter % plot_every == 0:
            plot_loss_avg = plot_loss_total / plot_every
            plot_losses.append(plot_loss_avg)
            plot_loss_total = 0

def evaluate(encoder, decoder, input_lang, output_lang, sentence, max_length):
    input_variable = variableFromSentence(input_lang, sentence)
    input_length = input_variable.size()[0]
    encoder_hidden = encoder.initHidden()
    encoder_outputs = Variable(torch.zeros(max_length, encoder.hidden_size))
    encoder_outputs = encoder_outputs.cuda() if use_cuda else encoder_outputs
    for ei in range(input_length):
        encoder_output, encoder_hidden = encoder(input_variable[ei],
                                                 encoder_hidden)
        encoder_outputs[ei] = encoder_outputs[ei] + encoder_output[0][0]
    decoder_input = Variable(torch.LongTensor([[SOS_token]]))  # SOS
    decoder_input = decoder_input.cuda() if use_cuda else decoder_input
    decoder_hidden = encoder_hidden
    decoded_words = []
    decoder_attentions = torch.zeros(max_length, max_length)
    for di in range(max_length):
        decoder_output, decoder_hidden, decoder_attention = decoder(
            decoder_input, decoder_hidden, encoder_outputs)
        decoder_attentions[di] = decoder_attention.data
        topv, topi = decoder_output.data.topk(1)
        ni = topi[0][0]
        if ni == EOS_token:
            decoded_words.append('<EOS>')
            break
        else:
            decoded_words.append(output_lang.index2word[ni])
        decoder_input = Variable(torch.LongTensor([[ni]]))
        decoder_input = decoder_input.cuda() if use_cuda else decoder_input
    return decoded_words, decoder_attentions[:di + 1]


def compute_prob(encoder, decoder, input_lang, output_lang, sentence, desired_output, max_length):
    input_variable = variableFromSentence(input_lang, sentence)
    desired_variable = list(variableFromSentence(output_lang, desired_output).data.view(-1))
    input_length = input_variable.size()[0]
    encoder_hidden = encoder.initHidden()
    encoder_outputs = Variable(torch.zeros(max_length, encoder.hidden_size))
    encoder_outputs = encoder_outputs.cuda() if use_cuda else encoder_outputs
    for ei in range(input_length):
        encoder_output, encoder_hidden = encoder(input_variable[ei],
                                                 encoder_hidden)
        encoder_outputs[ei] = encoder_outputs[ei] + encoder_output[0][0]
    decoder_input = Variable(torch.LongTensor([[SOS_token]]))  # SOS
    decoder_input = decoder_input.cuda() if use_cuda else decoder_input
    decoder_hidden = encoder_hidden
    decoded_words = []
    decoder_attentions = torch.zeros(max_length, max_length)
    for di in range(len(desired_variable)):
        decoder_output, decoder_hidden, decoder_attention = decoder(
            decoder_input, decoder_hidden, encoder_outputs)
        decoder_attentions[di] = decoder_attention.data
        desired_token = desired_variable[di]
        (top5probs, top5) = decoder_output.data.topk(5)
        import math
        top5probs = [math.exp(x) for x in list(top5probs.view(-1))]
        top5 = list(top5.view(-1))
        prob_map = {k: v for (k,v) in zip(top5, top5probs)}
        #print(prob_map)
        topv, topi = decoder_output.data.topk(1)
        ni = topi[0][0]
        #print('prob of next prediction: {}'.format(prob_map[desired_token]))
        decoded_words.append(prob_map[desired_token])
        #print(desired_token)
        decoder_input = Variable(torch.LongTensor([[desired_token]]))
        decoder_input = decoder_input.cuda() if use_cuda else decoder_input
    return decoded_words



class NeuralChunker:
    def __init__(self, encoder_file, decoder_file, input_lang, output_lang, max_length):
        self.encoder = torch.load(encoder_file)
        self.decoder = torch.load(decoder_file)
        self.input_lang = input_lang
        self.output_lang = output_lang
        self.max_length = max_length
        
    def chunk(self, input_sent):
        return evaluate(self.encoder, self.decoder, self.input_lang, self.output_lang, input_sent, self.max_length)[0]

def readableToken(tok):
    if '__' in tok:
        tok.rfind('__')
        return tok[2 + tok.rfind('__'):]
    else:
        return tok

def readableSentenceTokens(sent):
    return [readableToken(tok) for tok in sent.split()]

def tokensToSplit(sent):
    toks = readableSentenceTokens(sent)
    open_tok = toks.index('[[[')
    close_tok = toks.index(']]]')
    return toks[open_tok+1:close_tok]

def compileSplit(sent, split_str, probs):
    split_markers = list(split_str.split()) + ['E']
    zipped = [(x, y, z) for (x, (y, z)) in zip(tokensToSplit(sent), zip(split_markers, probs))]
    return zipped



def visualizeSplit(split):
    result = ''
    for (tok, split_marker, prob) in split:
        result += ' {}'.format(tok)
        if split_marker == 'X' and prob >= 0.5:
            result += ' |'
        elif split_marker == 'X':
            result += ' !|!__{:.1f}%'.format(prob * 100)
        elif split_marker == 'O' and prob >= 0.5:
            result += '  '
        elif split_marker == 'O':
            result += ' ?|?__{:.1f}%'.format(prob * 100)
        else:
            result += '  '
    return result.strip()

def visualizeSplitWithSent(sent, split):
    print('--')
    print(' '.join(readableSentenceTokens(sent)))
    print(visualizeSplit(split))
    

    
    
def lookForWrong(chunker, sent_pairs, not_that_wrong_thres = 0.1, num_to_eval=100):
    correct = 0
    not_that_wrong = 0
    pretty_wrong = 0
    for pair in sent_pairs[:num_to_eval]:
        try:
            probs = compute_prob(chunker.encoder, chunker.decoder, chunker.input_lang, chunker.output_lang, pair[0], pair[1], chunker.max_length)        
            if min(probs) < 0.5:                
                if min(probs) > not_that_wrong_thres:
                    not_that_wrong += 1
                else:
                    pretty_wrong += 1    
                print(min(probs))
            else:
                correct += 1
            if min(probs) < 0.5:
                visualizeSplitWithSent(pair[0], compileSplit(pair[0], pair[1], probs))
        except KeyError:
            pass
    total = float(correct + not_that_wrong + pretty_wrong)
    print('correct: {}'.format(100 * correct/total))
    print('not that wrong (> {}%): {}'.format(100 * not_that_wrong_thres, 100 * not_that_wrong/total))
    print('pretty wrong: {}'.format(100 * pretty_wrong/total))
    

def evaluateSents(encoder, decoder, input_lang, output_lang, sent_pairs, max_length, num_to_eval=100):
    num_sents = 0
    prev_tokens = set()
    num_correct = 0
    still_correct = True
    for pair in sent_pairs[:num_to_eval]:
        output_words, attentions = evaluate(encoder, decoder, input_lang, output_lang, pair[0], max_length)        
        output_sentence = ' '.join(output_words[:-1])
        if pair[1] != output_sentence:
            still_correct += False
        tokens = set([tok for tok in pair[0].split() if '_' in tok])
        if len(tokens & prev_tokens) == 0:
            if still_correct:
                num_correct += 1
            num_sents += 1
            prev_tokens = tokens
            still_correct = True
    return float(num_correct)/num_sents

def validate(encoder, decoder, input_lang, output_lang, sent_pairs, max_length, num_to_eval=100):
    num_correct = 0
    for pair in sent_pairs[:num_to_eval]:
        try:
            output_words, attentions = evaluate(encoder, decoder, input_lang, output_lang, pair[0], max_length)        
            output_sentence = ' '.join(output_words[:-1])
            if pair[1] == output_sentence:
                num_correct += 1
        except KeyError:
            pass
            #print('token not found')
    accuracy = float(num_correct)/num_to_eval
    return accuracy

def validateRandomSubset(encoder, decoder, input_lang, output_lang, sent_pairs, max_length, num_to_eval=100):
    num_correct = 0
    for i in range(num_to_eval):
        pair = random.choice(sent_pairs)
        try:
            output_words, attentions = evaluate(encoder, decoder, input_lang, output_lang, pair[0], max_length)        
            output_sentence = ' '.join(output_words[:-1])
            if pair[1] == output_sentence:
                num_correct += 1
        except KeyError:
            pass
            #print('token not found')
    accuracy = float(num_correct)/num_to_eval
    return accuracy

def evaluateRandomly(encoder, decoder, input_lang, output_lang, sent_pairs, max_length, n=10):
    for i in range(n):
        pair = random.choice(sent_pairs)
        print('>', pair[0])
        print('=', pair[1])
        output_words, attentions = evaluate(encoder, decoder, input_lang, output_lang, pair[0], max_length)
        output_sentence = ' '.join(output_words[:-1])
        print('<', output_sentence)
        print('')