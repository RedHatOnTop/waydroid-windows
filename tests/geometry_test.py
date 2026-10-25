"""wlproxy.Conn.app_rect against layer/task numbers taken from the live HWC at scale 1.25."""
import importlib.util
import io
import os

os.environ["WLPROXY_TASKD"] = "0"
spec = importlib.util.spec_from_file_location("wlproxy", os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "wlproxy.py"))
wp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wp)
wp.LOG = io.StringIO()


class FakeTasks:
    display = (1920, 1200)


wp.TASKS = FakeTasks()
c = wp.Conn(1)
c.dest[10] = (1536, 960)


def rect(task, layers):
    c.task_rect[10] = task
    return c.app_rect(10, [(i, *l) for i, l in enumerate(layers)])


# Instagram at ORIGIN, ring on every side (live: GEOMETRY 12,12 472x740, layer 0,0 492x764)
assert rect((16, 16, 606, 941), [(0, 0, 492, 764)]) == (12, 12, 472, 740)
# maximized at the display corner: ring cut on the left, top and right, kept at the bottom
assert rect((0, 0, 1920, 1160), [(0, 0, 1536, 940)]) == (0, 0, 1536, 928)
# a task reaching the display's right edge from ORIGIN: no ring on the right
assert rect((16, 16, 1920, 800), [(0, 0, 1536, 652)]) == (12, 12, 1523, 627)
# mid-resize, layers not caught up: size from the layers, not the task
assert rect((16, 16, 606, 941), [(0, 0, 400, 764)]) == (12, 12, 376, 740)
# Settings' two-pane layout has no ring: learned at ORIGIN, then nothing is taken off when maximized
c.ring.clear()
assert rect((16, 16, 1021, 641), [(12, 12, 804, 500)]) == (12, 12, 804, 500)
assert c.ring[10] == 0
assert rect((0, 0, 1920, 1160), [(0, 0, 559, 928), (558, 0, 978, 928)]) == (0, 0, 1536, 928)
# a ring app learns 12 and keeps it for the corner
c.ring.clear()
assert rect((16, 16, 606, 941), [(0, 0, 492, 764)]) == (12, 12, 472, 740) and c.ring[10] == 12
assert rect((0, 0, 1920, 1160), [(0, 0, 1536, 940)]) == (0, 0, 1536, 928)
# a transient frame (layers still where the task was) teaches nothing
c.ring[10] = 0
rect((16, 16, 1021, 641), [(0, 0, 559, 547), (558, 0, 292, 547)])
assert c.ring[10] == 0
# Settings learned a ring in its narrow layout, then goes two-pane (no ring) when maximized
c.ring[10] = 12
assert rect((0, 0, 1920, 1160), [(0, 0, 559, 928), (558, 0, 978, 928)]) == (0, 0, 1536, 928)
# and a ring app whose learned value is 0 still finds its ring at the corner
c.ring[10] = 0
assert rect((0, 0, 1920, 1160), [(0, 0, 1536, 940)]) == (0, 0, 1536, 928)
print("ALL OK")
