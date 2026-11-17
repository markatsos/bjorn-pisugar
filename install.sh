#!/usr/bin/env bash
# Install PiSugar support into an existing Bjorn installation.
# Safe to re-run: every step checks whether it has already been done.

set -euo pipefail

BJORN_DIR="${BJORN_DIR:-/home/bjorn/Bjorn}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DISPLAY_PY="$BJORN_DIR/display.py"
CONFIG_JSON="$BJORN_DIR/config/shared_config.json"

RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; YELLOW=$'\033[1;33m'; NC=$'\033[0m'
ok()   { echo "${GREEN}  ok${NC}   $*"; }
skip() { echo "${YELLOW}  skip${NC} $*"; }
warn() { echo "${YELLOW}  warn${NC} $*"; }
info() { echo "  ..   $*"; }
die()  { echo "${RED}error${NC}  $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run this with sudo"
# BJORN_DIR is written into the systemd units, so keep it to characters that
# need no quoting there.
case "$BJORN_DIR" in
  /*) ;;
  *) die "BJORN_DIR must be an absolute path" ;;
esac
case "$BJORN_DIR" in
  *[!A-Za-z0-9._/-]*) die "BJORN_DIR may only contain letters, digits and . _ - /" ;;
esac
[ -f "$DISPLAY_PY" ] || die "no Bjorn at $BJORN_DIR (set BJORN_DIR= to point elsewhere)"
[ -f "$CONFIG_JSON" ] || die "no config at $CONFIG_JSON"

BJORN_USER="$(stat -c '%U' "$BJORN_DIR")"

echo
echo "Installing PiSugar support into $BJORN_DIR"
echo

# ------------------------------------------------------------- what we found --
echo "1. This machine"
ARCH="$(uname -m)"
case "$ARCH" in
  aarch64) ok "architecture $ARCH (the tested one)" ;;
  armv6l|armv7l) warn "architecture $ARCH - untested, but nothing here is architecture-specific" ;;
  *) warn "architecture $ARCH - this is meant for a Raspberry Pi" ;;
esac

EPD_TYPE="$(python3 -c "
import json,io
try: print(json.load(io.open('$CONFIG_JSON')).get('epd_type',''))
except Exception: print('')
")"
case "$EPD_TYPE" in
  epd2in13*) PANEL_WIDTH=122; NARROW=yes ;;
  epd2in7*)  PANEL_WIDTH=176; NARROW=no  ;;
  "")        PANEL_WIDTH=122; NARROW=yes; warn "no epd_type in config - assuming a 2.13\" panel" ;;
  *)         PANEL_WIDTH=122; NARROW=yes; warn "unrecognised epd_type '$EPD_TYPE' - assuming a 2.13\" panel" ;;
esac
[ -n "$EPD_TYPE" ] && ok "display $EPD_TYPE (~${PANEL_WIDTH}px wide)"

# ---------------------------------------------------------------- packages ----
echo
echo "2. Packages"
missing=()
dpkg -s python3-smbus2 >/dev/null 2>&1 || missing+=(python3-smbus2)
dpkg -s i2c-tools      >/dev/null 2>&1 || missing+=(i2c-tools)
if [ ${#missing[@]} -gt 0 ]; then
  info "installing ${missing[*]}"
  apt-get update -qq
  if ! apt-get install -y "${missing[@]}" >/dev/null 2>&1; then
    warn "apt could not install ${missing[*]} - trying pip for smbus2"
    pip3 install smbus2 --break-system-packages >/dev/null
  fi
  ok "dependencies in place"
else
  skip "python3-smbus2 and i2c-tools already present"
fi

# --------------------------------------------------------------------- i2c ----
echo
echo "3. I2C"
if ! ls /dev/i2c-* >/dev/null 2>&1; then
  info "enabling I2C"
  raspi-config nonint do_i2c 0 || warn "raspi-config failed - enable I2C by hand"
  ok "I2C enabled - a reboot may be needed before the bus appears"
else
  skip "$(ls /dev/i2c-* | tr '\n' ' ')already present"
fi

FOUND_BOARD=""
if command -v i2cdetect >/dev/null; then
  for bus in 1 0; do
    [ -e "/dev/i2c-$bus" ] || continue
    table="$(i2cdetect -y "$bus" 2>/dev/null || true)"
    if echo "$table" | grep -q ' 57'; then
      FOUND_BOARD="PiSugar 3 (0x57) on i2c-$bus"; break
    elif echo "$table" | grep -q ' 75'; then
      FOUND_BOARD="PiSugar 2 / 2 Pro (0x75) on i2c-$bus"; break
    fi
  done
fi
if [ -n "$FOUND_BOARD" ]; then
  ok "$FOUND_BOARD"
  case "$FOUND_BOARD" in
    *0x75*) warn "PiSugar 2 boards are EXPERIMENTAL and read-only: battery level only,"
            warn "no charge state, no low-battery shutdown, no power cut"
            warn "please open an issue with what you see - good or bad" ;;
  esac
else
  warn "no PiSugar answering on any bus - is the board switched on?"
  warn "(installation continues; the indicator stays hidden until it answers)"
fi

# ------------------------------------------------------------------ module ----
echo
echo "4. Module"
install -o "$BJORN_USER" -g "$BJORN_USER" -m 644 "$SRC_DIR/pisugar.py" "$BJORN_DIR/pisugar.py"
ok "pisugar.py -> $BJORN_DIR/pisugar.py"

# ----------------------------------------------------------------- display ----
echo
echo "5. display.py"
if grep -q "from pisugar import" "$DISPLAY_PY"; then
  skip "already patched"
else
  [ -f "$DISPLAY_PY.stock" ] || cp "$DISPLAY_PY" "$DISPLAY_PY.stock"
  python3 - "$DISPLAY_PY" "$NARROW" <<'PYEOF'
import ast, io, sys

path, narrow = sys.argv[1], sys.argv[2] == "yes"
s = io.open(path, encoding="utf-8").read()

# Always: import the module, hold an instance, draw the indicator.
edits = [
 ("import subprocess  \n",
  "import subprocess  \nfrom pisugar import PiSugar, draw_battery\n"),
 ("        self.web_screen_reversed = self.shared_data.web_screen_reversed\n",
  "        self.web_screen_reversed = self.shared_data.web_screen_reversed\n"
  "        self.pisugar = PiSugar()\n"),
]

# On a 122px panel the header is full: shift the logo left and let the two
# network icons share one slot, otherwise the indicator has nowhere to go.
# A 2.7" panel has room to spare, so it keeps the stock layout.
if narrow:
    edits += [
     ('draw.text((int(37 * self.scale_factor_x), int(5 * self.scale_factor_y)), "BJORN"',
      'draw.text((int(23 * self.scale_factor_x), int(5 * self.scale_factor_y)), "BJORN"'),
     ("""                if self.shared_data.pan_connected:
                    image.paste(self.shared_data.connected, (int(104 * self.scale_factor_x), int(3 * self.scale_factor_y)))
                if self.shared_data.usb_active:
                    image.paste(self.shared_data.usb, (int(90 * self.scale_factor_x), int(4 * self.scale_factor_y)))
""",
      """                if self.shared_data.pan_connected:
                    image.paste(self.shared_data.connected, (int(76 * self.scale_factor_x), int(3 * self.scale_factor_y)))
                elif self.shared_data.usb_active:
                    image.paste(self.shared_data.usb, (int(76 * self.scale_factor_x), int(4 * self.scale_factor_y)))
                draw_battery(image, draw, self.shared_data, self.pisugar, self.scale_factor_x, self.scale_factor_y)
"""),
    ]
else:
    edits += [
     ("""                if self.shared_data.usb_active:
                    image.paste(self.shared_data.usb, (int(90 * self.scale_factor_x), int(4 * self.scale_factor_y)))
""",
      """                if self.shared_data.usb_active:
                    image.paste(self.shared_data.usb, (int(90 * self.scale_factor_x), int(4 * self.scale_factor_y)))
                draw_battery(image, draw, self.shared_data, self.pisugar, self.scale_factor_x, self.scale_factor_y)
"""),
    ]

for old, new in edits:
    n = s.count(old)
    if n != 1:
        sys.exit(f"display.py does not look like the expected version: "
                 f"found {n} matches for {old[:50]!r}")
    s = s.replace(old, new, 1)

ast.parse(s)
io.open(path, "w", encoding="utf-8").write(s)
PYEOF
  chown "$BJORN_USER:$BJORN_USER" "$DISPLAY_PY"
  if [ "$NARROW" = yes ]; then
    ok "patched, header rearranged for a narrow panel (original kept as display.py.stock)"
  else
    ok "patched, stock header kept - the panel is wide enough (original kept as display.py.stock)"
  fi
fi

# ------------------------------------------------------------------ config ----
echo
echo "6. Config keys"
python3 - "$CONFIG_JSON" <<'PYEOF'
import io, json, sys

path = sys.argv[1]
config = json.load(io.open(path, encoding="utf-8"))

additions = [
    ("__title_pisugar__", "PiSugar"),
    ("pisugar_show_battery", True),
    ("pisugar_autoshutdown", True),
    ("pisugar_shutdown_percent", 10),
    ("pisugar_cut_power", True),
    ("pisugar_cut_power_delay", 30),
    ("pisugar_model", "auto"),
]

added = [k for k, v in additions if k not in config]
for key, value in additions:
    config.setdefault(key, value)

if added:
    json.dump(config, io.open(path, "w", encoding="utf-8"), indent=4)
    print("added: " + ", ".join(added))
else:
    print("already present")
PYEOF
chown "$BJORN_USER:$BJORN_USER" "$CONFIG_JSON"
ok "settings available in the web UI under Config"

# ----------------------------------------------------------------- systemd ----
echo
echo "7. systemd"
for unit in pisugar-poweroff.service pisugar-guard.service pisugar-guard.timer; do
  sed "s#@BJORN_DIR@#$BJORN_DIR#g" "$SRC_DIR/systemd/$unit" > "/etc/systemd/system/$unit"
done
systemctl daemon-reload
systemctl enable pisugar-poweroff.service >/dev/null
systemctl enable --now pisugar-guard.timer >/dev/null
ok "pisugar-poweroff.service and pisugar-guard.timer enabled"

# ------------------------------------------------------------------ finish ----
echo
echo "8. Restarting Bjorn"
systemctl restart bjorn
ok "done"

echo
BJORN_DIR="$BJORN_DIR" python3 "$BJORN_DIR/pisugar.py" --status || true
echo
echo "Settings live in the web UI: Config -> PiSugar (press Save, it does not autosave)"
echo "Manual shutdown is already there: Playground -> gear dropdown -> Shutdown"
echo
