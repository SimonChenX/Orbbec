package main

// Feed —— 线程安全的「最新帧槽」。
//
// 生产者 Publish 覆盖最新帧并唤醒所有等待者；消费者 Wait 拿下一帧。
// 只要最新帧、不排队：慢客户端自动丢帧，永远不会把生产者拖住。
//
// 唤醒用「广播 channel」（close 唤醒所有等待者，再换一个新的），
// 而不是 sync.Cond —— 后者没有带超时的等待。

import (
	"sync"
	"time"
)

type Feed struct {
	Name string

	mu     sync.Mutex
	notify chan struct{}

	seq     uint64
	data    []byte
	meta    map[string]any
	state   string
	errMsg  string
	clients int

	// 统计
	nFrames     uint64
	nBytes      uint64
	fps         float64
	_fpsN       uint64
	_fpsT0      time.Time
	lastTsUs    uint64
	lastArrival time.Time
}

func NewFeed(name string) *Feed {
	return &Feed{
		Name:   name,
		state:  "waiting",
		meta:   map[string]any{},
		notify: make(chan struct{}),
		_fpsT0: time.Now(),
	}
}

// Publish 发布一帧。meta 为 nil 时保留上一帧的 meta。
func (f *Feed) Publish(data []byte, meta map[string]any) {
	f.mu.Lock()
	f.data = data
	f.seq++
	f.nFrames++
	f.nBytes += uint64(len(data))
	if meta != nil {
		f.meta = meta
	}
	now := time.Now()
	f.lastArrival = now
	if el := now.Sub(f._fpsT0); el >= 2*time.Second {
		f.fps = float64(f._fpsN) / el.Seconds()
		f._fpsN = 0
		f._fpsT0 = now
	}
	f._fpsN++
	f.state = "ok"
	f.errMsg = ""
	close(f.notify)
	f.notify = make(chan struct{})
	f.mu.Unlock()
}

// Fail 标记该路出错（保留最后一帧，前端可显示冻结画面 + 告警）。
func (f *Feed) Fail(msg string) {
	f.mu.Lock()
	f.state = "error"
	f.errMsg = msg
	close(f.notify)
	f.notify = make(chan struct{})
	f.mu.Unlock()
}

// MarkStale 超过 staleAfter 没来帧就把状态标成 stale（由看门狗 goroutine 调用）。
func (f *Feed) MarkStale(staleAfter time.Duration) bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.state == "ok" && !f.lastArrival.IsZero() && time.Since(f.lastArrival) > staleAfter {
		f.state = "stale"
		f.errMsg = "超过 " + staleAfter.String() + " 没有新帧"
		return true
	}
	return false
}

// Wait 等到 seq != last（或超时）。返回当前 seq 与数据。
func (f *Feed) Wait(last uint64, timeout time.Duration) (uint64, []byte) {
	f.mu.Lock()
	if f.seq != last {
		seq, data := f.seq, f.data
		f.mu.Unlock()
		return seq, data
	}
	ch := f.notify // 下一次 Publish 会 close 它
	f.mu.Unlock()

	select {
	case <-ch:
	case <-time.After(timeout):
	}

	f.mu.Lock()
	seq, data := f.seq, f.data
	f.mu.Unlock()
	return seq, data
}

// Snapshot 取当前帧（不阻塞）。
func (f *Feed) Snapshot() (uint64, []byte, map[string]any) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.seq, f.data, f.meta
}

func (f *Feed) SetLastTs(tsUs uint64) {
	f.mu.Lock()
	f.lastTsUs = tsUs
	f.mu.Unlock()
}

func (f *Feed) AddClient() {
	f.mu.Lock()
	f.clients++
	f.mu.Unlock()
}

func (f *Feed) RemoveClient() {
	f.mu.Lock()
	f.clients--
	if f.clients < 0 {
		f.clients = 0
	}
	f.mu.Unlock()
}

// Status 返回给 /api/status 用的快照。
func (f *Feed) Status() map[string]any {
	f.mu.Lock()
	defer f.mu.Unlock()
	out := map[string]any{
		"state":   f.state,
		"fps":     round1(f.fps),
		"seq":     f.seq,
		"clients": f.clients,
		"meta":    f.meta,
		"frames":  f.nFrames,
		"bytes":   f.nBytes,
		"error":   nil,
	}
	if f.errMsg != "" {
		out["error"] = f.errMsg
	}
	if f.lastTsUs > 0 {
		out["lag_ms"] = round1(float64(time.Now().UnixMicro()-int64(f.lastTsUs)) / 1000.0)
	}
	if !f.lastArrival.IsZero() {
		out["age_ms"] = round1(float64(time.Since(f.lastArrival).Microseconds()) / 1000.0)
	}
	return out
}

// depthMMPerUnit 深度原始值 → 毫米的换算系数（可在 /etc/default/ob-hub 用
// OB_HUB_DEPTH_MM_PER_UNIT 覆盖）。
//
// ⚠️ 为什么默认 10 而不是 SDK 说的 1：
//
//	SDK 文档写「mm = 像素值 × valueScale」，本机 valueScale 报 1.0，但那样
//	「人 61mm、背景 238mm」——人离相机 6cm 不可能。用**距离比值**判定：
//	原始最远/最近 = 238/61 = 3.9，与真实场景（人约 0.8m、背景约 3.1m）的比值一致
//	⇒ 原始单位是**厘米**，×10 得毫米。若现场用卷尺量出来不符，改这个系数即可。
var depthMMPerUnit = 10.0

func round1(v float64) float64 {
	return float64(int(v*10+0.5)) / 10
}
