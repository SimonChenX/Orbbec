#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Orbbec Astra Pro Plus —— 深度采集 (ctypes 直调 Orbbec SDK v1 纯 C API)
树莓派零编译、零重负载。

用法:  cd /opt/orbbec/Example/bin && LD_LIBRARY_PATH=/opt/orbbec/SDK/lib python3 ob_depth.py [帧数]
产物:  /opt/orbbec/cap/depth_<idx>.{png,bin}   8bit 伪彩 + 原始 16bit
       /opt/orbbec/cap/summary.json
       /opt/orbbec/cap/profiles.txt            设备支持的流清单
"""
import ctypes as C
import os, sys, time, zlib, struct, json

LIB = os.environ.get("OB_LIB", "/opt/orbbec/SDK/lib/libOrbbecSDK.so")
lib = C.CDLL(LIB)

SENSOR = {"DEPTH": 3, "IR": 1, "COLOR": 2}
FMT = {0: "YUYV", 1: "YUY2", 2: "UYVY", 3: "NV12", 4: "NV21", 5: "MJPG",
       6: "H264", 7: "H265", 8: "Y16", 9: "Y8", 10: "Y10", 11: "Y11",
       12: "Y12", 13: "GRAY", 15: "I420", 18: "Y16", 19: "POINT",
       22: "RGB", 23: "BGR", 24: "Y14", 0xff: "UNKNOWN"}


def bind(name, restype, argtypes):
    f = getattr(lib, name)
    f.restype = restype
    f.argtypes = argtypes
    return f


P = C.POINTER(C.c_void_p)
for _n, _r, _a in [
    ("ob_create_pipeline", C.c_void_p, [P]),
    ("ob_create_config", C.c_void_p, [P]),
    ("ob_pipeline_get_stream_profile_list", C.c_void_p, [C.c_void_p, C.c_int, P]),
    ("ob_stream_profile_list_count", C.c_uint32, [C.c_void_p, P]),
    ("ob_stream_profile_list_get_profile", C.c_void_p, [C.c_void_p, C.c_int, P]),
    ("ob_video_stream_profile_width", C.c_uint32, [C.c_void_p, P]),
    ("ob_video_stream_profile_height", C.c_uint32, [C.c_void_p, P]),
    ("ob_video_stream_profile_fps", C.c_uint32, [C.c_void_p, P]),
    ("ob_stream_profile_format", C.c_int, [C.c_void_p, P]),
    ("ob_config_enable_stream", None, [C.c_void_p, C.c_void_p, P]),
    ("ob_config_set_depth_scale_require", None, [C.c_void_p, C.c_bool, P]),
    ("ob_pipeline_start_with_config", None, [C.c_void_p, C.c_void_p, P]),
    ("ob_pipeline_stop", None, [C.c_void_p, P]),
    ("ob_pipeline_wait_for_frameset", C.c_void_p, [C.c_void_p, C.c_uint32, P]),
    ("ob_frameset_depth_frame", C.c_void_p, [C.c_void_p, P]),
    ("ob_video_frame_width", C.c_uint32, [C.c_void_p, P]),
    ("ob_video_frame_height", C.c_uint32, [C.c_void_p, P]),
    ("ob_depth_frame_get_value_scale", C.c_float, [C.c_void_p, P]),
    ("ob_frame_format", C.c_int, [C.c_void_p, P]),
    ("ob_frame_data", C.c_void_p, [C.c_void_p, P]),
    ("ob_frame_data_size", C.c_uint32, [C.c_void_p, P]),
    ("ob_frame_index", C.c_uint64, [C.c_void_p, P]),
    ("ob_frame_time_stamp", C.c_uint64, [C.c_void_p, P]),
    ("ob_delete_frame", None, [C.c_void_p, P]),
    ("ob_delete_pipeline", None, [C.c_void_p, P]),
    ("ob_delete_config", None, [C.c_void_p, P]),
    ("ob_delete_stream_profile_list", None, [C.c_void_p, P]),
    ("ob_delete_error", None, [C.c_void_p]),
    ("ob_error_message", C.c_char_p, [C.c_void_p]),
    ("ob_error_function", C.c_char_p, [C.c_void_p]),
]:
    bind(_n, _r, _a)


def new_err():
    return C.c_void_p(None)


def chk(err, what, fatal=True):
    if err.value:
        def s(fn):
            v = fn(err.value)
            return v.decode("utf-8", "replace") if v else "?"
        print(f"[错误] {what}: {s(lib.ob_error_message)} "
              f"(where {s(lib.ob_error_function)})", flush=True)
        lib.ob_delete_error(err.value)
        err.value = None
        if fatal:
            sys.exit(2)
        return False
    return True


def png_chunk(tag, data):
    return (struct.pack(">I", len(data)) + tag + data +
            struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff))


def write_png_rgb(path, w, h, rgb):
    raw = bytearray()
    stride = w * 3
    for y in range(h):
        raw.append(0)
        raw += rgb[y * stride:(y + 1) * stride]
    open(path, "wb").write(
        b"\x89PNG\r\n\x1a\n" +
        png_chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) +
        png_chunk(b"IDAT", zlib.compress(bytes(raw), 6)) +
        png_chunk(b"IEND", b""))


def colorize(v):
    """灰度 -> 伪彩 (蓝=近 -> 红=远)"""
    t = v / 255.0
    r = int(255 * max(0.0, min(1.0, 1.5 - abs(4 * t - 3))))
    g = int(255 * max(0.0, min(1.0, 1.5 - abs(4 * t - 2))))
    b = int(255 * max(0.0, min(1.0, 1.5 - abs(4 * t - 1))))
    return bytes((r, g, b))


def main():
    n_frames = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    outdir = "/opt/orbbec/cap"
    os.makedirs(outdir, exist_ok=True)

    # ---------- 探测各 sensor 支持的流 ----------
    probe = lib.ob_create_pipeline(C.byref((e := new_err())))
    chk(e, "ob_create_pipeline")
    lines = ["Orbbec Astra Pro Plus — 设备支持的流 (SDK v1.10.37)",
             "=" * 64]
    for name, sid in SENSOR.items():
        e = new_err()
        lst = lib.ob_pipeline_get_stream_profile_list(probe, sid, C.byref(e))
        if not chk(e, f"profile_list({name})", fatal=False):
            lines.append(f"{name:6s}: 获取失败")
            continue
        e = new_err()
        cnt = lib.ob_stream_profile_list_count(lst, C.byref(e))
        chk(e, f"count({name})", fatal=False)
        items = []
        for i in range(cnt):
            e = new_err()
            pr = lib.ob_stream_profile_list_get_profile(lst, i, C.byref(e))
            if not chk(e, f"profile({name},{i})", fatal=False):
                continue
            e = new_err(); w = lib.ob_video_stream_profile_width(pr, C.byref(e))
            e = new_err(); h = lib.ob_video_stream_profile_height(pr, C.byref(e))
            e = new_err(); fps = lib.ob_video_stream_profile_fps(pr, C.byref(e))
            e = new_err(); fmt = lib.ob_stream_profile_format(pr, C.byref(e))
            items.append(f"{w}x{h}@{fps} {FMT.get(fmt, fmt)}")
        lines.append(f"{name:6s}: {cnt} 个  " + ", ".join(items))
        e = new_err(); lib.ob_delete_stream_profile_list(lst, C.byref(e))
    e = new_err(); lib.ob_delete_pipeline(probe, C.byref(e))
    open(os.path.join(outdir, "profiles.txt"), "w").write("\n".join(lines) + "\n")
    print("\n".join(lines), flush=True)

    # ---------- 只开深度 ----------
    e = new_err(); pipeline = lib.ob_create_pipeline(C.byref(e))
    chk(e, "ob_create_pipeline")
    e = new_err(); cfg = lib.ob_create_config(C.byref(e))
    chk(e, "ob_create_config")
    # 要求直接输出毫米
    e = new_err(); lib.ob_config_set_depth_scale_require(cfg, True, C.byref(e))
    chk(e, "set_depth_scale_require", fatal=False)

    e = new_err()
    dl = lib.ob_pipeline_get_stream_profile_list(pipeline, SENSOR["DEPTH"], C.byref(e))
    chk(e, "depth profile list")
    e = new_err(); dc = lib.ob_stream_profile_list_count(dl, C.byref(e))
    chk(e, "depth count")
    if dc == 0:
        print("[致命] 深度 profile 列表为空", flush=True); sys.exit(3)

    cands = []
    for i in range(dc):
        e = new_err()
        pr = lib.ob_stream_profile_list_get_profile(dl, i, C.byref(e))
        if not chk(e, "get depth profile", fatal=False):
            continue
        e = new_err(); fmt = lib.ob_stream_profile_format(pr, C.byref(e))
        e = new_err(); w = lib.ob_video_stream_profile_width(pr, C.byref(e))
        e = new_err(); h = lib.ob_video_stream_profile_height(pr, C.byref(e))
        cands.append((pr, fmt, w, h, i))
    # 优先 640x480,否则取像素最多的一档
    pick = next((c for c in cands if (c[2], c[3]) == (640, 480)), None)
    if pick is None:
        pick = max(cands, key=lambda c: c[2] * c[3])
    pr, fmt, w, h, i = pick
    print(f"\n选用深度 profile #{i}: {w}x{h} {FMT.get(fmt, fmt)}", flush=True)

    e = new_err(); lib.ob_config_enable_stream(cfg, pr, C.byref(e))
    chk(e, "enable depth stream")
    e = new_err(); lib.ob_pipeline_start_with_config(pipeline, cfg, C.byref(e))
    chk(e, "pipeline start (depth only)")
    print("[OK] 深度流已启动\n", flush=True)

    saved = []
    t0 = time.time()
    got = 0
    while got < n_frames and time.time() - t0 < 30:
        e = new_err()
        fs = lib.ob_pipeline_wait_for_frameset(pipeline, 2000, C.byref(e))
        if not chk(e, "wait_for_frameset", fatal=False) or not fs:
            continue
        e = new_err(); df = lib.ob_frameset_depth_frame(fs, C.byref(e))
        if not chk(e, "depth frame", fatal=False) or not df:
            e = new_err(); lib.ob_delete_frame(fs, C.byref(e)); continue

        e = new_err(); fw = lib.ob_video_frame_width(df, C.byref(e))
        e = new_err(); fh = lib.ob_video_frame_height(df, C.byref(e))
        e = new_err(); ffmt = lib.ob_frame_format(df, C.byref(e))
        e = new_err(); size = lib.ob_frame_data_size(df, C.byref(e))
        e = new_err(); fidx = lib.ob_frame_index(df, C.byref(e))
        e = new_err(); ts = lib.ob_frame_time_stamp(df, C.byref(e))
        e = new_err(); scale = lib.ob_depth_frame_get_value_scale(df, C.byref(e))
        chk(e, "value_scale", fatal=False)
        e = new_err(); ptr = lib.ob_frame_data(df, C.byref(e))
        if not chk(e, "frame data", fatal=False) or not ptr:
            e = new_err(); lib.ob_delete_frame(fs, C.byref(e)); continue

        buf = C.string_at(ptr, size)
        px = fw * fh
        if len(buf) < px * 2:
            e = new_err(); lib.ob_delete_frame(df, C.byref(e))
            e = new_err(); lib.ob_delete_frame(fs, C.byref(e))
            continue

        raw = struct.unpack_from("<%dH" % px, buf, 0)
        mm = [v * scale for v in raw]
        valid = [v for v in mm if v > 0]
        vmin = min(valid) if valid else 0.0
        vmax = max(valid) if valid else 0.0
        vmean = (sum(valid) / len(valid)) if valid else 0.0
        # 直方图(按 500mm 分桶)
        hist = {}
        for v in valid:
            b = int(v // 500) * 500
            hist[b] = hist.get(b, 0) + 1

        base = os.path.join(outdir, f"depth_{fidx}")
        open(base + ".bin", "wb").write(buf[:px * 2])

        if vmax > vmin:
            sc = 255.0 / (vmax - vmin)
            gray = bytes(0 if v <= 0 else max(1, min(255, int((v - vmin) * sc))) for v in mm)
        else:
            gray = bytes(0 for _ in mm)
        rgb = bytearray()
        for g in gray:
            rgb += colorize(g)
        write_png_rgb(base + ".png", fw, fh, bytes(rgb))

        rec = {"frame": fidx, "ts_ms": ts, "w": fw, "h": fh,
               "fmt": FMT.get(ffmt, ffmt), "value_scale": scale, "bytes": size,
               "valid_px": len(valid), "valid_pct": round(100.0 * len(valid) / px, 2),
               "min_mm": round(vmin, 1), "max_mm": round(vmax, 1),
               "mean_mm": round(vmean, 1),
               "hist_mm": {str(k): v for k, v in sorted(hist.items())}}
        saved.append(rec)
        print(f"  帧 {fidx}: {fw}x{fh} scale={scale} | 有效 {len(valid)}/{px} "
              f"({100.0*len(valid)/px:.1f}%) | 深度 {vmin:.0f}~{vmax:.0f}mm 均值 {vmean:.0f}mm",
              flush=True)
        print(f"        直方图(mm): {dict(sorted(hist.items()))}", flush=True)
        got += 1

        e = new_err(); lib.ob_delete_frame(df, C.byref(e))
        e = new_err(); lib.ob_delete_frame(fs, C.byref(e))

    e = new_err(); lib.ob_pipeline_stop(pipeline, C.byref(e))
    e = new_err(); lib.ob_delete_config(cfg, C.byref(e))
    e = new_err(); lib.ob_delete_pipeline(pipeline, C.byref(e))

    json.dump(saved, open(os.path.join(outdir, "summary.json"), "w"),
              indent=2, ensure_ascii=False)
    print("\n摘要 -> " + os.path.join(outdir, "summary.json"), flush=True)
    if not saved:
        sys.exit(4)


main()