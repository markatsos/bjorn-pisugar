# Compatibility

Everything here works in theory. Only the first row of each table has actually been run
on hardware — the rest is written from the PiSugar datasheets and the Waveshare panel
dimensions, and needs somebody with the gear to confirm it.

If you try a combination that is not ticked, please
[open an issue](https://github.com/markatsos/bjorn-pisugar/issues) either way. A report
that it works is as useful as a report that it does not, and the tables below get updated
from them.

## Battery boards

| Board | Chip | Address | Level | Charge state | Power cut on halt | Status |
|---|---|---|---|---|---|---|
| PiSugar 3 | — | `0x57` | direct, `0x2A` | `0x02` bit 7 | timed countdown | ✅ verified |
| PiSugar 3 Plus | — | `0x57` | direct, `0x2A` | `0x02` bit 7 | timed countdown | ⬜ untested — same registers, expected to work |
| PiSugar 2 | IP5209 | `0x75` | from voltage curve | `0x55` bit 4 | light-load auto shutdown | ⬜ untested |
| PiSugar 1 | IP5209 | `0x75` | from voltage curve | `0x55` bit 4 | light-load auto shutdown | ⬜ untested |
| PiSugar 2 Pro | IP5312 | `0x75` | from voltage curve | `0xdd` = `0x1f` | light-load auto shutdown | ⬜ untested |
| PiSugar 2 Plus | IP5312 | `0x75` | from voltage curve | `0xdd` = `0x1f` | light-load auto shutdown | ⬜ untested |

Two differences worth knowing about the older boards.

**The level is derived, not measured.** The PiSugar 3 has a controller that reports a
percentage directly. The IP5209 and IP5312 only give you battery voltage, so the
percentage comes from the same ten-point interpolation curve the official PiSugar power
manager uses. Expect it to be less steady, particularly under load.

**Power cut works differently.** The PiSugar 3 takes a countdown in seconds and drops the
rail when it expires. The older chips have no such timer — instead they have *light-load
auto shutdown*: arm it, and once the current draw falls below a threshold for about eight
seconds, the board switches itself off. Since a halted Pi draws almost nothing, the effect
is the same, but `pisugar_cut_power_delay` is ignored on those boards.

## Displays

The indicator is anchored to the right edge of whatever panel is fitted, so the width is
handled automatically. What the installer decides from `epd_type` is whether the header
needs rearranging to make room.

| Panel | `epd_type` | Size | Header | Status |
|---|---|---|---|---|
| Waveshare 2.13" V4 | `epd2in13_V4` | 122×250 | logo shifted left, network icons share a slot | ✅ verified |
| Waveshare 2.13" V2 | `epd2in13_V2` | 122×250 | same | ⬜ untested — identical geometry |
| Waveshare 2.13" V3 | `epd2in13_V3` | 122×250 | same | ⬜ untested — identical geometry |
| Waveshare 2.13" V1 | `epd2in13` | 122×250 | same | ⬜ untested — identical geometry |
| Waveshare 2.7" | `epd2in7` | 176×264 | stock layout kept, there is room to spare | ⬜ untested |

On a 122-pixel header everything is already full, so the `BJORN` logo moves from `x=37` to
`x=23` and the PAN and USB icons share one slot at `x=76` — both at once will not fit. The
2.7" panel is wide enough that nothing has to move.

## Raspberry Pi models and OS

Nothing in this add-on is architecture-specific: `smbus2` is pure Python, I2C is a kernel
interface, and the systemd units are the same everywhere. A 32-bit and a 64-bit install
run identical code.

| | Status |
|---|---|
| Raspberry Pi Zero 2 W, Raspberry Pi OS Bookworm **arm64** Lite (`2024-10-22`) | ✅ verified |
| Raspberry Pi Zero W, Raspberry Pi OS Bookworm **armhf** Lite | ⬜ untested |
| Other Pi models | ⬜ untested — should work wherever Bjorn does |

The I2C bus is found by probing: `i2c-1` first, then `i2c-0` for the very old rev 1 boards
that wired the header differently.

## Bjorn versions

| Version | Status |
|---|---|
| `v1.0.0 alpha 2` — upstream `main` at [`61a4758`](https://github.com/infinition/Bjorn/commit/61a4758e5ee3d37d43c10eb690340626139912c0), 4 May 2026 | ✅ verified |
| Bjorn V2 — unreleased, upstream `dev` / `ai` branches | ❌ not supported yet — `display.py` was rewritten |

`install.sh` patches `display.py` by matching exact snippets of upstream source. If Bjorn
changes those lines, the installer stops before touching `display.py` rather than producing
a half-patched file. By that point it has already installed its packages and copied
`pisugar.py`, so run `sudo ./uninstall.sh` to remove the copied files. If that happens to
you, open an issue with the output of `cat /home/bjorn/Bjorn/version.txt` and we will add
the new snippets.

## Reporting a result

```bash
python3 /home/bjorn/Bjorn/pisugar.py          # board, level, charge state, settings
i2cdetect -y 1
cat /home/bjorn/Bjorn/version.txt
uname -m
```

Paste that, say which PiSugar and which panel you have, and whether the indicator, the
low-battery shutdown and the power cut behaved. That is enough to turn a ⬜ into a ✅.
