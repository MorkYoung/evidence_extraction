import copy
import random
import torch.nn as nn
import argparse
import sys
from load_data import *
from load_CDR_data import load_CDR
from model import *
from utils import *
import torch.optim as optim
from sklearn.metrics import f1_score, classification_report
import numpy as np
import transformers
CUT_OFF = 500

def get_dataset(dataset):
    """ Generate the dataset based on what is requested. """
    generate_data = {'evidence-inference': load_data, 'CDR': load_CDR}.get(dataset)
    return generate_data()

def create_model(dataset, bert_backprop, ner_loss, hard_attention, ner_path):
    """
    Create a model with the given specifications and return it.

    @param dataset specifies what dataset to use so we know how big our output dim should be.
    @param bert_backprop determines if we are to backprop through BERT.
    @param ner_loss determines if we are to add NER loss to our outputs.
    @param hard_attention is whether or not to use hard attention for alternate loss.
    @return a model set up with the given specification.
    """
    assert(dataset in set(['evidence-inference', 'CDR']))
    output_dimensions = {'evidence-inference': 4, 'CDR': 2}.get(dataset)
    relation_model = BERTVergaPytorch(output_dimensions, bert_backprop = bert_backprop, ner_loss = ner_loss, hard_attention = hard_attention).cuda()
    ner_model = transformers.BertForTokenClassification.from_pretrained(ner_path).cuda()
    return ner_model, relation_model

def evaluate_model(model, criterion, test, epoch, batch_size):
    # evaluate on validation set
    test_outputs = []
    test_labels  = []
    test_loss = 0
    for batch_range in range(0, len(test), batch_size):
        data = test[batch_range: batch_range + batch_size]
        inputs, labels = extract_data(data)
        if len(labels) == 0: continue

        # run model through validation data
        with torch.no_grad():
            outputs, _ = model(inputs)

        loss = criterion(outputs, torch.tensor(labels).cuda())
        test_loss += loss.item()
        test_outputs.extend(outputs.cpu().numpy()) # or something like this
        test_labels.extend(labels)

    outputs = [np.argmax(x) for x in test_outputs]
    labels  = test_labels
    f1 = f1_score(labels, outputs, average = 'macro')
    if max(labels) > 1 or max(outputs) > 1:
        print(classification_report(labels, outputs))
        outputs = [1 if x != 3 else 0 for x in outputs]
        labels  = [1 if x != 3 else 0 for x in labels]
        bin_f1  = f1_score(labels, outputs, average = 'macro')
    else:
        bin_f1 = 0

    print(classification_report(labels, outputs))
    print("Epoch {}\nF1 score: {}\nBinary F1: {}\nLoss: {}\n".format(epoch, f1, bin_f1, test_loss))
    return f1

def train_model(ner_model, relation_model, df, parameters):
    """ Take a model and train it with the given data. """
    # get parameters of how to train model
    epochs     = parameters.epochs
    batch_size = parameters.batch_size
    balance_classes = parameters.balance_classes
    learning_rate   = parameters.lr
    ner_loss_weighting = parameters.ner_loss_weight
    assert(ner_loss_weighting <= 1.0 and ner_loss_weighting >= 0.0)

    # split data, set up our optimizers
    best_model = None
    max_f1_score = 0 # best f1 score seen thus far
    train, dev, test = split_data(df, parameters.percent_train)
    criterion = nn.CrossEntropyLoss()
    optimizer = optim.Adam(list(ner_model.parameters()) + list(relation_model.parameters()), lr = learning_rate)
    for epoch in range(epochs):
        # define losses to use later
        label_offset  = 0 
        training_loss = 0
 
        train_data, train_labels, ner_labels = extract_data(train, balance_classes == 'True')        

        # single epoch train
        for batch_range in range(0, len(train), batch_size):
            inputs = train_data[batch_range: batch_range + batch_size] 
            ner_batch_labels = PaddedSequence.autopad([lab[:CUT_OFF] for lab in ner_labels[batch_range: batch_range + batch_size]], batch_first=True, padding_value=3, device='cuda')
            text_inputs = [torch.tensor(input_['text'][:CUT_OFF]).cuda() for input_ in inputs]    
            padded_text = PaddedSequence.autopad(text_inputs, batch_first = True, padding_value=0, device='cuda')

            # zero the parameter gradients
            optimizer.zero_grad()

            # forward + backwards + optimize
            ner_loss, ner_scores, hidden_states = ner_model(padded_text.data,
                                                            attention_mask=padded_text.mask(on=1.0, off=0.0, dtype=torch.float, device=padded_text.data.device),
                                                            labels = ner_batch_labels.data)[:3]
            ipdb.set_trace()

            relation_outputs, _ = relation_model(inputs)
            labels = train_labels[label_offset: label_offset + len(outputs)]

            # TODO ner loss
            ner_loss = float('nan')
            relation_loss = criterion(outputs, torch.tensor(labels).cuda())
            loss = ner_loss_weighting * ner_loss + (1 - ner_loss_weighting) * relation_loss
            loss.backward()
            optimizer.step()

            # add loss
            training_loss += loss.item()

            # next labels
            label_offset += len(outputs)

        ### Print the losses and evaluate on the dev set ###
        print("Epoch {} Training Loss: {}\n".format(epoch, training_loss))
        f1_score = evaluate_model(model, criterion, dev, epoch, batch_size)

        # update our scores to find the best possible model
        best_model   = copy.deepcopy(model) if max_f1_score < f1_score else best_model
        max_f1_score = max(max_f1_score, f1_score)

    print("Final test run:\n")
    evaluate_model(best_model, criterion, test, epoch, batch_size)

def main(args=sys.argv[1:]):
    ### Use arg parser to read in data. ###
    parser = argparse.ArgumentParser(description="Parses input specifications of how to run model.")
    parser.add_argument("--dataset", dest='dataset', required=True, help="Which dataset to use.")
    parser.add_argument("--epochs", dest='epochs', type=int, required=True, help="How many epochs to run model for.")
    parser.add_argument("--learning_rate", dest='lr', type=float, required=True, help="What should the learning rate be?")
    parser.add_argument("--batch_size", dest='batch_size', type=int, required=True, help="What should the batch_size be?")
    parser.add_argument("--balance_classes", dest="balance_classes", default=False, help="Should we balance the classes for you?")
    parser.add_argument("--bert_backprop", dest="bert_backprop", default=False, help="Should we backprop through BERT?")
    parser.add_argument("--ner_loss", dest="ner_loss", type=str, default="NULL", help="Should we add NER loss to model (select 'joint' or 'alternate'")
    parser.add_argument("--hard_attention", dest="hard_attention", default=False, help="Should we use hard attention?")
    parser.add_argument("--percent_train", dest="percent_train", type=float, default=1, help="What percent if the training data should we use?")
    parser.add_argument("--ner_loss_weight", dest="ner_loss_weight", type=float, required=True, help="Relative loss weight for NER task (b/w 0 and 1)")
    args = parser.parse_args()
    print("Running with the given arguments:\n\n{}".format(args))

    ### GET THE DATA ###
    df = get_dataset(args.dataset)

    ### LOAD THE MODEL ###
    ner_model, relation_model = create_model(dataset=args.dataset,
                                             bert_backprop=args.bert_backprop == 'True',
                                             ner_loss=args.ner_loss,
                                             hard_attention = args.hard_attention == 'True',
                                             ner_path=NER_BERT_LOCATION)

    ### TRAIN ###
    train_model(ner_model, relation_model, df, args)

if __name__ == '__main__':
    main()
