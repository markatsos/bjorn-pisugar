#!/usr/bin/env bash
# Remove PiSugar support: exactly what install.sh recorded changing, nothing else.
#
#   sudo ./uninstall.sh [--force]
#
# If a file the add-on installed was changed after installation, nothing is
# touched and the file is named. --force goes ahead anyway: the module and the
# units are deleted, and display.py loses only the PiSugar lines.

set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPER="$SRC_DIR/lib/installer.py"
# Only used for a 0.2.0 install, which kept no record of where it went.
BJORN_DIR="${BJORN_DIR:-/home/bjorn/Bjorn}"

RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; YELLOW=$'\033[1;33m'; NC=$'\033[0m'
ok()   { echo "${GREEN}  ok${NC}   $*"; }
warn() { echo "${YELLOW}  warn${NC} $*"; }
die()  { echo "${RED}error${NC}  $*" >&2; exit 1; }
usage() { echo "usage: sudo $0 [--force]"; }

FORCE=()
while [ $# -gt 0 ]; do
  case "$1" in
    --force)   FORCE=(--force); shift ;;
    -h|--help) usage; exit 0 ;;
    *)         usage >&2; exit 2 ;;
  esac
done

[ "$(id -u)" -eq 0 ] || die "run this with sudo"

echo
echo "Removing PiSugar support"
echo

echo "1. Checks"
status=0
python3 "$HELPER" check --bjorn-dir "$BJORN_DIR" ${FORCE[@]+"${FORCE[@]}"} || status=$?
case "$status" in
  0) ;;
  4) echo; exit 0 ;;
  *) die "nothing has been changed" ;;
esac

echo
echo "2. systemd"
systemctl disable --now pisugar-guard.timer >/dev/null 2>&1 || true
systemctl disable pisugar-poweroff.service >/dev/null 2>&1 || true
ok "units disabled"

echo
echo "3. Files"
status=0
python3 "$HELPER" uninstall --bjorn-dir "$BJORN_DIR" ${FORCE[@]+"${FORCE[@]}"} || status=$?
systemctl daemon-reload

echo
echo "4. Restarting Bjorn"
if systemctl restart bjorn; then
  ok "done"
else
  warn "could not restart bjorn.service - restart Bjorn yourself"
fi
echo
[ "$status" -eq 0 ] || die "finished with warnings - see above"
