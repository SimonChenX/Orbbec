package main

// 控制通道（服务器 → 树莓派下发命令）
//
// 为什么反向复用 relay 主动建立的连接：树莓派在防火墙后面，
// 服务器连不进去（那边只放行 SSH），而 relay 是主动外连的；
// 命令顺着这条已建立的 TCP 连接下发最省事，也不用在服务器上存 SSH 凭据。
//
// 协议：hub 发 TYPE_CONTROL（JSON 命令），relay 回 TYPE_STATUS（JSON 回执，带 ack=命令 id）。

import (
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"sync"
	"time"
)

type Control struct {
	mu       sync.Mutex
	conn     net.Conn
	writeMu  sync.Mutex
	peer     string
	who      string
	pending  map[uint32]chan map[string]any
	nextID   uint32
	lastSeen time.Time
	cmds     uint64
	errs     uint64
}

var errNoControl = errors.New("采集端控制通道未连接（relay 没上线或版本过旧）")

func NewControl() *Control {
	return &Control{pending: map[uint32]chan map[string]any{}}
}

// attach 注册一条控制连接（由 handleConn 在收到 who 以 /control 结尾的 HELLO 时调用）。
func (c *Control) attach(conn net.Conn, who, peer string) {
	c.mu.Lock()
	if c.conn != nil {
		_ = c.conn.Close() // 旧连接作废
	}
	c.conn, c.who, c.peer = conn, who, peer
	c.lastSeen = time.Now()
	c.mu.Unlock()
}

func (c *Control) detach(conn net.Conn) {
	c.mu.Lock()
	if c.conn == conn {
		c.conn = nil
	}
	c.mu.Unlock()
}

func (c *Control) Online() bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.conn != nil
}

func (c *Control) Status() map[string]any {
	c.mu.Lock()
	defer c.mu.Unlock()
	out := map[string]any{"online": c.conn != nil, "who": c.who, "peer": c.peer,
		"cmds": c.cmds, "errors": c.errs}
	if c.conn != nil {
		out["last_seen_ms"] = float64(time.Since(c.lastSeen).Microseconds()) / 1000.0
	}
	return out
}

// Do 下发一条命令并等回执。
func (c *Control) Do(cmd map[string]any, timeout time.Duration) (map[string]any, error) {
	c.mu.Lock()
	conn := c.conn
	if conn == nil {
		c.mu.Unlock()
		return nil, errNoControl
	}
	c.nextID++
	id := c.nextID
	cmd["id"] = id
	ch := make(chan map[string]any, 1)
	c.pending[id] = ch
	c.mu.Unlock()

	defer func() {
		c.mu.Lock()
		delete(c.pending, id)
		c.mu.Unlock()
	}()

	body, err := json.Marshal(cmd)
	if err != nil {
		return nil, err
	}
	frame := Header{Type: FrameControl, Seq: id, TsUs: uint64(time.Now().UnixMicro()),
		Format: FmtJSON, PayLen: uint32(len(body))}.Pack()

	c.writeMu.Lock()
	_, err = conn.Write(append(frame, body...))
	c.writeMu.Unlock()
	if err != nil {
		c.mu.Lock()
		c.errs++
		c.mu.Unlock()
		return nil, fmt.Errorf("下发失败: %w", err)
	}

	select {
	case resp := <-ch:
		c.mu.Lock()
		c.cmds++
		c.mu.Unlock()
		return resp, nil
	case <-time.After(timeout):
		c.mu.Lock()
		c.errs++
		c.mu.Unlock()
		return nil, fmt.Errorf("等待回执超时（%v）", timeout)
	}
}

// run 读回执循环（在控制连接上阻塞）。
//
// 控制通道平时是静默的，所以两端都要保活：
//   - relay 每 10 秒发一个 keepalive（TYPE_STATUS，{"keepalive":true}）
//   - 这里 90 秒收不到任何帧就判定连接已死并断开，让它重连
func (c *Control) run(conn net.Conn) {
	defer c.detach(conn)
	for {
		_ = conn.SetReadDeadline(time.Now().Add(90 * time.Second))
		hdr, payload, err := ReadFrame(conn)
		if err != nil {
			logf("control: 连接结束: %v", err)
			return
		}
		c.mu.Lock()
		c.lastSeen = time.Now()
		c.mu.Unlock()

		if hdr.Type != FrameStatus {
			continue
		}
		var resp map[string]any
		if err := json.Unmarshal(payload, &resp); err != nil {
			logf("control: 回执解析失败: %v", err)
			continue
		}
		ackF, ok := resp["ack"].(float64)
		if !ok {
			continue
		}
		id := uint32(ackF)
		c.mu.Lock()
		ch := c.pending[id]
		c.mu.Unlock()
		if ch != nil {
			select {
			case ch <- resp:
			default:
			}
		}
	}
}
