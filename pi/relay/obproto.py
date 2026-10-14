"""OBF1 帧协议 —— 与后端 /home/OrbbecPiApi/proto.go 必须逐字节一致。

头 28 字节定长，大端：

    偏移  长度  字段
    0     4     magic       "OBF1"
    4     1     type        0=彩色JPEG 1=深度RAW16 2=状态JSON 3=HELLO 4=控制JSON 5=红外RAW16
    5     1     flags       bit0 = payload 经 zlib 压缩
    6     4     seq
    10    8     ts_us
    18    2     width
    20    2     height
    22    1     format      0=MJPEG 1=Y16 2=JSON 3=BIN
    23    1     rsvd
    24    4     payload_len
    28    N     payload

改动这里必须同步改 proto.go（或反之），并跑两边的测试。
"""

import hashlib
import hmac
import json
import struct
import time

MAGIC = b"OBF1"
HEADER_LEN = 28
MAX_PAYLOAD = 8 << 20

TYPE_COLOR = 0
TYPE_DEPTH = 1
TYPE_STATUS = 2
TYPE_HELLO = 3
TYPE_CONTROL = 4
TYPE_IR = 5

FMT_MJPEG = 0
FMT_Y16 = 1
FMT_JSON = 2
FMT_BIN = 3

FLAG_ZLIB = 1 << 0

# 头字段顺序必须与 proto.go 的偏移定义逐字节一致：
# magic(4s) type(B) flags(B) seq(I) ts_us(Q) width(H) height(H) format(B) rsvd(B) paylen(I) = 28 字节
_HDR = struct.Struct(">4sBBIQHHBBI")


def now_us() -> int:
    return int(time.time() * 1_000_000)


def pack(type_, seq, ts_us, width, height, fmt, payload, flags=0) -> bytes:
    if len(payload) > MAX_PAYLOAD:
        raise ValueError("payload 过大: %d" % len(payload))
    return _HDR.pack(MAGIC, type_ & 0xFF, flags & 0xFF, seq & 0xFFFFFFFF, ts_us & 0xFFFFFFFFFFFFFFFF,
                     width & 0xFFFF, height & 0xFFFF, fmt & 0xFF, 0, len(payload)) + payload


def unpack_header(buf: bytes) -> dict:
    if len(buf) < HEADER_LEN:
        raise ValueError("头长度不足")
    magic, type_, flags, seq, ts_us, w, h, fmt, _rsvd, paylen = _HDR.unpack(buf[:HEADER_LEN])
    if magic != MAGIC:
        raise ValueError("魔数错误: %r" % magic)
    if paylen > MAX_PAYLOAD:
        raise ValueError("payload 过长: %d" % paylen)
    return {"type": type_, "flags": flags, "seq": seq, "ts_us": ts_us,
            "width": w, "height": h, "format": fmt, "paylen": paylen}


def hello_payload(psk: str, who: str) -> bytes:
    """对 8 字节大端微秒时间戳做 HMAC-SHA256，取前 16 字节。psk 为空则不带 mac。"""
    ts = now_us()
    raw = struct.pack(">Q", ts)
    mac = ""
    if psk:
        mac = hmac.new(psk.encode("utf-8"), raw, hashlib.sha256).hexdigest()[:32]
    return json.dumps({"ts_us": ts, "mac": mac, "who": who}).encode("utf-8")