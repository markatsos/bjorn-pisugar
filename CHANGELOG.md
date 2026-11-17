# Changelog

## 0.3.0 — 2026-11-17

Safety and correctness fixes from the PiSugar team's review in
[PiSugar/pisugar-doc#4](https://github.com/PiSugar/pisugar-doc/issues/4).

**Upgrading:** `git pull`, then `sudo ./install.sh`. The installer converts the 0.2.0 install
and asks which board you have. Power control (the low-battery shutdown and the power cut)
now stays off until `pisugar_model` names the board, so a 0.2.0 install that was left at
`auto` needs `pisugar3` set.

- **Nothing acts without an explicit board model.** Auto-detection only feeds the
  indicator. Register writes and the low-battery shutdown need `pisugar_model` set to
  `pisugar3`. Before anything is written, the board must also pass an identity check: a
  percentage of at most 100 and a battery voltage of 2.5-4.6 V. A named model is probed on
  its own and never falls back to another board.
- **New model names:** `pisugar3`, `pisugar2`, `pisugar2pro`, `auto`. Bjorn's Config page
  stored `3` as a number and then rendered a number field, which could no longer hold
  `auto`. The 0.2.0 values still work, and the installer rewrites them.
- **PiSugar 2 boards are read-only and experimental.** The 2-LED, 4-LED and Pro boards
  detect external power differently, and reading it needs set-up writes first. These
  boards now get a battery level from voltage only: no charge state, no low-battery
  shutdown, no power cut. The light-load shutdown writes are removed.
- **PiSugar 1 removed.** It uses an IP5306 with no I2C, so it could never have worked.
- **Write protection is restored in a `finally` block.** An I2C error during the PiSugar
  3 power cut can no longer leave the registers unlocked.
- **`BJORN_DIR` reaches the units.** With Bjorn installed elsewhere, the guard and the power
  cut used to read a config that did not exist and fall back to the defaults. The units now
  set `BJORN_DIR`, and the module defaults to its own directory. Both commands refuse to act
  when the config cannot be read.
- **`pisugar_model` reaches the indicator.** The display's reader used to keep
  auto-detecting whatever the setting said.
- **Uninstall reverts only what install changed.** The install is recorded in
  `/var/lib/bjorn-pisugar/install.json`, and uninstall works from that record:
  - Config keys that existed before the install are kept.
  - Files keep their owner, group and mode, instead of a hard-coded `bjorn:bjorn`.
  - The saved original `display.py` is always the version that was patched, so a re-install
    after a Bjorn update can no longer restore an older file.
  - A file changed after installation stops the uninstall, unless `--force` is given.
  - Packages and I2C are left in place, with the commands to remove them.
- **The installer checks before it changes anything.** An unsupported `display.py` or an
  unreadable config stops it with nothing installed.
- Hand-edited string booleans (`"false"`) are read correctly, and the threshold and delay
  are clamped to their valid ranges.
- `--status` shows the version, the battery voltage and why power control is on or off.
  New `--detect` command.
- 44 tests that run without hardware: `python3 -m unittest discover -s tests -v`.

Tested on Raspberry Pi Zero 2 W, PiSugar 3, Waveshare 2.13" V4, Raspberry Pi OS Bookworm
arm64 Lite (2024-10-22), Bjorn v1.0.0 alpha 2 (upstream `main` at `61a4758`).

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
