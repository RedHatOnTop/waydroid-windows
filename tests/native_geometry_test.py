"""Regressions for One UI surface insets and committed viewport geometry."""
import importlib.util
import io
import os
from pathlib import Path
import struct
import time
from unittest.mock import Mock, patch
import unittest
from dataclasses import replace

os.environ["WLPROXY_TASKD"] = "0"
ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("wlproxy", ROOT / "wlproxy.py")
wp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wp)
wp.LOG = io.StringIO()


class Tasks:
    display = (1920, 1200)
    density = 225


class NativeGeometryTest(unittest.TestCase):
    def setUp(self):
        wp.TASKS = Tasks()
        self.c = wp.Conn(1)
        self.c.dest = {10: (1536, 960), 36: (1087, 776)}
        self.c.pos = {36: (0, 0)}
        self.c.kids = {10: {36}}
        self.c.mapped = {36: True}
        self.c.main_surface = {10: 12}
        self.c.appid = {12: "waydroid.com.sec.android.gallery3d"}
        self.c.task_rect = {10: (16, 49, 1245, 857)}

    def test_live_oneui_frame_excludes_113_pixel_shadow(self):
        self.c.objs = {20: "zwp_linux_buffer_params_v1", 36: "wl_surface", 40: "wp_viewport"}
        self.c.viewport_of = {40: 36}
        self.c.track_c2s(20, 3, struct.pack("<IiiII", 60, 1455, 1034, 0, 0), bytearray())
        self.c.track_c2s(36, 1, struct.pack("<Iii", 60, 0, 0), bytearray())
        self.c.track_c2s(40, 1, struct.pack("<iiii", 97*256, 64*256, 1358*256, 970*256), bytearray())
        self.c.track_c2s(36, 6, b"", bytearray())
        dump = """  Window #0 Window{abc u0 com.sec.android.gallery3d/Activity}:
    mOwnerUid=10081 package=com.sec.android.gallery3d appop=NONE
    mAttrs={(0,0)(fillxfill) ty=BASE_APPLICATION fmt=TRANSLUCENT surfaceInsets=Rect(113, 113 - 113, 113)}
    mHasSurface=true isReadyForDisplay()=true
    Frames: parent=[16,49][1245,857] display=[16,49][1245,857] frame=[16,49][1245,857]
     surface=[113,113][113,113]
"""
        if hasattr(self.c, "surface_geometry"):
            self.c.surface_geometry = self.c.surface_geometry.with_windows(10, dump, self.c.pkg(10))
        got = self.c.app_rect(10, self.c.layer_rects(10))
        self.assertLessEqual(abs(got[2] - 983), 1, got)
        self.assertLessEqual(abs(got[3] - 646), 1, got)
        self.assertEqual(got[:2], (13, 39))

    def test_surfaceview_rooted_window_reads_its_package_layouts(self):
        from wlproxy_geometry import SurfaceGeometry
        self.c.appid = {12: "waydroid.SurfaceView[com.sec.android.gallery3d"}
        self.c.task_rect[10] = (16, 49, 766, 674)
        self.assertEqual(self.c.pkg(10), "com.sec.android.gallery3d")
        dump = """  Window #0 Window{abc u0 com.sec.android.gallery3d/Activity}:
    package=com.sec.android.gallery3d mHasSurface=true
    Frames: frame=[16,49][766,674]
     surface=[113,113][113,113]
"""
        self.c.surface_geometry = SurfaceGeometry(current={36: {"buffer": (976, 851), "source": (97, 64, 879, 787)}})
        self.c.surface_geometry = self.c.surface_geometry.with_windows(10, dump, self.c.pkg(10))
        self.assertEqual(self.c.app_rect(10, [(36, 0, 0, 704, 630)]), (13, 39, 600, 500))

    def test_oneui_caption_close_and_maximize_are_not_swapped(self):
        wp.TASKS.oneui = True
        self.c.app_rect = lambda *_: (13, 27, 600, 500)
        unit = wp.TASKS.density / 160 / 1.25
        for name, offset in (("menu", 34), ("close", 78), ("maximize", 122), ("minimize", 167)):
            self.assertEqual(self.c.caption_button(10, 613 - offset * unit, 40), name)

    def test_recent_restore_size_is_not_mistaken_for_focus_echo(self):
        self.c.geom[10] = (0, 27, 1536, 928)
        self.c.restore[10] = (600, 500)
        self.c.host_max[10] = True
        self.c.xdg_surface_of[11] = 10
        self.c.toplevel_xdg[12] = 11
        self.c.objs[11] = "xdg_surface"
        self.c.sent_sizes[10] = ((time.monotonic(), (600, 500)),)
        self.c.place = lambda *args, **kwargs: None
        self.c.expect_commit = lambda *_: None
        self.c.follow_configure(12, 600, 500, {4})
        configure = bytearray(struct.pack("<III", 11, 12 << 16, 42))
        self.c.rewrite_s2c(11, 0, configure)
        with patch.object(wp.threading, "Timer"):
            self.assertTrue(self.c.hold_ack(11, 42))
        self.assertEqual(self.c.held_ack[11], (42, (600, 500)))

    def test_activity_buffer_after_starting_window_reads_layouts_again(self):
        from wlproxy_geometry import SurfaceGeometry, WindowLayout
        self.c.dest[36] = (1536, 960)
        self.c.task_rect[10] = (16, 34, 1904, 1138)
        self.c.surface_geometry = SurfaceGeometry(
            windows={10: (WindowLayout((1888, 1104), (0,) * 4),)},
            current={36: {"buffer": (2114, 1330), "source": (97, 79, 1920, 1200)}})
        dump = """  Window #0 Window{abc u0 com.sec.android.gallery3d/Activity}:
    package=com.sec.android.gallery3d mHasSurface=true
    Frames: parent=[16,34][1904,1138] display=[16,34][1904,1138] frame=[16,34][1904,1138]
     surface=[113,113][113,113]
"""
        reads, refreshed = [], []
        wp.TASKS.adb = lambda *args: reads.append(args) or dump
        self.c.refresh_geometry = lambda main, ref: refreshed.append(main)
        rects = self.c.layer_rects(10)
        self.assertEqual(self.c.app_rect(10, rects), (0, 0, 1536, 960))
        with patch.object(wp.threading, "Thread", Inline):
            self.c.refresh_windows(10, rects)
            self.c.refresh_windows(10, rects)
        self.assertEqual(len(reads), 1)
        self.assertEqual(refreshed, [10])
        self.assertEqual(self.c.app_rect(10, rects), (13, 27, 1510, 883))
        self.assertEqual(self.c.windows_busy, frozenset())

    def test_resize_ack_does_not_accept_a_frame_three_pixels_short(self):
        self.c.held_ack[11] = (42, (680, 500))
        self.assertEqual(self.c.ack_for_commit(11, (13, 27, 677, 500)), b"")
        self.assertEqual(self.c.ack_for_commit(11, (13, 27, 680, 500)),
                         struct.pack("<III", 11, (12 << 16) | 4, 42))

    def test_resize_ack_is_held_when_current_frame_is_three_pixels_short(self):
        self.c.geom[10] = (13, 27, 677, 500)
        self.c.xdg_surface_of[11] = 10
        self.c.serial_conf[(11, 42)] = (680, 500, {3, 4})
        self.c.expect_commit = Mock()
        with patch.object(wp.threading, "Timer"):
            self.assertTrue(self.c.hold_ack(11, 42))
        self.c.expect_commit.assert_not_called()

    def test_maximized_repin_preserves_compositor_work_area(self):
        self.c.host_max[10] = True
        self.c.conf[12] = (1536, 928, {1, 4})
        self.c.place = Mock()
        self.c.repin(10)
        self.c.place.assert_called_once_with(10, (1536, 928), maximized=True)

    def test_maximized_repin_keeps_task_inside_android_display(self):
        self.c.stable_top = 34
        wp.TASKS.ensure = Mock()
        wp.TASKS.cmd = Mock()
        wp.TASKS.task = Mock(return_value=(5, (0, 34, 1920, 1200), (-1, -1)))
        wp.TASKS.submit = lambda key, job: job(wp.TASKS)
        self.c.place(10, None, maximized=True)
        wp.TASKS.cmd.assert_called_once_with("bounds com.sec.android.gallery3d 0 34 1920 1200")

    def test_buffer_no_layout_explains_is_not_read_again(self):
        from wlproxy_geometry import SurfaceGeometry, WindowLayout
        self.c.surface_geometry = SurfaceGeometry(windows={10: (WindowLayout((750, 625), (113,) * 4),)},
                                                  current={36: {"buffer": (1280, 720)}})
        reads = []
        wp.TASKS.adb = lambda *args: reads.append(args) or ""
        self.c.refresh_geometry = lambda *_: None
        with patch.object(wp.threading, "Thread", Inline):
            for _ in range(3):
                self.c.refresh_windows(10, self.c.layer_rects(10))
        self.assertEqual(len(reads), 1)
        self.assertEqual(self.c.windows_tried[10], {(1280, 720)})
        del self.c.task_rect[10]
        self.c.windows_tried.clear()
        self.c.refresh_windows(10, self.c.layer_rects(10))
        self.assertEqual(len(reads), 1)


class Inline:
    def __init__(self, target, daemon=None):
        self.target = target

    def start(self):
        self.target()



class SurfaceGeometryTest(unittest.TestCase):
    def setUp(self):
        from wlproxy_geometry import SurfaceGeometry, WindowLayout
        self.model = SurfaceGeometry(windows={10: (WindowLayout((750, 625), (113,) * 4),)})

    def track(self, model, iface, oid, name, fmt="", *args):
        return model.track(iface, oid, name, struct.pack("<" + fmt, *args), {40: 36})

    def test_buffer_factories(self):
        cases = (("wl_shm_pool", "create_buffer", "IIiiii", (60, 0, 976, 851, 3904, 0)),
                 ("wl_drm", "create_buffer", "IIiiI", (60, 0, 976, 851, 0)),
                 ("wl_drm", "create_prime_buffer", "IiiI", (60, 976, 851, 0)),
                 ("android_wlegl", "create_buffer", "Iiii", (60, 976, 851, 976)),
                 ("zwp_linux_buffer_params_v1", "create_immed", "IiiII", (60, 976, 851, 0, 0)))
        for iface, name, fmt, args in cases:
            with self.subTest(iface=iface, name=name):
                got = self.track(self.model, iface, 20, name, fmt, *args)
                self.assertEqual(got.buffers[60], (976, 851))
                self.assertEqual(self.model.buffers, {})

    def test_async_dmabuf(self):
        model = self.track(self.model, "zwp_linux_buffer_params_v1", 20, "create", "iiII", 976, 851, 0, 0)
        created = model.created(20, 60)
        self.assertEqual(created.buffers[60], (976, 851))
        self.assertEqual(created.params, {})
        self.assertIs(created.created(99, 61), created)
        self.assertEqual(self.track(model, "zwp_linux_buffer_params_v1", 20, "destroy").params, {})

    def ready(self):
        model = replace(self.model, buffers={60: (976, 851)})
        return self.track(model, "wl_surface", 36, "attach", "Iii", 60, 0, 0)

    def test_attach_and_crop_wait_for_commit(self):
        pending = self.track(self.ready(), "wp_viewport", 40, "set_source", "iiii", 97*256, 64*256, 879*256, 787*256)
        self.assertIsNone(pending.geometry(10, [(36, 0, 0, 704, 630)]))
        current = self.track(pending, "wl_surface", 36, "commit")
        self.assertEqual(current.geometry(10, [(36, 0, 0, 704, 630)]), (13, 39, 601, 500))
        changed = self.track(current, "wp_viewport", 40, "set_source", "iiii", -256, -256, -256, -256)
        self.assertEqual(changed.current, current.current)
        self.assertIsNone(changed.pending[36]["source"])

    def test_buffer_destroy_does_not_unmap_committed_content(self):
        current = self.track(self.ready(), "wl_surface", 36, "commit")
        destroyed = self.track(current, "wl_buffer", 60, "destroy")
        self.assertEqual(destroyed.buffers, {})
        self.assertEqual(destroyed.current[36]["buffer"], (976, 851))
        detached = self.track(destroyed, "wl_surface", 36, "attach", "Iii", 0, 0, 0)
        self.assertIsNone(self.track(detached, "wl_surface", 36, "commit").geometry(10, [(36, 0, 0, 781, 681)]))

    def test_surface_reuse_clears_committed_and_pending_state(self):
        current = self.track(self.ready(), "wl_surface", 36, "commit")
        destroyed = self.track(current, "wl_surface", 36, "destroy")
        self.assertEqual(destroyed.current, {})
        self.assertEqual(destroyed.pending, {})
        self.assertEqual(self.track(destroyed, "wl_surface", 10, "destroy").windows, {})

    def test_unknown_or_transformed_buffers_use_legacy_fallback(self):
        current = self.track(self.ready(), "wl_surface", 36, "commit")
        for name, value in (("set_buffer_transform", 1), ("set_buffer_scale", 2)):
            changed = self.track(current, "wl_surface", 36, name, "i", value)
            changed = self.track(changed, "wl_surface", 36, "commit")
            self.assertIsNone(changed.geometry(10, [(36, 0, 0, 781, 681)]))
        self.assertIsNone(current.geometry(99, [(36, 0, 0, 781, 681)]))
        self.assertIs(self.track(current, "other", 3, "other"), current)

    def test_layout_parsing_validates_owner_and_rectangles(self):
        from wlproxy_geometry import window_layouts
        block = "  Window #0 Window{{abc}}:\n package={package} mHasSurface={surface}\n frame=[16,49][766,674]\n surface=[113,113][113,113]\n"
        good = block.format(package="com.example.app", surface="true")
        self.assertEqual(window_layouts(good, "com.example.app"), self.model.windows[10])
        for bad in (good.replace("766", "15"), good.replace("surface=[", "missing=["),
                    block.format(package="com.example.app.other", surface="true"),
                    block.format(package="com.example.app", surface="false"), "malformed"):
            self.assertEqual(window_layouts(bad, "com.example.app"), ())
        self.assertIs(self.model.with_windows(10, "", "com.example.app"), self.model)

    def test_crop_clipping_and_invalid_inputs(self):
        from wlproxy_geometry import content_rect
        layer = (36, 0, 0, 800, 600)
        self.assertEqual(content_rect(layer, (1000, 750), None, (0,) * 4), (0, 0, 800, 600))
        self.assertEqual(content_rect(layer, (1000, 750), (100, 100, 800, 600), (0,) * 4), (0, 0, 800, 600))
        for buffer, source, insets in (((0, 750), None, (0,) * 4),
                                       ((1000, 750), (0, 0, 0, 1), (0,) * 4),
                                       ((1000, 750), None, (-1,) * 4),
                                       ((1000, 750), None, (1000,) * 4)):
            self.assertIsNone(content_rect(layer, buffer, source, insets))

    def test_split_layers_form_a_single_window(self):
        from wlproxy_geometry import WindowLayout
        model = replace(self.model, windows={10: (WindowLayout((400, 500), (0,) * 4),)},
                        current={36: {"buffer": (400, 500)}, 37: {"buffer": (600, 500)}})
        self.assertEqual(model.geometry(10, [(36, 12, 12, 320, 400), (37, 332, 12, 480, 400)]), (12, 12, 800, 400))

    def test_status_bar_origin_avoids_oneui_freeform_offset(self):
        from wlproxy_geometry import stable_top
        dump = "  Window #0 Window{bar}:\n mAttrs={ty=STATUS_BAR}\n frame=[0,0][1920,34]\n"
        self.assertEqual(stable_top(dump), 34)
        self.assertEqual(stable_top(dump.replace("STATUS_BAR", "STATUS_BAR_PANEL")), 0)
        self.assertEqual(stable_top(dump.replace("frame=", "missing=")), 0)
        self.assertEqual(stable_top(""), 0)

    def test_solid_color_background_does_not_expand_zero_inset_window(self):
        from wlproxy_geometry import WindowLayout
        model = replace(self.model, windows={10: (WindowLayout((400, 500), (0,) * 4),)},
                        current={36: {"buffer": (400, 500)}, 37: {"buffer": (1, 1)}})
        self.assertEqual(model.geometry(10, [(36, 12, 12, 320, 400), (37, 0, 0, 1536, 960)]), (12, 12, 320, 400))

    def test_input_region_excludes_large_shadow_on_parent_and_child(self):
        from wlproxy_geometry import input_region
        payload = input_region(10, 80, [(36, 0, 0, 1087, 776)], (13, 27, 600, 500), 8)
        messages = ()
        offset = 0
        while offset < len(payload):
            oid, header = struct.unpack_from("<II", payload, offset)
            size = header >> 16
            messages = (*messages, (oid, header & 0xffff, payload[offset+8:offset+size]))
            offset += size
        self.assertEqual([(oid, op) for oid, op, _ in messages],
                         [(80, 2), (80, 1), (10, 5), (80, 2), (80, 1), (36, 5), (36, 6)])
        self.assertEqual(struct.unpack("<iiii", messages[1][2]), (5, 19, 616, 516))
        self.assertEqual(struct.unpack("<iiii", messages[4][2]), (5, 19, 616, 516))

    def test_screen_sized_content_is_left_out_beside_other_layers(self):
        from wlproxy_geometry import WindowLayout
        splash = WindowLayout((1888, 1104), (0,) * 4)
        model = replace(self.model, windows={10: (splash,)},
                        current={46: {"buffer": (2114, 1330), "source": (97, 79, 1920, 1200)},
                                 59: {"buffer": (1888, 1104), "source": (0, 0, 1888, 1104)}})
        layers = [(59, 12, 27, 1511, 884), (46, 0, 0, 1536, 960)]
        self.assertEqual(model.geometry(10, layers), (0, 0, 1536, 960))
        self.assertEqual(model.geometry(10, layers, (1536, 960)), (12, 27, 1511, 884))
        self.assertEqual(model.geometry(10, layers[1:], (1536, 960)), (0, 0, 1536, 960))
        self.assertEqual(model.unknown_buffers(10, layers), {(2114, 1330)})
        fresh = replace(model, windows={10: (splash, WindowLayout((1888, 1104), (113,) * 4))})
        self.assertEqual(fresh.unknown_buffers(10, layers), frozenset())
        self.assertEqual(fresh.geometry(10, layers[1:], (1536, 960)), (13, 27, 1510, 883))

    def test_ambiguous_insets_require_matching_buffer(self):
        from wlproxy_geometry import WindowLayout
        model = replace(self.model, windows={10: (*self.model.windows[10], WindowLayout((400, 500), (0,) * 4))},
                        current={36: {"buffer": (400, 500)}})
        self.assertEqual(model.geometry(10, [(36, 0, 0, 320, 400)]), (0, 0, 320, 400))
        unknown = replace(model, current={36: {"buffer": (800, 500)}})
        self.assertIsNone(unknown.geometry(10, [(36, 0, 0, 640, 400)]))


if __name__ == "__main__":
    unittest.main()
