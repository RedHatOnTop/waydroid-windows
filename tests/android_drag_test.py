"""wlproxy.Conn: a forwarded press that Android turns into a task drag is handed to the host as a move,
and a click high in the task that makes Android grow it to the display becomes a host maximize."""
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


class FakeTasks:
    display = (1920, 1200)
    density = 225

    def submit(self, key, job):
        pass


class Sock:
    def __init__(self):
        self.data = b""

    def sendall(self, data):
        self.data += data


def msg(oid, opcode, fmt="", *args):
    body = struct.pack("<" + fmt, *args) if fmt else b""
    return struct.pack("<II", oid, ((8 + len(body)) << 16) | opcode) + body


def messages(raw):
    out, i = [], 0
    while i < len(raw):
        oid, so = struct.unpack_from("<II", raw, i)
        out.append((oid, so & 0xFFFF, raw[i + 8:i + (so >> 16)]))
        i += so >> 16
    return out


wp.TASKS = FakeTasks()
SEAT, PTR, TOUCH, MAIN, XS, TOP, LAYER = 4, 5, 6, 10, 34, 12, 36


# live 2026-10-09, Galaxy Store at ORIGIN: layer 0,0 457x762, GEOMETRY 12,12 433x738; its caption
# starts below CAPTION_DP, so a caption press at 242,75 is a client press
def window(ptr_version=7):
    c = wp.Conn(1)
    c.objs.update({SEAT: "wl_seat", PTR: "wl_pointer", TOUCH: "wl_touch", MAIN: "wl_surface",
                   XS: "xdg_surface", TOP: "xdg_toplevel", LAYER: "wl_surface"})
    c.version.update({SEAT: ptr_version, PTR: ptr_version, TOUCH: ptr_version})
    c.appid[TOP] = "waydroid.com.sec.android.app.samsungapps"
    c.main_surface[MAIN] = TOP
    c.toplevel_xdg[TOP] = XS
    c.xdg_surface_of[XS] = MAIN
    c.dest[MAIN] = (1536, 960)
    c.dest[LAYER] = (457, 762)
    c.kids[MAIN] = {LAYER}
    c.parent_of[LAYER] = MAIN
    c.mapped[LAYER] = True
    c.mapped_seq[LAYER] = 1
    c.pos[LAYER] = (0, 0)
    c.task_rect[MAIN] = (16, 16, 557, 939)
    c.geom[MAIN] = (12, 12, 433, 738)
    c.up, c.down = Sock(), Sock()
    c.placed = []
    c.place = lambda main, size, restoring=False, maximized=False: c.placed.append((size, maximized))
    return c


def s2c(c, *msgs):
    return [(o, op) for o, op, _ in messages(c.feed("s2c", b"".join(msgs)))]


def commit(c, layer_pos):
    c.pos[LAYER] = layer_pos
    c.feed("c2s", msg(MAIN, 6))


press = msg(PTR, 3, "IIII", 100, 0, wp.BTN_LEFT, 1)
release = msg(PTR, 3, "IIII", 101, 0, wp.BTN_LEFT, 0)
motion = msg(PTR, 2, "Iii", 0, 250 * 256, 80 * 256)

# a click and a drag inside the app reach Android untouched while the task stays put
c = window()
assert s2c(c, msg(PTR, 0, "IIii", 99, LAYER, 242 * 256, 75 * 256)) == [(PTR, 0)]
assert s2c(c, press) == [(PTR, 3)], "a client press is forwarded"
assert c.gesture["mode"] == "client"
assert s2c(c, motion) == [(PTR, 2)], "its motion is forwarded"
commit(c, (0, 0))
assert s2c(c, release) == [(PTR, 3)], "and its release"
assert c.gesture is None and c.down.data == b"" and c.up.data == b"" and c.placed == []

# Android moving the task under a held client press: release to Android, host move, task back at ORIGIN
c = window()
s2c(c, msg(PTR, 0, "IIii", 99, LAYER, 242 * 256, 75 * 256), press, motion)
commit(c, (15, 4))
sent = messages(c.down.data)
assert [(o, op) for o, op, _ in sent] == [(PTR, 3), (PTR, 5)], f"button release + frame to the app, got {sent}"
assert struct.unpack("<IIII", sent[0][2])[2:] == (wp.BTN_LEFT, 0)
move = messages(c.up.data)
assert move == [(TOP, 5, struct.pack("<II", SEAT, 100))], f"xdg_toplevel.move with the press serial, got {move}"
assert c.placed == [((None, None), False)], c.placed
assert c.gesture["mode"] == "move"
assert s2c(c, motion) == [], "motion after the handover is not the app's"
assert s2c(c, release) == [], "nor is the real release: Android already had one"
assert c.gesture is None
time.sleep(0.5)
assert c.placed == [((None, None), False)] * 2, f"repinned again once Android has read the release, got {c.placed}"
commit(c, (20, 8))
assert len(messages(c.up.data)) == 1, "a later commit starts nothing"

# a v4 pointer has no frame event
c = window(ptr_version=4)
s2c(c, msg(PTR, 0, "IIii", 99, LAYER, 242 * 256, 75 * 256), press)
commit(c, (15, 4))
assert [(o, op) for o, op, _ in messages(c.down.data)] == [(PTR, 3)]

# a maximized window goes back to the full display
c = window()
c.host_max[MAIN] = True
s2c(c, msg(PTR, 0, "IIii", 99, LAYER, 242 * 256, 75 * 256), press)
commit(c, (15, 4))
assert c.placed == [(None, True)], c.placed

# touch: up + frame for that touch point, move with the down serial
c = window()
assert s2c(c, msg(TOUCH, 0, "IIIiii", 200, 0, LAYER, 3, 242 * 256, 75 * 256)) == [(TOUCH, 0)]
assert s2c(c, msg(TOUCH, 2, "Iiii", 0, 3, 250 * 256, 80 * 256)) == [(TOUCH, 2)]
commit(c, (15, 4))
sent = messages(c.down.data)
assert [(o, op) for o, op, _ in sent] == [(TOUCH, 1), (TOUCH, 3)], sent
assert struct.unpack("<IIi", sent[0][2])[2] == 3
assert messages(c.up.data) == [(TOP, 5, struct.pack("<II", SEAT, 200))]
assert s2c(c, msg(TOUCH, 1, "IIi", 201, 0, 3)) == [], "the real up is dropped"

# the caption Notes draws where zone() expects it still becomes a host move on motion
c = window()
s2c(c, msg(PTR, 0, "IIii", 99, LAYER, 242 * 256, 30 * 256))
assert s2c(c, press) == [] and c.gesture["mode"] == "hold"
assert s2c(c, msg(PTR, 2, "Iii", 0, 260 * 256, 30 * 256)) == []
assert messages(c.up.data) == [(TOP, 5, struct.pack("<II", SEAT, 100))]
# a click high in the task can be Galaxy Store's maximize button (live 2026-10-10: at 410,88 in a
# 461x785 window): Android grows the task to the display and the window follows as a host maximize
c = window()
s2c(c, msg(PTR, 0, "IIii", 99, LAYER, 422 * 256, 88 * 256), press)
assert c.gesture["high"]
assert s2c(c, release) == [(PTR, 3)], "the click still reaches Android"
assert MAIN in c.clicked and c.restore[MAIN] == (433, 738)
c.dest[LAYER] = (1536, 960)
commit(c, (0, 0))
assert messages(c.up.data) == [(TOP, 9, b"")], f"set_maximized, got {messages(c.up.data)}"
commit(c, (0, 0))
assert len(messages(c.up.data)) == 1, "asked once"

# the same by touch
c = window()
s2c(c, msg(TOUCH, 0, "IIIiii", 200, 0, LAYER, 3, 422 * 256, 88 * 256), msg(TOUCH, 1, "IIi", 201, 0, 3))
assert MAIN in c.clicked

# lower down a click is the app's, and growing without one is left alone
c = window()
s2c(c, msg(PTR, 0, "IIii", 99, LAYER, 242 * 256, 400 * 256), press, release)
assert not c.gesture and MAIN not in c.clicked
c.dest[LAYER] = (1536, 960)
commit(c, (0, 0))
assert c.up.data == b""

# a caption drag Android took over is a move, not a click
c = window()
s2c(c, msg(PTR, 0, "IIii", 99, LAYER, 242 * 256, 75 * 256), press, motion)
commit(c, (15, 4))
s2c(c, release)
assert MAIN not in c.clicked
print("ALL OK")
