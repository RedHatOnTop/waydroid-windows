"""wlproxy.Conn: integer wl_output.scale kept from the HWC, and configure echoes not followed as resizes."""
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


def msg(oid, opcode, fmt="", *args):
    body = struct.pack("<" + fmt, *args) if fmt else b""
    return struct.pack("<II", oid, ((8 + len(body)) << 16) | opcode) + body


def forwarded(c, raw):
    out, i = [], 0
    while i < len(raw):
        oid, so = struct.unpack_from("<II", raw, i)
        out.append((oid, so & 0xFFFF))
        i += so >> 16
    return out


# wl_output.scale reaches the HWC until it has a fractional scale, then never again
c = wp.Conn(1)
c.objs.update({7: "wl_output", 8: "wp_fractional_scale_v1"})
assert forwarded(c, c.feed("s2c", msg(7, 3, "i", 2) + msg(7, 2))) == [(7, 3), (7, 2)], "startup scale passes"
assert forwarded(c, c.feed("s2c", msg(8, 0, "I", 150))) == [(8, 0)], "preferred_scale passes"
got = forwarded(c, c.feed("s2c", msg(7, 1, "Iiii", 3, 1920, 1200, 60000) + msg(7, 3, "i", 2) + msg(7, 2)))
assert got == [(7, 1), (7, 2)], f"a monitor change must not reach the HWC as an integer scale, got {got}"
c.objs[9] = "wl_output"
assert forwarded(c, c.feed("s2c", msg(9, 3, "i", 2) + msg(9, 2))) == [(9, 2)], "nor a newly bound output's"
assert "DROP wl_output#7.scale(2)" in wp.LOG.getvalue()


class FakeTasks:
    display = (1920, 1200)


wp.TASKS = FakeTasks()
MAIN, TOP = 10, 12


def window():
    c = wp.Conn(2)
    c.main_surface[MAIN] = TOP
    c.dest[MAIN] = (1536, 960)
    c.placed = []
    c.place = lambda main, size, restoring=False, maximized=False: c.placed.append((size, restoring, maximized))
    return c


def report(c, size, age=0.0):
    c.geom[MAIN] = (12, 12, *size)
    c.sent_sizes.setdefault(MAIN, wp.collections.deque(maxlen=8)).append((time.monotonic() - age, size))


# live 2026-10-08: Notes' first geometry 369x405 (measured before adopt), then 385x421; mutter's
# first focus configure still carried 369x405 and the task was shrunk to it
c = window()
report(c, (369, 405))
report(c, (385, 421))
c.follow_configure(TOP, 369, 405, {4})
assert c.placed == [], f"an echo of a geometry just left must not resize the task, got {c.placed}"
assert c.conf[TOP][:2] == (385, 421), "the ack is not held for the echoed size"
# the same size during an interactive resize is the user's
c.follow_configure(TOP, 369, 405, {3, 4})
assert c.placed == [((369, 405), False, False)], c.placed
# an old geometry coming back (restore from a tile) is a real request
c = window()
report(c, (628, 686), age=5)
report(c, (768, 928))
c.follow_configure(TOP, 628, 686, set())
assert c.placed == [((628, 686), False, False)], c.placed
# a size the window never had (min size enforced by mutter) is followed
c = window()
report(c, (222, 244))
c.follow_configure(TOP, 248, 248, set())
assert c.placed == [((248, 248), False, False)], c.placed
# maximize and restore are untouched by the echo rule
c = window()
report(c, (628, 686))
c.follow_configure(TOP, 1536, 928, {1, 4})
c.restore[MAIN] = (628, 686)
report(c, (1536, 928))
c.follow_configure(TOP, 628, 686, {4})
assert c.placed == [((1536, 928), False, True), ((628, 686), True, False)], c.placed
# rig 2026-10-08: settings.intelligence's first configure echoed its pre-adopt 224x528 after the
# geometry was 248x552; the ack must not wait 1.5 s for a size the task is never asked to take
c = window()
c.appid[TOP] = "waydroid.com.android.settings.intelligence"
c.xdg_surface_of[29] = MAIN
c.expect_commit = lambda surface: None
c.release_ack = lambda *args: None
report(c, (224, 528))
report(c, (248, 552))
c.serial_conf[(29, 19)] = (224, 528, {4})
assert c.hold_ack(29, 19) is False and 29 not in c.held_ack, "an echoed configure's ack is not held"
c.serial_conf[(29, 20)] = (300, 600, {3, 4})
assert c.hold_ack(29, 20) is True and c.held_ack[29] == (20, (300, 600)), "a resize's ack still waits for the task"
print("ALL OK")
