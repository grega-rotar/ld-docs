"""Fine-tune the digit CNN on cells cut from real scans, measured honestly by k-fold cross-validation.

    docker run --rm -v $PWD:/w -v digitdata:/tmp/digit-data -w /w ld-train \
      python -m reader.training.finetune_digits cells.npz [--folds 5]

MNIST is American handwriting; on these forms a 1 often has a long up-stroke and a 7 a bar, so a few
hundred real cells teach the model the local style. Each fold is fine-tuned without its own cells and
tested on them; the reported accuracy is over all folds. The final model is then fine-tuned on all cells
and written to reader/models/digits.local.onnx (git-ignored; the reader prefers it). The cells hold no names, but
they still come from real scans: keep the .npz out of the repo.
"""
import argparse

import numpy as np
import torch
import torch.nn.functional as F

from .train_digits import DATA, OUT, Net, augment, load

LOCAL = OUT.with_name("digits.local.onnx")  # learnt from real scans: git-ignored, never published

BASE = DATA / "digits.pt"


def finetune(base: dict, X: np.ndarray, Y: np.ndarray, mnist: tuple, steps: int = 600) -> Net:
    net = Net()
    net.load_state_dict(base)
    opt = torch.optim.AdamW(net.parameters(), lr=3e-4, weight_decay=1e-4)
    xr, yr = torch.tensor(X, dtype=torch.float32), torch.tensor(Y)
    xm, ym = mnist
    net.train()
    for _ in range(steps):
        i = torch.randint(0, len(yr), (96,))
        j = torch.randint(0, len(ym), (160,))  # keep seeing MNIST, so nothing it knew is forgotten
        x = augment(torch.cat([xr[i], xm[j]]))
        y = torch.cat([yr[i], ym[j]])
        loss = F.cross_entropy(net(x), y, label_smoothing=0.05)
        opt.zero_grad()
        loss.backward()
        opt.step()
    net.eval()
    return net


def predict(net: Net, X: np.ndarray) -> np.ndarray:
    with torch.no_grad():
        return net(torch.tensor(X, dtype=torch.float32)[:, None]).argmax(1).numpy()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cells")
    ap.add_argument("--folds", type=int, default=5)
    a = ap.parse_args()
    d = np.load(a.cells)
    X, Y, docs = d["X"], d["Y"], d["meta"]
    base = torch.load(BASE)
    mnist = load()
    before = predict(_net(base), X)
    print(f"base model on real cells: {(before == Y).mean():.4f} ({(before != Y).sum()} wrong of {len(Y)})")
    # folds by document, so a writer's other digits never leak into the test fold
    ids = np.array([m.split(":")[0] for m in docs])
    uniq = np.unique(ids)
    rng = np.random.default_rng(0)
    rng.shuffle(uniq)
    pred = np.zeros_like(Y)
    for k in range(a.folds):
        test = np.isin(ids, uniq[k :: a.folds])
        net = finetune(base, X[~test], Y[~test], mnist)
        pred[test] = predict(net, X[test])
        print(f"fold {k + 1}: {(pred[test] == Y[test]).mean():.4f} on {test.sum()} cells", flush=True)
    print(f"cross-validated accuracy after fine-tuning: {(pred == Y).mean():.4f} ({(pred != Y).sum()} wrong of {len(Y)})")
    for t, p in sorted({(int(t), int(p)) for t, p in zip(Y, pred) if t != p}):
        print(f"  {t} read as {p}: {int(((Y == t) & (pred == p)).sum())}x")
    net = finetune(base, X, Y, mnist)
    torch.onnx.export(net, torch.zeros(1, 1, 28, 28), LOCAL, input_names=["x"], output_names=["logits"],
                      dynamic_axes={"x": {0: "n"}, "logits": {0: "n"}}, dynamo=False)  # fmt: skip
    print("wrote", LOCAL)


def _net(state: dict) -> Net:
    net = Net()
    net.load_state_dict(state)
    net.eval()
    return net


if __name__ == "__main__":
    main()
