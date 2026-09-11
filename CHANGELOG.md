# Changelog

## 0.1.0 — 2026-09-11

First release.

- Battery percentage and glyph in the top right of the Bjorn e-ink header, with a
  lightning bolt while external power is connected.
- Low-battery shutdown on a systemd timer, threshold configurable from the web UI.
- The PiSugar cuts its own 5V rail after the Pi halts, wired to `poweroff.target` and
  `halt.target` only.
- Five settings exposed through Bjorn's existing config page — no HTML or JS changes.
- `install.sh` / `uninstall.sh`, both re-runnable; uninstall restores `display.py`
  byte-for-byte.

Tested on Raspberry Pi Zero 2 W, PiSugar 3, Waveshare 2.13" V4, Raspberry Pi OS Bookworm
arm64 Lite (2024-10-22), Bjorn v1.0.0 alpha 2 (upstream `main` at `61a4758`).
