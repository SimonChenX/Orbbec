#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
客观判定彩色图相对深度图的方向:
把两张图缩放/裁到同一网格,取梯度幅值(边缘结构),比较四种朝向
(原样 / 上下翻转 / 左右翻转 / 旋转180)与深度图的归一化相关性。
结构相关性最高者 = 与深度图一致的朝向。
"""
import numpy as np
from PIL import Image

def gray_grad(path, size=(128, 128), crop_aspect=None):
    im = Image.open(path).convert("L")
    w, h = im.size
    if crop_aspect:                      # 居中裁到指定宽高比
        ar = crop_aspect
        if w / h > ar:
            nw = int(h * ar); im = im.crop(((w - nw) // 2, 0, (w - nw) // 2 + nw, h))
        else:
            nh = int(w / ar); im = im.crop((0, (h - nh) // 2, w, (h - nh) // 2 + nh))
    im = im.resize(size, Image.BILINEAR)
    a = np.asarray(im, dtype=np.float64)
    gy, gx = np.gradient(a)
    g = np.hypot(gx, gy)
    g -= g.mean()
    s = g.std()
    return g / s if s > 1e-9 else g

def corr(a, b):
    return float((a * b).mean())

def variants(x):
    return {
        "原样":        x,
        "上下翻转(flipud)": np.flipud(x),
        "左右翻转(fliplr)": np.fliplr(x),
        "旋转180(rot180)":  np.flipud(np.fliplr(x)),
    }

# 4:3 是深度图的比例; 彩色 16:9 裁到 4:3 后再比，减少视场差异
depth = gray_grad("/root/orbbec/depth_0.png", crop_aspect=4/3)
color = gray_grad("/root/orbbec/color_1280x720.jpg", crop_aspect=4/3)

print("深度图 vs 彩色图(梯度结构相关性, 越高越一致)")
print(f"{'彩色朝向':<22}{'相关系数':>10}")
res = []
for name, v in variants(color).items():
    c = corr(v, depth)
    res.append((c, name))
    print(f"{name:<22}{c:>10.4f}")

res.sort(reverse=True)
print()
print(f"→ 与深度图最一致的彩色朝向: **{res[0][1]}** (r={res[0][0]:.4f})")
print(f"→ 最不一致: {res[-1][1]} (r={res[-1][0]:.4f})")
spread = res[0][0] - res[-1][0]
print(f"→ 区分度 {spread:.4f} " + ("(信号清晰,可判定)" if spread > 0.05 else "(信号弱,不足以判定)"))