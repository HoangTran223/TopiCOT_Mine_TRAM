import numpy as np
import torch
from torch import nn
import torch.nn.functional as F
from .ECR import ECR
from .XGR import XGR
import logging
import torch_kmeans


class XTM(nn.Module):
    def __init__(self, vocab_size, num_topics=50, num_groups=10, en_units=200,
                 dropout=0., pretrained_WE=None, embed_size=200, beta_temp=0.2,
                 weight_loss_XGR=250.0, weight_loss_ECR=250.0,
                 alpha_ECR=20.0, alpha_XGR=4.0, sinkhorn_max_iter=1000):
        super().__init__()

        self.cnt = 0

        self.num_topics = num_topics
        self.num_groups = num_groups
        self.beta_temp = beta_temp

        self.a = 1 * np.ones((1, num_topics)).astype(np.float32)
        self.mu2 = nn.Parameter(torch.as_tensor(
            (np.log(self.a).T - np.mean(np.log(self.a), 1)).T))
        self.var2 = nn.Parameter(torch.as_tensor(
            (((1.0 / self.a) * (1 - (2.0 / num_topics))).T + (1.0 / (num_topics * num_topics)) * np.sum(1.0 / self.a, 1)).T))

        self.mu2.requires_grad = False
        self.var2.requires_grad = False

        self.fc11 = nn.Linear(vocab_size, en_units)
        self.fc12 = nn.Linear(en_units, en_units)
        self.fc21 = nn.Linear(en_units, num_topics)
        self.fc22 = nn.Linear(en_units, num_topics)
        self.fc1_dropout = nn.Dropout(dropout)
        self.theta_dropout = nn.Dropout(dropout)

        self.mean_bn = nn.BatchNorm1d(num_topics)
        self.mean_bn.weight.requires_grad = False
        self.logvar_bn = nn.BatchNorm1d(num_topics)
        self.logvar_bn.weight.requires_grad = False
        self.decoder_bn = nn.BatchNorm1d(vocab_size, affine=True)
        self.decoder_bn.weight.requires_grad = False

        if pretrained_WE is not None:
            self.word_embeddings = torch.from_numpy(pretrained_WE).float()
        else:
            self.word_embeddings = nn.init.trunc_normal_(
                torch.empty(vocab_size, embed_size))
        self.word_embeddings = nn.Parameter(
            F.normalize(self.word_embeddings)).to()

        self.topic_embeddings = torch.empty(
            (num_topics, self.word_embeddings.shape[1]))
        nn.init.trunc_normal_(self.topic_embeddings, std=0.1)
        self.topic_embeddings = nn.Parameter(
            F.normalize(self.topic_embeddings)).to(self.word_embeddings.device)

        # assert (num_topics % num_groups == 0,
        #         'num_topics should be divisible by num_groups')
        self.num_topics_per_group = num_topics // num_groups

        self.ECR = ECR(weight_loss_ECR, alpha_ECR, sinkhorn_max_iter)
        self.XGR = XGR(weight_loss_XGR, alpha_XGR, sinkhorn_max_iter)
        self.group_connection_regularizer = None
        # self.group_connection_regularizer = torch.ones(
        #     (self.num_topics_per_group, self.num_topics_per_group))
        # for _ in range(num_groups-1):
        #     self.group_connection_regularizer = torch.block_diag(self.group_connection_regularizer, torch.ones(
        #         (self.num_topics_per_group, self.num_topics_per_group)))
        # self.group_connection_regularizer.fill_diagonal_(0)
        # self.group_connection_regularizer /= self.group_connection_regularizer.sum()
        
        self.logger = logging.getLogger('main')

    def create_group_connection_regularizer(self):
        kmean_model = torch_kmeans.KMeans(
            n_clusters=self.num_groups, max_iter=1000, seed=0, verbose=False,
            normalize='unit')
        group_id = kmean_model.fit_predict(self.topic_embeddings.reshape(
            1, self.topic_embeddings.shape[0], self.topic_embeddings.shape[1]))
        group_id = group_id.reshape(-1)
        self.group_topic = [[] for _ in range(self.num_groups)]
        for i in range(self.num_topics):
            self.group_topic[group_id[i]].append(i)

        self.group_connection_regularizer = torch.ones(
            (self.num_topics, self.num_topics), device=self.topic_embeddings.device) / 5.
        for i in range(self.num_topics):
            for j in range(self.num_topics):
                if group_id[i] == group_id[j]:
                    self.group_connection_regularizer[i][j] = 1
        self.group_connection_regularizer.fill_diagonal_(0)
        self.group_connection_regularizer = self.group_connection_regularizer.clamp(min=1e-4)
        self.group_connection_regularizer = self.group_connection_regularizer / \
            self.group_connection_regularizer.sum()

        logger = logging.getLogger('main')
        logger.info('groups:')
        for i in range(self.num_groups):
            logger.info(f'group {i}: {self.group_topic[i]}')
        logger.info('group_connection_reg')
        logger.info(group_id)

    def get_beta(self):
        dist = self.pairwise_euclidean_distance(
            self.topic_embeddings, self.word_embeddings)
        beta = F.softmax(-dist / self.beta_temp, dim=0)
        return beta

    def reparameterize(self, mu, logvar):
        if self.training:
            std = torch.exp(0.5 * logvar)
            eps = torch.randn_like(std)
            return mu + (eps * std)
        else:
            return mu

    def encode(self, input):
        e1 = F.softplus(self.fc11(input))
        e1 = F.softplus(self.fc12(e1))
        e1 = self.fc1_dropout(e1)
        mu = self.mean_bn(self.fc21(e1))
        logvar = self.logvar_bn(self.fc22(e1))
        z = self.reparameterize(mu, logvar)
        theta = F.softmax(z, dim=1)

        loss_KL = self.compute_loss_KL(mu, logvar)

        return theta, loss_KL

    def get_theta(self, input):
        theta, loss_KL = self.encode(input)
        if self.training:
            return theta, loss_KL
        else:
            return theta

    def compute_loss_KL(self, mu, logvar):
        var = logvar.exp()
        var_division = var / self.var2
        diff = mu - self.mu2
        diff_term = diff * diff / self.var2
        logvar_division = self.var2.log() - logvar
        # KLD: N*K
        KLD = 0.5 * ((var_division + diff_term +
                     logvar_division).sum(axis=1) - self.num_topics)
        KLD = KLD.mean()
        return KLD

    def get_loss_ECR(self):
        cost = self.pairwise_euclidean_distance(
            self.topic_embeddings, self.word_embeddings)
        loss_ECR = self.ECR(cost)
        return loss_ECR

    def get_loss_XGR(self):
        cost = self.pairwise_euclidean_distance(
            self.topic_embeddings, self.topic_embeddings) + \
            1e2 * torch.eye(self.num_topics,
                            device=self.topic_embeddings.device)
        loss_XGR = self.XGR(cost, self.group_connection_regularizer)
        return loss_XGR

    def pairwise_euclidean_distance(self, x, y):
        cost = torch.sum(x ** 2, axis=1, keepdim=True) + \
            torch.sum(y ** 2, dim=1) - 2 * torch.matmul(x, y.t())
        return cost

    def forward(self, input, epoch_id=None):
        input = input['data']
        theta, loss_KL = self.encode(input)
        beta = self.get_beta()

        recon = F.softmax(self.decoder_bn(torch.matmul(theta, beta)), dim=-1)
        recon_loss = -(input * recon.log()).sum(axis=1).mean()

        loss_TM = recon_loss + loss_KL

        loss_ECR = self.get_loss_ECR()
        if epoch_id == 10 and self.group_connection_regularizer is None:
            self.create_group_connection_regularizer()
        if self.group_connection_regularizer is not None and epoch_id > 10:
            loss_XGR = self.get_loss_XGR()
            self.cnt += 1
            if self.cnt == 100:
                self.logger.info(f'xgr transp:')
                self.logger.info(self.XGR.transp)
                self.cnt = 0
        else:
            loss_XGR = 0.
        loss = loss_TM + loss_ECR + loss_XGR

        rst_dict = {
            'loss': loss,
            'loss_TM': loss_TM,
            'loss_ECR': loss_ECR,
            'loss_XGR': loss_XGR
        }

        return rst_dict
