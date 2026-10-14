#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Orbbec Astra Pro Plus —— IR 流采集 (ctypes 直调 Orbbec SDK v1)
IR 与彩色是同一场景的两张亮度图,可用于交叉验证方向/对齐。
用法: cd /opt/orbbec/Example/bin && LD_LIBRARY_PATH=/opt/orbbec/SDK/lib python3 ob_ir.py [帧数]
产物: /opt/orbbec/cap/ir_<idx>.png  (+ .bin)
"""
import ctypes as C
import os, sys, time, zlib, struct

LIB = os.environ.get("OB_LIB", "/opt/orbbec/SDK/lib/libOrbbecSDK.so")
lib = C.CDLL(LIB)

SENSOR = {"DEPTH": 3, "IR": 1, "COLOR": 2}
FMT = {0: "YUYV", 2: "UYVY", 5: "MJPG", 8: "Y16", 9: "Y8", 10: "Y10",
       11: "Y11", 12: "Y12", 13: "GRAY", 18: "Y16", 22: "RGB", 23: "BGR"}


def bind(name, restype, argtypes):
    f = getattr(lib, name); f.restype = restype; f.argtypes = argtypes; return f


P = C.POINTER(C.c_void_p)
for _n, _r, _a in [
    ("ob_create_pipeline", C.c_void_p, [P]),
    ("ob_create_config", C.c_void_p, [P]),
    ("ob_pipeline_get_stream_profile_list", C.c_void_p, [C.c_void_p, C.c_int, P]),
    ("ob_stream_profile_list_count", C.c_uint32, [C.c_void_p, P]),
    ("ob_stream_profile_list_get_profile", C.c_void_p, [C.c_void_p, C.c_int, P]),
    ("ob_video_stream_profile_width", C.c_uint32, [C.c_void_p, P]),
    ("ob_video_stream_profile_height", C.c_uint32, [C.c_void_p, P]),
    ("ob_stream_profile_format", C.c_int, [C.c_void_p, P]),
    ("ob_config_enable_stream", None, [C.c_void_p, C.c_void_p, P]),
    ("ob_pipeline_start_with_config", None, [C.c_void_p, C.c_void_p, P]),
    ("ob_pipeline_stop", None, [C.c_void_p, P]),
    ("ob_pipeline_wait_for_frameset", C.c_void_p, [C.c_void_p, C.c_uint32, P]),
    ("ob_frameset_get_frame", C.c_void_p, [C.c_void_p, C.c_int, P]),
    ("ob_video_frame_width", C.c_uint32, [C.c_void_p, P]),
    ("ob_video_frame_height", C.c_uint32, [C.c_void_p, P]),
    ("ob_frame_format", C.c_int, [C.c_void_p, P]),
    ("ob_frame_data", C.c_void_p, [C.c_void_p, P]),
    ("ob_frame_data_size", C.c_uint32, [C.c_void_p, P]),
    ("ob_frame_index", C.c_uint64, [C.c_void_p, P]),
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
        print(f"[错误] {what}: {s(lib.ob_error_message)} (where {s(lib.ob_error_function)})", flush=True)
        lib.ob_delete_error(err.value); err.value = None
        if fatal:
            sys.exit(2)
        return False
    return True


def png_chunk(tag, data):
    return (struct.pack(">I", len(data)) + tag + data +
            struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff))


def write_png_gray(path, w, h, gray):
    raw = bytearray()
    for y in range(h):
        raw.append(0); raw += gray[y * w:(y + 1) * w]
    open(path, "wb").write(
        b"\x89PNG\r\n\x1a\n" +
        png_chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0)) +
        png_chunk(b"IDAT", zlib.compress(bytes(raw), 6)) +
        png_chunk(b"IEND", b""))


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    outdir = "/opt/orbbec/cap"
    os.makedirs(outdir, exist_ok=True)

    e = new_err(); pipeline = lib.ob_create_pipeline(C.byref(e)); chk(e, "create_pipeline")
    e = new_err(); cfg = lib.ob_create_config(C.byref(e)); chk(e, "create_config")

    e = new_err()
    pl = lib.ob_pipeline_get_stream_profile_list(pipeline, SENSOR["IR"], C.byref(e))
    chk(e, "ir profile list")
    e = new_err(); cnt = lib.ob_stream_profile_list_count(pl, C.byref(e)); chk(e, "count")
    print(f"IR profile 共 {cnt} 个", flush=True)

    pick = None
    for i in range(cnt):
        e = new_err(); pr = lib.ob_stream_profile_list_get_profile(pl, i, C.byref(e))
        if not chk(e, f"profile{i}", fatal=False):
            continue
        e = new_err(); w = lib.ob_video_stream_profile_width(pr, C.byref(e))
        e = new_err(); h = lib.ob_video_stream_profile_height(pr, C.byref(e))
        e = new_err(); f = lib.ob_stream_profile_format(pr, C.byref(e))
        print(f"  #{i}: {w}x{h} {FMT.get(f, f)}", flush=True)
        if (w, h) == (640, 480) and pick is None:
            pick = pr
    if pick is None:
        print("[致命] 没有 640x480 IR profile", flush=True); sys.exit(3)

    e = new_err(); lib.ob_config_enable_stream(cfg, pick, C.byref(e)); chk(e, "enable IR")
    e = new_err(); lib.ob_pipeline_start_with_config(pipeline, cfg, C.byref(e)); chk(e, "start")
    print("[OK] IR 流已启动", flush=True)

    got = 0; t0 = time.time()
    while got < n and time.time() - t0 < 30:
        e = new_err(); fs = lib.ob_pipeline_wait_for_frameset(pipeline, 2000, C.byref(e))
        if not chk(e, "wait", fatal=False) or not fs:
            continue
        e = new_err(); fr = lib.ob_frameset_get_frame(fs, SENSOR["IR"], C.byref(e))
        if not chk(e, "get IR frame", fatal=False) or not fr:
            e = new_err(); lib.ob_delete_frame(fs, C.byref(e)); continue
        e = new_err(); w = lib.ob_video_frame_width(fr, C.byref(e))
        e = new_err(); h = lib.ob_video_frame_height(fr, C.byref(e))
        e = new_err(); f = lib.ob_frame_format(fr, C.byref(e))
        e = new_err(); sz = lib.ob_frame_data_size(fr, C.byref(e))
        e = new_err(); idx = lib.ob_frame_index(fr, C.byref(e))
        e = new_err(); ptr = lib.ob_frame_data(fr, C.byref(e))
        if not chk(e, "data", fatal=False) or not ptr:
            e = new_err(); lib.ob_delete_frame(fs, C.byref(e)); continue

        buf = C.string_at(ptr, sz)
        open(os.path.join(outdir, f"ir_{idx}.bin"), "wb").write(buf)
        px = w * h
        if len(buf) >= px * 2:                       # 16bit 容器
            a = struct.unpack_from("<%dH" % px, buf, 0)
            mx = max(a) or 1
            gray = bytes(min(255, int(v * 255.0 / mx)) for v in a)
        else:                                        # 紧凑打包,退回 8bit 视图
            gray = bytes(buf[:px])
            gray = bytes(min(255, v) for v in gray)
        write_png_gray(os.path.join(outdir, f"ir_{idx}.png"), w, h, gray)
        print(f"  帧 {idx}: {w}x{h} {FMT.get(f, f)} bytes={sz} -> ir_{idx}.png", flush=True)
        got += 1
        e = new_err(); lib.ob_delete_frame(fr, C.byref(e))
        e = new_err(); lib.ob_delete_frame(fs, C.byref(e))

    e = new_err(); lib.ob_pipeline_stop(pipeline, C.byref(e))
    e = new_err(); lib.ob_delete_config(cfg, C.byref(e))
    e = new_err(); lib.ob_delete_pipeline(pipeline, C.byref(e))
    if not got:
        sys.exit(4)


main()