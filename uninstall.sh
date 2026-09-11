#!/usr/bin/env bash
# Remove PiSugar support and put Bjorn back the way it was.

set -euo pipefail

BJORN_DIR="${BJORN_DIR:-/home/bjorn/Bjorn}"
DISPLAY_PY="$BJORN_DIR/display.py"
CONFIG_JSON="$BJORN_DIR/config/shared_config.json"

GREEN=$'\033[0;32m'; YELLOW=$'\033[1;33m'; RED=$'\033[0;31m'; NC=$'\033[0m'
ok()   { echo "${GREEN}  ok${NC}   $*"; }
skip() { echo "${YELLOW}  skip${NC} $*"; }
die()  { echo "${RED}error${NC}  $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run this with sudo"

echo
echo "Removing PiSugar support from $BJORN_DIR"
echo

echo "1. systemd"
systemctl disable --now pisugar-guard.timer 2>/dev/null || true
systemctl disable pisugar-poweroff.service 2>/dev/null || true
rm -f /etc/systemd/system/pisugar-poweroff.service \
      /etc/systemd/system/pisugar-guard.service \
      /etc/systemd/system/pisugar-guard.timer
systemctl daemon-reload
ok "units removed"

echo
echo "2. display.py"
if [ -f "$DISPLAY_PY.stock" ]; then
  mv "$DISPLAY_PY.stock" "$DISPLAY_PY"
  chown bjorn:bjorn "$DISPLAY_PY"
  ok "restored from display.py.stock"
else
  skip "no display.py.stock - leaving display.py alone"
fi

echo
echo "3. Module"
rm -f "$BJORN_DIR/pisugar.py"
rm -rf "$BJORN_DIR/__pycache__/pisugar."*
ok "pisugar.py removed"

echo
echo "4. Config keys"
python3 - "$CONFIG_JSON" <<'PYEOF'
import io, json, sys
path = sys.argv[1]
try:
    config = json.load(io.open(path, encoding="utf-8"))
except Exception as e:
    sys.exit(f"could not read config: {e}")
keys = ["__title_pisugar__", "pisugar_show_battery", "pisugar_autoshutdown",
        "pisugar_shutdown_percent", "pisugar_cut_power", "pisugar_cut_power_delay",
        "pisugar_model"]
removed = [k for k in keys if config.pop(k, None) is not None]
if removed:
    json.dump(config, io.open(path, "w", encoding="utf-8"), indent=4)
    print("removed: " + ", ".join(removed))
else:
    print("nothing to remove")
PYEOF
chown bjorn:bjorn "$CONFIG_JSON" 2>/dev/null || true
ok "config cleaned"

echo
echo "5. Restarting Bjorn"
systemctl restart bjorn 2>/dev/null || true
ok "back to stock"
echo
