# OrbbecPiApi — 相机聚合后端 (ob-hub)

树莓派上只跑「采集 + 转发」，所有渲染/合成/Web 都在这里做。
访问入口：**http://camera.internal.jiayouexp.com**

```
┌─ 树莓派 192.168.10.76 ──┐        ┌─ 本机 192.168.5.20 ─────────────────────┐
│ Astra Pro Plus          │        │ ob-hub (本仓库)                          │
│  ├ 彩色 UVC 640x480@30 ─┼─ OBF1 ─┤  ├ 接收 → 最新帧槽(Feed)                 │
│  └ 深度 SDK 320x240@30 ─┘  帧协议 │  ├ 深度伪彩渲染(Y16→turbo 调色板→JPEG)    │
│                         │  :9501 │  ├ 整屏合成 800x330@12fps                │
│ ob-relay (Python)       │        │  ├ HTTP :9500（仅 127.0.0.1，nginx 反代） │
│ kiosk ──► 拉整屏流 ─────┼────────┤  └ 预留：录制接口 Recorder               │
└─────────────────────────┘        └──────────────────────────────────────────┘
                                   nginx ──► /www/wwwroot/camera.internal.jiayouexp.com
```

## 1. 快速上手

```bash
cd /home/OrbbecPiApi
sh deploy/install.sh                 # 构建 + 装 unit + 装配置
systemctl enable --now ob-hub
systemctl status ob-hub --no-pager
curl -s http://127.0.0.1:9500/api/status | python3 -m json.tool
```

配置在 `/etc/default/ob-hub`（权限 600），改完 `systemctl restart ob-hub`。

## 2. 端口与接口

| 端口/路径 | 说明 |
|---|---|
| `:9501` TCP | **采集入站**（OBF1 帧协议）。ufw 只放行 `192.168.10.0/23` |
| `127.0.0.1:9500` | HTTP（**不对外**，由 nginx 反代到域名） |
| `/` | 监控页（两路流 + 状态 + **参数设置** + 使用说明） |
| `/api/config` | **参数读写**：GET 读（相机可调项+当前值、码流可调项+当前值）；POST 下发（仅内网来源） |
| `/color.mjpg` | 彩色 MJPEG（相机原生透传，分辨率/帧率可网页调） |
| `/depth.mjpg` | 深度 MJPEG（原始 uint16 在服务端渲染成伪彩） |
| `/snapshot/color.jpg`、`/snapshot/depth.jpg` | 单帧快照 |
| `/api/status` | JSON 状态（各路 fps/序号/延迟、入站带宽、`control` 控制通道状态、设备上报） |
| `/screen.mjpg`、`/snapshot/screen.jpg` | 整屏合成流。**默认关闭**（`OB_HUB_SCREEN_FPS=0`）：树莓派屏已改回日历、用不到。设 >0 即恢复 |

## 3. 关键设计

* **只保留最新帧（`Feed`）**：接收端只存最新一帧，慢客户端自动丢帧，
  **绝不反压到采集端**。这是「服务器卡不会拖死相机」的根本原因。
* **绝不阻塞采集**（树莓派侧同理）：`采集线程 → 容量 1 的队列 → 发送线程`，队列满丢最旧帧。
* **深度渲染放在服务器**：树莓派只转发原始 uint16（150KB/帧），
  伪彩、调色板、量程平滑全在这里做（Go 里约 1ms/帧）。
* **整屏合成给树莓派省 CPU**：树莓派浏览器开销正比于「每秒画面刷新次数」，
  合成成 1 张图 @12fps，比两路流 @30+30fps 便宜得多。
* **整屏合成**（可选，默认关）：树莓派屏改回日历后用不到，`OB_HUB_SCREEN_FPS=0` 关闭；
  设为 >0 即恢复（同时会注册 `/screen.mjpg` 路由）。
* **控制通道（网页可调参数）**：relay 额外开一条 `who="<设备>/control"` 的连接常驻，
  服务器顺着**这条已建立的连接**下发命令（树莓派在防火墙后，服务器连不进去，反向复用最省事）。
  * 命令走 `TYPE_CONTROL`(JSON)，回执走 `TYPE_STATUS`(JSON，带 `ack`=命令 id)
  * 通道平时静默 → **两端保活**：relay 每 10s keepalive，hub 90s 收不到就断开重连
  * 动作：`get_config` / `set_camera` / `reset_camera` / `set_stream` / `restart`
  * `set_stream` 是写采集端配置后**让 relay 退出、由 systemd 拉起**（最省事最可靠）
* **深度传输开 zlib**（`flags bit0`）：深度图平滑，level 1 能压 7~10 倍。
  入站从 195.9 Mbps 降到 58.4 Mbps。
* **鉴权**：relay 建连先发 HELLO（对 8 字节时间戳做 HMAC-SHA256 取前 16 字节），
  校验失败直接断开。PSK 在 `/etc/default/ob-{hub,relay}`，两边必须一致。
* **写入保护**：`POST /api/config` 只允许内网来源（`isPrivateClient`），走 nginx 时读 `X-Real-IP`。

## 4. 代码结构

| 文件 | 作用 |
|---|---|
| `main.go` | 配置、Hub、采集入站、看门狗、启动 |
| `proto.go` | OBF1 帧协议（含 HELLO 鉴权） |
| `feed.go` | 最新帧槽 + 广播唤醒（`close(chan)` 惯用法） |
| `depth.go` | Y16 → turbo 调色板 → JPEG，含 5~95% 分位与平滑 |
| `screen.go` | 双画面合成（双线性缩放）+ 边框 |
| `http.go` | MJPEG / 快照 / 状态接口 |
| `proto_test.go` | 协议往返、**字节布局钉死**、鉴权、渲染统计 |

```bash
go test ./...        # 全部单测（协议 + 渲染）
go build -o ob-hub .
```

> ⚠️ `proto.go` 与树莓派侧 `/home/Orbbec/relay/obproto.py` 是**同一份协议的两个实现**，
> 两边的布局测试用相同的期望字节。改任一边必须同时改另一边并跑两边测试。

## 5. 录制（v1 预留，未实现）

`main.go` 里的 `Recorder` 接口已定义（`OnColorJPEG` / `OnDepthY16`），
设了 `OB_HUB_RECORD_DIR` 也只会打一条日志。v2 要做落盘时实现该接口并注册即可。

## 6. 排障

| 现象 | 排查 |
|---|---|
| `/api/status` 里 `ok=false`、state=waiting | relay 没连上：`journalctl -u ob-relay -n 50`（树莓派侧） |
| state=error「relay 连接已断开」 | relay 进程挂了或网络断 |
| state=stale | 有连接但没新帧（相机被别的进程占用？`fuser -v /dev/video0`） |
| 页面画面冻结但其它内容在动 | MJPEG 断流（Chromium 不报错）。页面有 3 层自愈；若仍冻结见 `app.js` 注释 |
| 从外网连不上 9501 | ufw：`ufw status \| grep 9501`，应只有 `192.168.10.0/23` |
| nginx 502 | 后端没起：`systemctl is-active ob-hub`；或端口被占：`ss -ltnp \| grep 9500` |