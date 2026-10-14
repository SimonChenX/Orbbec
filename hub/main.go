package main

// Orbbec 相机聚合后端（ob-hub）
//
// 树莓派侧 ob-relay 把两路原始数据按 OBF1 帧协议推过来，本服务负责：
//   • 接收（:9501，仅内网）
//   • 深度伪彩渲染（原图是原始 uint16，渲染放在这里，树莓派不干）
//   • MJPEG / 快照 / 状态 HTTP 接口（仅 127.0.0.1:9500，由 nginx 反代）
//   • 整屏合成流（800x480 单图，给树莓派那块屏用）
//
// 设计要点：只保留最新帧（Feed），慢客户端自动丢帧，绝不反压到采集端。

import (
	"bytes"
	"compress/zlib"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"
)

var errNoFrames = errors.New("还没有任何帧")

func logf(format string, a ...any) { log.Printf(format, a...) }

func bytesReader(b []byte) io.Reader { return bytes.NewReader(b) }
func itoa(i int) string              { return strconv.Itoa(i) }

// ---- 配置 ----

type Config struct {
	Ingest    string
	HTTP      string
	PSK       string
	ScreenFPS int
	ScreenW   int
	ScreenH   int
	Quality   int
	StaleMs   int
	RecordDir string // v1 预留，不实现
	ReadMs    int

	// 深度原始值 → 毫米的换算系数（见 depthMMPerUnit 注释）
	DepthMMPerUnit float64
}

func envFloat(k string, d float64) float64 {
	if v := strings.TrimSpace(os.Getenv(k)); v != "" {
		if f, err := strconv.ParseFloat(v, 64); err == nil && f > 0 {
			return f
		}
	}
	return d
}

func env(k, d string) string {
	if v := strings.TrimSpace(os.Getenv(k)); v != "" {
		return v
	}
	return d
}

func envInt(k string, d int) int {
	if v := strings.TrimSpace(os.Getenv(k)); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return d
}

func loadConfig() Config {
	c := Config{
		Ingest:    env("OB_HUB_INGEST", "0.0.0.0:9501"),
		HTTP:      env("OB_HUB_HTTP", "127.0.0.1:9500"),
		PSK:       env("OB_HUB_PSK", ""),
		ScreenFPS: envInt("OB_HUB_SCREEN_FPS", 12),
		Quality:   envInt("OB_HUB_JPEG_Q", 72),
		StaleMs:   envInt("OB_HUB_STALE_MS", 2500),
		RecordDir: env("OB_HUB_RECORD_DIR", ""),
		ReadMs:    envInt("OB_HUB_READ_MS", 10000),
	}
	depthMMPerUnit = envFloat("OB_HUB_DEPTH_MM_PER_UNIT", 10)
	c.DepthMMPerUnit = depthMMPerUnit
	size := env("OB_HUB_SCREEN_SIZE", "800x480")
	fmt.Sscanf(size, "%dx%d", &c.ScreenW, &c.ScreenH)
	if c.ScreenW <= 0 || c.ScreenH <= 0 {
		c.ScreenW, c.ScreenH = 800, 480
	}
	return c
}

// ---- Hub ----

type Hub struct {
	cfg     Config
	color   *Feed
	depth   *Feed
	ir      *Feed
	screen  *Feed
	depthR  *DepthRenderer
	irR     *IRRenderer
	pose    *PoseState
	depthAt *DepthSample
	control *Control

	boot string

	mu            sync.Mutex
	relayWho      map[string]any
	ingressBps    float64
	_ingressN     uint64
	_ingressT0    time.Time
	ingressTotal  uint64
	colorConns    uint64
	depthConns    uint64
	irConns       uint64
	lastColorConn time.Time
}

func NewHub(cfg Config) *Hub {
	return &Hub{
		cfg:        cfg,
		color:      NewFeed("color"),
		depth:      NewFeed("depth"),
		ir:         NewFeed("ir"),
		screen:     NewFeed("screen"),
		depthR:     NewDepthRenderer(cfg.Quality),
		irR:        NewIRRenderer(cfg.Quality),
		pose:       NewPoseState(),
		depthAt:    NewDepthSample(),
		control:    NewControl(),
		boot:       fmt.Sprintf("%d-%d", os.Getpid(), time.Now().Unix()),
		relayWho:   map[string]any{},
		_ingressT0: time.Now(),
	}
}

func (h *Hub) Status() map[string]any {
	cs := h.color.Status()
	ds := h.depth.Status()
	irs := h.ir.Status()
	ss := h.screen.Status()

	h.mu.Lock()
	relay := map[string]any{}
	for k, v := range h.relayWho {
		relay[k] = v
	}
	relay["ingress_bps"] = round1(h.ingressBps)
	relay["ingress_mbps"] = round1(h.ingressBps * 8 / 1e6)
	relay["ingress_total_bytes"] = h.ingressTotal
	relay["color_connects"] = h.colorConns
	relay["depth_connects"] = h.depthConns
	relay["ir_connects"] = h.irConns
	h.mu.Unlock()

	// 深度/红外各跑独立 pipeline、可以同时在线；只有采集端关掉的那一路不参与健康判定
	ok := cs["state"] == "ok"
	if h.depthEnabled() {
		ok = ok && ds["state"] == "ok"
	}
	if h.irEnabled() {
		ok = ok && irs["state"] == "ok"
	}
	return map[string]any{
		"ok":            ok,
		"boot":          h.boot,
		"uptime_s":      int(time.Since(startTime).Seconds()),
		"depth_enabled": h.depthEnabled(),
		"ir_enabled":    h.irEnabled(),
		"color":       cs,
		"depth":       ds,
		"ir":          irs,
		"screen":      ss,
		"relay":       relay,
		"pose":        h.pose.Status(),
		"control":     h.control.Status(),
		"config": map[string]any{
			"ingest":     h.cfg.Ingest,
			"http":       h.cfg.HTTP,
			"screen_fps": h.cfg.ScreenFPS,
			"screen":     itoa(h.cfg.ScreenW) + "x" + itoa(h.cfg.ScreenH),
			"jpeg_q":     h.cfg.Quality,
			"record_dir": h.cfg.RecordDir,
			"screen_on":  h.cfg.ScreenFPS > 0,
		},
	}
}

// depthEnabled / irEnabled：采集端是否启用了深度 / 红外。
// 两路各跑独立 pipeline，可以同时开（早期「共用 IR 传感器只能二选一」的结论是错的）。
// 采集端还没上报时按「开」处理。
func (h *Hub) depthEnabled() bool { return h.relayFlag("depth_enabled") }
func (h *Hub) irEnabled() bool    { return h.relayFlag("ir_enabled") }

func (h *Hub) relayFlag(key string) bool {
	h.mu.Lock()
	defer h.mu.Unlock()
	if v, ok := h.relayWho[key].(bool); ok {
		return v
	}
	return true
}

func (h *Hub) ConfigSummary() map[string]any {
	return map[string]any{
		"ingest":            h.cfg.Ingest,
		"http":              h.cfg.HTTP,
		"screen_fps":        h.cfg.ScreenFPS,
		"screen":            itoa(h.cfg.ScreenW) + "x" + itoa(h.cfg.ScreenH),
		"jpeg_q":            h.cfg.Quality,
		"record_dir":        h.cfg.RecordDir,
		"screen_on":         h.cfg.ScreenFPS > 0,
		"depth_mm_per_unit": h.cfg.DepthMMPerUnit,
		"boot":              h.boot,
	}
}

// ---- 采集入站 ----

func (h *Hub) runIngest() error {
	ln, err := net.Listen("tcp", h.cfg.Ingest)
	if err != nil {
		return fmt.Errorf("监听采集端口 %s 失败: %w", h.cfg.Ingest, err)
	}
	logf("采集入站已监听 %s（PSK %s）", h.cfg.Ingest, pskState(h.cfg.PSK))
	for {
		c, err := ln.Accept()
		if err != nil {
			logf("accept 失败: %v", err)
			time.Sleep(200 * time.Millisecond)
			continue
		}
		go h.handleConn(c)
	}
}

func pskState(psk string) string {
	if psk == "" {
		return "未设置（跳过鉴权，仅开发用！）"
	}
	return "已设置"
}

// inflate 解开 flags 里带 FlagZlib 的 payload（深度原始流会被树莓派压缩后再发）。
func inflate(b []byte) ([]byte, error) {
	zr, err := zlib.NewReader(bytes.NewReader(b))
	if err != nil {
		return nil, err
	}
	defer zr.Close()
	return io.ReadAll(zr)
}

func (h *Hub) handleConn(c net.Conn) {
	defer c.Close()
	peer := c.RemoteAddr().String()

	// 1) 第一帧必须是 HELLO
	_ = c.SetReadDeadline(time.Now().Add(5 * time.Second))
	hdr, payload, err := ReadFrame(c)
	if err != nil {
		logf("relay %s 读取 HELLO 失败: %v", peer, err)
		return
	}
	if hdr.Type != FrameHello {
		logf("relay %s 首帧不是 HELLO（type=%d），断开", peer, hdr.Type)
		return
	}
	if err := CheckHello(payload, h.cfg.PSK); err != nil {
		logf("relay %s HELLO 校验失败: %v", peer, err)
		return
	}
	var hello struct {
		Who string `json:"who"`
	}
	_ = json.Unmarshal(payload, &hello)
	logf("relay 已连接: %s (who=%q)", peer, hello.Who)

	// 控制通道：relay 用 who="<设备>/control" 连上来，专门收命令（与数据流分开）
	if strings.HasSuffix(hello.Who, "/control") {
		logf("control: 控制通道已连接 %s (who=%q)", peer, hello.Who)
		h.control.attach(c, hello.Who, peer)
		h.control.run(c)
		return
	}

	h.mu.Lock()
	if hello.Who != "" {
		h.relayWho["who"] = hello.Who
	}
	h.relayWho["peer"] = peer
	h.relayWho["connected_at"] = time.Now().Format(time.RFC3339)
	h.mu.Unlock()

	// 2) 收流
	var gotColor, gotDepth, gotIr bool
	for {
		_ = c.SetReadDeadline(time.Now().Add(time.Duration(h.cfg.ReadMs) * time.Millisecond))
		hdr, payload, err := ReadFrame(c)
		if err != nil {
			logf("relay %s 断开: %v", peer, err)
			break
		}
		atomic.AddUint64(&h._ingressN, uint64(len(payload))+HeaderLen)
		atomic.AddUint64(&h.ingressTotal, uint64(len(payload))+HeaderLen)
		h.maybeRollIngress()

		switch hdr.Type {
		case FrameColorJPEG:
			if !gotColor {
				gotColor = true
				h.mu.Lock()
				h.colorConns++
				h.lastColorConn = time.Now()
				h.mu.Unlock()
			}
			h.color.SetLastTs(hdr.TsUs)
			h.color.Publish(payload, map[string]any{
				"size":    itoa(int(hdr.Width)) + "x" + itoa(int(hdr.Height)),
				"format":  "MJPEG(原生透传)",
				"bytes":   len(payload),
				"seq_src": hdr.Seq,
			})

		case FrameDepthY16:
			if !gotDepth {
				gotDepth = true
				h.mu.Lock()
				h.depthConns++
				h.mu.Unlock()
			}
			h.depth.SetLastTs(hdr.TsUs)
			raw := payload
			if hdr.Flags&FlagZlib != 0 {
				inf, err := inflate(payload)
				if err != nil {
					logf("depth: zlib 解压失败: %v", err)
					continue
				}
				raw = inf
			}
			// 留一份最近的原始深度，供网页鼠标取深度值（/api/depth_at）
			h.depthAt.Update(raw, int(hdr.Width), int(hdr.Height), hdr.TsUs)
			jpg, meta := h.depthR.Render(raw, int(hdr.Width), int(hdr.Height))
			if jpg == nil {
				continue
			}
			meta["format"] = "Y16→伪彩"
			meta["raw_bytes"] = len(raw)
			meta["wire_bytes"] = len(payload)
			if len(payload) > 0 {
				meta["zlib_ratio"] = round1(float64(len(raw)) / float64(len(payload)))
			}
			meta["seq_src"] = hdr.Seq
			h.depth.Publish(jpg, meta)

		case FrameIrY16:
			if !gotIr {
				gotIr = true
				h.mu.Lock()
				h.irConns++
				h.mu.Unlock()
			}
			h.ir.SetLastTs(hdr.TsUs)
			raw := payload
			if hdr.Flags&FlagZlib != 0 {
				inf, err := inflate(payload)
				if err != nil {
					logf("ir: zlib 解压失败: %v", err)
					continue
				}
				raw = inf
			}
			jpg, meta := h.irR.Render(raw, int(hdr.Width), int(hdr.Height))
			if jpg == nil {
				continue
			}
			meta["wire_bytes"] = len(payload)
			if len(payload) > 0 {
				meta["zlib_ratio"] = round1(float64(len(raw)) / float64(len(payload)))
			}
			meta["seq_src"] = hdr.Seq
			h.ir.Publish(jpg, meta)

		case FrameStatus:
			var st map[string]any
			if err := json.Unmarshal(payload, &st); err == nil {
				h.mu.Lock()
				for k, v := range st {
					h.relayWho[k] = v
				}
				h.mu.Unlock()
			}
		}
	}

	h.mu.Lock()
	h.reduceConn(gotColor, gotDepth, gotIr)
	h.mu.Unlock()
	h.color.Fail("relay 连接已断开")
	h.depth.Fail("relay 连接已断开")
	h.ir.Fail("relay 连接已断开")
}

func (h *Hub) reduceConn(color, depth, ir bool) {
	if color && h.colorConns > 0 {
		h.colorConns--
	}
	if depth && h.depthConns > 0 {
		h.depthConns--
	}
	if ir && h.irConns > 0 {
		h.irConns--
	}
}

func (h *Hub) maybeRollIngress() {
	h.mu.Lock()
	now := time.Now()
	if el := now.Sub(h._ingressT0); el >= 2*time.Second {
		h.ingressBps = float64(h._ingressN) / el.Seconds()
		h._ingressN = 0
		h._ingressT0 = now
	}
	h.mu.Unlock()
}

// ---- 看门狗 ----

func (h *Hub) runWatchdog() {
	t := time.NewTicker(1 * time.Second)
	defer t.Stop()
	d := time.Duration(h.cfg.StaleMs) * time.Millisecond
	for range t.C {
		feeds := []*Feed{h.color}
		// 深度/红外各跑独立 pipeline，可同时在线：只盯采集端真正启用的那几路。
		// 关掉的那一路永远不会有帧，若也纳入看门狗会一直"停滞"，日志被刷屏。
		if h.depthEnabled() {
			feeds = append(feeds, h.depth)
		}
		if h.irEnabled() {
			feeds = append(feeds, h.ir)
		}
		for _, f := range feeds {
			if f.MarkStale(d) {
				logf("%s 数据流停滞（超过 %v 没有新帧）", f.Name, d)
			}
		}
	}
}

// ---- 录制接口（v1 预留，不实现）----

// Recorder 是给 v2 预留的落盘接口。v1 不注册任何实现（老板确认先不做）。
type Recorder interface {
	OnColorJPEG(jpeg []byte, tsUs uint64)
	OnDepthY16(raw []byte, w, h int, tsUs uint64)
}

// ---- main ----

var startTime = time.Now()

func main() {
	log.SetFlags(log.LstdFlags | log.Lmicroseconds)
	cfg := loadConfig()
	logf("ob-hub 启动 pid=%d", os.Getpid())
	logf("配置: ingest=%s http=%s screen=%dx%d@%dfps q=%d",
		cfg.Ingest, cfg.HTTP, cfg.ScreenW, cfg.ScreenH, cfg.ScreenFPS, cfg.Quality)
	if cfg.RecordDir != "" {
		logf("注意: OB_HUB_RECORD_DIR 已设置但 v1 未实现录制（仅预留接口）")
	}

	h := NewHub(cfg)

	go func() {
		if err := h.runIngest(); err != nil {
			logf("致命: %v", err)
			os.Exit(1)
		}
	}()

	go h.runWatchdog()
	if cfg.ScreenFPS > 0 {
		go NewScreenBuilder(h, cfg.ScreenW, cfg.ScreenH, cfg.ScreenFPS, cfg.Quality+6).Run()
	} else {
		logf("整屏合成已停用（OB_HUB_SCREEN_FPS=0）；要恢复把它设成 >0 即可")
	}

	mux := http.NewServeMux()
	NewServer(h).routes(mux)
	srv := &http.Server{
		Addr:              cfg.HTTP,
		Handler:           mux,
		ReadHeaderTimeout: 5 * time.Second,
		// MJPEG 是长连接，不能设 WriteTimeout
	}

	go func() {
		logf("HTTP 已监听 %s", cfg.HTTP)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			logf("致命: HTTP 服务失败: %v", err)
			os.Exit(1)
		}
	}()

	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGINT, syscall.SIGTERM)
	s := <-sig
	logf("收到 %v，退出", s)
	_ = srv.Close()
}
