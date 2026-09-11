# bjorn-pisugar

PiSugar 3 support for [Bjorn](https://github.com/infinition/Bjorn): battery level on the
e-ink display, automatic shutdown on low battery, and the board cutting its own power
when the Pi halts — all switchable from Bjorn's own web UI.

![Bjorn on a Pi Zero 2 W: the battery indicator in the header, on battery and while charging](docs/device.jpg)

Bjorn knows nothing about batteries and has no plugin system, so this is a small add-on:
one module dropped next to `Bjorn.py`, five lines patched into `display.py`, and two
systemd units. Nothing is forked and nothing is vendored — an uninstall puts everything
back byte-for-byte.

## What it adds

**Battery indicator.** Percentage and a battery glyph in the top right of the header. The
glyph fills proportionally, and when external power is connected it goes solid with a
lightning bolt knocked out of it. The `BJORN` logo shifts from `x=37` to `x=23` to make
room; the network icons move to `x=76` and share a slot, since 122 pixels do not fit both
at once.

![The header at 95%, 63% charging and 8%](docs/header.png)

**Low-battery shutdown.** A systemd timer checks every two minutes and calls
`systemctl poweroff` when the battery is at or below your threshold and unplugged. It
reads twice, ten seconds apart — a fuel gauge can report one bad sample, and a spurious
shutdown mid-scan is worse than one ten seconds late. Deliberately a separate timer
rather than a thread inside Bjorn: if `bjorn.service` dies, the protection has to survive.

**Power cut on halt.** `poweroff` stops the Pi but the PiSugar keeps feeding 5V into
nothing until the battery is flat. A unit hooked to `poweroff.target` tells the board to
drop the rail after a countdown. It is wired to `poweroff.target` and `halt.target` but
deliberately **not** `reboot.target`, so a reboot does not kill the board.

**Manual shutdown** already exists in stock Bjorn — Playground, the gear dropdown,
*Shutdown*. It runs `shutdown now`, which goes through `poweroff.target`, so with this
add-on installed the PiSugar switches off too. Nothing to add.

## Settings

Bjorn builds its config page straight from `shared_config.json` — booleans become
switches, numbers become fields — so these appear under **Config** with no HTML changes:

![The PiSugar switches and fields on Bjorn's Config page](docs/config.png)

| Key | Default | What it does |
|---|---|---|
| `pisugar_show_battery` | `true` | Draw the indicator in the header |
| `pisugar_autoshutdown` | `true` | Enable the low-battery shutdown |
| `pisugar_shutdown_percent` | `10` | Shut down at or below this level |
| `pisugar_cut_power` | `true` | Let the board cut its own power on halt |
| `pisugar_cut_power_delay` | `30` | Countdown before the rail drops |

Changes take effect on the next timer tick — no restart needed for the guard. The
indicator picks up `pisugar_show_battery` on the next screen refresh.

## Tested on

This is verified on exactly one configuration. It is not a claim that it works elsewhere.

| | |
|---|---|
| Board | Raspberry Pi Zero 2 W |
| Battery | PiSugar 3, 1200 mAh |
| Display | Waveshare 2.13" e-Paper HAT **V4** (`epd2in13_V4`) |
| OS | Raspberry Pi OS Bookworm **arm64** Lite, image `2024-10-22` |
| Bjorn | `v1.0.0 alpha 2`, upstream `main` at commit [`61a4758`](https://github.com/infinition/Bjorn/commit/61a4758e5ee3d37d43c10eb690340626139912c0) (4 May 2026) |

Known limits of that scope:

- **armhf (32-bit) is untested.** Bjorn supports it and nothing here is obviously
  architecture-dependent, but nobody has run it.
- **Only the 2.13" V4 layout is tested.** The header coordinates are tuned for a
  122×250 panel. On `epd2in7` the indicator will land in the wrong place.
- **PiSugar 2 will not work.** The register map is different.
- The `display.py` patch matches four exact snippets of upstream. If Bjorn changes those
  lines, the installer refuses rather than producing a half-patched file. That is by
  design — open an issue with your Bjorn version.

## Install

```bash
git clone https://github.com/markatsos/bjorn-pisugar.git
cd bjorn-pisugar
sudo ./install.sh
```

Point it elsewhere if your Bjorn is not in the default location:

```bash
sudo BJORN_DIR=/opt/Bjorn ./install.sh
```

The installer is safe to re-run — every step checks whether it has already been done.
It installs `python3-smbus2` and `i2c-tools`, enables I2C if needed, copies the module,
patches `display.py` (keeping the original as `display.py.stock`), adds the config keys,
and enables the two units.

### Before you start

The PiSugar has to be switched on. The classic trap: if you power the Pi from *its own*
USB port, the PiSugar controller stays off and there is no I2C at all. Press the button
on the board. Check with:

```bash
i2cdetect -y 1     # want 57 in the table
```

## CLI

The module is also usable on its own:

```bash
python3 /home/bjorn/Bjorn/pisugar.py               # level, charge state, current settings
python3 /home/bjorn/Bjorn/pisugar.py --guard 100   # force a guard run, any level counts as low
python3 /home/bjorn/Bjorn/pisugar.py --cut-power 30
python3 /home/bjorn/Bjorn/pisugar.py --cancel      # abort a countdown
```

`--guard 100` is the honest way to test the shutdown path without waiting for the
battery to drain. Unplug first, or it will correctly refuse to act.

## How it works

Everything goes over I2C at address `0x57`. No daemon, no `pisugar-power-manager`,
two byte reads every 30 seconds.

| Register | Use |
|---|---|
| `0x2A` | Battery percentage, 0-100 |
| `0x02` bit 7 | External power connected |
| `0x02` bit 5 | Output switch with delay — clearing it starts the countdown |
| `0x09` | Countdown length before the rail drops |
| `0x0B` | Write `0x29` to unlock the other registers, `0x00` to lock |

Two things the datasheet is easy to miss on: the registers are write-protected until you
unlock `0x0B`, and `0x09` has to be written **before** `0x02` is touched or the board
ignores the delay completely.

Note that bit 7 means "there is power at the USB port", not "the battery is filling". At
100% with the cable in, the bolt stays lit. That is usually the thing you actually want
to know.

## Troubleshooting

**Nothing in the header.** Is the PiSugar switched on? `i2cdetect -y 1` should show `57`.
Then check `pisugar_show_battery` in Config.

**The guard never fires.** It refuses while external power is present, by design. Unplug
and test with `--guard 100`.

**The Pi shuts down shortly after a reboot.** The poweroff unit fired when it should not
have. Check `systemctl cat pisugar-poweroff.service` — `WantedBy` must not list
`reboot.target`. Cancel the countdown with `--cancel`.

**Is a countdown running right now?**

```bash
i2cget -y 1 0x57 0x02
```

Bit 5 (`0x20`) set means no countdown. `0x6c` and `0xec` are both fine; anything with
bit 5 clear means the rail is about to drop.

**Pasting files over SSH.** A long heredoc over SSH drops lines silently — the terminal
outruns the flow control. If you install by hand rather than with `install.sh`, check
`md5sum` afterwards or send the file with `scp`.

## Uninstall

```bash
sudo ./uninstall.sh
```

Restores `display.py` from `display.py.stock`, removes the module and the config keys,
disables and deletes the units, restarts Bjorn.

## License

MIT, same as Bjorn. See [LICENSE](LICENSE).

Bjorn is by [infinition](https://github.com/infinition/Bjorn). The PiSugar register map
comes from the [PiSugar 3 I2C datasheet](https://github.com/PiSugar/PiSugar/wiki/PiSugar-3-I2C-Datasheet).
This add-on is not affiliated with either project.
