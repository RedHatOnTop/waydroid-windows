"""wlproxy.Conn: the answer to a wl_display.sync is followed by events for object 0 that wake an HWC
roundtrip its Wayland thread raced to the callback; frame callbacks are not."""
import importlib.util
import io
import os
import struct
import time

os.environ["WLPROXY_TASKD"] = "0"
spec = importlib.util.spec_from_file_location("wlproxy", os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "wlproxy.py"))
wp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wp)
wp.LOG = io.StringIO()


class Sock:
    def __init__(self):
        self.data = b""

    def sendall(self, data):
        self.data += data


def msg(oid, opcode, fmt="", *args):
    body = struct.pack("<" + fmt, *args) if fmt else b""
    return struct.pack("<II", oid, ((8 + len(body)) << 16) | opcode) + body


SURFACE, FRAME_CB, SYNC_CB = 26, 40, 41
NUDGE = struct.pack("<II", 0, 8 << 16)

c = wp.Conn(1)
c.objs[SURFACE] = "wl_surface"
c.up, c.down = Sock(), Sock()
c.feed("c2s", msg(SURFACE, 3, "I", FRAME_CB) + msg(1, 0, "I", SYNC_CB))
assert c.sync_cbs == {SYNC_CB}

done = msg(FRAME_CB, 0, "I", 1) + msg(1, 1, "I", FRAME_CB)
assert c.feed("s2c", done) == done, "a frame callback's answer passes as it is"
time.sleep(0.15)
assert c.down.data == b"", "and is not followed by a nudge"

done = msg(SYNC_CB, 0, "I", 2) + msg(1, 1, "I", SYNC_CB)
assert c.feed("s2c", done) == done, "the sync answer passes as it is"
time.sleep(0.15)
assert c.down.data == NUDGE * 2, c.down.data
assert c.sync_cbs == set()

# the id of a finished sync reused for a frame callback brings no nudge
c.down.data = b""
c.feed("c2s", msg(SURFACE, 3, "I", SYNC_CB))
c.feed("s2c", msg(SYNC_CB, 0, "I", 3))
time.sleep(0.15)
assert c.down.data == b""
print("ALL OK")
