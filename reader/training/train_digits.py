"""Train the digit CNN (reader/models/digits.onnx) on MNIST + EMNIST digits, augmented to look like
pen digits cut out of the form's dotted cells.

    docker run --rm -v $PWD:/w -w /w ldr-train python -m reader.training.train_digits [--real cells.npz]

--real is an .npz of labelled cells cut from real scans (X: Nx28x28 in 0..1, Y: N) used only to report
accuracy on real handwriting; it is never trained on, so the number stays honest.
"""
import argparse
import time
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torchvision import datasets

OUT = Path(__file__).parent.parent / "models" / "digits.onnx"  # public data only: committed
DATA = Path("/tmp/digit-data")


class Net(nn.Module):
    def __init__(self):
        super().__init__()

        def block(i, o):
            return nn.Sequential(
                nn.Conv2d(i, o, 3, padding=1), nn.BatchNorm2d(o), nn.ReLU(),
                nn.Conv2d(o, o, 3, padding=1), nn.BatchNorm2d(o), nn.ReLU(), nn.MaxPool2d(2),
            )  # fmt: skip

        self.f = nn.Sequential(block(1, 32), block(32, 64), block(64, 128))
        self.head = nn.Sequential(nn.Flatten(), nn.Dropout(0.3), nn.Linear(128 * 3 * 3, 256), nn.ReLU(), nn.Dropout(0.3), nn.Linear(256, 10))

    def forward(self, x):
        return self.head(self.f(x))


def load() -> tuple[torch.Tensor, torch.Tensor]:
    xs, ys = [], []
    for train in (True, False):
        m = datasets.MNIST(DATA, train=train, download=True)
        xs.append(m.data.float() / 255)
        ys.append(m.targets)
    try:
        for train in (True, False):
            e = datasets.EMNIST(DATA, split="digits", train=train, download=True)
            xs.append(e.data.transpose(1, 2).float() / 255)  # EMNIST is stored transposed
            ys.append(e.targets)
    except Exception as ex:  # the EMNIST mirror is flaky; MNIST alone still trains a good model
        print("EMNIST not available:", ex)
    return torch.cat(xs), torch.cat(ys)


def augment(x: torch.Tensor) -> torch.Tensor:
    """Pen thickness, slant, size, position, and the leftovers of cutting a digit out of a scan."""
    n = x.shape[0]
    x = x[:, None]
    # thickness: thinner (min-pool) or thicker (max-pool), like a fine or a felt pen
    r = torch.rand(n)
    thick = F.max_pool2d(x, 3, 1, 1)
    thin = -F.max_pool2d(-x, 2, 1, 0)
    thin = F.pad(thin, (0, 1, 0, 1))
    x = torch.where((r < 0.25)[:, None, None, None], thick, torch.where((r > 0.8)[:, None, None, None], thin, x))
    # affine: rotation ±15°, shear, scale, shift
    ang = (torch.rand(n) - 0.5) * np.radians(30)
    sh = (torch.rand(n) - 0.5) * 0.5
    sc = 0.85 + torch.rand(n) * 0.3
    tx, ty = (torch.rand(n) - 0.5) * 0.2, (torch.rand(n) - 0.5) * 0.2
    cos, sin = torch.cos(ang) / sc, torch.sin(ang) / sc
    theta = torch.stack([torch.stack([cos, -sin + sh, tx], 1), torch.stack([sin, cos, ty], 1)], 1)
    grid = F.affine_grid(theta, x.shape, align_corners=False)
    x = F.grid_sample(x, grid, align_corners=False, padding_mode="zeros")
    # bits of a neighbouring digit or the cell border at the edge (15%)
    edge = torch.rand(n) < 0.15
    if edge.any():
        side = torch.randint(0, 4, (int(edge.sum()),))
        m = torch.zeros(int(edge.sum()), 1, 28, 28)
        for i, s in enumerate(side.tolist()):
            k = np.random.randint(1, 3)
            if s == 0:
                m[i, 0, :, :k] = 1
            elif s == 1:
                m[i, 0, :, -k:] = 1
            elif s == 2:
                m[i, 0, :k, :] = 1
            else:
                m[i, 0, -k:, :] = 1
        m = m * (torch.rand_like(m) < 0.6)
        x[edge] = torch.maximum(x[edge], m)
    return x.clamp(0, 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--epochs", type=int, default=8)
    ap.add_argument("--real")
    a = ap.parse_args()
    torch.set_num_threads(max(1, torch.get_num_threads()))
    X, Y = load()
    print("training digits:", len(Y))
    real = np.load(a.real) if a.real else None
    net = Net()
    opt = torch.optim.AdamW(net.parameters(), lr=2e-3, weight_decay=1e-4)
    steps = a.epochs * (len(Y) // 256)
    sched = torch.optim.lr_scheduler.OneCycleLR(opt, max_lr=3e-3, total_steps=steps)
    t0 = time.time()
    for ep in range(a.epochs):
        net.train()
        perm = torch.randperm(len(Y))
        for i in range(0, len(Y) - 255, 256):
            idx = perm[i : i + 256]
            loss = F.cross_entropy(net(augment(X[idx])), Y[idx], label_smoothing=0.05)
            opt.zero_grad()
            loss.backward()
            opt.step()
            sched.step()
        msg = f"epoch {ep + 1} loss {loss.item():.3f} {time.time() - t0:.0f}s"
        if real is not None:
            net.eval()
            with torch.no_grad():
                p = net(torch.tensor(real["X"], dtype=torch.float32)[:, None]).argmax(1).numpy()
            msg += f" real-cells accuracy {(p == real['Y']).mean():.4f} ({(p != real['Y']).sum()} wrong of {len(p)})"
        print(msg, flush=True)
    net.eval()
    torch.save(net.state_dict(), DATA / "digits.pt")  # the start for finetune_digits.py
    OUT.parent.mkdir(exist_ok=True)
    torch.onnx.export(net, torch.zeros(1, 1, 28, 28), OUT, input_names=["x"], output_names=["logits"],
                      dynamic_axes={"x": {0: "n"}, "logits": {0: "n"}}, dynamo=False)  # fmt: skip
    print("wrote", OUT, OUT.stat().st_size, "bytes")


if __name__ == "__main__":
    main()
