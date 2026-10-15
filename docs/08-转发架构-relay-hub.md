# 08 · 转发架构：树莓派瘦采集端 + 服务器聚合（ob-relay / ob-hub）

> 交付物：树莓派只做「读设备 + 转发」，渲染/合成/Web 全部搬到服务器。
> 域名 **http://camera.internal.jiayouexp.com** ，2026-10-14 完成并验收。
> 方案原文：`~/.hermes/plans/2026-10-14_124319-树莓派瘦采集端转发方案.md`

---

## 1. 为什么改（改之前的瓶颈）

本地三合一方案（`docs/07`）实测：树莓派 4 核被吃掉一半，其中

| 进程 | CPU | 性质 |
|---|---|---|
| chromium（kiosk 显示） | ~100% | 每次画面刷新的固定成本 |
| ob-viewer（采集+渲染+Web） | ~50% | 深度引擎 + 伪彩渲染 + HTTP 分发 |

每加一个消费端、每加一种处理都要再啃树莓派。改成转发架构后，**加多少处理都不再影响它**。

## 2. 架构

```
┌─ 树莓派 192.168.10.76 ──┐        ┌─ 服务器 192.168.5.20（56 核）─────────────┐
│ Astra Pro Plus          │        │ ob-hub /home/OrbbecPiApi (Go)             │
│  ├ 彩色 UVC 640x480@30 ─┼─ OBF1 ─┤  ├ 接收(:9501,仅内网) → 最新帧槽            │
│  └ 深度 SDK 320x240@30 ─┘  帧协议 │  ├ 深度伪彩渲染 Y16→turbo LUT→JPEG          │
│                         │        │  ├ 整屏合成 800x330@12fps                   │
│ ob-relay (Python)       │        │  ├ HTTP :9500（仅 127.0.0.1）                │
│ kiosk ──► 拉整屏流 ─────┼────────┤  └ nginx 反代 → camera.internal.jiayouexp.com │
└─────────────────────────┘        └────────────────────────────────────────────┘
                                   前端 /www/wwwroot/camera.internal.jiayouexp.com
```

### 三条设计原则

1. **绝不阻塞采集**：网络/服务器再慢，树莓派只丢帧，绝不反压到相机与 SDK。
2. **按流分连接**：彩色、深度各一条 TCP，互不阻塞；状态走独立小消息。
3. **服务器是唯一消费方，树莓派保留降级能力**：服务器挂了可切回本地 `ob-viewer` 自看。

### 各流送什么

| 流 | 送什么 | 为什么 |
|---|---|---|
| 彩色 | 相机**原生 JPEG 原样转发** | 已在相机内压缩好，零 CPU、零画质损失 |
| 深度 | **原始 uint16**（Y16） | 服务器想怎么用都行；树莓派不做任何渲染 |
| 状态 | 小 JSON（1Hz）：fps/丢帧/温度/量程 | 便于在 `/api/status` 里看设备状态 |

## 3. 协议（OBF1，28 字节定长头，大端）

```
0  4  magic "OBF1"      4  1  type 0=彩JPEG 1=深Y16 2=状态 3=HELLO
5  1  flags(bit0=zlib)  6  4  seq        10 8  ts_us(Unix 微秒)
18 2  width             20 2  height     22 1  format
23 1  rsvd              24 4  payload_len
28 N  payload
```

* **latest-frame 语义**：接收端只留最新帧，落后就丢，绝不排队 → 延迟恒定不累积。
* **HELLO 鉴权**：relay 建连先发 `{"ts_us":…, "mac": HMAC-SHA256(psk, ts)[:16]}`，
  校验失败直接断开（含 ±300s 时间戳窗口防重放）。
* ⚠️ **两个实现必须同步**：`relay/obproto.py`（Python）与 `OrbbecPiApi/proto.go`（Go）。
  两边的布局测试用**相同的期望字节**（`test_layout_matches_spec` / `TestProtoLayout`），
  改一边不跑测试就会在线挂掉——开发中已经因为这个字段宽度写反踩过一次。

## 4. 实测数据（验收）

| 项 | 实测 | 判定 |
|---|---|---|
| 端到端延迟（采集 ts → 服务器收到） | 彩色 **14.6ms**、深度 **-1.1ms**（时钟偏差内） | ✅ 目标 P95≤200ms |
| 入站带宽 | **47.7 Mbps（5.96 MB/s）** | ✅ 目标 ≤8MB/s |
| 树莓派系统空闲 | **68~88%**（改前 52%，最差 7%） | ✅ |
| 树莓派进程 | relay(python3) **18%**、chromium **~25-30%** | ✅ 改前 50% + 100% |
| 丢帧 | 彩色 0 / 深度 0（稳态） | ✅ |
| 设备温度 | 43.8°C | ✅ |
| 链路实测能力 | scp 200MB / 5.5s ≈ **36 MB/s** | 用量仅 1/6 |

### 故障演练（全部通过）

| 演练 | 结果 |
|---|---|
| 停掉服务器后端 | relay 日志 `连接失败(Connection refused)，8.0s 后重试`，**采集不中断**、服务仍 active；树莓派屏幕画面冻结（无数据） |
| 重启后端 | 12s 内自动恢复 `ok=true`，seq 继续自增 |
| 停掉树莓派 relay | 服务器把两路标成 `error: relay 连接已断开`，页面显示 ⚠ |
| 恢复 relay | 自动恢复，各流正常 |
| **服务器重启后树莓派屏幕自愈** | 停 → 画面冻结；起 → **45.9% 像素在动，自动恢复**（未碰树莓派） |

## 5. 怎么操作

```bash
# ---- 服务器侧 ----
systemctl status ob-hub
curl -s http://127.0.0.1:9500/api/status | python3 -m json.tool
sh /home/OrbbecPiApi/deploy/install.sh     # 改完代码重新构建+装 unit
systemctl restart ob-hub

# ---- 树莓派侧 ----
systemctl status ob-relay
journalctl -u ob-relay -f                  # 看采集/连接/丢帧日志
sh /opt/orbbec/relay/deploy/install-relay.sh

# ---- 切换树莓派屏幕显示什么（会自动停掉互斥的服务）----
sh /opt/orbbec/viewer/deploy/switch-kiosk.sh server     # 服务器整屏流（默认）
sh /opt/orbbec/viewer/deploy/switch-kiosk.sh local      # 降级：本地 ob-viewer
sh /opt/orbbec/viewer/deploy/switch-kiosk.sh calendar   # 日历信息屏
sh /opt/orbbec/viewer/deploy/switch-kiosk.sh            # 查看当前
```

> ⚠️ `server` 与 `local` 互斥：两者都要占相机，脚本会自动停另一个服务。

## 6. 本次踩到的坑（都记在这）

1. **协议字段宽度写反**：Python 侧 `struct` 格式串把 `seq(I)`/`ts_us(Q)` 顺序写错，
   首个 HELLO 就 `struct.error`。→ 加了两侧**相同期望字节**的布局测试钉死。
2. **深度节流阈值与相机周期相等**：相机 33.3ms，阈值 `1/30s` 也 33.3ms，
   抖动导致丢一半帧（实测 14.8fps）。→ 阈值留 20% 容差，恢复 29.7fps。
3. **Chromium 对 MJPEG 断流不触发 `onerror`**：画面静默冻结。
   → 页面三层自愈（画面哈希探针 + 服务启动标识 + 帧号看门狗），已在本地方案验证过，
   本方案沿用（`/www/wwwroot/.../app.js`）。
4. **kiosk 启动白等 30 秒**：`kiosk.sh` 的健康检查把 `/api/status` 拼在**页面路径**后面
   （`.../screen.html/api/status` 必然 404）。→ 改成先取站点根再拼 `/api/status`，启动 **2 秒**。
   （同时同步了 `/home/Pi/dashboard/deploy/kiosk.sh`，原文件已备份为 `.bak.<时间戳>`）
5. **防火墙不是宝塔管的那套**：本机真正生效的是 **ufw**（宝塔 `status.json` = close），
   新端口默认被挡（实测 9501 超时）。→ `ufw allow from 192.168.10.0/23 to any port 9501`，
   **只放行树莓派网段**。
6. **合成版式浪费竖向空间**：两个 4:3 画面并排在 800x480 上只能占 300px 高（宽度受限），
   剩 180px 黑边。→ 合成图改 **800x330**，下方 150px 交给页面做信息条（中文标签、1Hz 刷新）。

## 7. 2026-10-14 下午追加：画质调优 + 网页可调参数

### 7.1 树莓派屏改回日历，整屏合成停用

老板确认「树莓派不用继续展示了，切回日历」「整屏合成用不到」。已做：

* kiosk 切回日历：`sh /opt/orbbec/viewer/deploy/switch-kiosk.sh calendar`
  （`ob-relay` 继续跑 → **相机服务照常，域名仍可访问**）
* 整屏合成关闭：`OB_HUB_SCREEN_FPS=0`（`/screen.mjpg`、`/snapshot/screen.jpg` 路由不再注册，也不起合成协程）。
  **代码保留**，要恢复把该值设成 >0 并重启 ob-hub 即可（`screen.html` 也还在）。
* 前端去掉了 SCREEN 面板；顺手修了 grid 遗留的第二行（曾导致预览区下方一大块空白）。

### 7.2 画质调优（彩色）

这台 UVC **只暴露 User Controls**（曝光/增益不可控，全自动）。可控项里对「逆光人像发黑」最有用的几项：

| 项 | 默认 | 调优值 | 效果 |
|---|---|---|---|
| `backlight_compensation` | 1 | **3** | 逆光场景关键 |
| `gamma` | 100 | **160** | 暗部提亮 |
| `brightness` | 0 | **8** | |
| `contrast` | 32 | **40** | |
| `sharpness` | 2 | **4** | |
| 分辨率 | 640x480 | **1280x720** | 官方 MJPG 最高档 |

**实测效果**（同一场景，调优前 → 后）：暗部像素(<40) **73.4% → 30.9%**，
中位亮度 13 → 71。视觉模型评价从「完全看不清细节、只有剪影」→「人物五官/衣物/桌面物品都能分辨，偏上水平」。
代价是高光过曝从 16% → 23%（窗户本来就过曝，无曝光控制可用）。

### 7.3 ⭐ 深度分辨率：**只有 320x240 可用**（重要设备事实）

| 深度档位 | 有效像素 | 结论 |
|---|---|---|
| **320x240** | **70~75%** | ✅ 唯一可用 |
| 640x480 | **2%** | ❌ 不可用 |
| 1280x1024 | 4.6% | ❌ 不可用 |

已排除带宽因素（把彩色降到 640x480 再测，640x480 深度反而只剩 0.3%），
是**设备/档位本身**的问题。别再往上调分辨率——「开最大」在这台机器上等于「开坏」。

### 7.4 深度原始流 zlib 压缩

深度图很平滑，**zlib level 1 能压 7~10 倍**（320x240 一帧 150KB → ~20KB）。
协议里本来就留了 `flags bit0`，现已启用（`OB_RELAY_DEPTH_ZLIB=1`）。

* 树莓派侧压缩：level 1，约 3~12ms/帧（比换来的带宽划算得多）
* 服务器侧用 Go `compress/zlib` 解压
* 状态里能看到 `zlib_ratio`（前端显示为「压缩比」）
* 实测入站：**195.9 Mbps → 58.4 Mbps**（彩色 720p 约 6.9MB/s + 深度约 0.6MB/s）

### 7.5 ⭐ 网页可调参数（相机 + 码流）

**为什么走反向通道**：树莓派在防火墙后面（只放行 SSH），服务器连不进去；
而 relay 是主动外连的，所以命令顺着这条已建立的连接下发最省事，也不用在服务器上存 SSH 凭据。

```
浏览器 ──POST /api/config──> ob-hub ──TYPE_CONTROL(JSON)──> relay 控制通道
                              ▲                              │
                              └──────TYPE_STATUS(回执)───────┘
```

* relay 额外开一条 `who="<设备>/control"` 的连接常驻，服务器认这个后缀就知道是控制通道
* 平时静默 → **两端都要保活**：relay 每 10s 发 keepalive，hub 90s 收不到就断开重连
* 命令：
  * `get_config` → 返回相机可调项(含 min/max/默认) + 当前值 + 码流可调项 + 当前值
  * `set_camera` / `reset_camera` → `v4l2-ctl -c` 立即生效，并落盘 `camera.json`（重启/重插后自动恢复）
  * `set_stream` → 写 `/etc/default/ob-relay` 后**退出进程**由 systemd 拉起（约 5 秒生效，最省事最可靠）
* 写入安全：`POST /api/config` 只允许**内网来源**（`isPrivateClient`，走 nginx 时看 `X-Real-IP`）
* 前端：监控页的「⚙ 参数设置」折叠区，控件按设备上报的 spec 动态生成；
  支持 `#settings` 深链接直接展开

### 7.6 本轮新踩的坑

1. **`import zlib` 漏了** → 深度线程直接抛异常、深度整路挂掉（日志里只有一行 `name 'zlib' is not defined`）。
2. **lambda 进了 JSON**：`STREAM_SPEC` 里的 `current` 是 lambda，`json.dumps(回执)` 直接抛
   → 控制线程死掉、通道静默失联。已改成回执前剥掉 lambda，并给回执序列化加了兜底 try/except
   （**控制线程不能因为一次回执失败就死**）。
3. **控制通道被读超时掐断**：通道平时静默，原本 120s 读超时 → 必须加双向保活。
4. **`max-width:100%` 只缩不放**：深度 320x240 在网页上显得很小，要 `width:100%` 才会放大填满。
5. **删掉一个 grid 行别忘了改 CSS**：整屏合成面板删了，但 `grid-template-rows` 还留着两行，
   预览区下方空出一大块。

### 7.7 ⭐ 页头开关 + 深度/红外两路独立 pipeline（2026-10-15）

**背景（推翻了早前一个错结论）**：原来的采集端是「传感器模式二选一」（`OB_RELAY_SENSOR_MODE=depth|ir`），
依据是「深度与红外共用 IR 传感器、只能开一个」。**这个结论是错的** ——
错因是把两条流 `enable` 到**同一个 pipeline**：那样深度正常、**IR 恒 0 帧**；
改成**两条各自独立的 pipeline**（各自 `start_with_config`）后两路可同时满帧（实测各 29.3 fps）。

**采集端**：`OB_RELAY_SENSOR_MODE` 删除，改为两个独立开关（`/etc/default/ob-relay`）：

```
OB_RELAY_DEPTH=1   # 深度（默认开）
OB_RELAY_IR=0      # 红外（默认关，见下）
```

**红外为什么默认关**：它是 10bit 噪声数据，zlib 只能压 ~1.5×（深度 7.2×），单它就占 ~130Mbps。
默认关掉后入站带宽 **144 → 58 Mbps**。要看红外随时开。

**前端**：页头常驻两个开关按钮（绿=开 / 灰=关），走的还是 §7.5 那套反向控制通道：

```
点「红外」→ POST /api/config {action:"set_stream", values:{OB_RELAY_IR:"1"}}
          → relay 写 /etc/default/ob-relay → 自己退出、systemd 拉起（~5 秒生效）
          → 页面轮询 /api/status 自动恢复画面
```

* **关掉的那一路是「off」语义，不是「error」**：`/api/status` 里给 `state=off`、清空 error；
  hub 的健康判定与看门狗**都跳过它**（否则关掉那路永远「停滞」，日志刷屏且 `ok` 永远 false）；
  前端把该面板 hidden、状态栏显示「关」（而不是 0 fps）。
* **布局自适应**：关掉一路时，剩下那路自动占满右列（`main.no-ir` / `main.no-depth` 两个 class）。
* 关掉一路也省树莓派 CPU（红外采集约占一个核）。

**踩过的坑**：`POST /api/config` 会把**表单里所有字段**一起下发（不是只发改动的那项）。
所以改了前端一定要给 `app.js?v=N` 递增版本号并让用户强刷；旧标签页的保存会按它渲染时的旧值
覆盖新字段。排查「谁改了我的参数」：nginx access log 看 POST 的来源 IP/UA →
relay journal 看 `control: set_stream -> 已写入 /etc/default/ob-relay` → 再核对 env 实际值。

## 8. 后续可做（本次未做，接口已留）

* **录制/存盘**：`Recorder` 接口已定义（`OnColorJPEG`/`OnDepthY16`），实现并注册即可。
* **点云/测量/CV**：服务器 56 核且拿到的是原始 uint16，做这些只是加 goroutine。
* **多路相机**：协议 `seq`/`ts_us` 已按多源设计，多台 relay 连同一个 hub 即可。
* **HTTPS**：域名目前只有 HTTP；要 HTTPS 可在宝塔面板给该站点一键部署证书
  （参考 `midscene.internal.jiayouexp.com` 用的是 `/home/ssl/internal.jiayouexp.com/`）。
* **彩色/深度配准（D2C 对齐）**：需要 SDK 同时开彩色流（本项目彩色走 UVC 独立链路），
  且该机型 D2C 会报 `switchD2CParams failed`，未做。
* **深度伪彩方案可选**（turbo / jet / 灰度）：目前固定 turbo。