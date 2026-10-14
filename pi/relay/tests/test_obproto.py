"""OBF1 协议一致性测试（Python 侧）。

把字节布局钉死：与 Go 侧 proto_test.go::TestProtoLayout 用**完全相同的期望字节**，
任一边改了偏移都会在这里挂掉。
"""

import os
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import obproto  # noqa: E402


def test_layout_matches_spec():
    b = obproto.pack(0, 0x01020304, 0x0102030405060708, 0x0A0B, 0x0C0D, 2, b"")
    assert len(b) == obproto.HEADER_LEN == 28, len(b)
    assert b[0:4] == b"OBF1", b[0:4]
    assert b[4] == 0                       # type
    assert b[5] == 0                       # flags
    assert b[6:10] == bytes([1, 2, 3, 4])            # seq
    assert b[10:18] == bytes([1, 2, 3, 4, 5, 6, 7, 8])  # ts_us
    assert b[18:20] == bytes([0x0A, 0x0B])           # width
    assert b[20:22] == bytes([0x0C, 0x0D])           # height
    assert b[22] == 2                      # format
    assert b[23] == 0                      # rsvd
    assert b[24:28] == bytes([0, 0, 0, 0])           # payload_len


def test_roundtrip_header():
    b = obproto.pack(1, 123, 456789, 320, 240, 1, b"")
    h = obproto.unpack_header(b)
    assert h["type"] == 1
    assert h["seq"] == 123
    assert h["ts_us"] == 456789
    assert h["width"] == 320
    assert h["height"] == 240
    assert h["format"] == 1
    assert h["paylen"] == 0


def test_payload_roundtrip():
    payload = bytes(range(256)) * 4
    b = obproto.pack(0, 7, 8, 640, 480, 0, payload)
    h = obproto.unpack_header(b)
    assert h["paylen"] == len(payload)
    assert b[obproto.HEADER_LEN:] == payload


def test_rejects_bad_magic():
    b = bytearray(obproto.pack(0, 0, 0, 0, 0, 0, b""))
    b[0:4] = b"XXXX"
    try:
        obproto.unpack_header(bytes(b))
    except ValueError:
        return
    raise AssertionError("错误魔数应当报错")


def test_rejects_short():
    try:
        obproto.unpack_header(b"OBF1")
    except ValueError:
        return
    raise AssertionError("短头应当报错")


def test_hello_has_mac():
    p = obproto.hello_payload("secret", "unit-test")
    import json
    d = json.loads(p.decode())
    assert d["who"] == "unit-test"
    assert len(d["mac"]) == 32
    assert d["ts_us"] > 0


def test_payload_too_large():
    try:
        obproto.pack(0, 0, 0, 0, 0, 0, b"x" * (obproto.MAX_PAYLOAD + 1))
    except ValueError:
        return
    raise AssertionError("超大 payload 应当被拒")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print("PASS %s" % fn.__name__)
        except Exception as ex:
            failed += 1
            print("FAIL %s: %s" % (fn.__name__, ex))
    print("\n%d/%d 通过" % (len(fns) - failed, len(fns)))
    sys.exit(1 if failed else 0)