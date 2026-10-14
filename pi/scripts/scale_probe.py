#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对照实验:不带 set_depth_scale_require 时的 value_scale,以及同时刻 UVC 彩图"""
import ctypes as C, os, sys, struct, subprocess, time

lib = C.CDLL("/opt/orbbec/SDK/lib/libOrbbecSDK.so")
P = C.POINTER(C.c_void_p)


def bind(n, r, a):
    f = getattr(lib, n); f.restype = r; f.argtypes = a; return f


for n, r, a in [
    ("ob_create_pipeline", C.c_void_p, [P]),
    ("ob_create_config", C.c_void_p, [P]),
    ("ob_pipeline_get_stream_profile_list", C.c_void_p, [C.c_void_p, C.c_int, P]),
    ("ob_stream_profile_list_count", C.c_uint32, [C.c_void_p, P]),
    ("ob_stream_profile_list_get_profile", C.c_void_p, [C.c_void_p, C.c_int, P]),
    ("ob_video_stream_profile_width", C.c_uint32, [C.c_void_p, P]),
    ("ob_video_stream_profile_height", C.c_uint32, [C.c_void_p, P]),
    ("ob_config_enable_stream", None, [C.c_void_p, C.c_void_p, P]),
    ("ob_config_set_depth_scale_require", None, [C.c_void_p, C.c_bool, P]),
    ("ob_pipeline_start_with_config", None, [C.c_void_p, C.c_void_p, P]),
    ("ob_pipeline_stop", None, [C.c_void_p, P]),
    ("ob_pipeline_wait_for_frameset", C.c_void_p, [C.c_void_p, C.c_uint32, P]),
    ("ob_frameset_depth_frame", C.c_void_p, [C.c_void_p, P]),
    ("ob_video_frame_width", C.c_uint32, [C.c_void_p, P]),
    ("ob_video_frame_height", C.c_uint32, [C.c_void_p, P]),
    ("ob_depth_frame_get_value_scale", C.c_float, [C.c_void_p, P]),
    ("ob_frame_data", C.c_void_p, [C.c_void_p, P]),
    ("ob_frame_data_size", C.c_uint32, [C.c_void_p, P]),
    ("ob_delete_frame", None, [C.c_void_p, P]),
    ("ob_delete_pipeline", None, [C.c_void_p, P]),
    ("ob_delete_config", None, [C.c_void_p, P]),
    ("ob_delete_stream_profile_list", None, [C.c_void_p, P]),
    ("ob_delete_error", None, [C.c_void_p]),
    ("ob_error_message", C.c_char_p, [C.c_void_p]),
]:
    bind(n, r, a)


def probe(require_mm):
    log = []
    e = C.c_void_p(None)
    pl = lib.ob_create_pipeline(C.byref(e))
    e = C.c_void_p(None); cfg = lib.ob_create_config(C.byref(e))
    if require_mm:
        e = C.c_void_p(None)
        lib.ob_config_set_depth_scale_require(cfg, True, C.byref(e))
        if e.value:
            log.append("  set_depth_scale_require 报错: " +
                       lib.ob_error_message(e.value).decode()); lib.ob_delete_error(e.value)
        else:
            log.append("  set_depth_scale_require(True) 成功")
    e = C.c_void_p(None)
    dl = lib.ob_pipeline_get_stream_profile_list(pl, 3, C.byref(e))
    # 选 640x480
    e = C.c_void_p(None); cnt = lib.ob_stream_profile_list_count(dl, C.byref(e))
    sel = None
    for i in range(cnt):
        e = C.c_void_p(None)
        pr = lib.ob_stream_profile_list_get_profile(dl, i, C.byref(e))
        e = C.c_void_p(None); w = lib.ob_video_stream_profile_width(pr, C.byref(e))
        e = C.c_void_p(None); h = lib.ob_video_stream_profile_height(pr, C.byref(e))
        if (w, h) == (640, 480):
            sel = pr; break
        sel = sel or pr
    e = C.c_void_p(None); lib.ob_config_enable_stream(cfg, sel, C.byref(e))
    e = C.c_void_p(None); lib.ob_pipeline_start_with_config(pl, cfg, C.byref(e))
    if e.value:
        log.append("  start 失败: " + lib.ob_error_message(e.value).decode())
        lib.ob_delete_error(e.value); return log
    for _ in range(3):
        e = C.c_void_p(None)
        fs = lib.ob_pipeline_wait_for_frameset(pl, 2000, C.byref(e))
        if not fs:
            continue
        e = C.c_void_p(None); df = lib.ob_frameset_depth_frame(fs, C.byref(e))
        if df:
            e = C.c_void_p(None); sc = lib.ob_depth_frame_get_value_scale(df, C.byref(e))
            e = C.c_void_p(None); sz = lib.ob_frame_data_size(df, C.byref(e))
            e = C.c_void_p(None); ptr = lib.ob_frame_data(df, C.byref(e))
            raw = struct.unpack_from("<%dH" % (sz // 2), C.string_at(ptr, sz), 0)
            nz = sorted(v for v in raw if v > 0)
            if nz:
                log.append(f"  value_scale={sc}  原始值(min/中位/max)= "
                           f"{nz[0]}/{nz[len(nz)//2]}/{nz[-1]}  -> 按scale换算(mm)= "
                           f"{nz[0]*sc:.0f}/{nz[len(nz)//2]*sc:.0f}/{nz[-1]*sc:.0f}")
            e = C.c_void_p(None); lib.ob_delete_frame(df, C.byref(e))
        e = C.c_void_p(None); lib.ob_delete_frame(fs, C.byref(e))
        break
    e = C.c_void_p(None); lib.ob_pipeline_stop(pl, C.byref(e))
    e = C.c_void_p(None); lib.ob_delete_config(cfg, C.byref(e))
    e = C.c_void_p(None); lib.ob_delete_pipeline(pl, C.byref(e))
    return log


print("### A) 默认(不要求毫米) ###")
for l in probe(False):
    print(l)
print("### B) 要求输出毫米 ###")
for l in probe(True):
    print(l)

# 同时刻彩色
print("### C) 同时刻 UVC 彩色 ###")
r = subprocess.run(["timeout", "20", "ffmpeg", "-hide_banner", "-loglevel", "error",
                    "-f", "v4l2", "-input_format", "mjpeg", "-video_size", "1280x720",
                    "-i", "/dev/video0", "-frames:v", "1", "-y", "/opt/orbbec/cap/color_now.jpg"],
                   capture_output=True)
print("  ffmpeg rc =", r.returncode, r.stderr.decode()[:200])