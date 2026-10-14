#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""IR 图 vs 彩色图 方向交叉验证(两者都是同场景亮度图,相关性应显著)
用法: python3 check_orient2.py <参考图> <待测图>"""
import sys
import numpy as np
from PIL import Image

def grad(path, size=(192, 192), crop_aspect=4/3, blur=True):
    im = Image.open(path).convert("L")
    w, h = im.size
    ar = crop_aspect
    if w / h > ar:
        nw = int(h * ar); im = im.crop(((w - nw) // 2, 0, (w - nw) // 2 + nw, h))
    else:
        nh = int(w / ar); im = im.crop((0, (h - nh) // 2, w, (h - nh) // 2 + nh))
    im = im.resize(size, Image.BILINEAR)
    if blur:
        im = im.filter(__import__("PIL.ImageFilter", fromlist=["ImageFilter"]).GaussianBlur(1.5))
    a = np.asarray(im, dtype=np.float64)
    gy, gx = np.gradient(a)
    g = np.hypot(gx, gy)
    g -= g.mean(); s = g.std()
    return g / s if s > 1e-9 else g

ref_p, tst_p = sys.argv[1], sys.argv[2]
ref = grad(ref_p)
tst = grad(tst_p)

variants = {
    "原样": tst,
    "上下翻转(flipud)": np.flipud(tst),
    "左右翻转(fliplr)": np.fliplr(tst),
    "旋转180(rot180)": np.flipud(np.fliplr(tst)),
}
print(f"参考图: {ref_p}")
print(f"待测图: {tst_p}")
print(f"{'待测朝向':<22}{'相关系数 r':>12}")
res = []
for k, v in variants.items():
    c = float((v * ref).mean()); res.append((c, k))
    print(f"{k:<22}{c:>12.4f}")
res.sort(reverse=True)
print()
print(f"→ 与参考图最一致: **{res[0][1]}**  (r={res[0][0]:.4f})")
print(f"→ 最不一致      : {res[-1][1]}  (r={res[-1][0]:.4f})")
print(f"→ 区分度 {res[0][0]-res[-1][0]:.4f}")