#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 AstraProPlus 的 Color 格式从 MJPG 改成设备实际支持的 UYVY"""
import re, shutil, os, xml.etree.ElementTree as ET

CFG = "/opt/orbbec/Example/bin/OrbbecSDKConfig_v1.0.xml"
BAK = CFG + ".orig"

if not os.path.exists(BAK):
    shutil.copy2(CFG, BAK); print("已备份 ->", BAK)
else:
    shutil.copy2(BAK, CFG); print("从备份重建")

src = open(CFG, encoding="utf-8").read()
mo = re.search(r"(<AstraProPlus>)(.*?)(</AstraProPlus>)", src, re.S)
if not mo:
    raise SystemExit("未找到 <AstraProPlus>")
body = mo.group(2)
mi = re.search(r"(<Color>)(.*?)(</Color>)", body, re.S)
if not mi:
    raise SystemExit("<AstraProPlus> 内未找到 <Color>")
seg = mi.group(2)
new_seg, n = re.subn(r"(<Format>)\s*MJPG\s*(</Format>)", r"\1UYVY\2", seg)
print(f"Color Format 替换: {n} 处")
out = src[:mo.start()] + mo.group(1) + body[:mi.start()] + "<Color>" + new_seg + "</Color>" + body[mi.end():] + mo.group(3) + src[mo.end():]
open(CFG, "w", encoding="utf-8").write(out)
ET.parse(CFG)
print("XML 校验通过")
print(re.search(r"<AstraProPlus>.*?</AstraProPlus>", out, re.S).group(0)[:700])