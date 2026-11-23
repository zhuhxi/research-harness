#!/usr/bin/env python3
"""
样本拼图（由 harness 自动生成，AI 不用自己写画图代码）。

约定：训练脚本在验证集上预测后保存 预测_验证_<标签>.npz，内含
    pred : (N,) 预测类别
    prob : (N, C) 各类概率（可选；有它才能挑"错得最自信"的样本）
本工具读取它和 eval.评估.load_split() 的验证集，画一张拼图 样本拼图_<标签>.png：
    上排：错得最自信的 8 个样本（没有 prob 时为随机抽的错例）
    下排：随机 8 个样本
每格标 T: 真实类别 / P: 预测类别（错的用深色标题）。图内英文、Nimbus Sans、白底细线。

不是图像分类的课题：在 eval/评估.py 里定义 画拼图(pred, 验证集, 输出路径, 附加数据) 即可替换默认画法；
既没定义、验证输入也不是图像时，跳过并打印原因。

用法：python 拼图.py <课题目录> <预测文件.npz>
"""
import importlib
import sys
from pathlib import Path

import numpy as np

N_PER_ROW = 8


def _is_image(x):
    """验证输入是否像图像：N×H×W 或 N×H×W×C（C 为 1/3）。"""
    return x.ndim == 3 or (x.ndim == 4 and x.shape[-1] in (1, 3))


def _unflatten(x):
    """展平的图像向量自动还原（11-22 巡检：digits 的 64 维 = 8×8 被跳过）：d = H×W 或 H×W×3，且 H 为整数。"""
    if x.ndim != 2:
        return x
    d = x.shape[1]
    h = int(round(d ** 0.5))
    if h * h == d and h >= 8:
        return x.reshape(len(x), h, h)
    h3 = int(round((d / 3) ** 0.5))
    if d % 3 == 0 and h3 * h3 * 3 == d and h3 >= 8:
        return x.reshape(len(x), 3, h3, h3).transpose(0, 2, 3, 1)   # 常见存法：通道在前
    return x


def draw(root: Path, pred_file: Path) -> str:
    sys.path.insert(0, str(root))
    ev = importlib.import_module("eval.评估")
    data = np.load(pred_file, allow_pickle=False)
    pred = np.asarray(data["pred"])
    tag = pred_file.stem.replace("预测_验证_", "")
    out = pred_file.parent / f"样本拼图_{tag}.png"
    (_, _), (xva, yva), _ = ev.load_split()

    # 课题自定义画法优先
    if hasattr(ev, "画拼图"):
        ev.画拼图(pred, (xva, yva), str(out), {k: data[k] for k in data.files})
        return f"已生成（课题自定义画法）：{out}"
    xva = _unflatten(np.asarray(xva))
    if not _is_image(xva):
        return f"跳过：验证输入不是图像（形状 {np.asarray(xva).shape}），可在 eval/评估.py 定义 画拼图()"
    if len(pred) != len(yva):
        return f"跳过：预测长度 {len(pred)} 与验证集 {len(yva)} 不符"

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams["font.family"] = ["Nimbus Sans", "DejaVu Sans"]

    names = getattr(ev, "CLASS_NAMES", None)
    lab = (lambda i: names[int(i)]) if names else (lambda i: str(int(i)))
    rng = np.random.RandomState(0)
    wrong = np.where(pred != yva)[0]
    # 上排：错得最自信的；没有概率就随机抽错例
    if "prob" in data.files and len(wrong):
        conf = np.asarray(data["prob"])[wrong, pred[wrong]]
        top = wrong[np.argsort(-conf)][:N_PER_ROW]
    else:
        top = rng.choice(wrong, min(N_PER_ROW, len(wrong)), replace=False) if len(wrong) else np.array([], int)
    rand = rng.choice(len(yva), N_PER_ROW, replace=False)

    fig, axes = plt.subplots(2, N_PER_ROW, figsize=(N_PER_ROW * 1.25, 3.9))
    rows = [(top, "Most confident errors" if "prob" in data.files else "Errors (random)"), (rand, "Random samples")]
    for r, (idx, title) in enumerate(rows):
        for c in range(N_PER_ROW):
            ax = axes[r, c]
            ax.set_xticks([]); ax.set_yticks([])
            for s in ax.spines.values():
                s.set_linewidth(0.4); s.set_color("#999999")
            if c >= len(idx):
                ax.axis("off"); continue
            i = idx[c]
            img = np.asarray(xva[i])
            ax.imshow(img.squeeze(), cmap="gray" if img.ndim == 2 or img.shape[-1] == 1 else None)
            ok = pred[i] == yva[i]
            ax.set_title(f"T: {lab(yva[i])}\nP: {lab(pred[i])}", fontsize=6.5,
                         color="#555555" if ok else "#000000", fontweight="normal" if ok else "bold")
        axes[r, 0].set_ylabel(title, fontsize=7, rotation=90, labelpad=4)
    acc = float((pred == yva).mean())
    fig.suptitle(f"{tag}   validation accuracy {acc*100:.2f}%   errors {len(wrong)}/{len(yva)}", fontsize=8)
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    fig.subplots_adjust(hspace=0.55)  # 两排之间留出标题空间，避免下排标题压到上排图
    fig.savefig(out, dpi=130, facecolor="white")
    plt.close(fig)
    return f"已生成：{out}"


if __name__ == "__main__":
    print(draw(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()))
