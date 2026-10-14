package main

// HTTP 接口。全部只监听 127.0.0.1，由 nginx 反代到 camera.internal.jiayouexp.com。

import (
	"encoding/json"
	"io"
	"net"
	"net/http"
	"strconv"
	"strings"
	"time"
)

type Server struct {
	hub *Hub
}

func NewServer(h *Hub) *Server { return &Server{hub: h} }

func (s *Server) routes(mux *http.ServeMux) {
	mux.HandleFunc("/api/status", s.handleStatus)
	mux.HandleFunc("/healthz", s.handleStatus)
	mux.HandleFunc("/api/config", s.handleConfig)

	mux.HandleFunc("/color.mjpg", func(w http.ResponseWriter, r *http.Request) { s.stream(w, r, s.hub.color) })
	mux.HandleFunc("/depth.mjpg", func(w http.ResponseWriter, r *http.Request) { s.stream(w, r, s.hub.depth) })
	mux.HandleFunc("/ir.mjpg", func(w http.ResponseWriter, r *http.Request) { s.stream(w, r, s.hub.ir) })

	mux.HandleFunc("/snapshot/color.jpg", func(w http.ResponseWriter, r *http.Request) { s.snapshot(w, r, s.hub.color) })
	mux.HandleFunc("/snapshot/depth.jpg", func(w http.ResponseWriter, r *http.Request) { s.snapshot(w, r, s.hub.depth) })
	mux.HandleFunc("/snapshot/ir.jpg", func(w http.ResponseWriter, r *http.Request) { s.snapshot(w, r, s.hub.ir) })

	// 鼠标取深度：/api/depth_at?x=&y=（坐标是深度图像素坐标）
	mux.HandleFunc("/api/depth_at", s.handleDepthAt)
	// 骨骼：GET 取最新（前端画骨架），POST 由 ob-pose 写（仅内网）
	mux.HandleFunc("/api/pose", s.handlePose)

	// 整屏合成为可选功能（树莓派改回日历后就不再需要）：OB_HUB_SCREEN_FPS=0 关闭
	if s.hub.cfg.ScreenFPS > 0 {
		mux.HandleFunc("/screen.mjpg", func(w http.ResponseWriter, r *http.Request) { s.stream(w, r, s.hub.screen) })
		mux.HandleFunc("/snapshot/screen.jpg", func(w http.ResponseWriter, r *http.Request) { s.snapshot(w, r, s.hub.screen) })
	}
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(code)
	enc := json.NewEncoder(w)
	enc.SetEscapeHTML(false)
	_ = enc.Encode(v)
}

// 参数读写：GET 读（含采集端当前相机/码流参数），POST 写（转发给采集端执行）。
//
// 写入是敏感操作（会改相机参数、重启采集端），所以限制来源必须是内网地址。
func (s *Server) handleConfig(w http.ResponseWriter, r *http.Request) {
	switch r.Method {
	case http.MethodGet:
		relay, err := s.hub.control.Do(map[string]any{"action": "get_config"}, 6*time.Second)
		out := map[string]any{
			"hub":     s.hub.ConfigSummary(),
			"control": s.hub.control.Status(),
		}
		if err != nil {
			out["ok"] = false
			out["error"] = err.Error()
		} else {
			out["ok"] = true
			out["relay"] = relay
		}
		writeJSON(w, 200, out)

	case http.MethodPost:
		if !isPrivateClient(r) {
			writeJSON(w, 403, map[string]any{"ok": false, "error": "只允许内网地址修改参数"})
			return
		}
		var cmd map[string]any
		if err := json.NewDecoder(r.Body).Decode(&cmd); err != nil {
			writeJSON(w, 400, map[string]any{"ok": false, "error": "请求体不是合法 JSON"})
			return
		}
		if cmd["action"] == nil {
			writeJSON(w, 400, map[string]any{"ok": false, "error": "缺少 action"})
			return
		}
		timeout := 8 * time.Second
		if cmd["action"] == "set_stream" || cmd["action"] == "restart" {
			timeout = 15 * time.Second // 采集端要重启
		}
		resp, err := s.hub.control.Do(cmd, timeout)
		if err != nil {
			writeJSON(w, 503, map[string]any{"ok": false, "error": err.Error()})
			return
		}
		writeJSON(w, 200, resp)

	default:
		writeJSON(w, 405, map[string]any{"ok": false, "error": "只支持 GET / POST"})
	}
}

// isPrivateClient 判断请求来源是不是内网（nginx 反代时用 X-Real-IP）。
func isPrivateClient(r *http.Request) bool {
	ip := r.Header.Get("X-Real-IP")
	if ip == "" {
		ip = r.Header.Get("X-Forwarded-For")
		if i := strings.IndexByte(ip, ','); i >= 0 {
			ip = ip[:i]
		}
	}
	if ip == "" {
		host, _, err := net.SplitHostPort(r.RemoteAddr)
		if err == nil {
			ip = host
		} else {
			ip = r.RemoteAddr
		}
	}
	ip = strings.TrimSpace(ip)
	parsed := net.ParseIP(ip)
	if parsed == nil {
		return false
	}
	return parsed.IsLoopback() || parsed.IsPrivate() || parsed.IsLinkLocalUnicast()
}

// handleDepthAt 返回深度图某个像素的深度值。深度图很小（320x240），
// 前端鼠标一动就问一次完全扛得住；服务端只查内存里的最新一帧原始 uint16。
func (s *Server) handleDepthAt(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	x, err1 := strconv.Atoi(q.Get("x"))
	y, err2 := strconv.Atoi(q.Get("y"))
	if err1 != nil || err2 != nil {
		writeJSON(w, 400, map[string]any{"ok": false, "error": "需要 x / y（整数，深度图像素坐标）"})
		return
	}
	v, mm, dw, dh, tsUs, at, ok := s.hub.depthAt.At(x, y)
	if !ok {
		writeJSON(w, 503, map[string]any{"ok": false, "error": "还没有深度帧，或坐标越界",
			"depth_size": strconv.Itoa(dw) + "x" + strconv.Itoa(dh)})
		return
	}
	writeJSON(w, 200, map[string]any{
		"ok": true,
		"x":  x, "y": y,
		"value":      v, // 原始值（本机型单位就是毫米）
		"mm":         round1(mm),
		"valid":      v > 0, // 0 = 该点无有效深度（黑色区域）
		"depth_size": strconv.Itoa(dw) + "x" + strconv.Itoa(dh),
		"ts_us":      tsUs,
		"age_ms":     time.Since(at).Milliseconds(),
	})
}

// handlePose 骨骼数据中转：ob-pose 进程 POST 推算结果，前端 GET 拿去画骨架。
func (s *Server) handlePose(w http.ResponseWriter, r *http.Request) {
	switch r.Method {
	case http.MethodGet:
		body, tsUs, ok := s.hub.pose.Get(3 * time.Second)
		if !ok {
			writeJSON(w, 200, map[string]any{"ok": true, "present": false,
				"pose": s.hub.pose.Status()})
			return
		}
		w.Header().Set("Content-Type", "application/json; charset=utf-8")
		w.Header().Set("Cache-Control", "no-store")
		w.WriteHeader(200)
		// 原样透传 ob-pose 的 JSON，外面包一层统一信封
		_, _ = w.Write([]byte(`{"ok":true,"present":true,"ts_us":` + strconv.FormatUint(tsUs, 10) + `,"data":`))
		_, _ = w.Write(body)
		_, _ = w.Write([]byte("}"))

	case http.MethodPost:
		if !isPrivateClient(r) {
			writeJSON(w, 403, map[string]any{"ok": false, "error": "只允许内网地址写入姿态数据"})
			return
		}
		var raw json.RawMessage
		if err := json.NewDecoder(io.LimitReader(r.Body, 256<<10)).Decode(&raw); err != nil {
			writeJSON(w, 400, map[string]any{"ok": false, "error": "请求体不是合法 JSON"})
			return
		}
		var meta struct {
			TsUs uint64 `json:"ts_us"`
		}
		_ = json.Unmarshal(raw, &meta)
		s.hub.pose.Update(raw, meta.TsUs)
		writeJSON(w, 200, map[string]any{"ok": true})

	default:
		writeJSON(w, 405, map[string]any{"ok": false, "error": "只支持 GET / POST"})
	}
}

func (s *Server) handleStatus(w http.ResponseWriter, r *http.Request) {
	st := s.hub.Status()
	code := 200
	if !st["ok"].(bool) {
		code = 503
	}
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(code)
	enc := json.NewEncoder(w)
	enc.SetEscapeHTML(false)
	_ = enc.Encode(st)
}

func (s *Server) snapshot(w http.ResponseWriter, r *http.Request, f *Feed) {
	_, data, _ := f.Snapshot()
	if data == nil {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(503)
		_, _ = w.Write([]byte(`{"error":"还没有帧"}`))
		return
	}
	w.Header().Set("Content-Type", "image/jpeg")
	w.Header().Set("Content-Length", strconv.Itoa(len(data)))
	w.Header().Set("Cache-Control", "no-store")
	_, _ = w.Write(data)
}

// stream 推 MJPEG。慢客户端靠 Feed 的「最新帧」语义自动丢帧。
func (s *Server) stream(w http.ResponseWriter, r *http.Request, f *Feed) {
	flusher, ok := w.(http.Flusher)
	if !ok {
		http.Error(w, "streaming unsupported", 500)
		return
	}
	w.Header().Set("Content-Type", "multipart/x-mixed-replace; boundary=--obframe")
	w.Header().Set("Cache-Control", "no-store, no-cache, must-revalidate")
	w.Header().Set("Pragma", "no-cache")
	w.Header().Set("X-Accel-Buffering", "no") // 让 nginx 别缓冲
	w.WriteHeader(200)
	flusher.Flush()

	f.AddClient()
	defer f.RemoveClient()

	var last uint64
	ctx := r.Context()
	for {
		seq, data := f.Wait(last, 3*time.Second)
		if ctx.Err() != nil {
			return
		}
		if seq == last || data == nil {
			continue
		}
		last = seq
		if _, err := w.Write([]byte("\r\n----obframe\r\nContent-Type: image/jpeg\r\nContent-Length: " +
			strconv.Itoa(len(data)) + "\r\n\r\n")); err != nil {
			return
		}
		if _, err := w.Write(data); err != nil {
			return
		}
		if _, err := w.Write([]byte("\r\n")); err != nil {
			return
		}
		flusher.Flush()
	}
}
