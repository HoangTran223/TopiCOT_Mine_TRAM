import torch
from torch.utils.data import DataLoader, Dataset
import numpy as np
import scipy.sparse
import scipy.io
from tqdm import tqdm
from sentence_transformers import SentenceTransformer
from . import file_utils
import os


def load_contextual_embed(texts, device, model_name="all-mpnet-base-v2", show_progress_bar=True):
    model = SentenceTransformer(model_name, device=device)
    embeddings = model.encode(texts, show_progress_bar=show_progress_bar)
    return embeddings


class DatasetHandler(Dataset):
    def __init__(self, data, contextual_embed=None):
        self.data = data
        self.contextual_embed = None
        if contextual_embed is not None:
            assert data.shape[0] == contextual_embed.shape[0], "Data and contextual embeddings should have the same number of samples"
            self.contextual_embed = contextual_embed

    def __len__(self):
        # Update this according to your data size
        return len(self.data)

    def __getitem__(self, idx):
        if torch.is_tensor(idx):
            idx = idx.tolist()

        if self.contextual_embed is None:
            return {
                'idx': idx,
                'data': self.data[idx]
            }

        return {
            'idx': idx,
            'data': self.data[idx],
            'contextual_embed': self.contextual_embed[idx]
        }

    def to(self, device):
        self.data = self.data.to(device)  
        if self.contextual_embed is not None:
            self.contextual_embed = self.contextual_embed.to(device)  


class RawDatasetHandler:
    def __init__(self, docs, preprocessing, batch_size=200, device='cuda', as_tensor=False, contextual_embed=False):

        rst = preprocessing.preprocess(docs)
        self.train_data = rst['train_bow']
        self.train_texts = rst['train_texts']
        self.vocab = rst['vocab']

        self.vocab_size = len(self.vocab)

        if contextual_embed:
            self.train_contextual_embed = load_contextual_embed(
                self.train_texts, device)
            self.contextual_embed_size = self.train_contextual_embed.shape[1]

        if as_tensor:
            if contextual_embed:
                self.train_data = np.concatenate(
                    (self.train_data, self.train_contextual_embed), axis=1)

            self.train_data = torch.from_numpy(
                self.train_data).float().to(device)
            self.train_dataloader = DataLoader(
                self.train_data, batch_size=batch_size, shuffle=True)


class BasicDatasetHandler:
    def __init__(self, dataset_dir, batch_size=200, read_labels=False, device='cuda', as_tensor=False, contextual_embed=False):
        # train_bow: NxV
        # test_bow: Nxv
        # word_emeddings: VxD
        # vocab: V, ordered by word id.

        self.load_data(dataset_dir, read_labels)
        self.vocab_size = len(self.vocab)

        print("===>train_size: ", self.train_bow.shape[0])
        print("===>test_size: ", self.test_bow.shape[0])
        print("===>vocab_size: ", self.vocab_size)
        print("===>average length: {:.3f}".format(
            self.train_bow.sum(1).sum() / self.train_bow.shape[0]))

        if contextual_embed:
            if os.path.isfile(os.path.join(dataset_dir, 'with_bert', 'train_bert.npz')):
                self.train_contextual_embed = np.load(os.path.join(
                    dataset_dir, 'with_bert', 'train_bert.npz'))['arr_0']
            else:
                self.train_contextual_embed = load_contextual_embed(
                    self.train_texts, device)

            if os.path.isfile(os.path.join(dataset_dir, 'with_bert', 'test_bert.npz')):
                self.test_contextual_embed = np.load(os.path.join(
                    dataset_dir, 'with_bert', 'test_bert.npz'))['arr_0']
            else:
                self.test_contextual_embed = load_contextual_embed(
                    self.test_texts, device)

            self.contextual_embed_size = self.train_contextual_embed.shape[1]
        print("1.0.1")
        if as_tensor:
            # if not contextual_embed:  # to be fixed with an additional argument
            #     self.train_data = self.train_bow
            #     self.test_data = self.test_bow
            # else:
            #     self.train_data = np.concatenate((self.train_bow, self.train_contextual_embed), axis=1)
            #     self.test_data = np.concatenate((self.test_bow, self.test_contextual_embed), axis=1)
            self.train_data = self.train_bow
            self.test_data = self.test_bow

            self.train_data = torch.from_numpy(self.train_data).to(device)
            self.test_data = torch.from_numpy(self.test_data).to(device)
            print("1.0.2")
            if contextual_embed:

                self.train_contextual_embed = torch.from_numpy(
                    self.train_contextual_embed).to(device)
                self.test_contextual_embed = torch.from_numpy(
                    self.test_contextual_embed).to(device)
                print("1.0.3")
                train_dataset = DatasetHandler(
                    self.train_data, self.train_contextual_embed)
                test_dataset = DatasetHandler(
                    self.test_data, self.test_contextual_embed)
                print("1.0.4")
                self.train_dataloader = DataLoader(
                    train_dataset, batch_size=batch_size, shuffle=True)
                self.test_dataloader = DataLoader(
                    test_dataset, batch_size=batch_size, shuffle=False)
                print("Done 1.")

            else:
                train_dataset = DatasetHandler(self.train_data)
                test_dataset = DatasetHandler(self.test_data)

                self.train_dataloader = DataLoader(
                    train_dataset, batch_size=batch_size, shuffle=True)
                self.test_dataloader = DataLoader(
                    test_dataset, batch_size=batch_size, shuffle=False)

    def load_data(self, path, read_labels):

        self.train_bow = scipy.sparse.load_npz(
            f'{path}/train_bow.npz').toarray().astype('float32')
        self.test_bow = scipy.sparse.load_npz(
            f'{path}/test_bow.npz').toarray().astype('float32')
        self.pretrained_WE = scipy.sparse.load_npz(
            f'{path}/word_embeddings.npz').toarray().astype('float32')

        self.train_texts = file_utils.read_text(f'{path}/train_texts.txt')
        self.test_texts = file_utils.read_text(f'{path}/test_texts.txt')

        if read_labels:
            self.train_labels = np.loadtxt(
                f'{path}/train_labels.txt', dtype=int)
            self.test_labels = np.loadtxt(f'{path}/test_labels.txt', dtype=int)

        self.vocab = file_utils.read_text(f'{path}/vocab.txt')


    def to(self, device):
        # Chuyển dữ liệu sang thiết bị mong muốn
        self.train_data = self.train_data.to(device)
        self.test_data = self.test_data.to(device)

        if hasattr(self, 'train_contextual_embed') and self.train_contextual_embed is not None:
            self.train_contextual_embed = self.train_contextual_embed.to(device)
        if hasattr(self, 'test_contextual_embed') and self.test_contextual_embed is not None:
            self.test_contextual_embed = self.test_contextual_embed.to(device)


        # Cập nhật lại dữ liệu trong DataLoader
        if hasattr(self, 'train_dataloader'):
            self.train_dataloader.dataset.data = self.train_data
            if self.train_contextual_embed is not None:
                self.train_dataloader.dataset.contextual_embed = self.train_contextual_embed

        if hasattr(self, 'test_dataloader'):
            self.test_dataloader.dataset.data = self.test_data
            if self.test_contextual_embed is not None:
                self.test_dataloader.dataset.contextual_embed = self.test_contextual_embed