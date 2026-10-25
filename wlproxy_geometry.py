"""Window geometry from Android surface insets and committed Wayland buffers."""
from dataclasses import dataclass, field, replace
import re
import struct


@dataclass(frozen=True)
class WindowLayout:
    size: tuple[int, int]
    insets: tuple[int, int, int, int]


def window_layouts(dump, package):
    layouts = []
    for block in re.split(r"(?m)^  Window #\d+ ", dump)[1:]:
        owner = re.search(r"\bpackage=(\S+)", block)
        if not owner or owner[1] != package or "mHasSurface=true" not in block:
            continue
        frame = re.search(r"\bframe=\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]", block)
        inset = re.search(r"\bsurface=\[(\d+),(\d+)\]\[(\d+),(\d+)\]", block)
        if not frame or not inset:
            continue
        left, top, right, bottom = map(int, frame.groups())
        margins = tuple(map(int, inset.groups()))
        if right > left and bottom > top:
            layouts = [*layouts, WindowLayout((right - left, bottom - top), margins)]
    return tuple(layouts)


def stable_top(dump):
    for block in re.split(r"(?m)^  Window #\d+ ", dump)[1:]:
        if not re.search(r"\bty=STATUS_BAR\b", block):
            continue
        frame = re.search(r"\bframe=\[0,0\]\[\d+,(\d+)\]", block)
        if frame:
            return int(frame[1])
    return 0


def content_rect(layer, buffer, source, insets):
    """Project the non-shadow buffer rectangle through the viewport, including clipping."""
    _, x, y, width, height = layer
    bw, bh = buffer
    sx, sy, sw, sh = source or (0, 0, bw, bh)
    left, top, right, bottom = insets
    if min(bw, bh, sw, sh, width, height) <= 0 or min(insets) < 0:
        return None
    x0, y0 = max(left, sx), max(top, sy)
    x1, y1 = min(bw - right, sx + sw), min(bh - bottom, sy + sh)
    if x1 <= x0 or y1 <= y0:
        return None
    return (round(x + (x0 - sx) * width / sw), round(y + (y0 - sy) * height / sh),
            round((x1 - x0) * width / sw), round((y1 - y0) * height / sh))


def input_region(main, region, layers, geometry, border):
    x, y, width, height = geometry
    extent = 0x3fffffff
    clear = struct.pack("<IIiiii", region, (24 << 16) | 2, -extent, -extent, 2 * extent, 2 * extent)
    surfaces = ((main, 0, 0), *((layer[0], layer[1], layer[2]) for layer in layers))
    messages = tuple(
        clear + struct.pack("<IIiiii", region, (24 << 16) | 1,
                            x - left - border, y - top - border, width + 2 * border, height + 2 * border)
        + struct.pack("<III", surface, (12 << 16) | 5, region)
        + (struct.pack("<II", surface, (8 << 16) | 6) if surface != main else b"")
        for surface, left, top in surfaces
    )
    return b"".join(messages)


def padded(layout):
    left, top, right, bottom = layout.insets
    return layout.size[0] + left + right, layout.size[1] + top + bottom


def without(mapping, key):
    return {k: v for k, v in mapping.items() if k != key}


@dataclass(frozen=True)
class SurfaceGeometry:
    buffers: dict = field(default_factory=dict)
    pending: dict = field(default_factory=dict)
    current: dict = field(default_factory=dict)
    params: dict = field(default_factory=dict)
    windows: dict = field(default_factory=dict)

    def with_windows(self, main, dump, package):
        layouts = window_layouts(dump, package)
        return replace(self, windows={**self.windows, main: layouts}) if layouts else self

    def surface_state(self, surface, **values):
        state = {**self.current.get(surface, {}), **self.pending.get(surface, {}), **values}
        return replace(self, pending={**self.pending, surface: state})

    def track(self, iface, oid, name, body, viewport_of):
        if iface == "wl_buffer" and name == "destroy":
            return replace(self, buffers=without(self.buffers, oid))
        if iface == "wl_surface":
            return self.track_surface(oid, name, body)
        if iface == "wp_viewport" and name == "set_source":
            source = tuple(v / 256 for v in struct.unpack_from("<iiii", body))
            return self.surface_state(viewport_of.get(oid), source=None if source == (-1,) * 4 else source)
        if iface == "zwp_linux_buffer_params_v1":
            if name == "create_immed":
                buffer, width, height = struct.unpack_from("<Iii", body)
                return replace(self, buffers={**self.buffers, buffer: (width, height)})
            if name == "create":
                return replace(self, params={**self.params, oid: struct.unpack_from("<ii", body)})
            if name == "destroy":
                return replace(self, params=without(self.params, oid))
        if iface in ("wl_shm_pool", "wl_drm", "android_wlegl") and name in ("create_buffer", "create_prime_buffer"):
            buffer = struct.unpack_from("<I", body)[0]
            offset = 4 if iface == "android_wlegl" or name == "create_prime_buffer" else 8
            return replace(self, buffers={**self.buffers, buffer: struct.unpack_from("<ii", body, offset)})
        return self

    def track_surface(self, surface, name, body):
        if name == "attach":
            buffer = struct.unpack_from("<I", body)[0]
            return self.surface_state(surface, buffer=self.buffers.get(buffer))
        if name in ("set_buffer_transform", "set_buffer_scale"):
            key = "transform" if name == "set_buffer_transform" else "scale"
            return self.surface_state(surface, **{key: struct.unpack_from("<i", body)[0]})
        if name == "commit":
            state = self.pending.get(surface)
            if state is not None:
                return replace(self, current={**self.current, surface: state}, pending=without(self.pending, surface))
        if name == "destroy":
            return replace(self, current=without(self.current, surface), pending=without(self.pending, surface),
                           windows=without(self.windows, surface))
        return self

    def created(self, params, buffer):
        size = self.params.get(params)
        if size is None:
            return self
        return replace(self, buffers={**self.buffers, buffer: size}, params=without(self.params, params))

    def geometry(self, main, layers, screen=None):
        """Union of the layers' content. Content covering the whole screen (the main surface) is left
        out while other layers have some: a window that is not maximized never fills the screen, so
        that is a buffer read with the wrong insets (the starting window's none on the activity's
        shadow) until refresh_windows has read the layouts again."""
        layouts = self.windows.get(main, ())
        if not layouts:
            return None
        margins = {layout.insets for layout in layouts}
        rects = tuple(self.layer_rect(layer, layouts, margins) for layer in layers)
        visible = tuple(rect for rect in rects if rect and min(rect[2:]) > 0)
        if screen:
            inner = tuple(r for r in visible if r[2] < screen[0] - 2 or r[3] < screen[1] - 2)
            visible = inner or visible
        if not visible:
            return None
        left, top = min(r[0] for r in visible), min(r[1] for r in visible)
        return (left, top, max(r[0] + r[2] for r in visible) - left,
                max(r[1] + r[3] for r in visible) - top)

    def unknown_buffers(self, main, layers):
        """Sizes of committed layer buffers that no known window layout of the task accounts for."""
        sizes = {padded(layout) for layout in self.windows.get(main, ())}
        buffers = (self.current.get(layer[0], {}).get("buffer") for layer in layers)
        return frozenset(b for b in buffers if b and min(b) > 1 and b not in sizes)

    def layer_rect(self, layer, layouts, margins):
        state = self.current.get(layer[0], {})
        buffer = state.get("buffer")
        if not buffer or min(buffer) <= 1 or state.get("transform", 0) or state.get("scale", 1) != 1:
            return None
        matches = tuple(layout.insets for layout in layouts if buffer == padded(layout))
        insets = matches[0] if matches else next(iter(margins)) if len(margins) == 1 else None
        if insets is None:
            return None
        return content_rect(layer, buffer, state.get("source"), insets)
