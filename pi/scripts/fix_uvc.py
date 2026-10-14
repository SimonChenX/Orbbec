#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""全局 LinuxUVCBackend 由 Auto 改为 V4L2(内核 uvcvideo 已占设备,LibUVC 抢不到)"""
import re, os, shutil, sys, xml.etree.ElementTree as ET

CFG = "/opt/orbbec/Example/bin/OrbbecSDKConfig_v1.0.xml"
BAK = CFG + ".orig"
mode = sys.argv[1] if len(sys.argv) > 1 else "v4l2"

if not os.path.exists(BAK):
    shutil.copy2(CFG, BAK)
src = open(BAK, encoding="utf-8").read() if mode == "restore" else None

if mode == "restore":
    shutil.copy2(BAK, CFG)
    print("已还原为原版配置")
else:
    src = open(CFG, encoding="utf-8").read()
    new, n = re.subn(r"(<LinuxUVCBackend>)\s*Auto\s*(</LinuxUVCBackend>)",
                     r"\g<1>V4L2\g<2>", src, count=1)
    print(f"LinuxUVCBackend Auto->V4L2 替换 {n} 处")
    # 顺带保持 Color 格式为 UYVY
    new, n2 = re.subn(r"(<AstraProPlus>.*?<Color>.*?<Format>)\s*MJPG\s*(</Format>)",
                      r"\g<1>UYVY\g<2>", new, flags=re.S)
    print(f"AstraProPlus Color MJPG->UYVY 替换 {n2} 处")
    open(CFG, "w", encoding="utf-8").write(new)
    ET.parse(CFG)
    print("XML 校验通过")
    print("当前:", re.search(r"<LinuxUVCBackend>[^<]*</LinuxUVCBackend>", new).group(0))