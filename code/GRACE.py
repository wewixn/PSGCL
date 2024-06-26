import torch
import os.path as osp
import GCL.losses as L
import GCL.augmentors as A
import torch.nn.functional as F
import torch_geometric.transforms as T

from tqdm import tqdm
from torch.optim import Adam
from GCL.eval import get_split
from Evaluator import LREvaluator
from GCL.models import DualBranchContrast
from torch_geometric.nn import GCNConv
from torch_geometric.datasets import Planetoid, Amazon, WikiCS


class GConv(torch.nn.Module):
    def __init__(self, input_dim, hidden_dim, activation, num_layers):
        super(GConv, self).__init__()
        self.activation = activation()
        self.layers = torch.nn.ModuleList()
        self.layers.append(GCNConv(input_dim, hidden_dim, cached=False))
        for _ in range(num_layers - 1):
            self.layers.append(GCNConv(hidden_dim, hidden_dim, cached=False))

    def forward(self, x, edge_index, edge_weight=None):
        z = x
        for i, conv in enumerate(self.layers):
            z = conv(z, edge_index, edge_weight)
            z = self.activation(z)
        return z


class Encoder(torch.nn.Module):
    def __init__(self, encoder, augmentor, hidden_dim, proj_dim):
        super(Encoder, self).__init__()
        self.encoder = encoder
        self.augmentor = augmentor

        self.fc1 = torch.nn.Linear(hidden_dim, proj_dim)
        self.fc2 = torch.nn.Linear(proj_dim, hidden_dim)

    def forward(self, x, edge_index, edge_weight=None):
        aug1, aug2 = self.augmentor
        x1, edge_index1, edge_weight1 = aug1(x, edge_index, edge_weight)
        x2, edge_index2, edge_weight2 = aug2(x, edge_index, edge_weight)
        z = self.encoder(x, edge_index, edge_weight)
        z1 = self.encoder(x1, edge_index1, edge_weight1)
        z2 = self.encoder(x2, edge_index2, edge_weight2)
        return z, z1, z2

    def project(self, z: torch.Tensor) -> torch.Tensor:
        z = F.elu(self.fc1(z))
        return self.fc2(z)


def train(encoder_model, contrast_model, data, optimizer):
    encoder_model.module.train()
    optimizer.zero_grad()
    z, z1, z2 = encoder_model.module(data.x, data.edge_index, data.edge_attr)
    h1, h2 = [encoder_model.module.project(x) for x in [z1, z2]]
    loss = contrast_model.module(h1, h2)
    loss.backward()
    optimizer.step()
    return loss.item()


def test(encoder_model, data):
    encoder_model.module.eval()
    z, _, _ = encoder_model.module(data.x, data.edge_index, data.edge_attr)
    split = get_split(num_samples=z.size()[0], train_ratio=0.1, test_ratio=0.8)
    result = LREvaluator()(z, data.y, split)
    return result


def main():
    device = torch.device('cuda')
    path = osp.join(osp.expanduser('.'), 'datasets', 'Planetoid')
    dataset = Planetoid(path, name='pubmed', transform=T.NormalizeFeatures())
    data = dataset[0].to(device)

    aug1 = A.Compose([A.EdgeRemoving(pe=0.3), A.FeatureMasking(pf=0.3)])
    aug2 = A.Compose([A.EdgeRemoving(pe=0.3), A.FeatureMasking(pf=0.3)])

    gconv = GConv(input_dim=dataset.num_features, hidden_dim=32, activation=torch.nn.ReLU, num_layers=2).to(device)
    encoder_model = Encoder(encoder=gconv, augmentor=(aug1, aug2), hidden_dim=32, proj_dim=32).to(device)
    contrast_model = DualBranchContrast(loss=L.InfoNCE(tau=0.2), mode='L2L', intraview_negs=True).to(device)
    encoder_model = torch.nn.DataParallel(encoder_model)
    contrast_model = torch.nn.DataParallel(contrast_model)
    optimizer = Adam(encoder_model.module.parameters(), lr=0.01)

    with tqdm(total=1000, desc='(T)') as pbar:
        for epoch in range(1, 1001):
            loss = train(encoder_model, contrast_model, data, optimizer)
            pbar.set_postfix({'loss': loss})
            pbar.update()

    test_result = test(encoder_model, data)
    print(f'(E): Best test F1Mi={test_result["micro_f1"]:.4f}, F1Ma={test_result["macro_f1"]:.4f}')
    print(f'test acc = {test_result["test_acc_mean"]:.4f} +/- {test_result["test_acc_std"]:.4f}, '
          f'test F1Ma = {test_result["test_macro_mean"]:.4f} +/- {test_result["test_macro_std"]:.4f}')
    return test_result


if __name__ == '__main__':
    num_runs = 5
    results = []
    for _ in range(num_runs):
        results.append(main())
    for i in range(num_runs):
        print(f'F1Mi={results[i]["micro_f1"]:.4f}, F1Ma={results[i]["macro_f1"]:.4f}')
    print(f'Average test F1Mi={sum([r["micro_f1"] for r in results]) / num_runs:.4f}, '
          f'F1Ma={sum([r["macro_f1"] for r in results]) / num_runs:.4f}')
