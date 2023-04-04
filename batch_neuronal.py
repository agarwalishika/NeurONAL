import os
import time
import math
import random
import numpy as np
from bisect import bisect

import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torch.utils.data import DataLoader, TensorDataset

from utils import get_data
from load_data import load_mnist_1d
from skimage.measure import block_reduce
from load_data_addon import Bandit_multi

class Network_exploitation(nn.Module):
    def __init__(self, dim, hidden_size=100, k=10):
        super(Network_exploitation, self).__init__()
        self.fc1 = nn.Linear(dim, hidden_size)
        self.activate = nn.ReLU()
        self.fc2 = nn.Linear(hidden_size, k)

    def forward(self, x):
        return self.fc2(self.activate(self.fc1(x)))
    
    
class Network_exploration(nn.Module):
    def __init__(self, dim, hidden_size=100, k=10):
        super(Network_exploration, self).__init__()
        self.fc1 = nn.Linear(dim, hidden_size)
        self.activate = nn.ReLU()
        self.fc2 = nn.Linear(hidden_size, k)

    def forward(self, x):
        return self.fc2(self.activate(self.fc1(x)))

class MLP(nn.Module):
    def __init__(self, input_dim, hidden_size=100, k=10):
        super(MLP, self).__init__()
        self.fc1 = nn.Linear(input_dim, hidden_size)
        self.activate = nn.ReLU()
        self.fc2 = nn.Linear(hidden_size, k)

    def forward(self, x):
        return torch.sigmoid(self.fc2(self.activate(self.fc1(x))))

def EE_forward(net1, net2, x):

    x.requires_grad = True
    f1 = net1(x)
    net1.zero_grad()
    f1.sum().backward(retain_graph=True)
    dc = torch.cat([p.grad.flatten().detach() for p in net1.parameters()])
    #dc = dc / torch.linalg.norm(dc)
    dc = block_reduce(dc.cpu(), block_size=51, func=np.mean)
    dc = torch.from_numpy(dc).to(x.device)
    f2 = net2(dc)
    return f1, f2, dc

def train_NN_batch(model, X, Y, num_epochs=10, lr=0.0001, batch_size=64):
    model.train()
    X = torch.cat(X).float()
    Y = torch.stack(Y).float().detach()
    optimizer = optim.Adam(model.parameters(), lr=lr)
    dataset = TensorDataset(X, Y)
    dataloader = DataLoader(dataset, batch_size=batch_size, shuffle=True)
    num = X.size(1)

    for i in range(num_epochs):
        batch_loss = 0.0
        for x, y in dataloader:
            x, y = x.to(device), y.to(device)
            y = torch.reshape(y, (1,-1))
            pred = model(x).view(-1)

            optimizer.zero_grad()
            loss = torch.mean((pred - y) ** 2)
            loss.backward()
            optimizer.step()

            batch_loss += loss.item()
        
        if batch_loss / num <= 1e-3:
            return batch_loss / num

    return batch_loss / num

def run(n=1000, margin=6, budget=0.05, num_epochs=10, dataset_name="covertype", explore_size=0, test=1, begin=0, lr=0.0001):
    data = Bandit_multi(dataset_name)
    X = data.X
    Y = data.y

    X = np.array(X)
    if len(X.shape) == 3:
        N, h, w = X.shape
        X = np.reshape(X, (N, h*w))
    Y = np.array(Y)
    Y = Y.astype(np.int64) - begin

    dataset = TensorDataset(torch.tensor(X.astype(np.float32)), torch.tensor(Y.astype(np.int64)))

    k = len(set(Y))
    hidden_size = 100 #default value
    regret = []
    net1 = Network_exploitation(X.shape[1], k=k).to(device)
    net2 = Network_exploration(explore_size, k=k).to(device)

    margin_model = MLP(X.shape[1], k=k).to(device)

    X1_train, X2_train, y1, y2 = [], [], [], []
    budget = int(n * budget)
    current_regret = 0.0
    query_num = 0
    ci = torch.zeros(1, X.shape[1]).to(device)
    inf_time = 0
    train_time = 0
    test_inf_time = 0

    train_batch_size = 20
    batch_size = 20
    batch_counter = 0
    x1_train_batch, x2_train_batch, y1_batch, y2_batch = [], [], [], []
    weights = []


    for i in range(n):
        # load data point
        try:
            x, y = dataset[i]
        except:
            break
        x = x.view(1, -1).to(device)

        # predict via NeurONAL
        temp = time.time()
        f1, f2, dc = EE_forward(net1, net2, x)
        inf_time = inf_time + time.time() - temp
        u = f1[0] + 1 / (i+1) * f2
        u_sort, u_ind = torch.sort(u)
        i_hat = u_sort[-1]
        i_deg = u_sort[-2]
        neuronal_pred = int(u_ind[-1].item())

        # predict via margin
        k_prob = margin_model(x)
        max_prob = k_prob.max().item()
        margin_pred = k_prob.argmax().item()

        ind = 0
        if abs(i_hat - i_deg) < margin * 0.1:
            ind = 1

        lbl = y.item()
        if neuronal_pred != lbl:
            current_regret += 1

        # construct training set
        if neuronal_pred != margin_pred:
            weight = abs(i_hat - i_deg).item()
            if batch_counter < train_batch_size:
                if batch_counter >= batch_size and weights[0] < weight:
                    # there is no space, we must remove the lowest weighted point
                    weights.pop(0)
                    x1_train_batch.pop(0)
                    x2_train_batch.pop(0)
                    y1_batch.pop(0)
                    y2_batch.pop(0)

                index = bisect(weights, weight)
                weights.insert(index, weight)
                x1_train_batch.insert(index, x)
                x2_train_batch.insert(index, torch.reshape(dc, (1, len(dc))))
                r_1 = torch.zeros(k).to(device)
                r_1[lbl] = 1
                y1_batch.insert(index, r_1) 
                y2_batch.insert(index, (r_1 - f1)[0])
                batch_counter += 1
        

        if batch_counter == train_batch_size and query_num < budget: 
            query_num += batch_size

            #add predicted rewards to the sets
            X1_train.extend(x1_train_batch)
            X2_train.extend(x2_train_batch)
            y1.extend(y1_batch) 
            y2.extend(y2_batch)

            temp = time.time()
            train_NN_batch(net1, X1_train, y1, num_epochs=num_epochs, lr=lr)
            train_NN_batch(net2, X2_train, y2, num_epochs=num_epochs, lr=lr)
            train_NN_batch(margin_model, X1_train, y1, num_epochs=num_epochs, lr=lr)
            train_time = train_time + time.time() - temp

            batch_counter = 0
            x1_train_batch, x2_train_batch, y1_batch, y2_batch = [], [], [], []
            weights = []
            print("trained batch")
            
        
        regret.append(current_regret)
        print(f'{i},{query_num},{budget},{num_epochs},{current_regret}')
        f = open(f"results/{dataset_name}/batch_neuronal_res.txt", 'a')
        f.write(f'{i},{query_num},{budget},{num_epochs},{current_regret}\n')
        f.close()

    if test > 0:
        
        print('-------TESTING-------')
        lim = min(n, len(dataset)-n)
        for _ in range(5):
            acc = 0
            for i in range(n, n+lim):
                ind = random.randint(n, len(dataset)-1)
                x, y = dataset[ind]
                x = x.view(1, -1).to(device)

                temp = time.time()
                f1, f2, dc = EE_forward(net1, net2, x)
                test_inf_time = test_inf_time + time.time() - temp
                u = f1[0] + 1 / (i+1) * f2
                u_sort, u_ind = torch.sort(u)
                i_hat = u_sort[-1]
                i_deg = u_sort[-2]
                    
                pred = int(u_ind[-1].item())
                lbl = y.item()
                if pred == lbl:
                    acc += 1
            print(f'Testing accuracy: {acc/lim}\n')
            f = open(f"results/{dataset_name}/batch_neuronal_res.txt", 'a')
            f.write(f'Testing accuracy: {acc/lim}\n')
            f.close()
            f = open('runtimes_batch_neuronal.txt', 'a')
            f.write(f'{test_inf_time}, ')
            f.close()
        
        f = open('runtimes_batch_neuronal.txt', 'a')
        f.write(f'\n')
        f.close()

    return inf_time, train_time, test_inf_time

device = 'cuda'
os.environ['CUDA_VISIBLE_DEVICES'] = '1'

random.seed(42)
np.random.seed(42)
torch.manual_seed(42)