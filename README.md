# waydroid-windows

**English** | [한국어](README.ko.md)

Makes Waydroid's multi-window mode usable on GNOME. Each Android app gets its own desktop window
that stays open, resizes from its edges, moves and maximizes from its caption, and minimizes to
the dock.

It is a Wayland proxy, `wlproxy.py`, between Waydroid's hardware composer (HWC) and GNOME's
compositor. It rewrites the few messages where the two disagree, and moves Android's tasks to
match what the desktop does with their windows.

## What it fixes

- **Windows that vanish on focus change.** Waydroid's vendor image
  `lineage-20.0-20260927-MAINLINE` hotplugs the Android display on every `xdg_toplevel.configure`
  that carries a size ([waydroid#2436](https://github.com/waydroid/waydroid/issues/2436)). GNOME
  sends one on each focus change, and every app window is erased. The proxy zeroes those sizes.
- **Resize, move, maximize, minimize.** A 12 px ring around each window resizes it from any edge or
  corner. GNOME does the resize, the proxy gives the Android task the same bounds and holds the
  acknowledgement until Android has drawn them, so a left or top edge keeps the opposite edge
  still. Dragging the caption moves the window. The caption's maximize and minimize buttons become
  the desktop's own: done inside Android, a fullscreen task hides every other task, and a
  minimized one loses its window and its dock icon.
- **Windows that shrink or turn tiny.** Once the HWC knows the fractional scale, the proxy drops
  `wl_output.scale`. GNOME re-sends it on a monitor layout change, and the HWC would then draw
  every window at 0.625x of its size for the rest of the session.
- **A screen-sized keyboard that eats clicks.** With a hardware keyboard, Android still maps an
  empty on-screen keyboard window the size of the display. It stays unmapped unless Android's
  "Use on-screen keyboard" setting is on.
- **New windows that hang.** A race in the HWC can leave `window::create` waiting forever for an
  event. The proxy sends an empty one after every sync answer to wake it.
- **The Super key.** It is filtered out, since Android opens its assistant search on it.

Tasks are driven through `wdtask/WdTask.java`, a small dex run as the shell user over one
persistent `adb shell app_process`. It uses Android's window organizer through reflection,
because `cmd activity task resize` only resizes freeform tasks and no shell command changes a
task's windowing mode.

It also handles One UI's window decorations (frames drawn from the content rather than the
shadow margin, and One UI's caption buttons), for images that run Samsung's framework.

## Requirements

- GNOME on Wayland. Tested with GNOME 50 on Fedora 44, one monitor at 125% scale.
- Waydroid with multi-window on, and its display the size of your monitor in logical pixels:

  ```sh
  sudo waydroid prop set persist.waydroid.multi_windows true
  sudo waydroid prop set persist.waydroid.width 1536   # 1920x1200 at 125%
  sudo waydroid prop set persist.waydroid.height 960
  ```

- `adb`, which reaches the container at Waydroid's default address `192.168.240.112:5555`
  (`WLPROXY_ADB` to change it). Let every app be resized:
  `adb shell settings put global force_resizable_activities 1`.
- Python 3, standard library only, plus the protocol files it decodes traffic with: `wayland.xml`
  (`wayland-devel` on Fedora, `libwayland-dev` on Debian) and the xdg-shell, viewporter and
  fractional-scale files (`wayland-protocols`, or Qt 6's copies, which Fedora installs with
  `qt6-qtbase-gui`).

## Install

```sh
git clone https://github.com/RedHatOnTop/waydroid-windows.git
cd waydroid-windows
./install.sh
waydroid session stop
```

`install.sh` installs `waydroid-wlproxy.service` as a user service and a `waydroid` wrapper in
`~/.local/bin` that starts the service if needed and points Waydroid at the proxy's socket.
Waydroid's own desktop entries call plain `waydroid`, so app launchers go through it too.
`waydroid status` shows `Wayland display: .../wayland-wd` once a session uses the proxy, and
`journalctl --user -u waydroid-wlproxy` has its log. `./install.sh --remove` takes both out.

- Restarting the service under a running session cuts the HWC's connection, so stop the session
  first.
- Open apps from their launchers or with `waydroid app launch`. `am start` leaves Waydroid's list
  of active apps empty, and the HWC then creates no window at all.

## Options

Environment variables of `wlproxy.py`, set in the service:

| Variable | Default | |
|---|---|---|
| `WLPROXY_ADB` | `192.168.240.112:5555` | adb address of the container |
| `WLPROXY_RING` | `12` | width of the resize ring in pixels |
| `WLPROXY_QUIET` | `0` | `1` logs window-level messages and the proxy's own actions, not all traffic (the service sets it) |
| `WLPROXY_TASKD` | `1` | `0` leaves Android tasks alone: no resizing or moving |
| `WLPROXY_GESTURES` | `1` | `0` stops acting on GNOME's events: the ring, the caption, the Super key |
| `WLPROXY_GEOMETRY` | `1` | `0` passes the HWC's requests through unchanged: its window geometry, its keyboard window |
| `WLPROXY_REWRITE` | `1` | `0` passes configure sizes through, hotplugs included |
| `WLPROXY_TASK_DEX` | `wdtask/wdtask.dex` | the WdTask build to push |
| `WLPROXY_HOSTCLIP` | `0` | `1` passes image-only host clipboards to an Android app serving `content://compat.hostclip`, which is not part of this repository |

`wdtask/build.sh` rebuilds `wdtask.dex` after a change to `WdTask.java`; it needs a JDK and
fetches Google's D8.

## Tests

```sh
for t in tests/*_test.py; do python3 -I "$t"; done
```

They feed the proxy message shapes recorded from the live HWC and check what it forwards and
injects, with no Waydroid running. The proxy has run as the author's user service since
2026-10-08. Its drag, resize and maximize checks ran in a nested GNOME session with a harness that
is not part of this repository.

## Limits

- A window the HWC recreates comes back at a new position.
- A second monitor, touch and pens have not been tried.
- Pop-ups and dialogs can still sit or size oddly.
- The clipboard works both ways for text, but only while a Waydroid window has the keyboard: the
  HWC is an ordinary Wayland client and gets the selection only then.

## License

GPL-3.0-or-later.
