# 03 · 深度 / IR 链路（Orbbec SDK v1 + Python ctypes）

> 结论：深度与 IR 走厂商私有协议，**必须用 Orbbec SDK**。
> 本方案 **零编译** —— 官方 aarch64 预编译库 + Python `ctypes` 直调纯 C API。

---

## 1. 为什么选 SDK v1

Astra Pro Plus 是 **PrimeSense / PS1080 世代**的老机型
（设备自报 `Astra Pro`，PID `0x0403`，固件 `RD108C-009`）。

- ✅ **OrbbecSDK v1**（`github.com/orbbec/OrbbecSDK`）—— 支持，用这个
- ❌ **OrbbecSDK v2** —— 已不再支持该老机型，不要用
- ⚪ OpenNI2（`github.com/orbbec/OpenNI_SDK`）—— 备选路线，本方案未采用

---

## 2. 部署（已经做完，此处为复现记录）

### 下载

官方 release 包（需科学上网）：

```
OrbbecSDK_C_C++_v1.10.37_20260707_3f75820_linux_arm64_release.zip   (18 MB)
```

开发主机上已有备份：`/root/orbbec/sdk_arm64.zip`。

> 代理：`http://192.168.5.8:7890`（拉 GitHub 用）。

### 部署到树莓派

```bash
# 开发主机打包（去掉用不上的 C#/Python wrapper，只留 lib + config + 样例 + 脚本）
tar czf sdk_deploy.tgz -C <解压目录>/OrbbecSDK_v1.10.37 SDK/lib SDK/config Example/bin Script

# 推到树莓派
sshpass -f ~/.pi-pass scp sdk_deploy.tgz root@192.168.10.76:/tmp/
sshpass -f ~/.pi-pass ssh root@192.168.10.76 \
  'mkdir -p /opt/orbbec && tar xzf /tmp/sdk_deploy.tgz -C /opt/orbbec'

# 装 udev 规则（一次性）
sshpass -f ~/.pi-pass ssh root@192.168.10.76 \
  'cd /opt/orbbec/Script && sh install_udev_rules.sh'
```

部署后目录：

```
/opt/orbbec/
├── SDK/
│   ├── lib/     libOrbbecSDK.so.1.10.37 (10.5MB, aarch64)
│   │            libdepthengine.so.2.0
│   │            libob_usb.so
│   ├── config/  OrbbecSDKConfig_v1.0.xml
│   └── include/ libobsensor/h/*.h        (纯 C 头文件)
├── Example/
│   ├── bin/     HelloOrbbec, SaveToDisk, DepthViewer, ColorViewer + config XML
│   └── cpp/     样例源码
├── Script/      install_udev_rules.sh
└── cap/         ← 抓帧产物输出目录（自建）
```

---

## 3. ⚠️ 两个必须遵守的运行约束

```bash
cd /opt/orbbec/Example/bin                                    # ①
LD_LIBRARY_PATH=/opt/orbbec/SDK/lib python3 /root/ob_depth.py  # ②
```

① **必须 `cd` 到 `Example/bin`** —— SDK 从 **当前工作目录** 读 `OrbbecSDKConfig_v1.0.xml`，
   找不到就退回内置默认值，行为会变。
② **必须设 `LD_LIBRARY_PATH`** —— 否则 `libOrbbecSDK.so` 加载失败。

---

## 4. 设备支持的流（实测全量清单）

见 `../samples/profiles.txt`：

```
DEPTH : 8 个  160x120@30 Y11, 320x240@30 Y11, 640x480@30 Y11, 1280x1024@7 Y11,
              160x120@30 Y12, 320x240@30 Y12, 640x480@30 Y12, 1280x1024@7 Y12
IR    : 5 个  320x240@30 Y10, 640x480@30 Y10, 1280x1024@7 Y10,
              1280x960@7 Y10, 1280x720@7 Y10
COLOR : 4 个  160x120@30 UYVY, 640x480@30 UYVY, 1280x720@30 UYVY, 1280x800@30 UYVY
```

### 🔑 三个关键观察

1. **SDK 侧的 COLOR 只有 UYVY，没有 MJPG** —— 这正是官方样例会挂的原因（见 `docs/04`）。
2. 深度有 **Y11 / Y12** 两种格式；IR 是 **Y10**。
3. 彩色走 SDK 无意义（UVC 那边有更好的 MJPG 720p@30），**彩色请走 UVC**。

### config 默认值（`OrbbecSDKConfig_v1.0.xml` 的 `<AstraProPlus>` 段）

| 流 | 默认配置 |
|---|---|
| Depth | `Y11 640x480@30` |
| Color | `MJPG 640x480@30` ← **设备不支持 MJPG，故匹配失败** |
| IR | `Y10` |

---

## 5. ctypes 调用序列（核心，`scripts/ob_depth.py` / `ob_ir.py` 已实现）

SDK 导出 **345 个 `ob_*` 纯 C 符号**，Python 用 `ctypes.CDLL` 直接绑。

```
ob_create_pipeline(&err)                          # 建 pipeline
ob_pipeline_get_stream_profile_list(pipe, sensor, &err)
ob_stream_profile_list_count(list, &err)
ob_stream_profile_list_get_profile(list, i, &err) # 逐个拿 profile
  ob_video_stream_profile_width/height/fps(profile, &err)
  ob_stream_profile_format(profile, &err)

ob_create_config(&err)
ob_config_enable_stream(cfg, profile, &err)       # 启用选中的 profile

# —— 深度专有 ——
ob_config_set_depth_scale_require(cfg, True, &err)   # 要求直接输出"真实单位"

ob_pipeline_start_with_config(pipe, cfg, &err)
ob_pipeline_wait_for_frameset(pipe, timeout_ms, &err)
ob_frameset_depth_frame(frameset, &err)   # 深度
# 或 ob_frameset_get_frame(frameset, OB_SENSOR_IR, &err)  # IR
  ob_video_frame_width/height(frame, &err)
  ob_frame_format(frame, &err)
  ob_frame_data(frame, &err)              # -> 裸指针
  ob_frame_data_size(frame, &err)
  ob_depth_frame_get_value_scale(frame, &err)   # 深度专有
ob_delete_frame / ob_delete_pipeline / ob_delete_config / ob_delete_error
```

### 枚举值

```
ob_sensor_type : IR = 1, COLOR = 2, DEPTH = 3
ob_format      : UYVY=2, MJPG=5, Y16=8, Y8=9, Y10=10, Y11=11, Y12=12, ...
```

### 🔑 深度读数必须乘 `valueScale`

```python
raw   = struct.unpack_from("<%dH" % (w*h), buf, 0)   # 16bit 无符号
scale = ob_depth_frame_get_value_scale(frame, &err)  # 本机实测 = 1.0
mm    = [v * scale for v in raw]                     # ← 乘完才是毫米
```

**不乘 `valueScale` 会得到偏小的假数值**（这是最初的坑）。

> ⚠️ 关于 `valueScale = 1.0` 与实测深度量程的关系，**存在未解疑点**，
> 详见 [`05-未决问题.md`](05-未决问题.md) §2。接手时请优先复验。

---

## 6. 运行结果（实测）

```bash
cd /opt/orbbec/Example/bin
LD_LIBRARY_PATH=/opt/orbbec/SDK/lib python3 /root/ob_depth.py 3
```

输出：

```
选用深度 profile #2: 640x480 Y11
[OK] 深度流已启动
  帧 0: 640x480 scale=1.0 | 有效 254196/307200 (82.7%) | 深度 66~252mm 均值 197mm
  帧 25: 640x480 scale=1.0 | 有效 256228/307200 (83.4%) | 深度 66~265mm 均值 196mm
摘要 -> /opt/orbbec/cap/summary.json
```

产物：

| 文件 | 内容 |
|---|---|
| `depth_<idx>.png` | 8bit 伪彩可视化（**蓝=近 → 红=远**，黑=无效） |
| `depth_<idx>.bin` | 原始 16bit（`w*h*2` 字节），供算法使用 |
| `summary.json` | 每帧：分辨率/格式/`value_scale`/有效像素比/min-max-mean mm/直方图 |
| `profiles.txt` | 全量流清单 |

> 伪彩是脚本内 `colorize()` 自绘的（纯 stdlib 写 PNG，**不依赖 PIL/OpenCV**）。

### 视觉核验结论（对 `depth_0.png`）

- **有效像素 82.7%** —— 对近距离桌面场景属正常
- 画面中：**椅子**（近景/蓝，椅背+扶手轮廓清晰）、**显示器**（中景/黄绿）、**背景墙**（远景/红）
- 黑色无效区 = 传感器无回波区 + 前景遮挡阴影，**不是故障**

### IR 链路

```bash
cd /opt/orbbec/Example/bin
LD_LIBRARY_PATH=/opt/orbbec/SDK/lib python3 /root/ob_ir.py 2
# -> /opt/orbbec/cap/ir_<idx>.png  (+ .bin)
```

IR（`640x480 Y10`）是**灰度照片式**成像，夜晚也能拍（自带红外补光）。

---

## 7. 为什么不用官方样例

| 样例 | 状态 | 原因 |
|---|---|---|
| `HelloOrbbec` | ✅ 可用 | 只枚举设备信息，不启流 |
| `SaveToDisk` | ❌ **挂** | 彩色走 OpenNI 侧时 `Match openni video mode failed` |
| `DepthViewer` / `ColorViewer` | ❌ | GUI 交互，无显示环境；且同样受彩色匹配问题影响 |

**根本原因**：config 里 `<AstraProPlus><Color>` 写的是 `MJPG`，
而 SDK 的 OpenNI 彩色侧只暴露 `UYVY` → 匹配失败 → `pipeline.start` 抛异常。

已尝试但**无效**的修法：改 config 格式为 UYVY、改全局 UVC 后端 `Auto→V4L2`、
用 `ProfileFilter` 屏蔽彩色 profile。（详见 `docs/04`）

**最终绕法：自写脚本，只开深度/IR；彩色另走 UVC。** 两条链路都可用。