#!/bin/bash
# Installs the user service that runs wlproxy, and a `waydroid` wrapper in ~/.local/bin that
# routes Waydroid's Wayland connection through it.
#
#   ./install.sh            install and start the service
#   ./install.sh --remove   stop the service and take both out
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
unit=$HOME/.config/systemd/user/waydroid-wlproxy.service
wrapper=$HOME/.local/bin/waydroid

if [[ ${1:-} == --remove ]]; then
  systemctl --user disable --now waydroid-wlproxy.service 2>/dev/null || true
  rm -f "$unit"
  if [[ -f $wrapper ]] && grep -q waydroid-wlproxy "$wrapper"; then
    rm -f "$wrapper"
  fi
  systemctl --user daemon-reload
  echo "Removed. Restart the Waydroid session (waydroid session stop) to go back to the direct connection."
  exit 0
fi

if [[ ! -x /usr/bin/waydroid ]]; then
  echo "/usr/bin/waydroid was not found; install Waydroid first." >&2
  exit 1
fi
if [[ -e $wrapper ]] && ! grep -q waydroid-wlproxy "$wrapper"; then
  echo "$wrapper exists and is not this wrapper; move it aside and run again." >&2
  exit 1
fi
command -v adb >/dev/null || echo "adb was not found: windows will stay put, since resizing and moving go through it." >&2

mkdir -p "$(dirname "$unit")" "$(dirname "$wrapper")"
sed "s|@ROOT@|$here|g" "$here/systemd/waydroid-wlproxy.service" > "$unit"
install -m 755 "$here/bin/waydroid" "$wrapper"
systemctl --user daemon-reload
systemctl --user enable --now waydroid-wlproxy.service

case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) echo "$HOME/.local/bin is not on PATH, so plain \`waydroid\` still runs /usr/bin/waydroid." >&2 ;;
esac
echo "Installed. Stop a running session (waydroid session stop), then open Android apps as usual;"
echo "\`waydroid status\` shows \"Wayland display: ...wayland-wd\" once they go through the proxy."
