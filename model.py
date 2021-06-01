import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
import numpy as np

import math
from dataloader import dataloader, NUM_SEQUENCE, UNKNOWN, PAD, START, NUM_CHEMICAL_LAYERS, color_to_id, id_to_color
from fsa import ExecutionFSA, EOS, ACTION_SEP, NO_ARG

from alchemy_fsa import AlchemyFSA
from alchemy_world_state import AlchemyWorldState
from torch.nn.utils.rnn import pad_packed_sequence, pack_padded_sequence
import os
import pandas as pd


device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
os.environ["CUDA_VISIBLE_DEVICES"] = "2"

class world_state_encoder(nn.Module):
    def __init__(self, pos_embedded_dim=10, color_embedded_dim=10, num_layers=1, hidden_dim=10):
        super(world_state_encoder, self).__init__()
        self.pos_embedded_dim = pos_embedded_dim
        self.color_embedded_dim = color_embedded_dim
        self.num_layers = num_layers
        self.hidden_dim = hidden_dim
        self.color_embedding = nn.Embedding(7, self.color_embedded_dim)
        self.pos_embedding = nn.Embedding(8, self.pos_embedded_dim)
        self.lstm = nn.LSTM(self.color_embedded_dim, self.hidden_dim, num_layers=self.num_layers, batch_first=True)
    
    def forward(self, X):
        batch_size = X.shape[0]
        beaker_id = torch.tensor([[1, 2, 3, 4, 5, 6, 7]], dtype=torch.long)
        beaker_id = beaker_id.repeat((batch_size, 1)).to(device)
        # print(beaker_id)
        # print(beaker_id.shape)
        beaker_id = self.pos_embedding(beaker_id)
        # print(beaker_id.shape)
        world_state = None
        all_colors = None
        for i in range(batch_size):
            colors = None
            for j in range(0, X.shape[1], 5):
                max_len = len([int(t) for t in X[i][j+1:j+5] if int(t) != 0])
                if max_len == 0: max_len = 1
                processed_color = self.color_embedding(X[i][j+1:j+5][:max_len]).reshape((1, max_len, -1))
                _, (encoded_color, _) = self.lstm(processed_color) # 1 x 1 x hidden
                # print(encoded_color)
                # print(encoded_color.shape)
                colors = encoded_color if j == 0 else torch.cat((colors, encoded_color), dim=1)
            # print(colors.shape)
            # exit()
            all_colors = colors if i == 0 else torch.cat((all_colors, colors), dim=0)
        # print(all_colors.shape)
        # exit()
        all_colors = all_colors.to(device)
        context = torch.cat((beaker_id, all_colors), dim=2)
        # print(context.shape)
        # exit()
        # context = all_colors
        # context = torch.reshape(context, (batch_size, 1, -1))
        # print(context.shape)
        # exit()
        # print(context.shape)
        return context

class instruction_encoder(nn.Module):
    def __init__(self, vocab_size, hidden_size=100, embedded_size=50, num_layers=1, bidirectional=True):
        super(instruction_encoder, self).__init__()
        self.embedding = nn.Embedding(vocab_size, embedded_size, padding_idx=0)
        self.lstm = nn.LSTM(embedded_size, hidden_size, num_layers=num_layers, batch_first=True, bidirectional=bidirectional)

    def forward(self, X, valid_length=None, is_predict=False):
        if is_predict:
            X = self.embedding(X)
            output, (_, _) = self.lstm(X)
            return output
        X = self.embedding(X)
        X = pack_padded_sequence(X, valid_length, batch_first=True, enforce_sorted=False)
        packed_output, (_, _) = self.lstm(X)
        output, input_sizes = pad_packed_sequence(packed_output, batch_first=True)
        return output

class attention_action_decoder(nn.Module):
    def __init__(self, action_size, input_size, ins_hidden_size, hidden_size, embedding_size, env_dim, num_layers=1):
        super(attention_action_decoder, self).__init__()
        self.embedding = nn.Embedding(action_size, embedding_size, padding_idx=0)
        self.lstm = nn.LSTMCell(input_size, hidden_size)
        self.hidden_to_action = nn.Linear(hidden_size, action_size)
        self.hidden_size = hidden_size
        nn.init.xavier_uniform_(self.hidden_to_action.weight)
        # init W for h_i, W, h^q
        # self.W_c = nn.Parameter(nn.init.xavier_uniform_(torch.zeros(2 * ins_hidden_size, hidden_size)))
        # self.W_p = nn.Parameter(nn.init.xavier_uniform_(torch.zeros(2 * ins_hidden_size, 2 * ins_hidden_size + hidden_size)))
        # self.W_s_b_1 = nn.Parameter(nn.init.xavier_uniform_(torch.zeros(env_dim, hidden_size + 2 * ins_hidden_size)))
        # self.W_s_b_2 = nn.Parameter(nn.init.xavier_uniform_(torch.zeros(env_dim, hidden_size + 2 * ins_hidden_size)))
        # self.W_s_c_1 = nn.Parameter(nn.init.xavier_uniform_(torch.zeros(env_dim, hidden_size + 2 * ins_hidden_size)))
        # self.W_s_c_2 = nn.Parameter(nn.init.xavier_uniform_(torch.zeros(env_dim, hidden_size + 2 * ins_hidden_size)))

        # weight matrix for lstm input
        # W_d_dim = 4 * env_dim + 4 * ins_hidden_size + embedding_size
        W_d_dim = 2 * ins_hidden_size + env_dim + embedding_size

        self.W_b_d = nn.Linear(W_d_dim, input_size)
        nn.init.xavier_uniform_(self.W_b_d.weight)

        # W_project = 2 * ins_hidden_size + env_dim
        W_project = 2 * ins_hidden_size
        # self.project = torch.randn((W_project, hidden_size),requires_grad=True)
        self.project = nn.Linear(W_project, hidden_size, bias=False)

        W_env_project = env_dim
        self.env_project = nn.Linear(W_env_project, hidden_size, bias=False)
        # self.env_project = torch.randn((W_env_project, hidden_size),requires_grad=True)
        nn.init.xavier_uniform_(self.env_project.weight)

    def attend(self, H, query, weight):
        """
        H: batch_size x sen_length x 2*ins_hidden
        weight: 2 * ins_hidden x hidden
        query:  batch_size x hidden
        """
        H_ = weight(H)
        alpha_bar = torch.einsum('ijk, ik -> ij', [H_, query])
        alpha = nn.Softmax(1)(alpha_bar)
        c = torch.einsum('ijk, ij -> ik', [H, alpha])
        return c

    def init_hidden(self, batch_size):
        h_zeros = torch.zeros(batch_size, self.hidden_size, device = device)
        c_zeros = torch.zeros(batch_size, self.hidden_size, device = device)
        return h_zeros, c_zeros

    def forward(self, ins, his, actions, current_env_context, ini_env_context, ins_valid, teacher_force=True):
        # lstm output: batch_size, seq_len, num_directions*hidden_size
        X = self.embedding(actions.to(device))
        batch_size = ins.shape[0]
        h, c = self.init_hidden(batch_size)
        act_len = X.shape[1]
        output = []
        m = nn.ReLU()
        for i in range(act_len):
            c_ins = self.attend(ins, h, self.project)
            c_env = self.attend(current_env_context, h, self.env_project)
            E_t = X.permute(1, 0, 2)[i]
            attended_X = torch.cat((c_ins, c_env, E_t), dim=1)
            attended_X = m(self.W_b_d(attended_X))
            h, c = self.lstm(attended_X, (h, c))
            output.append(h)
        output = torch.stack(output, dim=0)
        output = output.permute(1, 0, 2)
        output = self.hidden_to_action(output)
        if teacher_force:
            return output
        softmax = nn.Softmax(dim=0)
        return torch.argmax(softmax(output[0][0]))

def sequence_mask(X, valid_len, value=0):
    """Mask irrelevant entries in sequences."""
    maxlen = X.size(1)
    mask = torch.arange((maxlen), dtype=torch.float32,
                        device=X.device)[None, :] < valid_len[:, None]
    X[~mask] = value
    return X

class MaskedSoftmaxCELoss(nn.CrossEntropyLoss):
    def forward(self, pred, label, valid_len):
        weights = torch.ones_like(label)
        weights = sequence_mask(weights, valid_len)
        self.reduction = 'none'
        unweighted_loss = super(MaskedSoftmaxCELoss,
                                self).forward(pred.permute(0, 2, 1), label)
        weighted_loss = (unweighted_loss * weights).mean(dim=1)
        return weighted_loss

class Seq2Seq(nn.Module):
    def __init__(self, vocab_size, action_size, ins_hidden_size, ins_embedding_size, act_embedding_size, act_input_size, act_hidden_size, pos_embedding_size, color_embedding_size, color_hidden_size):
        super(Seq2Seq, self).__init__()
        self.encoder = instruction_encoder(vocab_size, hidden_size=ins_hidden_size, embedded_size=ins_embedding_size, num_layers=1, bidirectional=True)
        self.env_encoder = world_state_encoder(pos_embedded_dim=pos_embedding_size, color_embedded_dim=color_embedding_size, hidden_dim=color_hidden_size)
        env_dim = (pos_embedding_size + color_hidden_size)
        self.decoder = attention_action_decoder(action_size, act_input_size, ins_hidden_size, act_hidden_size, act_embedding_size, env_dim)
    
    def train(self, dl, batch_size=32, epoch=10, learning_rate=0.01):
        loss_function = MaskedSoftmaxCELoss()
        optimizer = torch.optim.Adam(self.parameters(), lr=learning_rate)
        for i in range(epoch):
            avg_loss = 0
            batch_count = 0
            for step, batch in enumerate(dl):
                batch_count += 1
                batch = tuple(t.to(device) for t in batch)
                ins, his_ins, ins_valid, his_valid, ini_env, current_env, act_id, valid_act, y_true, y_true_valid = batch
                optimizer.zero_grad()
                ins_out = self.encoder(ins, ins_valid)
                his_out = self.encoder(his_ins, his_valid)
                ini_env_context = self.env_encoder(ini_env)
                current_env_context = self.env_encoder(current_env)
                pred = self.decoder(ins_out, his_out, act_id, current_env_context, ini_env_context, ins_valid, teacher_force=True)
                l = loss_function(pred, y_true.to(device), y_true_valid)
                l.sum().backward()
                if step % 20 == 0:
                    print("batch_loss:", l.sum().item() / batch_size)
                avg_loss = avg_loss + l.sum().item() / batch_size
                optimizer.step()
            print("loss at epoch " + str(i) + ":")
            print(avg_loss / batch_count)
    
    def clean_action(self, action, action_split=False):
        valid_action = []
        i = 0
        while i < len(action):
            if action_split == False:
                if action[i] == START or action[i] == PAD or action[i] == EOS:
                    i += 1
                else:
                    split_action = action[i].split(" ")
                    if split_action[0] == "pop":
                        split_action.append(NO_ARG)
                    valid_action.append(split_action)
                    i += 1
            else:
                """
                not implemented
                """
        return valid_action

    def clean_ws(self, ws):
        ws = ws.split(" ")
        cleaned_ws = ""
        for i in ws:
            cleaned_ws += i[:6]
            cleaned_ws += " "
        return cleaned_ws[:-1]

    def recover_ws(self, ws):
        str_ws = ""
        for i in range(0, len(ws), 5):
            str_ws += " "
            str_ws += str(ws[i] + 1)
            str_ws += ":"
            str_ws += id_to_color[ws[i+1]]

            if id_to_color[ws[i+2]] != "_":
                str_ws += id_to_color[ws[i+2]]
            else:
                continue
            
            if id_to_color[ws[i+3]] != "_":
                str_ws += id_to_color[ws[i+3]]
            else:
                continue
            
            if id_to_color[ws[i+4]] != "_":
                str_ws += id_to_color[ws[i+4]]
            else:
                continue


        return str_ws[1:]

    def predict(self, ins, his, ini_env, act_ix, ix_act, DL, max_act_len = 8):
        data_length = len(ins)

        # initial of the environment
        curr_env = ini_env[0]
        pred_act = START
        ws = self.recover_ws(ini_env[0][0].tolist())
        all_ws = []
        for index in range(data_length):
            act_sequence = []
            encoded_ins = self.encoder(ins[index], is_predict=True)
            encoded_his = self.encoder(his[index], is_predict=True)
            encoded_ini = self.env_encoder(ini_env[index])
            encoded_curr = encoded_ini if index % NUM_SEQUENCE == 0 else self.env_encoder(curr_env)
            if index % NUM_SEQUENCE == 0:
                ws = self.recover_ws(ini_env[index][0].tolist())
            pred_act = START
            pred_count = 0
            while pred_act != EOS:
                pred_act = torch.tensor([[act_ix[pred_act]]], dtype=torch.long)
                pred_act = ix_act[self.decoder(encoded_ins, encoded_his, pred_act, encoded_curr, encoded_ini, None, teacher_force=False).item()]
                act_sequence.append(pred_act)
                pred_count += 1
                if pred_count >= max_act_len:
                    break
            print("prediction:" + str(index) + " complete.")
            act_sequence = self.clean_action(act_sequence)
            print(act_sequence)
            ws = AlchemyWorldState(ws).execute_seq(act_sequence).__str__()
            print(ws)
            ws = self.clean_ws(ws)
            print(ws)
            all_ws.append(ws)
            curr_env = DL.process_raw_ws(ws)
        print("Prediction complete.")
        return all_ws


def save_output(ws, idf, ins_file, inter_file):
    ins_df = pd.DataFrame()
    inter_df = pd.DataFrame()
    ins = []
    ins_id = []
    inter = []
    inter_id = []

    for i in range(len(ws)):
        ins.append(ws[i])
        ins_id.append(idf[i])
        if i % NUM_SEQUENCE == (NUM_SEQUENCE-1):
            inter.append(ws[i])
            inter_id.append(idf[i][:-2])
    ins_df["id"] = ins_id
    ins_df["final_world_state"] = ins
    inter_df["id"] = inter_id
    inter_df["final_world_state"] = inter
    ins_df.to_csv(ins_file, index=False)
    inter_df.to_csv(inter_file, index=False)
    print("Results saved.")

def main():
    train = "train.json"
    dev = "dev.json"
    test = "test_leaderboard.json"
    batch_size = 32
    num_filter = 1
    DL = dataloader(train, dev, test, batch_size, num_filter)
    train_loader = DL.train_loader()

    vocab_size = len(DL.instruction_vocab)
    action_size = len(DL.action_vocab)

    # instruction encoder setting
    ins_hidden_size = 100
    ins_embedding_size = 50
    ins_num_lstm_layers = 1

    # world state encoder setting
    pos_embedding_size = 10
    color_embedding_size = 10
    color_hidden_size = 20

    # action setting
    act_embedding_size = 50
    act_input_size = 100
    act_hidden_size = 100

    model = Seq2Seq(vocab_size, action_size, ins_hidden_size, ins_embedding_size, act_embedding_size, act_input_size, act_hidden_size, pos_embedding_size, color_embedding_size, color_hidden_size)
    model.to(device)
    epoch = 8
    learning_rate = 0.001
    model.train(train_loader, batch_size, epoch, learning_rate)
    dev_ins, dev_his, dev_ini_env, dev_id = DL.dev_data()
    result = model.predict(dev_ins, dev_his, dev_ini_env, DL.actions_to_id, DL.id_to_actions, DL, max_act_len=8)
    result_ins_file = "dev_instruction_pred_18.csv"
    result_inter_file = "dev_inter_pred_18.csv"
    save_output(result, dev_id, result_ins_file, result_inter_file)


if __name__ == "__main__":
    main()