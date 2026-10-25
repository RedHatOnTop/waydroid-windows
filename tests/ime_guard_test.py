"""Feed wlproxy.Conn the HWC's message shapes for an IME window and check what it forwards."""
import importlib.util
import io
import os
import socket
import struct
import time

os.environ["WLPROXY_TASKD"] = "0"
spec = importlib.util.spec_from_file_location("wlproxy", os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "wlproxy.py"))
wp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wp)
wp.LOG = io.StringIO()


class FakeTasks:
    def __init__(self, osk):
        self.osk, self.osk_at, self.refreshes = osk, time.monotonic(), 0

    def refresh_osk(self, conn):
        self.refreshes += 1


def msg(oid, opcode, fmt="", *args, tail=b""):
    body = (struct.pack("<" + fmt, *args) if fmt else b"") + tail
    return struct.pack("<II", oid, ((8 + len(body)) << 16) | opcode) + body


def string(s):
    b = s.encode() + b"\0"
    return struct.pack("<I", len(b)) + b + b"\0" * (-len(b) % 4)


def messages(raw):
    i = 0
    while i < len(raw):
        oid, so = struct.unpack_from("<II", raw, i)
        yield oid, so & 0xFFFF, raw[i + 8:i + (so >> 16)]
        i += so >> 16


def last_main_attach(raw):
    buf = None
    for oid, op, body in messages(raw):
        if oid == 10 and op == 1:
            buf = struct.unpack_from("<I", body)[0]
        if oid == 10 and op == 6:
            return buf
    raise AssertionError("no main commit")


def requests(raw):
    return [(oid, op, struct.unpack_from("<I", body)[0] if len(body) == 4 else len(body)) for oid, op, body in messages(raw)]


def recv_or_empty(sock):
    try:
        return sock.recv(4096)
    except socket.timeout:
        return b""


OBJS = {2: "wl_compositor", 3: "wl_subcompositor", 4: "xdg_wm_base", 5: "wp_viewporter", 10: "wl_surface",
        11: "xdg_surface", 12: "xdg_toplevel", 13: "wp_viewport", 20: "wl_surface", 21: "wl_subsurface",
        30: "wl_region", 40: "wl_buffer", 41: "wl_buffer", 50: "wp_viewport"}
ALL = (0, 0, 0x7FFFFFFF, 0x7FFFFFFF)


def window(cid, appid):
    c = wp.Conn(cid)
    c.objs.update(OBJS)
    c.feed("c2s", msg(4, 2, "II", 11, 10) + msg(11, 1, "I", 12) + msg(12, 3, tail=string(appid))
           + msg(5, 1, "II", 13, 10) + msg(13, 2, "ii", 1536, 853) + msg(3, 1, "III", 21, 20, 10))
    c.appid[12] = appid
    c.mapped[20] = True
    return c


def layer(h):
    return (msg(5, 1, "II", 50, 20) + msg(50, 2, "ii", 1536, h) + msg(21, 1, "ii", 0, 853 - h)
            + msg(20, 1, "Iii", 41, 0, 0) + msg(20, 6))


first = msg(10, 1, "Iii", 40, 0, 0) + layer(853) + msg(10, 6)
input_frame = msg(30, 2, "iiii", *ALL) + msg(30, 1, "iiii", -15, -15, 1566, 883) + msg(10, 5, "I", 30)

# switch off: hidden even while the layer is keyboard-sized, and its frame callbacks are answered
wp.TASKS = FakeTasks(False)
c = window(1, "waydroid.InputMethod")
client_end, c.down = socket.socketpair()
client_end.settimeout(0.3)
raw = c.feed("c2s", msg(20, 3, "I", 60) + msg(10, 3, "I", 61) + first)
assert last_main_attach(raw) == 0, "switch off: first commit must carry a null attach"
assert last_main_attach(c.feed("c2s", layer(466) + msg(10, 6))) == 0, "switch off: a keyboard-sized layer stays hidden"
time.sleep(0.1)
done = sorted(oid for oid, op, body in messages(client_end.recv(4096)) if op == 0)
assert done == [60, 61], f"hidden IME frame callbacks must be answered, got {done}"
c.feed("c2s", msg(20, 3, "I", 62))
c.feed("s2c", msg(62, 0, "I", 123))
time.sleep(0.1)
assert recv_or_empty(client_end) == b"", "a callback the compositor answered must not be faked"

# switch turns on: the next commit maps with the HWC's own buffer, even with a display-sized layer
wp.TASKS.osk = True
assert last_main_attach(c.feed("c2s", layer(853) + msg(10, 6))) == 40, "switch on: the hidden window must get its buffer back"
assert 10 in c.ime_mapped and 10 not in c.ime_hidden
assert last_main_attach(c.feed("c2s", msg(10, 6))) is None, "a mapped window is left alone"
c.feed("c2s", msg(20, 3, "I", 63))
time.sleep(0.1)
assert recv_or_empty(client_end) == b"", "a mapped IME is left to the compositor"

# switch on: a visible keyboard keeps the HWC's input region
out = requests(c.feed("c2s", input_frame))
assert out == [(30, 2, 16), (30, 1, 16), (10, 5, 30)], f"switch on: input region untouched, got {out}"

# switch off while mapped: emptied region for the window and its layers, restored when back on
wp.TASKS.osk = False
raw = c.feed("c2s", input_frame)
ms = list(messages(raw))
assert requests(raw)[-4:] == [(30, 1, 16), (30, 2, 16), (20, 5, 30), (10, 5, 30)], requests(raw)
assert struct.unpack_from("<iiii", ms[-3][2]) == ALL
assert (20, 5, 30) not in requests(c.feed("c2s", input_frame)), "layer region is set once"
wp.TASKS.osk = True
out = requests(c.feed("c2s", input_frame))
assert (20, 5, 0) in out and out.count((30, 2, 16)) == 1, f"switch back on: layer region restored, got {out}"

# switch unknown (first read pending): hidden, and osk_known maps it once the read says on
wp.TASKS = FakeTasks(None)
wp.TASKS.osk_at = 0
c2 = window(2, "waydroid.InputMethod")
c2.up, server_end = socket.socketpair()
assert last_main_attach(c2.feed("c2s", first)) == 0 and wp.TASKS.refreshes == 1, "unknown switch: hidden, read requested"
wp.TASKS.osk = True
c2.osk_known()
server_end.settimeout(0.3)
got = server_end.recv(4096)
assert [(o, op) for o, op, _ in messages(got)] == [(10, 1), (10, 6)] and struct.unpack_from("<I", got, 8)[0] == 40, requests(got)
assert 10 in c2.ime_mapped

# a stale "on" reading is not trusted: hidden until a fresh read
wp.TASKS = FakeTasks(True)
wp.TASKS.osk_at = time.monotonic() - 5
c5 = window(6, "waydroid.InputMethod")
assert last_main_attach(c5.feed("c2s", first)) == 0 and wp.TASKS.refreshes == 1, "stale on: hidden, read requested"
assert 10 in c5.ime_hidden and 10 not in c5.ime_mapped

# a destroyed buffer is never re-attached
wp.TASKS = FakeTasks(False)
c3 = window(3, "waydroid.InputMethod")
c3.feed("c2s", first)
c3.dead.add(40)
wp.TASKS.osk = True
assert last_main_attach(c3.feed("c2s", layer(466) + msg(10, 6))) == 0, "a destroyed buffer must never be re-attached"

# app windows are never touched
wp.TASKS = FakeTasks(False)
app = window(4, "waydroid.com.example.app")
assert last_main_attach(app.feed("c2s", first)) == 40, "app windows keep their buffer"
assert requests(app.feed("c2s", input_frame)) == [(30, 2, 16), (30, 1, 16), (10, 5, 30)], "app input region untouched"

# no TaskD: old behaviour, nothing hidden
wp.TASKS = None
c4 = window(5, "waydroid.InputMethod")
assert last_main_attach(c4.feed("c2s", first)) == 40, "without TaskD the IME is not hidden"

print("\n".join(l for l in wp.LOG.getvalue().splitlines() if "IME" in l))
print("ALL OK")
