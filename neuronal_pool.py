import os
import time
import random
import numpy as np
from numpy.random import choice
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torch.utils.data import DataLoader, TensorDataset
from tqdm import tqdm
from models import CNNnet, MLP, ResNet18, ResNet10, VGG11, CNNAvgPool
import pickle
from skimage.measure import block_reduce
from load_data_addon import Bandit_multi

# Model
class Network_exploitation(nn.Module):
    def __init__(self, dev, dim, hidden_size=100, k=10):
        super(Network_exploitation, self).__init__()
        self.fc1 = nn.Linear(dim, hidden_size)
        self.activate = nn.ReLU()
        self.fc2 = nn.Linear(hidden_size, k)

        self.device = f'cuda:{dev}'
        os.environ['CUDA_VISIBLE_DEVICES'] = dev
        self.cuda()

    def forward(self, x):
        return self.fc2(self.activate(self.fc1(x)))
    
    
class Network_exploration(nn.Module):
    def __init__(self, dev, dim, hidden_size=100, k=10):
        super(Network_exploration, self).__init__()
        self.fc1 = nn.Linear(dim, hidden_size)
        self.activate = nn.ReLU()
        self.fc2 = nn.Linear(hidden_size, k)

        self.device = f'cuda:{dev}'
        os.environ['CUDA_VISIBLE_DEVICES'] = dev
        self.cuda()

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

def EE_forward(net1, net2, x, dataset):

    x.requires_grad = True
    f1 = net1(x)
    net1.zero_grad()
    f1.sum().backward(retain_graph=True)
    dc = torch.cat([p.grad.flatten().detach() for p in net1.parameters()])
    dc = block_reduce(dc.cpu(), block_size=51, func=np.mean)
    dc = torch.from_numpy(dc).to(x.device)
    f2 = net2(dc)
    return f1, f2, dc

def train_NN_batch(model, X, Y, dataset, dc, num_epochs=64, lr=0.0005, batch_size=256, num_batch=4):
    model.train()
    X = torch.cat(X).float()
    Y = torch.stack(Y).float().detach()
    
    optimizer = optim.Adam(model.parameters(), lr=lr)
    num = X.size(1)

    for _ in range(num_batch):
        index = np.arange(len(X))
        np.random.shuffle(index)
        index = index[:batch_size]
        for _ in range(num_epochs):
            batch_loss = 0.0
            for i in index:
                x, y = X[i].to(device), Y[i].to(device)
                y = torch.reshape(y, (1,-1))
                pred = model(x)

                optimizer.zero_grad()
                loss = torch.mean((pred - y) ** 2)
                loss.backward()
                optimizer.step()

                batch_loss += loss.item()
            
            if batch_loss / num <= 1e-3:
                return batch_loss / num

    return batch_loss / num

# Training/Testing script

def run(dev, n=10000, margin=6, budget=0.05, num_epochs=10, dataset_name="covertype", explore_size=0, begin=0, lr=0.0001, j=0, mu=1000, gamma=1000):
    data = Bandit_multi(dataset_name)
    X = data.X
    Y = data.y

    X = np.array(X)[:n]
    if len(X.shape) == 3:
        N, h, w = X.shape
        X = np.reshape(X, (N, h*w))[:n]
    Y = np.array(Y)
    Y = (Y.astype(np.int64) - begin)[:n]

    train_dataset = TensorDataset(torch.tensor(X.astype(np.float32)), torch.tensor(Y.astype(np.int64)))

    test_x = data.X[n:]
    test_y = data.y[n:]
    test_y = np.array(test_y)
    test_y = (test_y.astype(np.int64) - begin)
    
    test_dataset = TensorDataset(torch.tensor(test_x.astype(np.float32)), torch.tensor(test_y.astype(np.int64)))

    k = len(set(Y))
    net1 = Network_exploitation(dev, X.shape[1], k=k).to(device)
    net2 = Network_exploration(dev, explore_size, k=k).to(device)

    X1_train, X2_train, y1, y2 = [], [], [], []
    budget = int(n * budget)
    inf_time = 0
    train_time = 0
    test_inf_time = 0
    R = 1000
    batch_size = 100
    
    X1_train, X2_train, y1, y2 = [], [], [], []
    queried_rows = []
    j = 0
    net1 = Network_exploitation(dev, X.shape[1], k=k).to(device)
    net2 = Network_exploration(dev, explore_size, k=k).to(device)

    total_time = 0
    while j < R:
        weights = []
        indices = []
        for i in tqdm(range(n)):
            if i in queried_rows:
                continue
            # load data point
            try:
                x, y = train_dataset[i]
            except:
                break
            x = x.view(1, -1).to(device)

            # predict via NeurONAL
            temp = time.time()
            f1, f2, dc = EE_forward(net1, net2, x, dataset_name)
            inf_time = inf_time + time.time() - temp
            u = f1[0] + 1 / (i+1) * f2
            u_sort, u_ind = torch.sort(u)
            i_hat = u_sort[-1]
            i_deg = u_sort[-2]
            neuronal_pred = int(u_ind[-1].item())

            # calculate weight
            weight = abs(i_hat - i_deg).item()
            weights.append(weight)
            indices.append(i)
        

        # create the distribution and sample b points from it
        i_hat = np.argmin(weights)
        w_hat = weights[i_hat]
        distribution = []
        temp = time.time()
        for x in range(len(weights)):
            if x != i_hat:
                quotient = (mu * w_hat + gamma * (weights[x] - w_hat))
                distribution.append((w_hat / quotient))
            else:
                distribution.append(0)
        distribution[i_hat] = max(1 - sum(distribution), 0)

        total = sum(distribution)
        distribution = [w/total for w in distribution]
        inf_time = time.time() - temp

        # sample from distribution
        ind = np.random.choice(indices, size=batch_size, replace=False, p=distribution)

        for i in ind:
            x, y = train_dataset[i]
            x = x.view(1, -1).to(device)

            temp = time.time()
            f1, f2, dc = EE_forward(net1, net2, x, dataset_name)
            inf_time = inf_time + time.time() - temp
            u = f1[0] + 1 / (i+1) * f2
            u_sort, u_ind = torch.sort(u)
            i_hat = u_sort[-1]
            i_deg = u_sort[-2]
            neuronal_pred = int(u_ind[-1].item())
            
            # add predicted rewards to the sets
            X1_train.append(x)
            X2_train.append(torch.reshape(dc, (1, len(dc))))
            r_1 = torch.zeros(k).to(device)
            r_1[y.item()] = 1
            y1.append(r_1) 
            y2.append((r_1 - f1)[0])

            # update unlabeled set
            queried_rows.append(i)

        # update the model
        temp = time.time()
        train_NN_batch(net1, X1_train, y1, dataset_name, dc=False, lr=lr)
        train_NN_batch(net2, X2_train, y2, dataset_name, dc=True, lr=lr)
        train_time = train_time + time.time() - temp

        # calculate testing regret   
        current_acc = 0
        for i in tqdm(range(n)):
            # load data point
            try:
                x, y = test_dataset[i]
            except:
                break
            x = x.view(1, -1).to(device)

            # predict via NeurONAL
            temp = time.time()
            f1, f2, dc = EE_forward(net1, net2, x, dataset_name)
            inf_time = inf_time + time.time() - temp
            u = f1[0] + 1 / (i+1) * f2
            u_sort, u_ind = torch.sort(u)
            i_hat = u_sort[-1]
            i_deg = u_sort[-2]
            neuronal_pred = int(u_ind[-1].item())

            lbl = y.item()
            if neuronal_pred == lbl:
                current_acc += 1
        
        testing_acc = current_acc / n
        j += batch_size
        
        print(f'testing acc after {j} queries: {testing_acc}')
        f = open(f"results_np/{dataset_name}/NeurONAL_pool_res.txt", 'a')
        f.write(f'testing acc after {j} queries: {testing_acc}\n')
        f.close()
        
    # Calculating the STD for testing acc
    for _ in range(10):
        test_ind = np.arange(len(test_dataset))
        np.random.shuffle(test_ind)
        test_ind = test_ind[:n]
        current_acc = 0
        for i in tqdm(test_ind):
            # load data point
            try:
                x, y = test_dataset[i]
            except:
                break
            x = x.view(1, -1).to(device)

            # predict via NeurONAL
            temp = time.time()
            f1, f2, dc = EE_forward(net1, net2, x, dataset_name)
            test_inf_time = test_inf_time + time.time() - temp
            u = f1[0] + 1 / (i+1) * f2
            u_sort, u_ind = torch.sort(u)
            i_hat = u_sort[-1]
            i_deg = u_sort[-2]
            neuronal_pred = int(u_ind[-1].item())

            lbl = y.item()
            if neuronal_pred == lbl:
                current_acc += 1
        
        testing_acc = current_acc / len(test_ind)
        
        print(f'testing acc after {j} queries: {testing_acc}')
        f = open(f"results/{dataset_name}/NeurONAL_pool_res.txt", 'a')
        f.write(f'testing acc after {j} queries: {testing_acc}\n')
        f.close()

    with open(f'times/neuronal.txt', 'a+') as f:
        f.write(f'NeurONAL on {dataset_name} took: {inf_time} + {train_time} = {inf_time + train_time}\n')
    return inf_time, train_time, test_inf_time


device = 'cuda'
random.seed(42)
np.random.seed(42)
torch.manual_seed(42)
