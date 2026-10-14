package main

// 整屏合成：把彩色 + 深度并排画进一张 800x480 的图，编成 JPEG。
//
// 这是给树莓派那块屏用的「方案 B」：服务器合成好，树莓派只显示一张图，
// 画面更新次数从 ~48 次/秒降到 ~12 次/秒，树莓派的浏览器开销直接减半。
// 版式/叠加信息以后都在服务器改，树莓派完全不用动。

import (
	"image"
	"image/color"
	"image/draw"
	"image/jpeg"
	"math"
	"sync"
	"time"
)

var (
	screenBG     = color.RGBA{11, 13, 16, 255}
	screenPanel  = color.RGBA{18, 22, 27, 255}
	screenBorder = color.RGBA{35, 43, 52, 255}
)

type ScreenBuilder struct {
	hub     *Hub
	W, H    int
	FPS     int
	Quality int
	mu      sync.Mutex
}

func NewScreenBuilder(h *Hub, w, hgt, fps, quality int) *ScreenBuilder {
	return &ScreenBuilder{hub: h, W: w, H: hgt, FPS: fps, Quality: quality}
}

// Run 按 FPS 合成整屏流（自己的 goroutine）。
func (s *ScreenBuilder) Run() {
	interval := time.Second / time.Duration(maxInt(s.FPS, 1))
	tick := time.NewTicker(interval)
	defer tick.Stop()
	fails := 0
	for range tick.C {
		jpg, err := s.build()
		if err != nil {
			fails++
			if fails == 1 || fails%30 == 0 {
				logf("screen: 合成失败(%d): %v", fails, err)
			}
			continue
		}
		fails = 0
		s.hub.screen.Publish(jpg, map[string]any{
			"size": itoa(s.W) + "x" + itoa(s.H),
			"fps":  s.FPS,
		})
	}
}

func (s *ScreenBuilder) build() ([]byte, error) {
	_, colorJPEG, _ := s.hub.color.Snapshot()
	_, depthJPEG, _ := s.hub.depth.Snapshot()
	if colorJPEG == nil && depthJPEG == nil {
		return nil, errNoFrames
	}

	canvas := image.NewRGBA(image.Rect(0, 0, s.W, s.H))
	fillRGBA(canvas, screenBG)

	gap := 8
	pad := 8
	halfW := (s.W - gap - 2*pad) / 2
	boxH := s.H - 2*pad

	left := image.Rect(pad, pad, pad+halfW, pad+boxH)
	right := image.Rect(pad+halfW+gap, pad, s.W-pad, pad+boxH)

	// 先铺面板底与外框
	fillRGBA(canvas, screenPanel) // 整屏底（面板之间不留黑缝）
	drawBorder(canvas, left, screenBorder, 2)
	drawBorder(canvas, right, screenBorder, 2)

	if colorJPEG != nil {
		if img, err := jpeg.Decode(bytesReader(colorJPEG)); err == nil {
			drawFit(canvas, img, inset(left, 1))
		}
	}
	if depthJPEG != nil {
		if img, err := jpeg.Decode(bytesReader(depthJPEG)); err == nil {
			drawFit(canvas, img, inset(right, 1))
		}
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	return encodeJPEG(canvas, s.Quality), nil
}

// ---- 小工具 ----

// drawFit 等比缩放 src 并居中画进 box（双线性，够快也够清晰）。
func drawFit(dst *image.RGBA, src image.Image, box image.Rectangle) {
	sb := src.Bounds()
	sw, sh := sb.Dx(), sb.Dy()
	if sw <= 0 || sh <= 0 {
		return
	}
	bw, bh := box.Dx(), box.Dy()
	if bw <= 0 || bh <= 0 {
		return
	}
	scale := math.Min(float64(bw)/float64(sw), float64(bh)/float64(sh))
	dw := int(float64(sw) * scale)
	dh := int(float64(sh) * scale)
	if dw <= 0 || dh <= 0 {
		return
	}
	ox := box.Min.X + (bw-dw)/2
	oy := box.Min.Y + (bh-dh)/2

	// 统一转成 RGBA 后再做双线性，避免逐点走接口（At() 太慢）
	srgba := toRGBA(src)
	sp := srgba.Pix
	ss := srgba.Stride
	dp := dst.Pix
	ds := dst.Stride

	for y := 0; y < dh; y++ {
		fy := (float64(y)+0.5)/scale - 0.5
		y0 := int(math.Floor(fy))
		wy := fy - float64(y0)
		y0c := clampI(y0, 0, sh-1)
		y1c := clampI(y0+1, 0, sh-1)
		dy := (oy + y) * ds
		for x := 0; x < dw; x++ {
			fx := (float64(x)+0.5)/scale - 0.5
			x0 := int(math.Floor(fx))
			wx := fx - float64(x0)
			x0c := clampI(x0, 0, sw-1)
			x1c := clampI(x0+1, 0, sw-1)

			o00 := y0c*ss + x0c*4
			o01 := y0c*ss + x1c*4
			o10 := y1c*ss + x0c*4
			o11 := y1c*ss + x1c*4
			do := dy + (ox+x)*4
			for c := 0; c < 3; c++ {
				top := float64(sp[o00+c])*(1-wx) + float64(sp[o01+c])*wx
				bot := float64(sp[o10+c])*(1-wx) + float64(sp[o11+c])*wx
				dp[do+c] = uint8(top*(1-wy) + bot*wy)
			}
			dp[do+3] = 255
		}
	}
}

func toRGBA(src image.Image) *image.RGBA {
	if r, ok := src.(*image.RGBA); ok {
		return r
	}
	b := src.Bounds()
	out := image.NewRGBA(image.Rect(0, 0, b.Dx(), b.Dy()))
	draw.Draw(out, out.Bounds(), src, b.Min, draw.Src)
	return out
}

func fillRGBA(img *image.RGBA, c color.RGBA) {
	b := img.Bounds()
	draw.Draw(img, b, &image.Uniform{C: c}, image.Point{}, draw.Src)
}

func drawBorder(img *image.RGBA, r image.Rectangle, c color.RGBA, w int) {
	for i := 0; i < w; i++ {
		rr := image.Rect(r.Min.X-i, r.Min.Y-i, r.Max.X+i, r.Max.Y+i)
		draw.Draw(img, image.Rect(rr.Min.X, rr.Min.Y, rr.Max.X, rr.Min.Y+1), &image.Uniform{C: c}, image.Point{}, draw.Src)
		draw.Draw(img, image.Rect(rr.Min.X, rr.Max.Y-1, rr.Max.X, rr.Max.Y), &image.Uniform{C: c}, image.Point{}, draw.Src)
		draw.Draw(img, image.Rect(rr.Min.X, rr.Min.Y, rr.Min.X+1, rr.Max.Y), &image.Uniform{C: c}, image.Point{}, draw.Src)
		draw.Draw(img, image.Rect(rr.Max.X-1, rr.Min.Y, rr.Max.X, rr.Max.Y), &image.Uniform{C: c}, image.Point{}, draw.Src)
	}
}

func inset(r image.Rectangle, n int) image.Rectangle {
	return image.Rect(r.Min.X+n, r.Min.Y+n, r.Max.X-n, r.Max.Y-n)
}

func clampI(v, lo, hi int) int {
	if v < lo {
		return lo
	}
	if v > hi {
		return hi
	}
	return v
}

func maxInt(a, b int) int {
	if a > b {
		return a
	}
	return b
}
