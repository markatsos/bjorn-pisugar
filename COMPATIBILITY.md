# Compatibility

Only the first row of each table has actually been run on hardware. The rest is written
from the PiSugar datasheets, the official power manager's source and the Waveshare panel
dimensions, and needs somebody with the gear to confirm it.

If you try a combination that is not ticked, please
[open an issue](https://github.com/markatsos/bjorn-pisugar/issues) either way. A report
that it works is as useful as a report that it does not, and the tables below get updated
from them.

## Battery boards

| Board | `pisugar_model` | Chip | Address | Level | Charge state | Low-battery shutdown, power cut | Status |
|---|---|---|---|---|---|---|---|
| PiSugar 3 | `pisugar3` | — | `0x57` | direct, `0x2A` | `0x02` bit 7 | yes | ✅ verified |
| PiSugar 3 Plus | `pisugar3` | — | `0x57` | direct, `0x2A` | `0x02` bit 7 | yes | ⬜ untested — same registers, expected to work |
| PiSugar 2, 2-LED | `pisugar2` | IP5209 | `0x75` | from voltage | not read | not available | 🧪 experimental, read-only |
| PiSugar 2, 4-LED | `pisugar2` | IP5209 | `0x75` | from voltage | not read | not available | 🧪 experimental, read-only |
| PiSugar 2 Pro / 2 Plus | `pisugar2pro` | IP5312 | `0x75` | from voltage | not read | not available | 🧪 experimental, read-only |
| PiSugar 1 | — | IP5306 | — | — | — | — | ❌ not supported — no I2C |
| PiSugar S / S Plus | — | — | — | — | — | — | ❌ not supported — no I2C |

**Power control needs the model named.** With `pisugar_model` at `auto`, the board is
detected for the indicator only: nothing is written and the Pi is never shut down. On a
PiSugar 3, set `pisugar_model` to `pisugar3` to enable the low-battery shutdown and the
power cut. Before anything is written, the board must also report a percentage of at most
100 and a battery voltage between 2.5 and 4.6 V.

**The PiSugar 2 boards are read-only.** They report only battery voltage, so the level
comes from the same ten-point curve the official power manager uses. Expect it to be less
steady, particularly under load. Their charge state is not read: the 2-LED and 4-LED boards
detect external power in different ways, and both need the chip's GPIOs configured first,
which means writing to it. Without a charge state, the low-battery shutdown cannot tell a
low battery on its own from one that is being charged. So until somebody tests these boards
on real hardware, the add-on only reads their voltage and never writes to them.

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

The add-on is pinned to the tested Bjorn version: `install.sh` patches `display.py` by
matching exact snippets of upstream source. It checks for those snippets before it changes
anything. If Bjorn has changed them, the installer stops with nothing installed, rather than
producing a half-patched file. If that happens to you, open an issue with the output of
`cat /home/bjorn/Bjorn/version.txt` and we will add the new snippets.

## Reporting a result

```bash
python3 /home/bjorn/Bjorn/pisugar.py          # version, board, level, voltage, power control
i2cdetect -y 1
cat /home/bjorn/Bjorn/version.txt
uname -m
```

Paste that, and say which PiSugar and which panel you have. Then say how the indicator
behaved, and on a PiSugar 3 the low-battery shutdown and the power cut. That is enough to
turn a ⬜ into a ✅. For a PiSugar 2, a reading of the battery level at a few known
voltages is a good start towards making it more than read-only.
