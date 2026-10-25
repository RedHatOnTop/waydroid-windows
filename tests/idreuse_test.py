"""An injection decided for an object must not reach a newer object that reuses its id."""
import importlib.util
import io
import os
import socket
import struct

os.environ["WLPROXY_TASKD"] = "0"
spec = importlib.util.spec_from_file_location("wlproxy", os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "wlproxy.py"))
wp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wp)
wp.LOG = io.StringIO()


def msg(oid, opcode, fmt="", *args):
    body = struct.pack("<" + fmt, *args) if fmt else b""
    return struct.pack("<II", oid, ((8 + len(body)) << 16) | opcode) + body


c = wp.Conn(1)
c.objs.update({2: "wl_compositor", 4: "xdg_wm_base", 24: "wl_surface"})
c.up, server = socket.socketpair()
server.settimeout(0.2)
c.feed("c2s", msg(4, 2, "II", 27, 24) + msg(27, 1, "I", 28))   # xdg_surface#27, xdg_toplevel#28
top_ref = c.ref(28)
assert c.objs[28] == "xdg_toplevel"
c.feed("c2s", msg(28, 0))                                          # the HWC destroys the toplevel
c.feed("s2c", msg(1, 1, "I", 28))                                  # delete_id
c.feed("c2s", msg(2, 0, "I", 28))                                  # and reuses 28 for a wl_surface
assert c.objs[28] == "wl_surface"
assert not c.send_c2s(msg(28, 7, "ii", 1510, 934), top_ref), "stale toplevel ref must be refused"
try:
    leaked = server.recv(64)
except socket.timeout:
    leaked = b""
assert leaked == b"", "nothing may reach the compositor"
assert c.send_c2s(msg(28, 6), c.ref(28)), "a fresh ref to the new surface goes through"
assert server.recv(64) == msg(28, 6)
print("ALL OK")
