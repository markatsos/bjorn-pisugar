#!/usr/bin/env bash
# Install PiSugar support into an existing Bjorn installation.
#
#   sudo ./install.sh [--model auto|pisugar3|pisugar2|pisugar2pro]
#
# Set BJORN_DIR=... if Bjorn is not in /home/bjorn/Bjorn. Safe to re-run. Every
# change to Bjorn's files is recorded in /var/lib/bjorn-pisugar, so that
# uninstall.sh can undo exactly that and nothing else.

set -euo pipefail

export BJORN_DIR="${BJORN_DIR:-/home/bjorn/Bjorn}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPER="$SRC_DIR/lib/installer.py"
DISPLAY_PY="$BJORN_DIR/display.py"
CONFIG_JSON="$BJORN_DIR/config/shared_config.json"

RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; YELLOW=$'\033[1;33m'; NC=$'\033[0m'
ok()   { echo "${GREEN}  ok${NC}   $*"; }
skip() { echo "${YELLOW}  skip${NC} $*"; }
warn() { echo "${YELLOW}  warn${NC} $*"; }
info() { echo "  ..   $*"; }
die()  { echo "${RED}error${NC}  $*" >&2; exit 1; }
usage() { echo "usage: sudo [BJORN_DIR=/path/to/Bjorn] $0 [--model auto|pisugar3|pisugar2|pisugar2pro]"; }

MODEL=""
while [ $# -gt 0 ]; do
  case "$1" in
    --model)   [ $# -ge 2 ] || { usage >&2; exit 2; }; MODEL="$2"; shift 2 ;;
    --model=*) MODEL="${1#--model=}"; shift ;;
    -h|--help) usage; exit 0 ;;
    *)         usage >&2; exit 2 ;;
  esac
done

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
[ -f "$CONFIG_JSON" ] || die "no config at $CONFIG_JSON - start Bjorn once so it creates one"
if [ -n "$MODEL" ]; then
  MODEL="$(python3 "$HELPER" model "$MODEL")" \
    || die "unknown model - use auto, pisugar3, pisugar2 or pisugar2pro"
fi

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

EPD_TYPE="$(python3 - "$CONFIG_JSON" <<'PYEOF'
import io, json, sys
try:
    print(json.load(io.open(sys.argv[1], encoding="utf-8")).get("epd_type", ""))
except Exception:
    print("")
PYEOF
)"
case "$EPD_TYPE" in
  epd2in13*) LAYOUT=narrow; ok "display $EPD_TYPE (122px wide)" ;;
  epd2in7*)  LAYOUT=wide;   ok "display $EPD_TYPE (176px wide)" ;;
  "")        LAYOUT=narrow; warn "no epd_type in config - assuming a 2.13\" panel" ;;
  *)         LAYOUT=narrow; warn "unrecognised epd_type '$EPD_TYPE' - assuming a 2.13\" panel" ;;
esac

# ------------------------------------------------------------------- checks ---
# Everything that could stop the install is checked here, before any change.
echo
echo "2. Checks"
python3 "$HELPER" preflight --bjorn-dir "$BJORN_DIR" --layout "$LAYOUT" \
  || die "nothing has been changed"

# ---------------------------------------------------------------- packages ----
echo
echo "3. Packages"
INSTALLED=()
missing=()
dpkg -s python3-smbus2 >/dev/null 2>&1 || missing+=(python3-smbus2)
dpkg -s i2c-tools      >/dev/null 2>&1 || missing+=(i2c-tools)
if [ ${#missing[@]} -gt 0 ]; then
  info "installing ${missing[*]}"
  apt-get update -qq
  if apt-get install -y "${missing[@]}" >/dev/null 2>&1; then
    INSTALLED+=("${missing[@]}")
  elif python3 -c "import smbus2" 2>/dev/null; then
    warn "apt could not install ${missing[*]} - smbus2 is importable anyway, carrying on"
  else
    warn "apt could not install ${missing[*]} - trying pip for smbus2"
    pip3 install smbus2 --break-system-packages >/dev/null \
      || die "could not install smbus2 - install python3-smbus2 by hand and re-run"
    INSTALLED+=("pip:smbus2")
  fi
  ok "dependencies in place"
else
  skip "python3-smbus2 and i2c-tools already present"
fi

# --------------------------------------------------------------------- i2c ----
echo
echo "4. I2C"
I2C_ENABLED=no
buses=(/dev/i2c-*)
if [ ! -e "${buses[0]}" ]; then
  info "enabling I2C"
  if raspi-config nonint do_i2c 0; then
    I2C_ENABLED=yes
    ok "I2C enabled - a reboot may be needed before the bus appears"
  else
    warn "raspi-config failed - enable I2C by hand"
  fi
else
  skip "${buses[*]} already present"
fi

# ------------------------------------------------------------------- board ----
echo
echo "5. Board"
DETECTED="$(python3 "$SRC_DIR/pisugar.py" --detect 2>/dev/null || true)"
read -r FOUND_MODEL FOUND_BUS FOUND_NAME <<<"${DETECTED:-none}" || true
case "$FOUND_MODEL" in
  pisugar3)
    ok "$FOUND_NAME answering on i2c-$FOUND_BUS" ;;
  pisugar2|pisugar2pro)
    ok "$FOUND_NAME answering on i2c-$FOUND_BUS"
    warn "PiSugar 2 boards are EXPERIMENTAL here and read-only: battery level only -"
    warn "no charge state, no low-battery shutdown, no power cut. Please report how it goes." ;;
  *)
    warn "no PiSugar answering - is the board switched on?"
    [ "$I2C_ENABLED" = no ] || info "I2C was only just enabled - the bus appears after a reboot" ;;
esac

# Power control is never switched on by detection alone: the board has to be
# named, here or later in the web UI.
if [ -z "$MODEL" ]; then
  CURRENT="$(python3 "$HELPER" config-model --bjorn-dir "$BJORN_DIR")"
  if [ "$CURRENT" != auto ]; then
    MODEL="$CURRENT"
    skip "pisugar_model is already $MODEL"
  elif [ -t 0 ]; then
    echo
    echo "  Which board is this? Power control - the low-battery shutdown and the"
    echo "  power cut - stays off until the board is named. Type one of:"
    echo
    echo "    pisugar3      PiSugar 3 / 3 Plus       verified, everything works"
    echo "    pisugar2      PiSugar 2 (2 or 4 LEDs)  experimental, battery level only"
    echo "    pisugar2pro   PiSugar 2 Pro / 2 Plus   experimental, battery level only"
    echo "    auto          not sure                 battery level only"
    echo
    while :; do
      read -r -p "  pisugar_model: " answer || die "no answer - re-run with --model"
      if MODEL="$(python3 "$HELPER" model "$answer")"; then
        break
      fi
      warn "type pisugar3, pisugar2, pisugar2pro or auto"
    done
  else
    MODEL=auto
    warn "no terminal to ask on - pisugar_model stays auto (battery level only)"
  fi
fi
ok "pisugar_model: $MODEL"
case "$MODEL:$FOUND_MODEL" in
  pisugar3:pisugar2|pisugar3:pisugar2pro)
    warn "but the board answering looks like $FOUND_NAME - power control will refuse to act" ;;
esac

# ------------------------------------------------------------------- files ----
echo
echo "6. Files"
args=(install --bjorn-dir "$BJORN_DIR" --src "$SRC_DIR" --layout "$LAYOUT" --model "$MODEL")
for package in ${INSTALLED[@]+"${INSTALLED[@]}"}; do
  args+=(--package "$package")
done
[ "$I2C_ENABLED" = no ] || args+=(--i2c-enabled)
python3 "$HELPER" "${args[@]}" || die "install stopped - re-run once the problem above is fixed"

# ----------------------------------------------------------------- systemd ----
echo
echo "7. systemd"
systemctl daemon-reload
systemctl enable pisugar-poweroff.service >/dev/null
systemctl enable --now pisugar-guard.timer >/dev/null
ok "pisugar-poweroff.service and pisugar-guard.timer enabled"

# ------------------------------------------------------------------ finish ----
echo
echo "8. Restarting Bjorn"
if systemctl restart bjorn; then
  ok "done"
else
  warn "could not restart bjorn.service - restart Bjorn yourself"
fi

echo
python3 "$BJORN_DIR/pisugar.py" --status || true
echo
echo "Settings live in the web UI: Config -> PiSugar (press Save, it does not autosave)"
if [ "$MODEL" = auto ]; then
  echo "Power control is off while pisugar_model is auto. For a PiSugar 3, set it to"
  echo "pisugar3 in Config, or re-run: sudo ./install.sh --model pisugar3"
fi
echo "Manual shutdown is already there: Playground -> gear dropdown -> Shutdown"
echo
