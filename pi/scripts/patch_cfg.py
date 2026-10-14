#!/usr/bin/env python3
# 在 OrbbecSDKConfig 的 <AstraProPlus><Color> 段插入 ProfileFilter,
# 屏蔽所有彩色 profile,使 SaveToDisk 自动降级为纯深度采集。
import re, shutil, sys, os

CFG = "/opt/orbbec/Example/bin/OrbbecSDKConfig_v1.0.xml"
BAK = CFG + ".orig"

if not os.path.exists(BAK):
    shutil.copy2(CFG, BAK)
    print("已备份 ->", BAK)
else:
    shutil.copy2(BAK, CFG)          # 每次从原始文件重建,保证可重复
    print("从备份重建配置")

src = open(CFG, encoding="utf-8").read()

FILTER = """            <ProfileFilter>
                <profile>
                    <Format>MJPG|YUYV|UYVY|RGB|BGR|YUV|NV12|I420</Format>
                </profile>
                <profile>
                    <Width>160|320|352|640|800|1280|1920</Width>
                </profile>
            </ProfileFilter>
"""

def patch_block(text, outer, inner):
    """在 outer 段内的 inner 子段结尾前插入 FILTER"""
    mo = re.search(rf"(<{outer}>)(.*?)(</{outer}>)", text, re.S)
    if not mo:
        return text, f"未找到 <{outer}>"
    body = mo.group(2)
    mi = re.search(r"(<Color>)(.*?)(</Color>)", body, re.S)
    if not mi:
        return text, f"<{outer}> 内未找到 <Color>"
    if "ProfileFilter" in mi.group(2):
        return text, "已有 ProfileFilter,跳过"
    new_body = body[:mi.start()] + "<Color>" + mi.group(2) + FILTER + "</Color>" + body[mi.end():]
    return text[:mo.start()] + mo.group(1) + new_body + mo.group(3) + text[mo.end():], "已插入"

out, msg = patch_block(src, "AstraProPlus", "Color")
print("AstraProPlus:", msg)
open(CFG, "w", encoding="utf-8").write(out)

# 校验 XML 仍然合法
import xml.etree.ElementTree as ET
ET.parse(CFG)
print("XML 校验通过, 大小:", os.path.getsize(CFG))