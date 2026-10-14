package main

// 深度伪彩渲染：原始 uint16(Y16) → 伪彩 JPEG。
//
// 与树莓派上 viewer.py 的观感保持一致：同一套 turbo 调色板，
// 5%~95% 分位做量程（带平滑，避免逐帧跳变），无效像素 = 黑。
// 用整数索引 + 查表，不用浮点乘法链，Go 里 320x240 约 1ms。

import (
	"bytes"
	"encoding/binary"
	"image"
	"image/color"
	"image/jpeg"
	"sort"
	"strconv"
	"sync"
)

// turbo 调色板控制点（与 viewer.py 的 _TURBO 一致）
var turboStops = [][4]float64{
	{0.00, 48, 18, 59}, {0.10, 70, 107, 227}, {0.20, 33, 181, 231},
	{0.30, 40, 222, 155}, {0.40, 126, 244, 73}, {0.50, 212, 226, 44},
	{0.60, 251, 172, 38}, {0.70, 250, 105, 16}, {0.80, 228, 45, 4},
	{0.90, 171, 11, 3}, {1.00, 122, 4, 3},
}

var lut [256]color.RGBA

func init() {
	for i := 0; i < 256; i++ {
		t := float64(i) / 255.0
		lut[i] = color.RGBA{
			R: uint8(stopInterp(t, 1)),
			G: uint8(stopInterp(t, 2)),
			B: uint8(stopInterp(t, 3)),
			A: 255,
		}
	}
}

func stopInterp(t float64, ch int) float64 {
	for i := 0; i < len(turboStops)-1; i++ {
		a, b := turboStops[i], turboStops[i+1]
		if t <= b[0] {
			span := b[0] - a[0]
			if span <= 0 {
				return a[ch]
			}
			k := (t - a[0]) / span
			return a[ch] + k*(b[ch]-a[ch])
		}
	}
	return turboStops[len(turboStops)-1][ch]
}

// DepthRenderer 保存量程平滑状态。
type DepthRenderer struct {
	mu      sync.Mutex
	lo, hi  float64
	inited  bool
	alpha   float64
	quality int
}

func NewDepthRenderer(quality int) *DepthRenderer {
	return &DepthRenderer{alpha: 0.12, quality: quality}
}

// Render 返回伪彩 JPEG 与统计。
func (d *DepthRenderer) Render(raw []byte, w, h int) ([]byte, map[string]any) {
	n := w * h
	if len(raw) < n*2 {
		return nil, map[string]any{"error": "payload 太小"}
	}

	// 1) 解析 uint16 小端 + 抽样求分位
	samples := make([]uint16, 0, n/16+1)
	var nValid, minV, maxV uint16
	haveMin := false
	centerSum, centerN := 0, 0
	cy, cx := h/2, w/2
	for i := 0; i < n; i++ {
		v := binary.LittleEndian.Uint16(raw[i*2 : i*2+2])
		if v == 0 {
			continue
		}
		nValid++
		if !haveMin || v < minV {
			minV = v
		}
		if !haveMin || v > maxV {
			maxV = v
		}
		haveMin = true
		if i%16 == 0 {
			samples = append(samples, v)
		}
		y, x := i/w, i%w
		if y >= cy-3 && y <= cy+3 && x >= cx-3 && x <= cx+3 {
			centerSum += int(v)
			centerN++
		}
	}
	if nValid == 0 {
		img := image.NewRGBA(image.Rect(0, 0, w, h))
		jpg := encodeJPEG(img, d.quality)
		return jpg, map[string]any{"valid_pct": 0.0, "n_valid": 0, "min_mm": 0, "max_mm": 0, "center_mm": 0}
	}

	sort.Slice(samples, func(i, j int) bool { return samples[i] < samples[j] })
	lo := float64(percentile(samples, 0.05))
	hi := float64(percentile(samples, 0.95))
	if hi-lo < 40 {
		hi = lo + 40
	}

	d.mu.Lock()
	if !d.inited {
		d.lo, d.hi, d.inited = lo, hi, true
	} else {
		d.lo += d.alpha * (lo - d.lo)
		d.hi += d.alpha * (hi - d.hi)
		if d.hi-d.lo < 40 {
			d.hi = d.lo + 40
		}
	}
	rlo, rhi := d.lo, d.hi
	d.mu.Unlock()

	// 2) 上色
	k := 254.0 / (rhi - rlo)
	img := image.NewRGBA(image.Rect(0, 0, w, h))
	pix := img.Pix
	for i := 0; i < n; i++ {
		v := binary.LittleEndian.Uint16(raw[i*2 : i*2+2])
		if v == 0 {
			continue // 无效像素保持 (0,0,0) 且 alpha=0；下面统一补 alpha
		}
		idx := int((float64(v) - rlo) * k)
		if idx < 0 {
			idx = 0
		} else if idx > 254 {
			idx = 254
		}
		c := lut[idx+1]
		o := i * 4
		pix[o], pix[o+1], pix[o+2], pix[o+3] = c.R, c.G, c.B, 255
	}
	// 无效像素补成不透明黑
	for i := 0; i < n; i++ {
		if binary.LittleEndian.Uint16(raw[i*2:i*2+2]) == 0 {
			pix[i*4+3] = 255
		}
	}

	center := 0.0
	if centerN > 0 {
		center = float64(centerSum) / float64(centerN)
	}
	meta := map[string]any{
		"valid_pct": round1(100.0 * float64(nValid) / float64(n)),
		"n_valid":   int(nValid),
		"min_mm":    round1(float64(minV) * depthMMPerUnit),
		"max_mm":    round1(float64(maxV) * depthMMPerUnit),
		"center_mm": round1(center * depthMMPerUnit),
		"range_mm":  []float64{round1(rlo * depthMMPerUnit), round1(rhi * depthMMPerUnit)},
		"unit_mm":   depthMMPerUnit,
		"size":      strconv.Itoa(w) + "x" + strconv.Itoa(h),
	}
	return encodeJPEG(img, d.quality), meta
}

func percentile(sorted []uint16, q float64) uint16 {
	if len(sorted) == 0 {
		return 0
	}
	pos := q * float64(len(sorted)-1)
	i := int(pos)
	if i >= len(sorted)-1 {
		return sorted[len(sorted)-1]
	}
	frac := pos - float64(i)
	return uint16(float64(sorted[i]) + frac*(float64(sorted[i+1])-float64(sorted[i])))
}

func encodeJPEG(img image.Image, quality int) []byte {
	var buf bytes.Buffer
	_ = jpeg.Encode(&buf, img, &jpeg.Options{Quality: quality})
	return buf.Bytes()
}
