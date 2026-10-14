package main

// OBF1 帧协议 —— 树莓派 relay 与服务器 hub 之间的线路格式。
//
// 一条 TCP 连接承载一路流；所有字段**大端**；头 28 字节定长，后跟 payload。
//
//	偏移  长度  字段        说明
//	0     4     magic       "OBF1"
//	4     1     type        0=彩色JPEG 1=深度RAW16 2=状态JSON 3=HELLO
//	5     1     flags       bit0 = payload 经 zlib 压缩
//	6     4     seq         帧序号（每流独立自增）
//	10    8     ts_us       采集时刻（Unix 微秒）
//	18    2     width
//	20    2     height
//	22    1     format      0=MJPEG 1=Y16 2=JSON 3=BIN
//	23    1     rsvd
//	24    4     payload_len
//	28    N     payload
//
// 语义：latest-frame。接收端只保留最新帧，落后就丢，绝不排队——
// 这样延迟恒定，不会越积越多。

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"time"
)

const (
	ProtoMagic = "OBF1"
	HeaderLen  = 28

	// 帧类型
	FrameColorJPEG = 0
	FrameDepthY16  = 1
	FrameStatus    = 2
	FrameHello     = 3
	FrameControl   = 4 // 服务器 → 树莓派：命令（JSON）
	FrameIrY16     = 5 // 红外原始 10bit（16bit 容器）：近红外灰度，非热成像

	// payload 格式
	FmtMJPEG = 0
	FmtY16   = 1
	FmtJSON  = 2
	FmtBin   = 3

	FlagZlib = 1 << 0

	// 单帧上限：8MB。彩色 640x480 JPEG 约 51KB，深度 320x240 Y16 = 150KB，
	// 留足余量同时防止垃圾数据打爆内存。
	MaxPayload = 8 << 20

	// HELLO 时间戳允许的偏差，防重放
	helloSkew = 300 * time.Second
)

// Header 是 OBF1 定长头。
type Header struct {
	Type   byte
	Flags  byte
	Seq    uint32
	TsUs   uint64
	Width  uint16
	Height uint16
	Format byte
	PayLen uint32
}

func (h Header) Pack() []byte {
	b := make([]byte, HeaderLen)
	copy(b[0:4], ProtoMagic)
	b[4] = h.Type
	b[5] = h.Flags
	binary.BigEndian.PutUint32(b[6:10], h.Seq)
	binary.BigEndian.PutUint64(b[10:18], h.TsUs)
	binary.BigEndian.PutUint16(b[18:20], h.Width)
	binary.BigEndian.PutUint16(b[20:22], h.Height)
	b[22] = h.Format
	b[23] = 0
	binary.BigEndian.PutUint32(b[24:28], h.PayLen)
	return b
}

// ParseHeader 解析 28 字节头；长度不足或魔数不对返回错误。
func ParseHeader(b []byte) (Header, error) {
	var h Header
	if len(b) < HeaderLen {
		return h, errors.New("header too short")
	}
	if string(b[0:4]) != ProtoMagic {
		return h, fmt.Errorf("bad magic %q", b[0:4])
	}
	h.Type = b[4]
	h.Flags = b[5]
	h.Seq = binary.BigEndian.Uint32(b[6:10])
	h.TsUs = binary.BigEndian.Uint64(b[10:18])
	h.Width = binary.BigEndian.Uint16(b[18:20])
	h.Height = binary.BigEndian.Uint16(b[20:22])
	h.Format = b[22]
	h.PayLen = binary.BigEndian.Uint32(b[24:28])
	if h.PayLen > MaxPayload {
		return h, fmt.Errorf("payload too large: %d", h.PayLen)
	}
	return h, nil
}

// ReadFrame 从连接读一整帧（头 + payload）。
func ReadFrame(r io.Reader) (Header, []byte, error) {
	hb := make([]byte, HeaderLen)
	if _, err := io.ReadFull(r, hb); err != nil {
		return Header{}, nil, err
	}
	h, err := ParseHeader(hb)
	if err != nil {
		return h, nil, err
	}
	payload := make([]byte, h.PayLen)
	if _, err := io.ReadFull(r, payload); err != nil {
		return h, nil, err
	}
	return h, payload, nil
}

// ---- HELLO 鉴权（预共享密钥 HMAC-SHA256）----

type helloMsg struct {
	TsUs uint64 `json:"ts_us"`
	Mac  string `json:"mac"`
	Who  string `json:"who,omitempty"`
}

// HelloPayload 由 relay 侧生成：对 8 字节大端时间戳做 HMAC，取前 16 字节。
func HelloPayload(psk, who string) ([]byte, error) {
	ts := uint64(time.Now().UnixMicro())
	raw := make([]byte, 8)
	binary.BigEndian.PutUint64(raw, ts)
	m := hmac.New(sha256.New, []byte(psk))
	m.Write(raw)
	msg := helloMsg{TsUs: ts, Mac: hex.EncodeToString(m.Sum(nil)[:16]), Who: who}
	return json.Marshal(msg)
}

// CheckHello 校验 relay 的 HELLO。psk 为空时跳过校验（仅开发用）。
func CheckHello(payload []byte, psk string) error {
	if psk == "" {
		return nil
	}
	var msg helloMsg
	if err := json.Unmarshal(payload, &msg); err != nil {
		return fmt.Errorf("hello 不是合法 JSON: %w", err)
	}
	raw := make([]byte, 8)
	binary.BigEndian.PutUint64(raw, msg.TsUs)
	m := hmac.New(sha256.New, []byte(psk))
	m.Write(raw)
	want := hex.EncodeToString(m.Sum(nil)[:16])
	got, err := hex.DecodeString(msg.Mac)
	if err != nil || !hmac.Equal([]byte(want), []byte(hex.EncodeToString(got))) {
		return errors.New("hello 鉴权失败")
	}
	skew := time.Since(time.UnixMicro(int64(msg.TsUs)))
	if skew < 0 {
		skew = -skew
	}
	if skew > helloSkew {
		return fmt.Errorf("hello 时间戳偏差过大: %v", skew)
	}
	return nil
}
