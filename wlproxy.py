#!/usr/bin/env python3
"""Logging Wayland proxy.

    wlproxy.py LISTEN_SOCKET UPSTREAM_SOCKET LOGFILE

A relative UPSTREAM_SOCKET is taken from XDG_RUNTIME_DIR; LOGFILE "-" logs to stdout (the journal
under waydroid-wlproxy.service).

Relays every byte and SCM_RIGHTS fd and decodes the traffic into LOGFILE using the
protocol XMLs found below. With WLPROXY_REWRITE=1 (default) the size of every xdg_toplevel.configure
for a toplevel whose app_id starts with "waydroid." is replaced by 0x0: Waydroid's HWC hotplugs the
Android display on any configure that carries a size, which wipes all app windows whenever GNOME
reports a focus change. High-rate messages are summarised instead of logged one by one.
"""
import array
import collections
import contextlib
import glob
import importlib
import hashlib
import math
import os
import queue
import re
import select
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET

sys.path = [os.path.dirname(os.path.abspath(__file__)), *sys.path]
geometry_model = importlib.import_module("wlproxy_geometry")

# Later files win, so where both are installed Qt 6's copies are the ones in use, as on Fedora
XML_GLOBS = ["/usr/share/wayland/wayland.xml", "/usr/share/wayland-protocols/*/*/*.xml",
             "/usr/share/qt6/wayland/protocols/*/*.xml", "/usr/share/qt6/wayland/protocols/*/*/*.xml"]
NOISY = {
    "wl_surface.damage", "wl_surface.damage_buffer", "wl_surface.commit", "wl_surface.frame",
    "wl_callback.done", "wl_buffer.release", "wl_pointer.motion", "wl_pointer.frame",
    "wl_pointer.axis", "wl_touch.motion", "wl_touch.frame", "wl_region.add", "wl_region.destroy",
    "wl_surface.set_opaque_region", "wl_surface.set_input_region", "wl_subsurface.set_position",
    "wl_surface.set_buffer_scale", "wl_surface.set_buffer_transform",
    "wp_viewport.set_destination", "wp_viewport.set_source", "wl_compositor.create_region",
    "zwp_relative_pointer_v1.relative_motion", "xdg_surface.set_window_geometry",
    "wl_surface.attach",
}
QUIET_KEEP = {"wl_keyboard.enter", "wl_keyboard.leave", "wl_display.error", "xdg_wm_base.get_xdg_surface"}
TOPLEVEL_STATES = {1: "maximized", 2: "fullscreen", 3: "resizing", 4: "activated",
                   5: "tiled_left", 6: "tiled_right", 7: "tiled_top", 8: "tiled_bottom",
                   9: "suspended"}
# The types the HWC reads the host clipboard as (WaydroidClipboard.cpp). A selection with none of them but an image
# can reach Android through an app that serves content://compat.hostclip, which is not part of this repository;
# WLPROXY_HOSTCLIP=1 turns that on.
HWC_TEXT = {"text/plain;charset=utf-8", "UTF8_STRING", "text/plain", "TEXT", "STRING"}
IMAGE_TYPES = ("image/png", "image/jpeg", "image/webp", "image/gif", "image/bmp")
HOSTCLIP = "content://compat.hostclip"
CLIP_MAX = 64 << 20

IFACES = {}
for pattern in XML_GLOBS:
    for path in sorted(glob.glob(pattern)):
        try:
            root = ET.parse(path).getroot()
        except Exception:
            continue
        for itf in root.iter("interface"):
            def sig(tag):
                out = []
                for m in itf.findall(tag):
                    args = [(a.get("name"), a.get("type"), a.get("interface")) for a in m.findall("arg")]
                    out.append((m.get("name"), args))
                return out
            IFACES[itf.get("name")] = {"req": sig("request"), "evt": sig("event")}

REWRITE = os.environ.get("WLPROXY_REWRITE", "1") == "1"
GEOMETRY = os.environ.get("WLPROXY_GEOMETRY", "1") == "1"
QUIET = os.environ.get("WLPROXY_QUIET", "0") == "1"
GESTURES = os.environ.get("WLPROXY_GESTURES", "1") == "1"
TASKD = os.environ.get("WLPROXY_TASKD", "1") == "1"
HOSTCLIP_ON = os.environ.get("WLPROXY_HOSTCLIP", "0") == "1"
ADB_SERIAL = os.environ.get("WLPROXY_ADB", "192.168.240.112:5555")
TASK_DEX = os.path.expanduser(os.environ.get("WLPROXY_TASK_DEX", os.path.join(os.path.dirname(os.path.abspath(__file__)), "wdtask", "wdtask.dex")))
REMOTE_DEX = "/data/local/tmp/wdtask.dex"
FULLSCREEN = 1
CAPTION_DP = 42
CAPTION_BAND_DP = 100  # Galaxy Store's caption sits under a strip about 40dp high
CAPTION_BUTTONS = (("close", 5, 37), ("maximize", 47, 79), ("minimize", 89, 121))
ONEUI_CAPTION_BUTTONS = (("menu", 18, 50), ("close", 62, 94), ("maximize", 106, 138), ("minimize", 151, 183))
RING = int(os.environ.get("WLPROXY_RING", "12"))
CORNER = 24
RESIZE_BORDER = 8
ORIGIN = 16
MIN_TASK = 248
MIN_SIZE_DP = 220  # Android's default minimum for a resizeable task (mMinSizeOfResizeableTaskDp)
RESIZING = 3
CONFIGURE_TOLERANCE = 2
ECHO_WINDOW = 0.5
WINDOWS_INTERVAL = 0.25  # the least time between two reads of a task's window layouts
DRAG_THRESHOLD = 6
BTN_LEFT = 272
KEY_LEFTMETA, KEY_RIGHTMETA = 125, 126
MOD_SUPER = 0x40
LOG = None
LOG_LOCK = threading.Lock()
T0 = time.time()
COUNTS = {}


def log(conn, text):
    with LOG_LOCK:
        LOG.write(f"{time.time() - T0:9.3f} c{conn} {text}\n")
        LOG.flush()


class TaskD:
    """Drives Android tasks through WdTask (wdtask/WdTask.java) over one persistent `adb shell`.

    Jobs are keyed and run in order on one thread; a newer job with the same key replaces a pending
    one, so a resize drag only ever applies the latest size. The shell is (re)started lazily because
    the proxy outlives Waydroid sessions."""

    def __init__(self):
        self.proc = None
        self.lines = None
        self.display = None
        self.density = None
        self.oneui = False
        self.osk = None
        self.osk_at = 0.0
        self.jobs = {}
        self.order = []
        self.cv = threading.Condition()
        threading.Thread(target=self.run, daemon=True).start()

    def submit(self, key, fn):
        with self.cv:
            if key not in self.jobs:
                self.order.append(key)
            self.jobs[key] = fn
            self.cv.notify()

    def run(self):
        while True:
            with self.cv:
                while not self.order:
                    self.cv.wait()
                key = self.order.pop(0)
                fn = self.jobs.pop(key)
            try:
                fn(self)
            except Exception as e:
                log(0, f"   TASKD {key[0]} failed: {e!r}")
                self.reset()

    def reset(self):
        if self.proc:
            with contextlib.suppress(OSError):
                self.proc.kill()
        self.proc = None

    def adb(self, *args, timeout=15):
        return subprocess.run(["adb", "-s", ADB_SERIAL, *args], capture_output=True, text=True,
                              timeout=timeout).stdout

    def ensure(self):
        if self.proc and self.proc.poll() is None:
            return
        # The first window appears before adbd answers, so keep trying for a while.
        deadline = time.monotonic() + 45
        while True:
            subprocess.run(["adb", "connect", ADB_SERIAL], capture_output=True, timeout=15)
            out = self.adb("shell", "wm size; wm density")
            size, density = re.findall(r"size: (\d+)x(\d+)", out), re.findall(r"density: (\d+)", out)
            if size and density:
                break
            if time.monotonic() > deadline:
                raise RuntimeError(f"adb {ADB_SERIAL} does not answer: {out.strip()!r}")
            time.sleep(2)
        self.display = tuple(map(int, size[-1]))
        self.density = int(density[-1])
        self.oneui = bool(self.adb("shell", "getprop ro.build.version.oneui").strip())
        if self.adb("shell", f"stat -c %s {REMOTE_DEX} 2>/dev/null").strip() != str(os.path.getsize(TASK_DEX)):
            self.adb("push", TASK_DEX, REMOTE_DEX, timeout=30)
        self.proc = subprocess.Popen(["adb", "-s", ADB_SERIAL, "shell", f"CLASSPATH={REMOTE_DEX} app_process / WdTask"],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     text=True, bufsize=1, start_new_session=True)
        self.lines = queue.Queue()
        threading.Thread(target=lambda p=self.proc, q=self.lines: [q.put(line.strip()) for line in p.stdout],
                         daemon=True).start()
        ready = self.lines.get(timeout=15)
        if ready != "ready":
            raise RuntimeError(f"WdTask said {ready!r}")
        log(0, f"   TASKD up: display {self.display[0]}x{self.display[1]} density {self.density}")

    def cmd(self, line):
        self.ensure()
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()
        out = []
        while (reply := self.lines.get(timeout=5)) != ".":
            out.append(reply)
        if out and out[0].startswith("err"):
            raise RuntimeError(f"{line}: {out[0]}")
        return out

    def refresh_osk(self, conn):
        """Read Android's "use on-screen keyboard" switch (show_ime_with_hard_keyboard). Waydroid
        always has a hardware keyboard, so this switch alone decides whether the IME draws one.
        Keyed, so the reads asked for by a burst of IME commits collapse into one."""
        def job(td):
            td.ensure()
            td.osk = td.adb("shell", "settings get secure show_ime_with_hard_keyboard").strip() == "1"
            td.osk_at = time.monotonic()
            conn.osk_known()
        self.submit(("osk", conn.cid), job)

    def task(self, pkg):
        """(mode, (l, t, r, b), (min_w, min_h)) of the package's visible task, else of any task of it,
        else None. The minimums are the activity's declared ones in display px, -1 where it has none."""
        found = None
        for row in self.cmd("list"):
            f = row.split()
            if len(f) == 10 and f[1] == pkg:
                entry = (int(f[2]), tuple(map(int, f[4:8])), (int(f[8]), int(f[9])))
                if f[3] == "true":
                    return entry
                found = found or entry
        return found


TASKS = TaskD() if TASKD and os.path.exists(TASK_DEX) else None


def pad4(n):
    return (n + 3) & ~3


class Conn:
    def __init__(self, cid):
        self.cid = cid
        self.objs = {1: "wl_display"}
        self.dead = set()
        self.gen = {}
        self.version = {}
        self.mapped = {}
        self.appid = {}
        self.xdg_surface_of = {}
        self.toplevel_xdg = {}
        self.main_surface = {}
        self.viewport_of = {}
        self.surface_geometry = geometry_model.SurfaceGeometry()
        self.dest = {}
        self.sub_of = {}
        self.pos = {}
        self.kids = {}
        self.geom = {}
        self.maximized = {}
        self.host_max = {}
        self.restore = {}
        self.task_rect = {}
        self.clicked = {}
        self.max_asked = set()
        self.conf = {}
        self.serial_conf = {}
        self.configure_resizes = frozenset()
        self.resize_serials = frozenset()
        self.held_ack = {}
        self.pending_commit = {}
        self.min_size = {}
        self.ring = {}
        self.stable_top = 0
        self.windows_busy = frozenset()
        self.windows_tried = {}
        self.windows_at = {}
        self.sent_sizes = {}
        self.fractional = False
        self.ime_blocked = {}
        self.ime_buffer = {}
        self.ime_hidden = {}
        self.ime_mapped = set()
        self.ack_lock = threading.Lock()
        self.parent_of = {}
        self.mapped_seq = {}
        self.ptr = None
        self.touch_surface = {}
        self.gesture = None
        self.up = None
        self.up_lock = threading.RLock()
        self.down = None
        self.down_lock = threading.Lock()
        self.ime_cbs = {}
        self.sync_cbs = set()
        self.offer_types = {}
        self.clip_gen = 0
        self.clip_digest = None
        self.stage = {"c2s": b"", "s2c": b""}

    def name(self, oid):
        return f"{self.objs.get(oid, '?')}#{oid}"

    def decode(self, direction, oid, opcode, body):
        iface = self.objs.get(oid)
        table = IFACES.get(iface, {}).get("req" if direction == "c2s" else "evt")
        if table is None or opcode >= len(table):
            return iface, f"op{opcode}", [f"<{len(body)}B>"]
        mname, sig = table[opcode]
        out = []
        pos = 0
        for aname, atype, aif in sig:
            if atype in ("int", "uint"):
                (v,) = struct.unpack_from("<i" if atype == "int" else "<I", body, pos)
                pos += 4
                out.append(f"{aname}={v}")
            elif atype == "fixed":
                (v,) = struct.unpack_from("<i", body, pos)
                pos += 4
                out.append(f"{aname}={v / 256:.2f}")
            elif atype in ("string", "array"):
                (n,) = struct.unpack_from("<I", body, pos)
                raw = body[pos + 4:pos + 4 + n]
                pos += 4 + pad4(n)
                if atype == "string":
                    out.append(f"{aname}={raw.rstrip(chr(0).encode()).decode('utf-8', 'replace')!r}")
                elif iface == "xdg_toplevel" and mname == "configure":
                    states = [TOPLEVEL_STATES.get(s, s) for s in struct.unpack(f"<{n // 4}I", raw)]
                    out.append(f"{aname}={states}")
                else:
                    out.append(f"{aname}=<{n}B>")
            elif atype == "fd":
                out.append(f"{aname}=fd")
            elif atype == "object":
                (v,) = struct.unpack_from("<I", body, pos)
                pos += 4
                out.append(f"{aname}={self.name(v) if v else 'null'}")
            elif atype == "new_id":
                if aif is None:
                    (n,) = struct.unpack_from("<I", body, pos)
                    sname = body[pos + 4:pos + 4 + n].rstrip(b"\0").decode()
                    pos += 4 + pad4(n)
                    (ver, nid) = struct.unpack_from("<II", body, pos)
                    pos += 8
                    self.objs[nid] = sname
                    self.dead.discard(nid)
                    self.gen[nid] = self.gen.get(nid, 0) + 1
                    self.version[nid] = ver
                    out.append(f"{aname}={sname}#{nid}@v{ver}")
                else:
                    (nid,) = struct.unpack_from("<I", body, pos)
                    pos += 4
                    self.objs[nid] = aif
                    self.dead.discard(nid)
                    self.gen[nid] = self.gen.get(nid, 0) + 1
                    self.version[nid] = self.version.get(oid, 1)
                    out.append(f"{aname}={aif}#{nid}")
        if direction == "s2c" and iface == "wl_display" and mname == "delete_id":
            self.objs.pop(struct.unpack_from("<I", body, 0)[0], None)
        if direction == "c2s" and mname == "destroy":
            self.dead.add(oid)
        return iface, mname, out

    def feed(self, direction, data):
        """Log the stream; return the bytes to forward (whole messages only on s2c)."""
        buf = self.stage[direction] + data
        out = bytearray()
        while len(buf) >= 8:
            oid, so = struct.unpack_from("<II", buf)
            size, opcode = so >> 16, so & 0xFFFF
            if size < 8 or len(buf) < size:
                break
            msg = bytearray(buf[:size])
            buf = buf[size:]
            drop = False
            try:
                self.emit(direction, oid, opcode, bytes(msg[8:]))
                if direction == "s2c" and self.ime_cbs:
                    if oid in self.ime_cbs and opcode == 0:
                        del self.ime_cbs[oid]
                    elif oid == 1 and opcode == 1:
                        self.ime_cbs.pop(struct.unpack_from("<I", msg, 8)[0], None)
                if direction == "c2s" and oid == 1 and opcode == 0:
                    self.sync_cbs.add(struct.unpack_from("<I", msg, 8)[0])
                elif direction == "s2c" and opcode == 0 and oid in self.sync_cbs:
                    self.sync_cbs.discard(oid)
                    for delay in (0.01, 0.1):
                        threading.Timer(delay, self.nudge).start()
                if direction == "s2c" and TASKS:
                    self.track_selection(oid, opcode, msg)
                if direction == "s2c" and REWRITE:
                    self.rewrite_s2c(oid, opcode, msg)
                if direction == "c2s" and GEOMETRY:
                    drop = self.track_c2s(oid, opcode, bytes(msg[8:]), out)
                if direction == "s2c" and GESTURES:
                    drop = self.track_s2c(oid, opcode, bytes(msg[8:]), msg, out)
                if direction == "s2c" and self.integer_output_scale(oid, opcode, msg):
                    drop = True
            except Exception as e:
                log(self.cid, f"{direction} DECODE-ERR obj#{oid} op{opcode}: {e!r}")
            if not drop:
                out += msg
        self.stage[direction] = bytes(buf)
        return data if direction == "c2s" and not GEOMETRY else bytes(out)

    def integer_output_scale(self, oid, opcode, msg):
        """Keep wl_output.scale away from the HWC once it has its fractional scale. The HWC sets
        `scale = max((int)scale, output_scale)`, so mutter's integer scale (2 for a 1.25 monitor),
        re-sent whenever the monitor layout changes (the Duo's lower panel going off or on), turns
        its 1.25 into 2 for the rest of the session: every window is then drawn at 1.25/2 of its
        task, and the fractional scale is only read again when a session starts."""
        iface = self.objs.get(oid)
        if iface == "wp_fractional_scale_v1" and opcode == 0:
            self.fractional = True
        elif iface == "wl_output" and opcode == 3 and self.fractional:
            log(self.cid, f"   DROP wl_output#{oid}.scale({struct.unpack_from('<i', msg, 8)[0]}): the HWC keeps its fractional scale")
            return True
        return False

    def is_app_main(self, surface):
        top = self.main_surface.get(surface)
        return top is not None and self.appid.get(top, "").startswith("waydroid.")

    def forget_surface(self, surface):
        self.dest.pop(surface, None)
        self.pos.pop(surface, None)
        self.mapped.pop(surface, None)
        self.mapped_seq.pop(surface, None)
        self.geom.pop(surface, None)
        self.kids.pop(surface, None)
        self.main_surface.pop(surface, None)
        self.parent_of.pop(surface, None)
        for kids in self.kids.values():
            kids.discard(surface)

    def ref(self, oid):
        """An object as it is now: its id plus how often the id has been created. The HWC reuses
        ids at once, so an id alone may name another object by the time a timer or a TaskD job
        injects: an xdg_toplevel.set_max_size once reached a fresh wl_surface as an invalid
        set_buffer_transform, a protocol error that disconnects every Waydroid window."""
        return oid, self.gen.get(oid, 0)

    def send_c2s(self, data, *refs, fds=()):
        """Inject requests unless one of the objects they address is gone or its id now names a
        newer object (refs from ref(), taken when the injection was decided). The check runs under
        the lock the c2s pump sends with, after it has seen any destroy it is about to forward."""
        with self.up_lock:
            if any(o in self.dead or o not in self.objs or self.gen.get(o, 0) != g for o, g in refs):
                log(self.cid, f"   SKIP injection to gone object(s) {[o for o, _ in refs]}")
                return False
            if fds:
                sent = self.up.sendmsg([data], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", fds))])
                data = data[sent:]
            self.up.sendall(data)
            return True

    def layer_rects(self, main):
        return [(k, *self.pos[k], *self.dest[k]) for k in self.kids.get(main, ())
                if self.mapped.get(k) and k in self.pos and k in self.dest and min(self.dest[k]) > 0]

    def window_of(self, surface):
        main = surface if surface in self.main_surface else self.parent_of.get(surface)
        if main is None or not self.is_app_main(main):
            return None
        if self.appid[self.main_surface[main]].endswith("InputMethod"):
            return None
        return main

    def window_xy(self, surface, x, y):
        if surface in self.main_surface:
            return x, y
        px, py = self.pos.get(surface, (0, 0))
        return px + x, py + y

    def app_layer(self, main, rects):
        """The app layer is the one wrapped by another layer with a small outset (Android's resize
        handle area, 12px here). Once the starting-window layer is gone only the outset-inclusive
        layer is left, so the first-mapped layer is inset by the handle ring unless it fills the
        display (a maximized task has no ring)."""
        wrapped = []
        for r in rects:
            for o in rects:
                if o is r:
                    continue
                gaps = (r[1] - o[1], r[2] - o[2], o[1] + o[3] - r[1] - r[3], o[2] + o[4] - r[2] - r[4])
                if min(gaps) >= 0 and max(gaps) <= 24:
                    wrapped.append(r)
        if wrapped:
            return min(wrapped, key=lambda r: r[3] * r[4])
        k, x, y, w, h = min(rects, key=lambda r: self.mapped_seq.get(r[0], 1 << 30))
        dw, dh = self.dest.get(main, (1 << 30, 1 << 30))
        if w >= dw - 2 and h >= dh - 2:
            return k, x, y, w, h
        return k, x + RING, y + RING, w - 2 * RING, h - 2 * RING

    def pkg(self, main):
        aid = self.appid[self.main_surface[main]].removeprefix("waydroid.")
        m = re.search(r"[A-Za-z]\w*(?:\.\w+)+", aid)
        return m.group() if m else aid

    def scale(self, main):
        dw = self.dest.get(main, (0, 0))[0]
        return TASKS.display[0] / dw if TASKS and TASKS.display and dw else None

    def caption_height(self, main, dp=CAPTION_DP):
        s = self.scale(main)
        return dp * TASKS.density / 160 / s if s and TASKS.density else dp

    def app_rect(self, main, rects):
        """The task's own rectangle in main-surface coordinates. Android draws a freeform task with
        its resize ring around it in the same buffer and splits others into one layer per activity,
        so the layers alone are ambiguous. The left/top gap between the layers and the task bounds
        WdTask reported is that ring, and some layouts have none (Settings goes two-pane when wide
        and draws each activity in a layer of its own, edge to edge). It is cut where the display
        ends, on the left and top of a maximized task at the display's corner and on the right or
        bottom of a task reaching the display's edge, so a gap of 0 there says nothing: the ring
        last measured, none and the usual one are tried in turn for the one that makes the layers
        match the task. The size still comes from the layers, which lag the task during a resize
        but always match what is on screen. Once they have caught up, the task size itself is used:
        the ring is rounded per layer, and the next resize starts from this size, so a pixel off
        here would add up."""
        actual = self.surface_geometry.geometry(main, rects, None if self.host_max.get(main) else self.dest.get(main))
        if actual is not None:
            scale, task = self.scale(main), self.task_rect.get(main)
            if scale and task:
                size = (round((task[2] - task[0]) / scale), round((task[3] - task[1]) / scale))
                if all(abs(a - b) <= 1 for a, b in zip(actual[2:], size)):
                    return (*actual[:2], *size)
            return actual
        dw, dh = self.dest.get(main, (1 << 30, 1 << 30))
        inner = [r for r in rects if not (r[3] >= dw - 2 and r[4] >= dh - 2)]
        if not inner or self.host_max.get(main):
            inner = rects
        x0, y0 = min(r[1] for r in inner), min(r[2] for r in inner)
        x1, y1 = max(r[1] + r[3] for r in inner), max(r[2] + r[4] for r in inner)
        s, tr = self.scale(main), self.task_rect.get(main)
        if s and tr:
            left, top = int(tr[0] / s), int(tr[1] / s)
            mx, my = left - x0, top - y0
            clear = left >= RING and top >= RING
            rings = (mx,) if clear else tuple(dict.fromkeys((self.ring.get(main, RING), 0, RING)))
            tw, th = round((tr[2] - tr[0]) / s), round((tr[3] - tr[1]) / s)
            sizes = []
            for ring in rings:
                rx = min(ring, max(0, int((TASKS.display[0] - tr[2]) / s)))
                ry = min(ring, max(0, int((TASKS.display[1] - tr[3]) / s)))
                sizes.append((x1 - x0 - mx - rx, y1 - y0 - my - ry))
            if 0 <= mx <= 2 * RING + 8 and 0 <= my <= 2 * RING + 8 and sizes[0][0] > 0 and sizes[0][1] > 0:
                for ring, (w, h) in zip(rings, sizes):
                    if abs(w - tw) <= 4 and abs(h - th) <= 4:
                        if clear and mx == my:
                            self.ring[main] = mx
                        return left, top, tw, th
                return x0 + mx, y0 + my, *sizes[0]
        return self.app_layer(main, inner)[1:]

    def zone(self, main, wx, wy):
        """Classify a point of an app window as (zone, xdg resize edges). Android's own caption drag
        and resize handles move the task inside the display-sized surface while the host frame stays
        put, so the pointer drifts against the content and the task runs away. The caption strip
        becomes a host move and everything outside the task a host resize; the compositor's sizes
        then reach the task through follow_configure."""
        rects = self.layer_rects(main)
        if not rects:
            return "client", 0
        ax, ay, aw, ah = self.app_rect(main, rects)
        if ax <= wx < ax + aw and ay <= wy < ay + ah:
            return ("caption" if wy < ay + self.caption_height(main) else "client"), 0
        left, right, top, bottom = wx < ax, wx >= ax + aw, wy < ay, wy >= ay + ah
        if not TASKS:
            return ("swallow" if left or top else "client"), 0
        if self.host_max.get(main):
            return "swallow", 0
        if (left or right) and not (top or bottom):
            top, bottom = wy < ay + CORNER, wy >= ay + ah - CORNER
        elif (top or bottom) and not (left or right):
            left, right = wx < ax + CORNER, wx >= ax + aw - CORNER
        return "resize", top * 1 + bottom * 2 + left * 4 + right * 8

    def caption_button(self, main, wx, wy):
        """Which of Android's caption buttons a caption point is on. They sit at fixed dp offsets
        from the right end of each activity's caption (an activity split puts one caption on every
        pane), measured with uiautomator on LineageOS 20."""
        s = self.scale(main)
        rects = self.layer_rects(main)
        if not s or not TASKS.density or not rects:
            return None
        ax, ay, aw, ah = self.app_rect(main, rects)
        right = ax + aw
        for _, x, y, w, h in rects:
            if x <= wx < x + w and y <= wy < y + h:
                right = min(right, x + w)
        dp = (right - wx) * s * 160 / TASKS.density
        buttons = ONEUI_CAPTION_BUTTONS if getattr(TASKS, "oneui", False) else CAPTION_BUTTONS
        return next((name for name, lo, hi in buttons if lo <= dp <= hi), None)

    def seat(self):
        return next((i for i, v in self.objs.items() if v == "wl_seat"), None)

    def start_move(self, main, serial):
        seat = self.seat()
        if seat is None:
            return
        top = self.main_surface[main]
        self.send_c2s(struct.pack("<IIII", top, (16 << 16) | 5, seat, serial), self.ref(top), self.ref(seat))
        log(self.cid, f"   GESTURE move xdg_toplevel#{top} serial={serial}")

    def start_resize(self, main, serial, edges):
        seat = self.seat()
        if seat is None:
            return
        top = self.main_surface[main]
        self.send_c2s(struct.pack("<IIIII", top, (20 << 16) | 6, seat, serial, edges), self.ref(top), self.ref(seat))
        log(self.cid, f"   GESTURE resize xdg_toplevel#{top} edges={edges} serial={serial}")

    def watch_client(self, main, kind, dev, serial, wy, tid=None):
        """A press forwarded to the app, remembered with the geometry it started on: android_drag
        hands the drag to the host if Android starts moving the task with it. One high in the task
        may be on a caption zone() did not see, so its click goes through after_caption_click."""
        if TASKS and main in self.geom:
            self.gesture = {"kind": kind, "id": tid, "mode": "client", "main": main, "dev": dev,
                            "serial": serial, "rect": self.geom[main],
                            "high": wy < self.geom[main][1] + self.caption_height(main, CAPTION_BAND_DP)}

    def client_click(self, g):
        if g["mode"] == "client" and g["high"] and g["main"] in self.main_surface:
            self.after_caption_click(g["main"])

    def android_drag(self, main, geom):
        """A forwarded press is moving the task: it was on Android's caption where zone() did not see
        one (Galaxy Store has a strip about 40dp high above its caption, so its caption starts
        below CAPTION_DP). Android would drag the task inside the display, the
        host frame staying put, and the display edges would cut it down to a sliver. Android gets
        the release, the task goes back to ORIGIN and the compositor moves the window instead."""
        with self.down_lock:
            g = self.gesture
            if (not g or g["mode"] != "client" or g["main"] != main
                    or sum(abs(a - b) for a, b in zip(geom, g["rect"])) <= DRAG_THRESHOLD):
                return
            dev = g["dev"]
            if dev in self.dead or dev not in self.objs or self.down is None:
                return
            g["mode"] = "move"
            now = int(time.monotonic() * 1000) & 0xFFFFFFFF
            if g["kind"] == "pointer":
                release = struct.pack("<IIIIII", dev, (24 << 16) | 3, g["serial"], now, BTN_LEFT, 0)
                if self.version.get(dev, 1) >= 5:
                    release += struct.pack("<II", dev, (8 << 16) | 5)
            else:
                release = struct.pack("<IIIIi", dev, (20 << 16) | 1, g["serial"], now, g["id"])
                release += struct.pack("<II", dev, (8 << 16) | 3)
            with contextlib.suppress(OSError):
                self.down.sendall(release)
            log(self.cid, f"   GESTURE Android drags the task from {g['rect']} to {geom}: released, host move")
            self.start_move(main, g["serial"])
        self.repin(main)
        # Android sets the bounds its drag ended on once it reads the release, which can land after
        # the first repin.
        threading.Timer(0.4, self.repin, (main,)).start()

    def repin(self, main):
        if main not in self.task_rect or main not in self.dest or main not in self.main_surface:
            return
        if self.host_max.get(main):
            conf = self.conf.get(self.main_surface[main])
            size = conf[:2] if conf and min(conf[:2]) > 0 else None
            self.place(main, size, maximized=True)
        else:
            self.place(main, (None, None))

    def request_maximized(self, top, on, ref=None):
        self.send_c2s(struct.pack("<II", top, (8 << 16) | (9 if on else 10)), ref or self.ref(top))
        log(self.cid, f"   MAXIMIZE xdg_toplevel#{top} {'set' if on else 'unset'} (requested)")

    def request_minimized(self, top, ref=None):
        self.send_c2s(struct.pack("<II", top, (8 << 16) | 13), ref or self.ref(top))
        log(self.cid, f"   MINIMIZE xdg_toplevel#{top} (requested)")

    def adopt(self, main):
        """Once per new window: learn its task, keep it ORIGIN px off the display edge so the ring
        around it stays inside, tell the compositor how small and large the task can get, and turn a
        task Android brought back in fullscreen (it remembers that per app) into a maximized window."""
        pkg, top, (dw, _) = self.pkg(main), self.main_surface[main], self.dest[main]
        top_ref = self.ref(top)

        def job(td):
            found = td.task(pkg)
            if found is None:
                log(self.cid, f"   TASKD adopt {pkg}: no task")
                return
            mode, (left, t, r, b), declared = found
            dump = td.adb("shell", "dumpsys window windows")
            with self.up_lock:
                if self.ref(top) != top_ref or top in self.dead:
                    return
                self.surface_geometry = self.surface_geometry.with_windows(main, dump, pkg)
                self.stable_top = geometry_model.stable_top(dump)
            width, height = td.display
            s = width / dw
            floor = MIN_SIZE_DP * td.density / 160
            mins = tuple(max(MIN_TASK, math.ceil(max(m, floor) / s)) for m in declared)
            self.min_size[main] = mins
            self.send_c2s(struct.pack("<IIii", top, (16 << 16) | 8, *mins)
                          + struct.pack("<IIii", top, (16 << 16) | 7, int((width - 2 * ORIGIN) / s),
                                        int((height - max(ORIGIN, self.stable_top)) / s)), top_ref)
            if mode == FULLSCREEN:
                self.task_rect[main] = (left, t, r, b)
                log(self.cid, f"   TASKD adopt {pkg}: fullscreen -> maximized window")
                self.request_maximized(top, True, top_ref)
                return
            y = max(ORIGIN, self.stable_top)
            w, h = min(r - left, width - 2 * ORIGIN), min(b - t, height - y)
            bounds = (ORIGIN, y, ORIGIN + w, y + h)
            if bounds != (left, t, r, b):
                td.cmd(f"bounds {pkg} {' '.join(map(str, bounds))}")
            accepted = td.task(pkg)
            self.task_rect[main] = accepted[1] if accepted else bounds
            self.refresh_geometry(main, top_ref)
            log(self.cid, f"   TASKD adopt {pkg}: [{left},{t}][{r},{b}] -> {list(bounds)}")

        TASKS.submit(("adopt", self.cid, main), job)

    def refresh_windows(self, main, rects):
        """Read the task's window layouts again for a layer buffer none of them accounts for. Adopt
        reads them once, often while the starting window, which has no shadow, is the only one with
        a surface, and the activity's buffer that replaces it would then count as content all over.
        A size a fresh read still cannot account for (a SurfaceView's) is not asked about again."""
        if main not in self.task_rect or main in self.windows_busy:
            return
        sizes = self.surface_geometry.unknown_buffers(main, rects) - self.windows_tried.get(main, frozenset())
        if not sizes:
            return
        self.windows_busy = self.windows_busy | {main}
        pkg, top_ref = self.pkg(main), self.ref(self.main_surface[main])
        due = self.windows_at.get(main, 0) + WINDOWS_INTERVAL

        def run():
            time.sleep(max(0, due - time.monotonic()))
            try:
                dump = TASKS.adb("shell", "dumpsys window windows")
            except (OSError, subprocess.SubprocessError) as e:
                log(self.cid, f"   WINDOWS {pkg}: {e!r}")
                dump = ""
            with self.up_lock:
                self.windows_busy = self.windows_busy - {main}
                self.windows_at[main] = time.monotonic()
                if self.ref(top_ref[0]) != top_ref or top_ref[0] in self.dead:
                    return
                self.surface_geometry = self.surface_geometry.with_windows(main, dump, pkg)
                left = self.surface_geometry.unknown_buffers(main, self.layer_rects(main))
                self.windows_tried[main] = self.windows_tried.get(main, frozenset()) | (sizes & left)
            self.refresh_geometry(main, top_ref)

        threading.Thread(target=run, daemon=True).start()

    def refresh_geometry(self, main, top_ref):
        with self.up_lock:
            if self.ref(top_ref[0]) != top_ref or top_ref[0] in self.dead or self.stage["c2s"]:
                return
            out = bytearray()
            self.track_c2s(main, 6, b"", out)
            if out:
                self.send_c2s(bytes(out) + struct.pack("<II", main, (8 << 16) | 6), self.ref(main), top_ref)

    def place(self, main, size, restoring=False, maximized=False):
        """Give the task a logical size at ORIGIN. A maximized window gets the compositor's size at
        the display's top-left (the whole display when size is None), one row short if that is the
        whole display: a task whose bounds equal the display hides every task below it, and the HWC
        then destroys their windows. A None dimension keeps the task's current one (the layers
        round each activity separately, so a size read back from them would creep by a pixel or two
        per resize). The WdTask call also leaves fullscreen, which a shell resize cannot."""
        pkg, (dw, _) = self.pkg(main), self.dest[main]
        mins = self.min_size.get(main, (MIN_TASK, MIN_TASK))
        top_ref = self.ref(self.main_surface[main])

        def job(td):
            if self.ref(top_ref[0]) != top_ref or top_ref[0] in self.dead:
                return
            td.ensure()
            width, height = td.display
            s = width / dw
            if maximized:
                w, h = (width, height - self.stable_top) if size is None else (min(round(size[0] * s), width), min(round(size[1] * s), height - self.stable_top))
                bounds = (0, self.stable_top, w, self.stable_top + h - 1 if (w, h) == (width, height) else self.stable_top + h)
            else:
                y = max(ORIGIN, self.stable_top)
                room_w, room_h = width - 2 * ORIGIN, height - y
                now = self.task_rect.get(main)
                w = now[2] - now[0] if size[0] is None and now else round((size[0] or mins[0]) * s)
                h = now[3] - now[1] if size[1] is None and now else round((size[1] or mins[1]) * s)
                w = min(max(w, round(mins[0] * s)), room_w)
                h = min(max(h, round(mins[1] * s)), room_h)
                if restoring and w >= room_w * 0.97 and h >= room_h * 0.97:
                    w, h = round(room_w * 0.6), round(room_h * 0.8)
                bounds = (ORIGIN, y, ORIGIN + w, y + h)
            td.cmd(f"bounds {pkg} {' '.join(map(str, bounds))}")
            accepted = td.task(pkg)
            self.task_rect[main] = accepted[1] if accepted else bounds
            log(self.cid, f"   TASKD place {pkg} {list(bounds)}")

        TASKS.submit(("place", self.cid, main), job)

    def caption_click(self, g, out):
        """A caption press released without a drag. Android's maximize button would switch the task
        to fullscreen, which hides every other task (their windows are destroyed and come back
        elsewhere), so it becomes the host's maximize toggle. Its minimize button backgrounds the
        task, which also destroys the window, leaving nothing to restore and no host taskbar entry;
        so it too becomes a host-side minimize that keeps the task mapped. Other clicks go to Android."""
        main = g["main"]
        if g.get("button") == "maximize" and TASKS and main in self.main_surface:
            log(self.cid, "   GESTURE caption maximize button -> host toggle")
            self.request_maximized(self.main_surface[main], not self.host_max.get(main, False))
            return True
        if g.get("button") == "minimize" and TASKS and main in self.main_surface:
            log(self.cid, "   GESTURE caption minimize button -> host minimize")
            self.request_minimized(self.main_surface[main])
            return True
        out += g["held"]
        self.after_caption_click(main)
        return False

    def after_caption_click(self, main):
        """Android's caption maximize button switches the task to fullscreen, which has no caption
        left to restore it from; turn that into the host's maximize toggle (the configure that
        follows puts the task back into freeform at the right size). Growing to the display is
        caught at the next commit; the way back changes no geometry, so WdTask is asked."""
        if not TASKS:
            return
        pkg, top, was = self.pkg(main), self.main_surface[main], self.host_max.get(main, False)
        top_ref = self.ref(top)
        self.clicked[main] = time.time()
        if was:
            self.max_asked.add(main)
        else:
            self.max_asked.discard(main)
        if not was and main in self.geom:
            self.restore[main] = self.geom[main][2:]

        def job(td):
            found = td.task(pkg)
            if found and found[0] == FULLSCREEN and self.host_max.get(main, False) == was:
                self.request_maximized(top, not was, top_ref)

        for delay in (0.3, 0.8):
            threading.Timer(delay, TASKS.submit, (("check", self.cid, main), job)).start()

    def target_size(self, main, conf):
        """The geometry a configure ends in, as far as the task can follow it."""
        w, h, states = conf
        dw, dh = self.dest[main]
        s = self.scale(main)
        if states & {1, 2}:
            max_h = int((TASKS.display[1] - self.stable_top) / s) if s else dh
            return (min(w, dw), min(h, max_h)) if w > 0 and h > 0 else (dw, max_h)
        if w <= 0 or h <= 0 or not s:
            return None
        mins = self.min_size.get(main, (MIN_TASK, MIN_TASK))
        max_w, max_h = int((TASKS.display[0] - 2 * ORIGIN) / s), int((TASKS.display[1] - max(ORIGIN, self.stable_top)) / s)
        return min(max(w, mins[0]), max_w), min(max(h, mins[1]), max_h)

    def hold_ack(self, xs, serial):
        """Keep a resize's ack_configure back until the buffer of that size is committed. Mutter
        moves a window resized from its left or top edge by the size change of the commit that acks
        the configure; the HWC acks at once and the task follows tens of ms later, so mutter would
        take the new size for the client resizing itself and anchor it at the top-left. The first
        configure of a window is never held: a buffer committed before any ack is a protocol error."""
        surface = self.xdg_surface_of.get(xs)
        requested = (xs, serial) in self.resize_serials
        self.resize_serials = self.resize_serials - {(xs, serial)}
        conf = self.serial_conf.pop((xs, serial), None)
        if not TASKS or not self.is_app_main(surface) or surface not in self.geom:
            return False
        if self.appid[self.main_surface[surface]].endswith("InputMethod"):
            return False
        if conf and not requested and RESIZING not in conf[2] and self.echoes(surface, *conf[:2]):
            conf = None
        target = self.target_size(surface, conf) if conf else None
        geom = self.geom[surface]
        with self.ack_lock:
            if target is None or (abs(target[0] - geom[2]) <= CONFIGURE_TOLERANCE and abs(target[1] - geom[3]) <= CONFIGURE_TOLERANCE):
                self.held_ack.pop(xs, None)
                self.expect_commit(surface)
                return False
            self.held_ack[xs] = (serial, target)
        threading.Timer(1.5, self.release_ack, (xs, serial, self.ref(xs))).start()
        return True

    def hide_empty_ime(self, main, out):
        """Keep IME windows unmapped while Android's on-screen keyboard is off. With a hardware
        keyboard and that switch off, AOSP's LatinIME still shows its window, empty and as large as
        the display, and GNOME would map it, focus it, list it and let it take every click. Its
        layer is display-sized while a keyboard slides in too, so the switch decides, not the size.
        Committing without a buffer leaves the toplevel unmapped; the HWC's main-surface buffer is
        put back once the switch is on. A mapped window is never unmapped here, because mapping it
        again needs a fresh initial commit and configure; guard_ime_input takes its clicks away."""
        if not TASKS:
            return
        fresh = time.monotonic() - TASKS.osk_at < 2
        if not fresh:
            TASKS.refresh_osk(self)
        if main in self.ime_mapped:
            return
        if not (fresh and TASKS.osk):
            out += struct.pack("<IIIii", main, (20 << 16) | 1, 0, 0, 0)
            if main not in self.ime_hidden:
                self.ime_hidden[main] = self.ref(main)
                log(self.cid, f"   IME wl_surface#{main} kept unmapped: on-screen keyboard off or not read yet")
                for cb in [cb for cb, m in self.ime_cbs.items() if m == main]:
                    threading.Timer(0.016, self.fake_frame, (cb,)).start()
            return
        buffer, buffer_gen = self.ime_buffer.get(main, (None, 0))
        if main in self.ime_hidden:
            if not buffer or buffer in self.dead or buffer not in self.objs or self.gen.get(buffer, 0) != buffer_gen:
                log(self.cid, f"   IME wl_surface#{main} lost its buffer; keyboard stays hidden")
                out += struct.pack("<IIIii", main, (20 << 16) | 1, 0, 0, 0)
                return
            out += struct.pack("<IIIii", main, (20 << 16) | 1, buffer, 0, 0)
            self.ime_hidden.pop(main, None)
            log(self.cid, f"   IME wl_surface#{main} on-screen keyboard on: mapped")
        self.ime_mapped.add(main)

    def osk_known(self):
        """Map the IME windows hidden while the switch was unknown or off, once it reads on; the
        HWC may not commit them again by itself after the keyboard has slid in."""
        if not TASKS.osk:
            return
        for main, main_ref in list(self.ime_hidden.items()):
            buffer_ref = self.ime_buffer.get(main)
            if buffer_ref and self.send_c2s(struct.pack("<IIIii", main, (20 << 16) | 1, buffer_ref[0], 0, 0)
                                            + struct.pack("<II", main, (8 << 16) | 6), main_ref, buffer_ref):
                self.ime_hidden.pop(main, None)
                self.ime_mapped.add(main)
                log(self.cid, f"   IME wl_surface#{main} on-screen keyboard on: mapped")

    def fake_frame(self, cb):
        """Answer a frame callback of a hidden IME window. Mutter never runs frame callbacks of an
        unmapped surface, and the HWC stops updating a window until its callback is done, so a
        hidden IME would never get to draw the keyboard that maps it. The compositor's own answer
        may still come later; the client then drops it as an event for a destroyed object."""
        with self.down_lock:
            main = self.ime_cbs.get(cb)
            if main is None or main not in self.ime_hidden or self.down is None:
                return
            del self.ime_cbs[cb]
            with contextlib.suppress(OSError):
                self.down.sendall(struct.pack("<III", cb, (12 << 16) | 0, int(time.monotonic() * 1000) & 0xFFFFFFFF))

    def nudge(self):
        """Follow the answer to a wl_display.sync with an event for object 0, which libwayland
        drops. The HWC creates a window with wl_display_roundtrip while its Wayland thread
        dispatches the same default queue; when that thread runs the sync callback, the roundtrip
        can already be back in poll, and a window without a buffer gets no further events, so the
        HWC and SurfaceFlinger with it waited for the next pointer motion over a Waydroid window."""
        with self.down_lock:
            if self.down is not None:
                with contextlib.suppress(OSError):
                    self.down.sendall(struct.pack("<II", 0, 8 << 16))

    def track_selection(self, oid, opcode, msg):
        """Hand an image-only host selection to Android. The HWC reads text only and destroys any other offer as soon
        as it sees the selection, so the image is asked for here, before the selection is forwarded. The HWC gets the
        selection whenever one of its windows has the keyboard, and again on every return of the keyboard."""
        iface = self.objs.get(oid)
        if iface == "wl_data_offer" and opcode == 0:
            (n,) = struct.unpack_from("<I", msg, 8)
            mime = bytes(msg[12:12 + n]).rstrip(b"\0").decode("utf-8", "replace")
            self.offer_types.setdefault(oid, []).append(mime)
        elif iface == "wl_data_device" and opcode == 5:
            (offer,) = struct.unpack_from("<I", msg, 8)
            types = self.offer_types.get(offer, [])
            self.offer_types.clear()
            self.clip_gen += 1
            image = next((t for t in IMAGE_TYPES if t in types), None)
            if image and HOSTCLIP_ON and not HWC_TEXT & set(types):
                self.receive_image(offer, image, self.clip_gen)
            else:
                self.clip_digest = None

    def receive_image(self, offer, mime, gen):
        r, w = os.pipe()
        n = len(mime) + 1
        body = struct.pack("<I", n) + mime.encode() + b"\0" * (pad4(n) - n + 1)
        sent = self.send_c2s(struct.pack("<II", offer, ((8 + len(body)) << 16) | 1) + body, self.ref(offer), fds=[w])
        os.close(w)
        if not sent:
            os.close(r)
            return
        threading.Thread(target=self.deliver_image, args=(r, mime, gen), daemon=True).start()

    def deliver_image(self, r, mime, gen):
        """Read the image the source writes and make it Android's clip, unless a newer selection came meanwhile or
        Android has it already (the same selection again on the keyboard's return)."""
        data = bytearray()
        deadline = time.monotonic() + 5
        try:
            while len(data) <= CLIP_MAX:
                left = deadline - time.monotonic()
                if left <= 0 or not select.select([r], [], [], left)[0]:
                    log(self.cid, f"   CLIPBOARD {mime}: no end from the source in 5 s ({len(data)} bytes)")
                    return
                chunk = os.read(r, 1 << 16)
                if not chunk:
                    break
                data += chunk
        finally:
            os.close(r)
        if not data or len(data) > CLIP_MAX:
            log(self.cid, f"   CLIPBOARD {mime}: {len(data)} bytes, not passed on")
            return
        digest = hashlib.sha256(data).digest()
        if gen != self.clip_gen or digest == self.clip_digest:
            return
        cmd = f"content write --uri {HOSTCLIP}/incoming && content call --uri {HOSTCLIP} --method set --arg {mime}"
        try:
            p = subprocess.run(["adb", "-s", ADB_SERIAL, "shell", cmd], input=bytes(data), capture_output=True,
                               timeout=30)
        except subprocess.TimeoutExpired:
            log(self.cid, f"   CLIPBOARD {mime}: adb timed out")
            return
        out = (p.stdout + p.stderr).decode("utf-8", "replace").strip()
        if p.returncode != 0 or "Error" in out or "Exception" in out:
            log(self.cid, f"   CLIPBOARD {mime}: Android refused ({p.returncode}): {out.splitlines()[:3]}")
            return
        self.clip_digest = digest
        log(self.cid, f"   CLIPBOARD {mime} {len(data)} bytes -> Android")

    def guard_ime_input(self, main, region, out):
        """An IME with its keyboard hidden (Android's on-screen keyboard turned off while a hardware
        keyboard is present) keeps its window shown with nothing drawn, and the HWC gives that
        window a layer the size of the display. Layer subsurfaces never get an input region from
        the HWC, so they take input over their whole area and the invisible window swallows every
        click on the screen. While the on-screen keyboard is off, a window that is already mapped
        and its layers get the HWC's own region emptied instead; it rebuilds that region each frame."""
        blocked = self.ime_blocked.get(main)
        layers = self.kids.get(main, set())
        if region and TASKS and TASKS.osk is False:
            out += struct.pack("<IIiiii", region, (24 << 16) | 2, 0, 0, 0x7FFFFFFF, 0x7FFFFFFF)
            for layer in layers - (blocked or set()):
                out += struct.pack("<III", layer, (12 << 16) | 5, region)
            if blocked is None:
                log(self.cid, f"   IME wl_surface#{main} on-screen keyboard off: input region emptied")
            self.ime_blocked[main] = set(layers) | (blocked or set())
        elif blocked is not None:
            for layer in blocked & layers:
                out += struct.pack("<III", layer, (12 << 16) | 5, 0)
            del self.ime_blocked[main]
            log(self.cid, f"   IME wl_surface#{main} on-screen keyboard on: input region restored")

    def expect_commit(self, surface):
        """Mutter applies a configure (a tile or maximize that moves the window without resizing
        it) at the commit after its ack, and an app that has nothing to redraw does not commit, so
        the window would only move at its next frame. Commit for it if the HWC does not."""
        token = object()
        self.pending_commit[surface] = token
        surface_ref = self.ref(surface)

        def kick():
            if self.pending_commit.get(surface) is token:
                del self.pending_commit[surface]
                if self.send_c2s(struct.pack("<II", surface, (8 << 16) | 6), surface_ref):
                    log(self.cid, f"   COMMIT wl_surface#{surface} for an acked configure")

        threading.Timer(0.05, kick).start()

    def release_ack(self, xs, serial, xs_ref):
        with self.ack_lock:
            held = self.held_ack.get(xs)
            if not held or held[0] != serial:
                return
            del self.held_ack[xs]
        if self.send_c2s(struct.pack("<III", xs, (12 << 16) | 4, serial), xs_ref):
            log(self.cid, f"   ACK xdg_surface#{xs} serial={serial} released on timeout (task did not reach {held[1]})")
            if self.xdg_surface_of.get(xs) is not None:
                self.expect_commit(self.xdg_surface_of[xs])

    def ack_for_commit(self, xs, geom):
        """The held ack to send with a commit whose geometry reached the configured size."""
        with self.ack_lock:
            held = self.held_ack.get(xs)
            if held and abs(held[1][0] - geom[2]) <= CONFIGURE_TOLERANCE and abs(held[1][1] - geom[3]) <= CONFIGURE_TOLERANCE:
                del self.held_ack[xs]
                return struct.pack("<III", xs, (12 << 16) | 4, held[0])
        return b""

    def follow_configure(self, top, w, h, states):
        """Make the task take what the compositor asked for. Interactive and keyboard resizes,
        tiling, maximize and restore all arrive as configure sizes, which the HWC itself must never
        see (it hotplugs the display on them)."""
        self.configure_resizes = self.configure_resizes - {top}
        main = next((sf for sf, t in self.main_surface.items() if t == top), None)
        geom = self.geom.get(main)
        if geom is None:
            return
        maximized = bool(states & {1, 2})
        was = self.host_max.get(main, False)
        self.host_max[main] = maximized
        sized = w > 0 and h > 0
        if maximized and (not was or sized and (abs(w - geom[2]) > CONFIGURE_TOLERANCE or abs(h - geom[3]) > CONFIGURE_TOLERANCE)):
            self.configure_resizes = self.configure_resizes | {top}
            self.place(main, (w, h) if sized else None, maximized=True)
        elif was and not maximized:
            size = self.restore.get(main) or ((w, h) if w > 0 and h > 0 else geom[2:])
            self.conf[top] = (size[0], size[1], states)
            self.configure_resizes = self.configure_resizes | {top}
            self.place(main, size, restoring=True)
        elif not maximized and w > 0 and h > 0 and (abs(w - geom[2]) > CONFIGURE_TOLERANCE or abs(h - geom[3]) > CONFIGURE_TOLERANCE):
            if RESIZING not in states and self.echoes(main, w, h):
                self.conf[top] = (geom[2], geom[3], states)
                log(self.cid, f"   ECHO xdg_toplevel#{top}.configure {w}x{h}: a geometry this window just left, task kept")
                return
            self.configure_resizes = self.configure_resizes | {top}
            self.place(main, (w if abs(w - geom[2]) > CONFIGURE_TOLERANCE else None, h if abs(h - geom[3]) > CONFIGURE_TOLERANCE else None))

    def echoes(self, main, w, h):
        """Whether a configure size is one this window reported a moment ago. Mutter puts the size
        of the last geometry it applied into every configure (focus changes send one each), so a
        configure built just before a new geometry reached it carries the old one back. Following
        that shrank tasks a step per launch: the first geometry of a new window is measured before
        adopt moves the task, and a second toplevel of the same task can lag behind the first."""
        now = time.monotonic()
        return any(now - t < ECHO_WINDOW and abs(w - sw) <= 2 and abs(h - sh) <= 2
                   for t, (sw, sh) in self.sent_sizes.get(main, ()))

    def filter_super(self, opcode, body, msg):
        """Keep Super away from Android: GNOME owns it (overview, Super+arrows), but the press still
        reaches the focused client first, and Android answers a lone Meta by opening the assistant
        search with its keyboard window on top of whatever the shortcut was for."""
        if opcode == 3 and struct.unpack_from("<I", body, 8)[0] in (KEY_LEFTMETA, KEY_RIGHTMETA):
            return True
        if opcode == 4:
            mods = struct.unpack_from("<III", msg, 12)
            struct.pack_into("<III", msg, 12, *(m & ~MOD_SUPER for m in mods))
        return False

    def track_s2c(self, oid, opcode, body, msg, out):
        """Turn caption drags into host moves and presses around the task into host resizes;
        returns True to drop the message."""
        iface = self.objs.get(oid)
        if iface == "wl_keyboard":
            return self.filter_super(opcode, body, msg)
        if iface not in ("wl_pointer", "wl_touch"):
            return False
        table = IFACES.get(iface, {}).get("evt")
        if table is None or opcode >= len(table):
            return False
        mname = table[opcode][0]
        def u(fmt):
            return struct.unpack_from(fmt, body)
        g = self.gesture
        if iface == "wl_pointer":
            if mname in ("enter", "leave"):
                self.gesture = None
                if mname == "enter":
                    _, surface, sx, sy = u("<IIii")
                    self.ptr = (surface, sx / 256, sy / 256)
                else:
                    self.ptr = None
            elif mname == "motion":
                _, sx, sy = u("<Iii")
                if self.ptr:
                    self.ptr = (self.ptr[0], sx / 256, sy / 256)
                if g and g["kind"] == "pointer" and g["mode"] != "client":
                    if g["mode"] == "hold" and self.ptr:
                        wx, wy = self.window_xy(*self.ptr)
                        if abs(wx - g["x"]) + abs(wy - g["y"]) > DRAG_THRESHOLD:
                            g["mode"] = "move"
                            self.start_move(g["main"], g["serial"])
                        else:
                            return False
                    return True
            elif mname == "button" and self.ptr:
                serial, _, button, state = u("<IIII")
                if button != BTN_LEFT:
                    return False
                if state == 1:
                    main = self.window_of(self.ptr[0])
                    if main is None:
                        return False
                    wx, wy = self.window_xy(*self.ptr)
                    z, edges = self.zone(main, wx, wy)
                    rects = self.layer_rects(main)
                    log(self.cid, f"   GESTURE press zone={z} edges={edges} at {wx:.0f},{wy:.0f} on #{self.ptr[0]} "
                                  f"app={self.app_rect(main, rects) if rects else None} layers={rects}")
                    if z == "caption":
                        self.gesture = {"kind": "pointer", "mode": "hold", "main": main, "serial": serial,
                                        "x": wx, "y": wy, "held": bytes(msg),
                                        "button": self.caption_button(main, wx, wy)}
                        return True
                    if z == "resize":
                        self.start_resize(main, serial, edges)
                    if z in ("resize", "swallow"):
                        self.gesture = {"kind": "pointer", "mode": "swallow"}
                        return True
                    self.watch_client(main, "pointer", oid, serial, wy)
                elif g and g["kind"] == "pointer":
                    self.gesture = None
                    if g["mode"] == "hold":
                        return self.caption_click(g, out)
                    self.client_click(g)
                    return g["mode"] != "client"
        else:
            if mname == "down":
                serial, _, surface, tid, x, y = u("<IIIiii")
                self.touch_surface[tid] = surface
                main = self.window_of(surface)
                if main is None:
                    return False
                wx, wy = self.window_xy(surface, x / 256, y / 256)
                z, edges = self.zone(main, wx, wy)
                log(self.cid, f"   GESTURE touch zone={z} edges={edges} at {wx:.0f},{wy:.0f}")
                if z == "caption":
                    self.gesture = {"kind": "touch", "id": tid, "mode": "hold", "main": main, "serial": serial,
                                    "x": wx, "y": wy, "held": bytes(msg), "button": self.caption_button(main, wx, wy)}
                    return True
                if z == "resize":
                    self.start_resize(main, serial, edges)
                if z in ("resize", "swallow"):
                    self.gesture = {"kind": "touch", "id": tid, "mode": "swallow"}
                    return True
                self.watch_client(main, "touch", oid, serial, wy, tid)
            elif mname == "motion":
                _, tid, x, y = u("<Iiii")
                if g and g["kind"] == "touch" and g["id"] == tid and g["mode"] != "client":
                    if g["mode"] == "hold":
                        wx, wy = self.window_xy(self.touch_surface.get(tid), x / 256, y / 256)
                        if abs(wx - g["x"]) + abs(wy - g["y"]) > DRAG_THRESHOLD:
                            g["mode"] = "move"
                            self.start_move(g["main"], g["serial"])
                        else:
                            return False
                    return True
            elif mname == "up":
                _, _, tid = u("<IIi")
                if g and g["kind"] == "touch" and g["id"] == tid:
                    self.gesture = None
                    if g["mode"] == "hold":
                        return self.caption_click(g, out)
                    self.client_click(g)
                    return g["mode"] != "client"
            elif mname == "cancel":
                self.gesture = None
        return False

    def track_c2s(self, oid, opcode, body, out):
        """Follow the surface tree of app windows and keep each window's geometry on its visible layers.

        The HWC makes every app window a display-sized transparent toplevel with the app as a
        subsurface and pins the window geometry to the display, so mutter frames the whole display
        and refuses to move the app above its offset inside it. Returns True to drop the message."""
        iface = self.objs.get(oid)
        table = IFACES.get(iface, {}).get("req")
        if table is None or opcode >= len(table):
            return False
        mname = table[opcode][0]
        def u(fmt):
            return struct.unpack_from(fmt, body)
        self.surface_geometry = self.surface_geometry.track(iface, oid, mname, body, self.viewport_of)
        if iface == "xdg_wm_base" and mname == "get_xdg_surface":
            nid, surface = u("<II")
            self.xdg_surface_of[nid] = surface
        elif iface == "xdg_surface" and mname == "get_toplevel":
            (nid,) = u("<I")
            self.toplevel_xdg[nid] = oid
            self.main_surface[self.xdg_surface_of.get(oid)] = nid
        elif iface == "wp_viewporter" and mname == "get_viewport":
            nid, surface = u("<II")
            self.viewport_of[nid] = surface
        elif iface == "wp_viewport" and mname == "set_destination":
            w, h = u("<ii")
            surface = self.viewport_of.get(oid)
            if surface is not None:
                self.dest[surface] = (w, h)
        elif iface == "wl_subcompositor" and mname == "get_subsurface":
            nid, surface, parent = u("<III")
            self.sub_of[nid] = surface
            self.parent_of[surface] = parent
            self.kids.setdefault(parent, set()).add(surface)
        elif iface == "wl_subsurface" and mname == "set_position":
            x, y = u("<ii")
            surface = self.sub_of.get(oid)
            if surface is not None:
                self.pos[surface] = (x, y)
        elif (iface == "wl_surface" and mname == "set_input_region" and self.is_app_main(oid)
              and self.appid[self.main_surface[oid]].endswith("InputMethod")):
            self.guard_ime_input(oid, u("<I")[0], out)
        elif iface == "wl_surface" and mname == "set_input_region" and self.is_app_main(oid):
            region = u("<I")[0]
            layers = self.layer_rects(oid)
            if region and self.surface_geometry.geometry(oid, layers) is not None:
                border = 0 if self.host_max.get(oid) else RESIZE_BORDER
                out += geometry_model.input_region(oid, region, layers, self.app_rect(oid, layers), border)
                return True
        elif iface == "wl_surface" and mname == "frame":
            main = oid if oid in self.main_surface else self.parent_of.get(oid)
            if main is not None and self.is_app_main(main) and self.appid[self.main_surface[main]].endswith("InputMethod"):
                (cb,) = u("<I")
                self.ime_cbs[cb] = main
                threading.Timer(0.016, self.fake_frame, (cb,)).start()
        elif (iface == "wl_surface" and mname == "attach" and self.is_app_main(oid)
              and self.appid[self.main_surface[oid]].endswith("InputMethod")):
            buffer = u("<I")[0]
            if buffer:
                self.ime_buffer[oid] = self.ref(buffer)
        elif iface == "xdg_surface" and mname == "ack_configure":
            return self.hold_ack(oid, u("<I")[0])
        elif iface == "xdg_surface" and mname == "set_window_geometry":
            surface = self.xdg_surface_of.get(oid)
            if self.is_app_main(surface):
                return True
        elif iface == "wl_surface" and mname == "commit" and self.is_app_main(oid):
            if self.appid[self.main_surface[oid]].endswith("InputMethod"):
                self.hide_empty_ime(oid, out)
            self.pending_commit.pop(oid, None)
            rects = self.layer_rects(oid)
            if rects:
                if TASKS:
                    self.refresh_windows(oid, rects)
                    geom = tuple(self.app_rect(oid, rects))
                else:
                    x0, y0 = min(r[1] for r in rects), min(r[2] for r in rects)
                    geom = (x0, y0, max(r[1] + r[3] for r in rects) - x0, max(r[2] + r[4] for r in rects) - y0)
                g = self.gesture
                if g and g["mode"] == "client" and g["main"] == oid:
                    self.android_drag(oid, geom)
                dw, dh = self.dest.get(oid, (1 << 30, 1 << 30))
                full = geom[2] >= dw - 2 and geom[3] >= dh - 2
                before = self.restore.get(oid, (1 << 30, 1 << 30))
                growing = full or (geom[2] > before[0] + 4 and geom[3] > before[1] + 4)
                if (TASKS and growing and not self.host_max.get(oid) and geom != self.geom.get(oid)
                        and time.time() - self.clicked.get(oid, 0) < 1.5 and oid not in self.max_asked):
                    self.max_asked.add(oid)
                    self.request_maximized(self.main_surface[oid], True)
                    return False
                if geom != self.geom.get(oid):
                    first = oid not in self.geom
                    self.geom[oid] = geom
                    self.sent_sizes.setdefault(oid, collections.deque(maxlen=8)).append((time.monotonic(), geom[2:]))
                    top = self.main_surface[oid]
                    if (not self.host_max.get(oid) and not full and time.time() - self.clicked.get(oid, 0) > 1.5):
                        self.restore[oid] = geom[2:]
                    if TASKS and first and not self.appid[top].endswith("InputMethod"):
                        self.adopt(oid)
                    elif not TASKS:
                        if full != self.maximized.get(oid, False):
                            self.maximized[oid] = full
                            out += struct.pack("<II", top, (8 << 16) | (9 if full else 10))
                            log(self.cid, f"   MAXIMIZE xdg_toplevel#{top} {'set' if full else 'unset'}")
                    xs = self.toplevel_xdg[top]
                    out += self.ack_for_commit(xs, geom)
                    out += struct.pack("<IIiiii", xs, (24 << 16) | 3, *geom)
                    layers = " ".join(f"#{r[0]}@{r[1]},{r[2]} {r[3]}x{r[4]}" for r in rects)
                    log(self.cid, f"   GEOMETRY xdg_surface#{xs} -> x={geom[0]} y={geom[1]} {geom[2]}x{geom[3]}  layers: {layers}")
        elif mname == "destroy" and iface in ("wl_surface", "wl_subsurface", "wp_viewport", "xdg_surface",
                                              "xdg_toplevel"):
            if iface == "wl_surface":
                self.forget_surface(oid)
                for state in (self.ime_blocked, self.ime_buffer):
                    state.pop(oid, None)
                self.ime_hidden.pop(oid, None)
                self.ime_mapped.discard(oid)
            elif iface == "wl_subsurface":
                self.sub_of.pop(oid, None)
            elif iface == "wp_viewport":
                self.viewport_of.pop(oid, None)
            elif iface == "xdg_surface":
                self.xdg_surface_of.pop(oid, None)
                self.resize_serials = frozenset(key for key in self.resize_serials if key[0] != oid)
                self.serial_conf = {key: value for key, value in self.serial_conf.items() if key[0] != oid}
                with self.ack_lock:
                    self.held_ack.pop(oid, None)
            elif iface == "xdg_toplevel":
                self.toplevel_xdg.pop(oid, None)
                self.conf.pop(oid, None)
                self.configure_resizes = self.configure_resizes - {oid}
                for surface, top in self.main_surface.items():
                    if top == oid:
                        for state in (self.geom, self.maximized, self.host_max, self.restore, self.task_rect, self.clicked, self.min_size, self.ring, self.sent_sizes,
                                      self.windows_tried, self.windows_at):
                            state.pop(surface, None)
        return False

    def rewrite_s2c(self, oid, opcode, msg):
        iface = self.objs.get(oid)
        if iface == "zwp_linux_buffer_params_v1" and opcode == 0:
            self.surface_geometry = self.surface_geometry.created(oid, struct.unpack_from("<I", msg, 8)[0])
        if iface == "xdg_surface" and opcode == 0:
            top = next((t for t, xs in self.toplevel_xdg.items() if xs == oid), None)
            if top in self.conf:
                key = (oid, struct.unpack_from("<I", msg, 8)[0])
                self.serial_conf[key] = self.conf[top]
                if top in self.configure_resizes:
                    self.resize_serials = self.resize_serials | {key}
            return
        if iface != "xdg_toplevel" or opcode != 0 or len(msg) < 20:
            return
        appid = self.appid.get(oid, "")
        if not appid.startswith("waydroid."):
            return
        w, h, n = struct.unpack_from("<iiI", msg, 8)
        states = set(struct.unpack_from(f"<{n // 4}I", msg, 20))
        self.conf[oid] = (w, h, states)
        if TASKS and not appid.endswith("InputMethod"):
            self.follow_configure(oid, w, h, states)
        if w >= 2 and h >= 2:
            struct.pack_into("<ii", msg, 8, 0, 0)
            log(self.cid, f"   REWRITE xdg_toplevel#{oid}.configure size {w}x{h} -> 0x0 (app window)")

    def emit(self, direction, oid, opcode, body):
        iface, mname, args = self.decode(direction, oid, opcode, body)
        if direction == "c2s" and iface == "xdg_toplevel" and mname == "set_app_id":
            (n,) = struct.unpack_from("<I", body, 0)
            self.appid[oid] = body[4:4 + n].rstrip(b"\0").decode("utf-8", "replace")
        key = f"{iface}.{mname}"
        arrow = "->" if direction == "c2s" else "<-"
        quiet_skip = QUIET and not (iface == "xdg_toplevel" or key in QUIET_KEEP)
        if iface == "wl_surface" and mname == "attach" and direction == "c2s":
            null = args and args[0].endswith("null")
            was = self.mapped.get(oid)
            self.mapped[oid] = not null
            if not null and not was:
                self.mapped_seq[oid] = len(self.mapped_seq) + 1
            if (was is None or was != (not null)) and not quiet_skip:
                log(self.cid, f"{arrow} wl_surface#{oid} {'MAP(buffer attached)' if not null else 'UNMAP(null buffer)'}")
        if quiet_skip:
            return
        if key in NOISY:
            COUNTS[key] = COUNTS.get(key, 0) + 1
            if COUNTS[key] <= 2:
                log(self.cid, f"{arrow} {self.name(oid)}.{mname}({', '.join(args)})")
            elif COUNTS[key] % 500 == 0:
                log(self.cid, f"   [{key} x{COUNTS[key]}]")
            return
        log(self.cid, f"{arrow} {self.name(oid)}.{mname}({', '.join(args)})")


def pump(conn, src, dst, direction, peer_closed):
    held = []
    try:
        while True:
            data, anc, _flags, _addr = src.recvmsg(65536, socket.CMSG_SPACE(4 * 64))
            fds = []
            for level, typ, cdata in anc:
                if level == socket.SOL_SOCKET and typ == socket.SCM_RIGHTS:
                    a = array.array("i")
                    a.frombytes(cdata[:len(cdata) - (len(cdata) % a.itemsize)])
                    fds.extend(a)
            if not data and not fds:
                break
            if fds:
                if not QUIET:
                    log(conn.cid, f"   ({direction} carries {len(fds)} fd)")
                held.extend(fds)
            # s2c is parsed and sent under down_lock so a faked frame callback (fake_frame) can tell
            # whether the compositor's own answer is already on its way to the client.
            with conn.down_lock if direction == "s2c" else conn.up_lock:
                out = conn.feed(direction, data)
                if not out:
                    continue
                ancdata = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", held))] if held else []
                with conn.up_lock if direction == "c2s" else contextlib.nullcontext():
                    sent = dst.sendmsg([out], ancdata)
                    while sent < len(out):
                        sent += dst.send(out[sent:])
            for fd in held:
                os.close(fd)
            held = []
    except OSError as e:
        log(conn.cid, f"{direction} socket error: {e!r}")
    finally:
        log(conn.cid, f"{direction} closed")
        for s in (src, dst):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def main():
    global LOG
    listen_path, upstream_path, log_path = sys.argv[1:4]
    if not os.path.isabs(upstream_path):
        upstream_path = os.path.join(os.environ["XDG_RUNTIME_DIR"], upstream_path)
    LOG = os.fdopen(1, "w", buffering=1) if log_path == "-" else open(log_path, "a", buffering=1)
    log(0, f"proxy start; {len(IFACES)} interfaces known; listen={listen_path} upstream={upstream_path}")
    if os.path.exists(listen_path):
        os.unlink(listen_path)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(listen_path)
    srv.listen(8)
    signal.signal(signal.SIGTERM, lambda *_: (log(0, "SIGTERM"), os._exit(0)))
    conns = []
    signal.signal(signal.SIGUSR1, lambda *_: [c.nudge() for c in list(conns)])
    cid = 0
    while True:
        client, _ = srv.accept()
        cid += 1
        up = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        up.connect(upstream_path)
        conn = Conn(cid)
        conn.up = up
        conn.down = client
        conns.append(conn)
        log(cid, "client connected")
        for args in ((client, up, "c2s"), (up, client, "s2c")):
            threading.Thread(target=pump, args=(conn, *args, None), daemon=True).start()


if __name__ == "__main__":
    main()
