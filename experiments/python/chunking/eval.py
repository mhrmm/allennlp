import sys
import torch
import h5py
import codecs
import random
from chunking.data import variableFromSentence
from torch.autograd import Variable
from chunking.elmo import elmo_variable_from_sentence


__all__ = ['validate', 'evaluate', 'NeuralChunker']

SOS_token = 0
EOS_token = 1
use_cuda = torch.cuda.is_available()




def evaluate(encoder, decoder, output_lang, sentence, max_length):
    input_variable = elmo_variable_from_sentence(sentence)
    encoder_hidden = encoder.initHidden()
    encoder_outputs = Variable(torch.zeros(max_length, encoder.hidden_size))
    encoder_outputs = encoder_outputs.cuda() if use_cuda else encoder_outputs
    input_length = input_variable.size()[1]
    for ei in range(input_length):
        encoder_output, encoder_hidden = encoder(
            input_variable, ei, encoder_hidden)
        #encoder_outputs[ei] = encoder_output[0][0]
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


def compute_prob(encoder, decoder, output_lang, sentence, desired_output, max_length):
    input_variable = elmo_variable_from_sentence(sentence)
    encoder_hidden = encoder.initHidden()
    desired_variable = list(variableFromSentence(output_lang, desired_output).data.view(-1))
    encoder_outputs = Variable(torch.zeros(max_length, encoder.hidden_size))
    encoder_outputs = encoder_outputs.cuda() if use_cuda else encoder_outputs
    input_length = input_variable.size()[1]
    for ei in range(input_length):
        encoder_output, encoder_hidden = encoder(
            input_variable, ei, encoder_hidden)
        #encoder_outputs[ei] = encoder_output[0][0]
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
            probs = compute_prob(chunker.encoder, chunker.decoder, chunker.output_lang, pair[0], pair[1], chunker.max_length)        
            if min(probs) < 0.5:                
                if min(probs) > not_that_wrong_thres:
                    not_that_wrong += 1
                else:
                    pretty_wrong += 1   
            else:
                correct += 1
            if min(probs) < not_that_wrong_thres:
                visualizeSplitWithSent(pair[0], compileSplit(pair[0], pair[1], probs))
        except KeyError:
            pass
    total = float(correct + not_that_wrong + pretty_wrong)
    print('correct: {}'.format(100 * correct/total))
    print('not that wrong (> {}%): {}'.format(100 * not_that_wrong_thres, 100 * not_that_wrong/total))
    print('pretty wrong: {}'.format(100 * pretty_wrong/total))
    

def evaluateSents(encoder, decoder, output_lang, sent_pairs, max_length, num_to_eval=100):
    num_sents = 0
    prev_tokens = set()
    num_correct = 0
    still_correct = True
    for pair in sent_pairs[:num_to_eval]:
        output_words, attentions = evaluate(encoder, decoder, output_lang, pair[0], max_length)        
        output_sentence = ' '.join(output_words[:-1])
        if pair[1] != output_sentence:
            still_correct = False
        tokens = set([tok for tok in pair[0].split() if '_' in tok])
        if len(tokens & prev_tokens) == 0:
            if still_correct:
                num_correct += 1
            num_sents += 1
            prev_tokens = tokens
            still_correct = True
    return float(num_correct)/num_sents

def validate(encoder, decoder, output_lang, sent_pairs, max_length, num_to_eval=100):
    num_correct = 0
    import random
    random.shuffle(sent_pairs)
    for (index, pair) in enumerate(sent_pairs[:num_to_eval]):
        if (index+1) % 100 == 0:
            print('accuracy after {} instances: {}'.format(index+1, float(num_correct)/index))
        try:
            output_words, attentions = evaluate(encoder, decoder, output_lang, pair[0], max_length)        
            output_sentence = ' '.join(output_words[:-1])
            if pair[1] == output_sentence:
                num_correct += 1
        except KeyError:
            pass
            #print('token not found')
    accuracy = float(num_correct)/num_to_eval
    return accuracy

def validateRandomSubset(encoder, decoder, output_lang, sent_pairs, max_length, num_to_eval=100):
    num_correct = 0
    for i in range(num_to_eval):
        pair = random.choice(sent_pairs)
        try:
            output_words, attentions = evaluate(encoder, decoder, output_lang, pair[0], max_length)        
            output_sentence = ' '.join(output_words[:-1])
            if pair[1] == output_sentence:
                num_correct += 1
        except KeyError:
            pass
            #print('token not found')
    accuracy = float(num_correct)/num_to_eval
    return accuracy

def evaluateRandomly(encoder, decoder, output_lang, sent_pairs, max_length, n=10):
    for i in range(n):
        pair = random.choice(sent_pairs)
        print('>', pair[0])
        print('=', pair[1])
        output_words, attentions = evaluate(encoder, decoder, output_lang, pair[0], max_length)
        output_sentence = ' '.join(output_words[:-1])
        print('<', output_sentence)
        print('')