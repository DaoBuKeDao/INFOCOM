# -*- coding: utf-8 -*-

import argparse
import os
import sys
import time
import numpy as np

import torch
import torch.distributed.deprecated as dist
from cjltest.divide_data import partition_dataset, select_dataset
from cjltest.models import MnistCNN, AlexNetForCIFAR
from cjltest.utils_data import get_data_transform
from cjltest.utils_model import MySGD, test_model
from torch.autograd import Variable
from torch.multiprocessing import Process as TorchProcess
from torch.utils.data import DataLoader
from torchvision import datasets, models, transforms
import ResNetOnCifar10
import mldatasets

parser = argparse.ArgumentParser()
# 集群信息
parser.add_argument('--ps-ip', type=str, default='127.0.0.1')
parser.add_argument('--ps-port', type=str, default='29500')
parser.add_argument('--this-rank', type=int, default=1)
parser.add_argument('--workers-num', type=int, default=2)

# 模型与数据集
parser.add_argument('--data-dir', type=str, default='~/dataset')
parser.add_argument('--data-name', type=str, default='cifar10')
parser.add_argument('--model', type=str, default='MnistCNN')
parser.add_argument('--save-path', type=str, default='./')

# 参数信息
parser.add_argument('--epochs', type=int, default=100)
parser.add_argument('--lr', type=float, default=0.1)
parser.add_argument('--train-bsz', type=int, default=200)
parser.add_argument('--ratio', type=float, default=0.001)
parser.add_argument('--isCompensate', type=bool, default=False)
parser.add_argument('--loops', type=int, default=75)
args = parser.parse_args()

# select top-k gradient changes
def get_upload(g_remain, g_new, ratio, isCompensate):
    for idx, g_layer in enumerate(g_new):
        g_remain[idx] += g_layer

    g_remain_abs_vector = torch.empty(0)
    g_remain_abs = []
    for idx, g_layer in enumerate(g_remain):
        g_remain_layer_abs = torch.abs(g_remain[idx])
        g_remain_abs.append(g_remain_layer_abs)
        g_remain_layer_abs_reshape = g_remain_layer_abs.reshape(torch.numel(g_remain_layer_abs))
        g_remain_abs_vector = torch.cat((g_remain_abs_vector, g_remain_layer_abs_reshape),dim=0)  # merge two vectors into one vector

    param_num = torch.numel(g_remain_abs_vector)
    k = int(param_num * ratio)
    k = k if k>0 else 1
    top_k = torch.topk(g_remain_abs_vector, k)
    threshold = top_k[0][k-1].item()

    g_upload = []
    for idx, g_layer in enumerate(g_remain_abs):
        mask = g_layer >= threshold
        g_upload_layer = torch.zeros_like(g_layer)
        g_upload_layer[mask] += g_remain[idx][mask]
        g_remain[idx][mask] = 0.
        g_upload.append(g_upload_layer)

    return g_remain, g_upload



# noinspection PyTypeChecker
def run(rank, workers, model, save_path, train_data, test_data):
    # 获取ps端传来的模型初始参数
    _group = [w for w in workers].append(0)
    group = dist.new_group(_group)

    param_num = 0
    for p in model.parameters():
        tmp_p = torch.zeros_like(p)
        param_num += torch.numel(tmp_p)
        dist.scatter(tensor=tmp_p, src=0, group=group)
        p.data = tmp_p
    print('Model recved successfully!')

    compression_num = int(param_num * args.ratio)
    compression_num = compression_num if compression_num > 0 else 1
    dist.gather(torch.tensor([compression_num/param_num]), dst=0, group=group)

    if args.model in ['MnistCNN', 'AlexNet', 'ResNet18OnCifar10']:
        learning_rate = 0.1
    else:
        learning_rate = args.lr
    optimizer = MySGD(model.parameters(), lr=learning_rate)

    if args.model in ['MnistCNN', 'AlexNet']:
        criterion = torch.nn.NLLLoss()
    elif args.model in ['Abalone', 'Bodyfat', 'Housing']:
        criterion = torch.nn.MSELoss()
    else:
        criterion = torch.nn.CrossEntropyLoss()


    if args.model in ['AlexNet', 'ResNet18OnCifar10']:
        decay_period = 50
    elif args.model in ['LROnMnist', 'LROnCifar10', 'LROnCifar100','Abalone', 'Bodyfat', 'Housing']:
        decay_period = 1000000  # learning rate is constant for LR (convex) models
    else:
        decay_period = 100

    print('Begin!')

    global_clock = 0
    g_remain = [torch.zeros_like(param.data) for param in model.parameters()]
    time_logs = open("./record" + str(rank), 'w')
    for epoch in range(args.epochs):
        batch_interval = 0.0
        batch_comp_interval = 0.0
        batch_comm_interval = 0.0
        s_time = time.time()
        model.train()

        # AlexNet在指定epoch减少学习率LR
        #if args.model == 'AlexNet':
        if (epoch+1) % decay_period == 0:
            for param_group in optimizer.param_groups:
                param_group['lr'] *= 0.1
                print('LR Decreased! Now: {}'.format(param_group['lr']))

        epoch_train_loss = 0
        for batch_idx, (data, target) in enumerate(train_data):
            batch_start_time = time.time()
            data, target = Variable(data), Variable(target)
            optimizer.zero_grad()
            output = model(data)
            loss = criterion(output, target)
            loss.backward()
            delta_ws = optimizer.get_delta_w()

            g_remain, g_large_change = get_upload(g_remain,delta_ws,args.ratio,args.isCompensate)

            batch_comp_time = time.time()
            # 同步操作
            # send epoch train loss firstly
            dist.gather(loss.data, dst = 0, group = group)
            for idx, param in enumerate(model.parameters()):
                dist.gather(tensor=g_large_change[idx], dst=0, group=group)
                recv = torch.zeros_like(delta_ws[idx])
                dist.scatter(tensor=recv, src=0, group=group)
                param.data = recv

            epoch_train_loss += loss.data.item()
            batch_end_time = time.time()

            batch_interval += batch_end_time - batch_start_time
            batch_comp_interval += batch_comp_time - batch_start_time
            batch_comm_interval += batch_end_time - batch_comp_time

            logs = torch.tensor([0.0, batch_interval/(batch_idx+1), batch_comp_interval/(batch_idx+1), batch_comm_interval/(batch_idx+1)])
            time_logs.write(str(logs) + '\n')
            time_logs.flush()

        print('Rank {}, Epoch {}, Loss:{}'
             .format(rank, epoch, loss.data.item()))

        e_time = time.time()
        #epoch_train_loss /= len(train_data)
        #epoch_train_loss = format(epoch_train_loss, '.4f')
        # 训练结束后进行test
        #test_loss, acc = test_model(rank, model, test_data, criterion=criterion)
        acc = 0.0
        batch_interval /= batch_idx+1
        batch_comp_interval /= batch_idx+1
        batch_comm_interval /= batch_idx+1
        logs = torch.tensor([acc, batch_interval, batch_comp_interval, batch_comm_interval])
        time_logs.write(str(logs) + '\n')
        time_logs.flush()
        #dist.gather(tensor=logs, dst = 0, group = group)
    time_logs.close()




def init_processes(rank, size, workers,
                   model, save_path,
                   train_dataset, test_dataset,
                   fn, backend='tcp'):
    os.environ['MASTER_ADDR'] = args.ps_ip
    os.environ['MASTER_PORT'] = args.ps_port
    dist.init_process_group(backend, rank=rank, world_size=size)
    fn(rank, workers, model, save_path, train_dataset, test_dataset)


if __name__ == '__main__':

    workers = [v+1 for v in range(args.workers_num)]

    if args.model == 'MnistCNN':
        model = MnistCNN()

        train_transform, test_transform = get_data_transform('mnist')

        train_dataset = datasets.MNIST(args.data_dir, train=True, download=False,
                                       transform=train_transform)
        test_dataset = datasets.MNIST(args.data_dir, train=False, download=False,
                                      transform=test_transform)
    elif args.model == 'LROnMnist':
        model = ResNetOnCifar10.LROnMnist()
        train_transform, test_transform = get_data_transform('mnist')

        train_dataset = datasets.MNIST(args.data_dir, train=True, download=False,
                                       transform=train_transform)
        test_dataset = datasets.MNIST(args.data_dir, train=False, download=False,
                                      transform=test_transform)
    elif args.model == 'LROnCifar10':
        model = ResNetOnCifar10.LROnCifar10()
        train_transform, test_transform = get_data_transform('cifar')

        train_dataset = datasets.CIFAR10(args.data_dir, train=True, download=False,
                                       transform=train_transform)
        test_dataset = datasets.CIFAR10(args.data_dir, train=False, download=False,
                                      transform=test_transform)
    elif args.model == 'LROnCifar100':
        model = ResNetOnCifar10.LROnCifar100()
        train_transform, test_transform = get_data_transform('cifar')
        train_dataset = datasets.CIFAR100(args.data_dir, train=True, download=True,
                                          transform=train_transform)
        test_dataset = datasets.CIFAR100(args.data_dir, train=False, download=True,
                                         transform=test_transform)
    elif args.model == 'AlexNet':

        train_transform, test_transform = get_data_transform('cifar')

        if args.data_name == 'cifar10':
            model = AlexNetForCIFAR()
            train_dataset = datasets.CIFAR10(args.data_dir, train=True, download=False,
                                             transform=train_transform)
            test_dataset = datasets.CIFAR10(args.data_dir, train=False, download=False,
                                            transform=test_transform)
        else:
            model = AlexNetForCIFAR(num_classes=100)
            train_dataset = datasets.CIFAR100(args.data_dir, train=True, download=False,
                                              transform=train_transform)
            test_dataset = datasets.CIFAR100(args.data_dir, train=False, download=False,
                                             transform=test_transform)
    elif args.model == 'ResNet18OnCifar10':
        model = ResNetOnCifar10.ResNet18()

        train_transform, test_transform = get_data_transform('cifar')
        train_dataset = datasets.CIFAR10(args.data_dir, train=True, download=False,
                                         transform=train_transform)
        test_dataset = datasets.CIFAR10(args.data_dir, train=False, download=False,
                                        transform=test_transform)
    elif args.model == 'ResNet34':
        model = models.resnet34(pretrained=False)

        train_transform = transforms.Compose([
            transforms.ToTensor(),
            transforms.Normalize(mean=[0.485, 0.456, 0.406],
                                 std=[0.229, 0.224, 0.225])
        ])
        test_transform = train_transform
        train_dataset = datasets.ImageFolder(args.data_dir, train=True, download=False,
                                         transform=train_transform)
        test_dataset = datasets.ImageFolder(args.data_dir, train=False, download=False,
                                        transform=test_transform)
    elif args.model == 'Abalone':
        model = ResNetOnCifar10.abalone_model()
        train_dataset = mldatasets.abalone(args.data_dir, True)
        test_dataset = mldatasets.abalone(args.data_dir, False)
    elif args.model == "Bodyfat":
        model = ResNetOnCifar10.bodyfat_model()
        train_dataset = mldatasets.bodyfat(args.data_dir, True)
        test_dataset = mldatasets.bodyfat(args.data_dir, False)
    elif args.model == 'Housing':
        model = ResNetOnCifar10.housing_model()
        train_dataset = mldatasets.housing(args.data_dir, True)
        test_dataset = mldatasets.housing(args.data_dir, False)
    else:
        print('Model must be {} or {}!'.format('MnistCNN', 'AlexNet'))
        sys.exit(-1)

    train_bsz = args.train_bsz
    test_bsz = 400

    train_bsz /= len(workers)
    train_bsz = int(train_bsz)

    train_data = partition_dataset(train_dataset, workers)
    test_data = partition_dataset(test_dataset, workers)

    this_rank = args.this_rank
    train_data = select_dataset(workers, this_rank, train_data, batch_size=train_bsz)
    test_data = select_dataset(workers, this_rank, test_data, batch_size=test_bsz)

    # 用所有的测试数据测试
    #test_data = DataLoader(test_dataset, batch_size=test_bsz, shuffle=True)

    world_size = len(workers) + 1

    save_path = str(args.save_path)
    save_path = save_path.rstrip('/')

    p = TorchProcess(target=init_processes, args=(this_rank, world_size, workers,
                                                  model, save_path,
                                                  train_data, test_data,
                                                  run))
    p.start()
    p.join()
