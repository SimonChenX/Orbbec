package main

// 红外（IR）渲染。
//
// ⚠️ 重要：这台 Astra Pro Plus 的「红外」是 850nm 近红外**补光相机**，拍的是反射率，
//    不是热成像 —— **没有温度信息**，不能用来判断温度。渲染只做「灰度 + 分位拉伸」。
//    （要测温度得另配热像仪，如 MLX90640 / FLIR。）
//
// 数据：SDK 的 IR 流是 10bit 灰度，装在 16bit 容器里（0~1023）。
// 量程用 2%~98% 分位并做时间平滑，避免画面忽明忽暗地闪。
// 分位用 1024 桶直方图求（别排序 30 万个值 —— 那样只能跑到 ~20fps 且白烧 CPU）。

import (
	"image"
	"strconv"
)

type IRRenderer struct {
	quality int
	lo, hi  uint16
	inited  bool
}

func NewIRRenderer(quality int) *IRRenderer { return &IRRenderer{quality: quality} }

func (r *IRRenderer) Render(raw []byte, w, h int) ([]byte, map[string]any) {
	n := w * h
	if w <= 0 || h <= 0 || len(raw) < n*2 {
		return nil, nil
	}

	buf := make([]uint16, n)
	var hist [1024]int32
	var sum uint64
	var mx uint16
	for i := 0; i < n; i++ {
		v := uint16(raw[i*2]) | uint16(raw[i*2+1])<<8
		buf[i] = v
		sum += uint64(v)
		if v > mx {
			mx = v
		}
		b := v
		if b > 1023 { // 10bit 容器，越界值都算到最亮
			b = 1023
		}
		hist[b]++
	}
	if n == 0 || mx == 0 {
		return nil, nil
	}

	rank := func(q float64) uint16 {
		want := int32(q * float64(n))
		if want < 0 {
			want = 0
		}
		var acc int32
		for v := 0; v < 1024; v++ {
			acc += hist[v]
			if acc >= want {
				return uint16(v)
			}
		}
		return 1023
	}
	lo, hi := rank(0.02), rank(0.98)
	if hi <= lo {
		hi = lo + 1
	}
	if r.inited {
		r.lo = uint16(0.7*float64(r.lo) + 0.3*float64(lo))
		r.hi = uint16(0.7*float64(r.hi) + 0.3*float64(hi))
	} else {
		r.lo, r.hi, r.inited = lo, hi, true
	}
	if r.hi <= r.lo {
		r.hi = r.lo + 1
	}

	img := image.NewGray(image.Rect(0, 0, w, h))
	span := float64(r.hi - r.lo)
	hot := 0
	for i := 0; i < n; i++ {
		g := float64(buf[i]-r.lo) / span
		if g < 0 {
			g = 0
		} else if g > 1 {
			g = 1
		}
		img.Pix[i] = uint8(g * 255.0)
		if buf[i] >= r.hi {
			hot++
		}
	}

	meta := map[string]any{
		"format":     "Y10→灰度(近红外, 非热成像)",
		"size":       strconv.Itoa(w) + "x" + strconv.Itoa(h),
		"min":        int(rank(0)),
		"max":        int(mx),
		"p50":        int(rank(0.5)),
		"p95":        int(rank(0.95)),
		"mean":       int(sum / uint64(n)),
		"range_lo":   int(r.lo),
		"range_hi":   int(r.hi),
		"bright_pct": round1(100 * float64(hot) / float64(n)),
		"raw_bytes":  len(raw),
	}
	return encodeJPEG(img, r.quality), meta
}
