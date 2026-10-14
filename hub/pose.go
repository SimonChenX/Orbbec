package main

// 姿态（骨骼）状态。
//
// hub 自己**不做**推理：骨骼分析由独立的 ob-pose 进程（Python + mediapipe）算完，
// POST 到 /api/pose；这里只保留「最新一帧」并转发给前端画骨架。
// 这样做好处：推理吃 CPU 且可能崩，放独立进程里互不影响；不想用的时候停掉它即可，
// hub 和其余功能照常。
//
// 前端拿到的是归一化坐标（0~1，相对彩色画面），直接按显示尺寸换算即可。

import (
	"sync"
	"time"
)

type PoseState struct {
	mu      sync.Mutex
	body    []byte // 原样保存 ob-pose POST 上来的 JSON
	tsUs    uint64 // 源帧时间戳（用于和画面同步）
	recvAt  time.Time
	updates uint64
	lastErr string
}

func NewPoseState() *PoseState { return &PoseState{} }

// Update 记录一帧姿态。body 必须已经是合法 JSON（由调用方保证）。
func (p *PoseState) Update(body []byte, tsUs uint64) {
	p.mu.Lock()
	p.body = body
	p.tsUs = tsUs
	p.recvAt = time.Now()
	p.updates++
	p.lastErr = ""
	p.mu.Unlock()
}

// Fail 记一次失败（不影响已有结果，仅用于状态展示）。
func (p *PoseState) Fail(msg string) {
	p.mu.Lock()
	p.lastErr = msg
	p.mu.Unlock()
}

// Get 取最新姿态。超过 ttl 没有更新就返回 false（前端据此隐藏骨架，避免"假骨架"停在那）。
func (p *PoseState) Get(ttl time.Duration) ([]byte, uint64, bool) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.body == nil || time.Since(p.recvAt) > ttl {
		return nil, 0, false
	}
	return p.body, p.tsUs, true
}

func (p *PoseState) Status() map[string]any {
	p.mu.Lock()
	defer p.mu.Unlock()
	out := map[string]any{
		"updates": p.updates,
		"online":  p.body != nil && time.Since(p.recvAt) <= 3*time.Second,
	}
	if p.body != nil {
		out["age_ms"] = time.Since(p.recvAt).Milliseconds()
		out["ts_us"] = p.tsUs
	}
	if p.lastErr != "" {
		out["last_error"] = p.lastErr
	}
	return out
}

// DepthSample 保存「最近一帧原始深度」，供网页鼠标取深度值（/api/depth_at）。
type DepthSample struct {
	mu   sync.Mutex
	raw  []byte
	w, h int
	tsUs uint64
	at   time.Time
}

func NewDepthSample() *DepthSample { return &DepthSample{} }

func (d *DepthSample) Update(raw []byte, w, h int, tsUs uint64) {
	d.mu.Lock()
	d.raw = raw
	d.w, d.h = w, h
	d.tsUs = tsUs
	d.at = time.Now()
	d.mu.Unlock()
}

// At 取某个像素的深度。返回 0 表示该点无效/超范围。
// mm 由 value*scale 得到（本机型 scale=1.0，即值本身就是毫米）。
func (d *DepthSample) At(x, y int) (value int, mm float64, w, h int, tsUs uint64, at time.Time, ok bool) {
	d.mu.Lock()
	defer d.mu.Unlock()
	if d.raw == nil || x < 0 || y < 0 || x >= d.w || y >= d.h {
		return 0, 0, d.w, d.h, d.tsUs, d.at, false
	}
	i := (y*d.w + x) * 2
	if i+1 >= len(d.raw) {
		return 0, 0, d.w, d.h, d.tsUs, d.at, false
	}
	v := int(uint16(d.raw[i]) | uint16(d.raw[i+1])<<8)
	return v, float64(v) * depthScale(), d.w, d.h, d.tsUs, d.at, true
}

// depthScale 深度原始值 → 毫米的换算系数（见 depthMMPerUnit 的说明）。
func depthScale() float64 { return depthMMPerUnit }
