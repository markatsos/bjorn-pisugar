# Changelog

## 0.2.0 — 2026-09-11

Works on more hardware, without pretending any of it is proven.

- **Board auto-detection.** Probes `i2c-1` then `i2c-0`, and identifies the board from
  which address answers: PiSugar 3 at `0x57`, IP5209 or IP5312 at `0x75`. New
  `pisugar_model` setting overrides it if the guess is wrong.
- **PiSugar 1 / 2 / 2 Pro / 2 Plus support** — level interpolated from battery voltage,
  charge state, and light-load auto shutdown in place of the timed power cut those chips
  do not have. Written from the datasheets, **untested on hardware**.
- **Panel-independent indicator.** Anchored to the right edge of whatever display is
  fitted rather than fixed coordinates, so a 2.7" panel works without changes. The
  installer only rearranges the header on narrow 2.13" panels, where there is no room.
- `--status` now names the detected board and flags it when it is on an untested path.
- New [COMPATIBILITY.md](COMPATIBILITY.md) tracking what is confirmed and what is not.

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
