package main

import (
	"bytes"
	"encoding/binary"
	"testing"
)

func TestProtoRoundTrip(t *testing.T) {
	h := Header{Type: FrameColorJPEG, Seq: 12345, TsUs: 1712345678901234, Width: 640, Height: 480, Format: FmtMJPEG, PayLen: 4}
	raw := h.Pack()
	if len(raw) != HeaderLen {
		t.Fatalf("头长度应为 %d，实际 %d", HeaderLen, len(raw))
	}
	got, err := ParseHeader(raw)
	if err != nil {
		t.Fatalf("解析失败: %v", err)
	}
	if got != (Header{Type: h.Type, Seq: h.Seq, TsUs: h.TsUs, Width: h.Width, Height: h.Height, Format: h.Format, PayLen: h.PayLen}) {
		t.Fatalf("往返不一致: %+v != %+v", got, h)
	}
	if string(raw[0:4]) != ProtoMagic {
		t.Fatalf("魔数错误: %q", raw[0:4])
	}
}

func TestProtoRejectsShortHeader(t *testing.T) {
	if _, err := ParseHeader([]byte("OBF")); err == nil {
		t.Fatal("短头应当报错")
	}
}

func TestProtoRejectsBadMagic(t *testing.T) {
	raw := Header{Type: FrameDepthY16, PayLen: 2}.Pack()
	copy(raw[0:4], "XXXX")
	if _, err := ParseHeader(raw); err == nil {
		t.Fatal("错误魔数应当报错")
	}
}

// TestProtoLayout 把字节布局钉死：与 Python 侧 test_obproto.py::test_layout_matches_spec
// 用完全相同的期望字节，任一边改了偏移都会在这里挂掉。
func TestProtoLayout(t *testing.T) {
	raw := Header{Type: 0, Seq: 0x01020304, TsUs: 0x0102030405060708,
		Width: 0x0A0B, Height: 0x0C0D, Format: 2, PayLen: 0}.Pack()
	if len(raw) != 28 {
		t.Fatalf("头应为 28 字节，实际 %d", len(raw))
	}
	want := []byte{
		0x4F, 0x42, 0x46, 0x31, // "OBF1"
		0x00,                   // type
		0x00,                   // flags
		0x01, 0x02, 0x03, 0x04, // seq
		0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, // ts_us
		0x0A, 0x0B, // width
		0x0C, 0x0D, // height
		0x02,                   // format
		0x00,                   // rsvd
		0x00, 0x00, 0x00, 0x00, // payload_len
	}
	for i := range want {
		if raw[i] != want[i] {
			t.Fatalf("字节 %d 不符: got 0x%02X want 0x%02X", i, raw[i], want[i])
		}
	}
}

func TestProtoRejectsHugePayload(t *testing.T) {
	raw := Header{Type: FrameDepthY16}.Pack()
	binary.BigEndian.PutUint32(raw[24:28], MaxPayload+1)
	if _, err := ParseHeader(raw); err == nil {
		t.Fatal("超大 payload 应当被拒")
	}
}

func TestReadFrame(t *testing.T) {
	payload := []byte{1, 2, 3, 4, 5}
	h := Header{Type: FrameStatus, Seq: 7, Width: 1, Height: 1, Format: FmtJSON, PayLen: uint32(len(payload))}
	var buf bytes.Buffer
	buf.Write(h.Pack())
	buf.Write(payload)
	gh, gp, err := ReadFrame(&buf)
	if err != nil {
		t.Fatalf("ReadFrame 失败: %v", err)
	}
	if gh.Seq != 7 || !bytes.Equal(gp, payload) {
		t.Fatalf("读回的帧不对: %+v %v", gh, gp)
	}
}

func TestHelloAuth(t *testing.T) {
	psk := "s3cret"
	payload, err := HelloPayload(psk, "test-relay")
	if err != nil {
		t.Fatalf("生成 HELLO 失败: %v", err)
	}
	if err := CheckHello(payload, psk); err != nil {
		t.Fatalf("正确 PSK 应当通过: %v", err)
	}
	if err := CheckHello(payload, "wrong"); err == nil {
		t.Fatal("错误 PSK 应当被拒")
	}
	if err := CheckHello(payload, ""); err != nil {
		t.Fatalf("空 PSK 表示不校验: %v", err)
	}
}

func TestDepthRender(t *testing.T) {
	w, h := 64, 48
	raw := make([]byte, w*h*2)
	wantValid := 0
	for i := 0; i < w*h; i++ {
		v := uint16(100 + (i*7)%400) // 100~499
		if i%10 == 0 {
			v = 0 // 无效像素
		} else {
			wantValid++
		}
		binary.LittleEndian.PutUint16(raw[i*2:], v)
	}
	r := NewDepthRenderer(70)
	jpg, meta := r.Render(raw, w, h)
	if jpg == nil {
		t.Fatal("渲染结果为空")
	}
	if len(jpg) < 100 {
		t.Fatalf("JPEG 太小: %d", len(jpg))
	}
	if meta["n_valid"].(int) != wantValid {
		t.Fatalf("有效像素统计不对: got %v want %d", meta["n_valid"], wantValid)
	}
	if meta["size"].(string) != "64x48" {
		t.Fatalf("size 不对: %v", meta["size"])
	}
}

func TestDepthRenderAllInvalid(t *testing.T) {
	w, h := 16, 16
	raw := make([]byte, w*h*2)
	r := NewDepthRenderer(70)
	jpg, meta := r.Render(raw, w, h)
	if jpg == nil {
		t.Fatal("全无效也应当出图")
	}
	if meta["n_valid"].(int) != 0 {
		t.Fatal("应当 0 个有效像素")
	}
}
