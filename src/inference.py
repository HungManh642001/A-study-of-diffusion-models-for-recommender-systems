"""
Train a diffusion model for recommendation
"""

import argparse
from ast import parse
import os
import time
import numpy as np
import copy

import torch
import torch.nn as nn
import torch.optim as optim
import torch.utils.data as data
from torch.utils.data import DataLoader
import torch.backends.cudnn as cudnn
import torch.nn.functional as F
import scipy.sparse as sp

import models.gaussian_diffusion as gd
from models.Autoencoder import AutoEncoder as AE
from models.Autoencoder import compute_loss
from models.DNN import DNN, EmbedFC
import evaluate_utils
import data_utils
from copy import deepcopy
from sklearn import preprocessing
from tqdm import tqdm

import random
random_seed = 1
torch.manual_seed(random_seed) # cpu
torch.cuda.manual_seed(random_seed) #gpu
np.random.seed(random_seed) #numpy
random.seed(random_seed) #random and transforms
torch.backends.cudnn.deterministic=True # cudnn
def worker_init_fn(worker_id):
    np.random.seed(random_seed + worker_id)
def seed_worker(worker_id):
    worker_seed = torch.initial_seed() % 2**32
    np.random.seed(worker_seed)

parser = argparse.ArgumentParser()
parser.add_argument('--dataset', type=str, default='ml-1m_clean', help='choose the dataset')
parser.add_argument('--data_path', type=str, default='../datasets/ml-1m_clean/', help='load data path')
parser.add_argument('--emb_path', type=str, default='../datasets/nl-1m_clean/')
parser.add_argument('--batch_size', type=int, default=400)
parser.add_argument('--topN', type=str, default='[10, 20, 50, 100]')
parser.add_argument('--tst_w_val', action='store_true', help='test with validation')
parser.add_argument('--cuda', action='store_true', help='use CUDA')
parser.add_argument('--gpu', type=str, default='0', help='gpu card ID')
parser.add_argument('--log_name', type=str, default='log', help='the log name')
parser.add_argument('--n_cate', type=int, default=3, help='category num of items')

# params for diffusion
parser.add_argument('--mean_type', type=str, default='eps', help='MeanType for diffusion: x0, eps')
parser.add_argument('--steps', type=int, default=20, help='diffusion steps')
parser.add_argument('--noise_schedule', type=str, default='linear-var', help='the schedule for noise generating')
parser.add_argument('--noise_scale', type=float, default=0.1, help='noise scale for noise generating')
parser.add_argument('--noise_min', type=float, default=0.0001)
parser.add_argument('--noise_max', type=float, default=0.02)
parser.add_argument('--sampling_noise', type=bool, default=False, help='sampling with noise or not')
parser.add_argument('--sampling_steps', type=int, default=5, help='steps for sampling/denoising')
parser.add_argument('--classifier', type=str, default='age', help='conditional information')

args = parser.parse_args()

if args.dataset == 'amazon-book_clean':
    args.steps = 5
    args.noise_scale = 0.5
    args.noise_min = 0.001
    args.noise_max = 0.005
elif args.dataset == 'yelp_clean':
    args.steps = 5
    args.noise_scale = 0.01
    args.noise_min = 0.005
    args.noise_max = 0.01
elif args.dataset == 'ml-1m_clean':
    args.steps = 40
    args.noise_scale = 0.005
    args.noise_min = 0.005
    args.noise_max = 0.02
elif args.dataset == 'amazon-book_noisy':
    args.steps = 10
    args.noise_scale = 0.5
    args.noise_min = 0.001
    args.noise_max = 0.005
elif args.dataset == 'yelp_noisy':
    args.steps = 5
    args.noise_scale = 0.01
    args.noise_min = 0.005
    args.noise_max = 0.01
elif args.dataset == 'ml-1m_noisy':
    args.steps = 100
    args.noise_scale = 0.005
    args.noise_min = 0.005
    args.noise_max = 0.02
else:
    raise ValueError

print("args:", args)

os.environ["CUDA_VISIBLE_DEVICES"] = args.gpu
device = torch.device("cpu")

print("Starting time: ", time.strftime('%Y-%m-%d %H:%M:%S',time.localtime(time.time())))

### DATA LOAD ###
train_path = args.data_path + 'train_list.npy'
valid_path = args.data_path + 'valid_list.npy'
test_path = args.data_path + 'test_list.npy'
gender_path = args.data_path + 'gender.npy'
age_path = args.data_path + 'age.npy'

train_data, valid_y_data, test_y_data, n_user, n_item = data_utils.data_load(train_path, valid_path, test_path)

### GENDER
label_encoder = preprocessing.LabelEncoder()
gender = np.array(np.load(gender_path, allow_pickle=True))
gender = label_encoder.fit_transform(gender)

### AGE
label_encoder = preprocessing.LabelEncoder()
age = np.array(np.load(age_path, allow_pickle=True))
age = label_encoder.fit_transform(age)

### DATA LOADER

train_dataset = data_utils.Conditional_DataDiffusion(torch.FloatTensor(train_data.A), torch.IntTensor(gender), torch.IntTensor(age))

train_loader = DataLoader(train_dataset, batch_size=args.batch_size, pin_memory=True, shuffle=True, num_workers=4, worker_init_fn=worker_init_fn)
test_loader = DataLoader(train_dataset, batch_size=args.batch_size, shuffle=False)

if args.tst_w_val:
    tv_dataset = data_utils.Conditional_DataDiffusion(torch.FloatTensor(train_data.A) + torch.FloatTensor(valid_y_data.A), torch.IntTensor(gender), torch.IntTensor(age))
    test_twv_loader = DataLoader(tv_dataset, batch_size=args.batch_size, shuffle=False)
mask_tv = train_data + valid_y_data

print('data ready.')

### Build Gaussian Diffusion ###
if args.mean_type == 'x0':
    mean_type = gd.ModelMeanType.START_X
elif args.mean_type == 'eps':
    mean_type = gd.ModelMeanType.EPSILON
else:
    raise ValueError("Unimplemented mean type %s" % args.mean_type)

diffusion = gd.GaussianDiffusion(mean_type, args.noise_schedule, \
        args.noise_scale, args.noise_min, args.noise_max, args.steps, device).to(device)

### Build Autoencoder & MLP ###
model_path = "checkpoints/"

if args.dataset == "ml-1m_clean":
    model_name = "ml-1m_clean_0.0001lr1_0.0001lr2_0.0wd1_0.0wd2_bs400_cate3_in[300]_out[]_lam0.03_dims[300]_emb10_eps_steps20_scale0.1_min0.0001_max0.02_sample5_reweightTrue_log.pth"
    AE_name = "ml-1m_clean_0.0001lr1_0.0001lr2_0.0wd1_0.0wd2_bs400_cate3_in[300]_out[]_lam0.03_dims[300]_emb10_eps_steps20_scale0.1_min0.0001_max0.02_sample5_reweightTrue_log_AE.pth"

model = torch.load(model_path + model_name).to(device)
Autoencoder = torch.load(model_path + AE_name).to(device)

def evaluate(data_loader, data_te, mask_his, topN):
    model.eval()
    Autoencoder.eval()
    e_idxlist = list(range(mask_his.shape[0]))
    e_N = mask_his.shape[0]

    predict_items = []
    target_items = []
    for i in range(e_N):
        target_items.append(data_te[i, :].nonzero()[1].tolist())
    
    if args.n_cate > 1:
        category_map = Autoencoder.category_map.to(device)
    
    with torch.no_grad():
        p_bar = tqdm(data_loader)

        for batch_idx, (x, gender, age) in enumerate(p_bar):
            x = x.to(device)
            gender = gender.to(device)
            age = age.to(device)

            # mask map
            his_data = mask_his[e_idxlist[batch_idx*args.batch_size:batch_idx*args.batch_size+len(x)]]

            _, batch_latent, _ = Autoencoder.Encode(x)
            batch_latent_recon = diffusion.p_sample(model, batch_latent, gender, age, args.sampling_steps, args.sampling_noise)
            prediction = Autoencoder.Decode(batch_latent_recon)  # [batch_size, n1_items + n2_items + n3_items]

            prediction[his_data.nonzero()] = -np.inf  # mask ui pairs in train & validation set

            _, mapped_indices = torch.topk(prediction, topN[-1])  # topk category idx

            if args.n_cate > 1:
                indices = category_map[mapped_indices]
            else:
                indices = mapped_indices

            indices = indices.cpu().numpy().tolist()
            predict_items.extend(indices)

    test_results = evaluate_utils.computeTopNAccuracy(target_items, predict_items, topN)

    return test_results

if args.n_cate > 1:
    start_time = time.time()
    category_map = Autoencoder.category_map.cpu().numpy()
    reverse_map = {category_map[i]:i for i in range(len(category_map))}
    # mask for validation: train_dataset
    mask_idx_train = list(train_data.nonzero())
    mapped_mask_iid_train = np.array([reverse_map[mask_idx_train[1][i]] for i in range(len(mask_idx_train[0]))])
    mask_train = sp.csr_matrix((np.ones_like(mask_idx_train[0]), (mask_idx_train[0], mapped_mask_iid_train)), \
        dtype='float64', shape=(n_user, n_item))

    # mask for test: train_dataset + valid_dataset
    mask_idx_val = list(valid_y_data.nonzero())  # valid dataset
    mapped_mask_iid_val = np.array([reverse_map[mask_idx_val[1][i]] for i in range(len(mask_idx_val[0]))])
    mask_val = sp.csr_matrix((np.ones_like(mask_idx_val[0]), (mask_idx_val[0], mapped_mask_iid_val)), \
        dtype='float64', shape=(n_user, n_item))

    mask_tv = mask_train + mask_val

    print("Preparing mask for validation & test costs " + time.strftime(
                            "%H: %M: %S", time.gmtime(time.time()-start_time)))
else:
    mask_train = train_data

valid_results = evaluate(test_loader, valid_y_data, mask_train, eval(args.topN))
if args.tst_w_val:
    test_results = evaluate(test_twv_loader, test_y_data, mask_tv, eval(args.topN))
else:
    test_results = evaluate(test_loader, test_y_data, mask_tv, eval(args.topN))
evaluate_utils.print_results(None, valid_results, test_results)




