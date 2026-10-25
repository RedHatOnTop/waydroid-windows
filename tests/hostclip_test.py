"""wlproxy.Conn: an image-only host selection is asked for before the HWC sees it and set as Android's clip through
compat.hostclip, once per image; a selection the HWC reads as text is left to it."""
import importlib.util
import io
import os
import struct
import time

os.environ["WLPROXY_TASKD"] = "0"
os.environ["WLPROXY_HOSTCLIP"] = "1"
spec = importlib.util.spec_from_file_location("wlproxy", os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "wlproxy.py"))
wp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wp)
wp.LOG = io.StringIO()


class Up:
    def __init__(self):
        self.data = b""
        self.fds = []

    def sendmsg(self, bufs, anc):
        for _level, _type, fds in anc:
            self.fds += [os.dup(fd) for fd in fds]
        self.data += b"".join(bufs)
        return sum(map(len, bufs))

    def sendall(self, data):
        self.data += data


class Run:
    def __init__(self):
        self.calls = []

    def __call__(self, args, input=None, capture_output=False, timeout=None):
        self.calls.append((args, input))
        return type("Done", (), {"returncode": 0, "stdout": b"Result: null\n", "stderr": b""})()


def msg(oid, opcode, fmt="", *args):
    body = struct.pack("<" + fmt, *args) if fmt else b""
    return struct.pack("<II", oid, ((8 + len(body)) << 16) | opcode) + body


def wl_string(s):
    raw = s.encode() + b"\0"
    return struct.pack("<I", len(raw)) + raw + b"\0" * (wp.pad4(len(raw)) - len(raw))


def offer(c, oid, *types):
    c.feed("s2c", msg(DEVICE, 0, "I", oid) + b"".join(
        struct.pack("<II", oid, ((8 + len(wl_string(t))) << 16) | 0) + wl_string(t) for t in types))


def requests(raw):
    out, i = [], 0
    while i < len(raw):
        oid, so = struct.unpack_from("<II", raw, i)
        out.append((oid, so & 0xFFFF, raw[i + 8:i + (so >> 16)]))
        i += so >> 16
    return out


DEVICE, OFFER, OFFER2, OFFER3 = 7, 0xFF000010, 0xFF000011, 0xFF000012
PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 300
wp.TASKS = object()
run = wp.subprocess.run = Run()

c = wp.Conn(1)
c.objs[DEVICE] = "wl_data_device"
c.up = Up()
offer(c, OFFER, "image/png", "text/html")
selection = msg(DEVICE, 5, "I", OFFER)
assert c.feed("s2c", selection) == selection, "the selection still reaches the HWC"
((oid, op, body),) = requests(c.up.data)
assert (oid, op, body) == (OFFER, 1, wl_string("image/png")), "receive(image/png) went up first"
assert len(c.up.fds) == 1
os.write(c.up.fds[0], PNG)
os.close(c.up.fds[0])
for _ in range(50):
    if run.calls:
        break
    time.sleep(0.02)
((args, data),) = run.calls
assert args[-1] == ("content write --uri content://compat.hostclip/incoming && "
                    "content call --uri content://compat.hostclip --method set --arg image/png"), args
assert data == PNG

# the same image when the keyboard comes back is asked for again but not set again
c.up = Up()
offer(c, OFFER2, "image/png")
c.feed("s2c", msg(DEVICE, 5, "I", OFFER2))
os.write(c.up.fds[0], PNG)
os.close(c.up.fds[0])
time.sleep(0.3)
assert len(run.calls) == 1

# text goes the HWC's way; after it, the same image is set again
c.up = Up()
offer(c, OFFER3, "image/png", "text/plain;charset=utf-8")
c.feed("s2c", msg(DEVICE, 5, "I", OFFER3))
assert c.up.data == b"" and c.clip_digest is None
c.feed("s2c", msg(DEVICE, 5, "I", 0))
assert c.up.data == b""
offer(c, OFFER, "image/png")
c.feed("s2c", msg(DEVICE, 5, "I", OFFER))
os.write(c.up.fds[0], PNG)
os.close(c.up.fds[0])
time.sleep(0.3)
assert len(run.calls) == 2
print("ALL OK")
