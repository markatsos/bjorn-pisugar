#!/usr/bin/env bash
# Install PiSugar 3 support into an existing Bjorn installation.
# Safe to re-run: every step checks whether it has already been done.

set -euo pipefail

BJORN_DIR="${BJORN_DIR:-/home/bjorn/Bjorn}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DISPLAY_PY="$BJORN_DIR/display.py"
CONFIG_JSON="$BJORN_DIR/config/shared_config.json"

RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; YELLOW=$'\033[1;33m'; NC=$'\033[0m'
ok()   { echo "${GREEN}  ok${NC}   $*"; }
skip() { echo "${YELLOW}  skip${NC} $*"; }
info() { echo "${NC}  ..${NC}   $*"; }
die()  { echo "${RED}error${NC}  $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run this with sudo"
[ -f "$DISPLAY_PY" ] || die "no Bjorn at $BJORN_DIR (set BJORN_DIR= to point elsewhere)"
[ -f "$CONFIG_JSON" ] || die "no config at $CONFIG_JSON"

echo
echo "Installing PiSugar support into $BJORN_DIR"
echo

# ---------------------------------------------------------------- packages --
echo "1. Packages"
missing=()
dpkg -s python3-smbus2 >/dev/null 2>&1 || missing+=(python3-smbus2)
dpkg -s i2c-tools      >/dev/null 2>&1 || missing+=(i2c-tools)
if [ ${#missing[@]} -gt 0 ]; then
  info "installing ${missing[*]}"
  apt-get update -qq
  apt-get install -y "${missing[@]}" >/dev/null
  ok "installed ${missing[*]}"
else
  skip "python3-smbus2 and i2c-tools already present"
fi

# -------------------------------------------------------------------- i2c ---
echo
echo "2. I2C"
if [ ! -e /dev/i2c-1 ]; then
  info "enabling I2C"
  raspi-config nonint do_i2c 0
  ok "I2C enabled - a reboot may be needed before the bus appears"
else
  skip "/dev/i2c-1 already there"
fi

if command -v i2cdetect >/dev/null && [ -e /dev/i2c-1 ]; then
  if i2cdetect -y 1 2>/dev/null | grep -qi ' 57'; then
    ok "PiSugar answering at 0x57"
  else
    echo "${YELLOW}  warn${NC} nothing at 0x57 - is the PiSugar switched on?"
    echo "         (installation continues; the indicator stays hidden until it answers)"
  fi
fi

# ------------------------------------------------------------------ module --
echo
echo "3. Module"
install -o bjorn -g bjorn -m 644 "$SRC_DIR/pisugar.py" "$BJORN_DIR/pisugar.py"
ok "pisugar.py -> $BJORN_DIR/pisugar.py"

# ----------------------------------------------------------------- display --
echo
echo "4. display.py"
if grep -q "from pisugar import" "$DISPLAY_PY"; then
  skip "already patched"
else
  [ -f "$DISPLAY_PY.stock" ] || cp "$DISPLAY_PY" "$DISPLAY_PY.stock"
  python3 - "$DISPLAY_PY" <<'PYEOF'
import io, sys

path = sys.argv[1]
s = io.open(path, encoding="utf-8").read()

edits = [
 ("import subprocess  \n",
  "import subprocess  \nfrom pisugar import PiSugar, draw_battery\n"),
 ("        self.web_screen_reversed = self.shared_data.web_screen_reversed\n",
  "        self.web_screen_reversed = self.shared_data.web_screen_reversed\n"
  "        self.pisugar = PiSugar()\n"),
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

for old, new in edits:
    n = s.count(old)
    if n != 1:
        sys.exit(f"display.py does not look like the expected version: "
                 f"found {n} matches for {old[:50]!r}")
    s = s.replace(old, new, 1)

io.open(path, "w", encoding="utf-8").write(s)
import ast
ast.parse(s)
PYEOF
  chown bjorn:bjorn "$DISPLAY_PY"
  ok "patched (original kept as display.py.stock)"
fi

# ------------------------------------------------------------------ config --
echo
echo "5. Config keys"
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
chown bjorn:bjorn "$CONFIG_JSON"
ok "settings available in the web UI under Config"

# ----------------------------------------------------------------- systemd --
echo
echo "6. systemd"
for unit in pisugar-poweroff.service pisugar-guard.service pisugar-guard.timer; do
  sed "s#@BJORN_DIR@#$BJORN_DIR#g" "$SRC_DIR/systemd/$unit" > "/etc/systemd/system/$unit"
done
systemctl daemon-reload
systemctl enable pisugar-poweroff.service >/dev/null
systemctl enable --now pisugar-guard.timer >/dev/null
ok "pisugar-poweroff.service and pisugar-guard.timer enabled"

# ------------------------------------------------------------------ finish --
echo
echo "7. Restarting Bjorn"
systemctl restart bjorn
ok "done"

echo
python3 "$BJORN_DIR/pisugar.py" --status || true
echo
echo "Settings live in the web UI: Config -> PiSugar"
echo "Manual shutdown is already there: Playground -> gear dropdown -> Shutdown"
echo
