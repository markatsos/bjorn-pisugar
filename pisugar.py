"""PiSugar 3 support for Bjorn.

Three jobs, one module:

  draw_battery()  level indicator in the top-right of the e-ink header
  --cut-power     tell the board to drop the 5V rail N seconds from now
  --guard         clean shutdown when the battery runs low

Everything is driven by keys in Bjorn's own config/shared_config.json, so the
settings show up as switches and fields in the web UI without touching any HTML.

PiSugar 3 I2C map (address 0x57):
  0x02 bit 7  external power connected (read only)
  0x02 bit 5  output switch with delay; clearing it starts the 0x09 countdown
  0x09        countdown length before the rail is cut, 0-255
  0x0B        write 0x29 to unlock the other registers, 0x00 to lock again
  0x2A        calculated battery percentage, 0-100
"""

import json
import logging
import os
import subprocess
import sys
import time

logger = logging.getLogger("pisugar")

I2C_BUS = 1
I2C_ADDR = 0x57
REG_CTRL1 = 0x02
REG_DELAY = 0x09
REG_WRITE_ENABLE = 0x0B
REG_PERCENT = 0x2A
WRITE_UNLOCK = 0x29
WRITE_LOCK = 0x00
BIT_EXTERNAL_POWER = 0x80
BIT_OUTPUT_DELAY = 0x20

BJORN_DIR = os.environ.get("BJORN_DIR", "/home/bjorn/Bjorn")
CONFIG_PATH = os.path.join(BJORN_DIR, "config", "shared_config.json")

DEFAULTS = {
    "pisugar_show_battery": True,
    "pisugar_autoshutdown": True,
    "pisugar_shutdown_percent": 10,
    "pisugar_cut_power": True,
    "pisugar_cut_power_delay": 30,
}


def load_settings(config=None):
    """Merge Bjorn's config over the defaults.

    `config` is the live dict when we are running inside Bjorn; standalone runs
    read the file. A missing or broken file falls back to the defaults rather
    than disabling the guard, because a typo should not cost you the battery.
    """
    values = dict(DEFAULTS)
    if config is None:
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                config = json.load(f)
        except Exception as e:
            logger.warning("Could not read %s (%s) - using defaults", CONFIG_PATH, e)
            config = {}
    for key, fallback in DEFAULTS.items():
        value = config.get(key, fallback)
        if isinstance(fallback, bool):
            values[key] = bool(value)
        else:
            try:
                values[key] = int(value)
            except (TypeError, ValueError):
                values[key] = fallback
    return values


def _smbus():
    from smbus2 import SMBus
    return SMBus


class PiSugar:
    """Cached PiSugar 3 reader. read() only touches the bus every `refresh` seconds."""

    def __init__(self, bus_id=I2C_BUS, refresh=30):
        self.bus_id = bus_id
        self.refresh = refresh
        self.percent = 0
        self.charging = False
        self.available = False
        self._last_read = 0.0
        self._warned = False
        try:
            self._SMBus = _smbus()
        except ImportError:
            self._SMBus = None
            logger.warning("smbus2 not installed - PiSugar support disabled")

    def read(self, force=False):
        if self._SMBus is None:
            return
        now = time.time()
        if not force and now - self._last_read < self.refresh:
            return
        self._last_read = now
        try:
            with self._SMBus(self.bus_id) as bus:
                percent = bus.read_byte_data(I2C_ADDR, REG_PERCENT)
                ctrl1 = bus.read_byte_data(I2C_ADDR, REG_CTRL1)
            self.percent = max(0, min(100, percent))
            self.charging = bool(ctrl1 & BIT_EXTERNAL_POWER)
            self.available = True
            self._warned = False
        except Exception as e:
            self.available = False
            if not self._warned:
                logger.warning("PiSugar not answering on I2C: %s", e)
                self._warned = True


def draw_battery(image, draw, shared_data, sugar, sx=1.0, sy=1.0):
    """Percentage + battery glyph at the top right.

    Silent no-op when the board is switched off or the setting is disabled, so
    the header simply looks stock instead of showing a broken indicator.
    """
    if not load_settings(getattr(shared_data, "config", None))["pisugar_show_battery"]:
        return

    sugar.read()
    if not sugar.available:
        return

    font = shared_data.font_arial9
    pct = sugar.percent

    label = str(pct)
    tw = draw.textlength(label, font=font)
    draw.text((int(106 * sx) - tw, int(6 * sy)), label, font=font, fill=0)

    x, y = int(107 * sx), int(5 * sy)
    w, h = 12, 10
    draw.rectangle((x, y, x + w, y + h), outline=0, fill=255)
    draw.rectangle((x + w + 1, y + 3, x + w + 2, y + h - 3), fill=0)

    if sugar.charging:
        draw.rectangle((x + 1, y + 1, x + w - 1, y + h - 1), fill=0)
        bx, by = x + 3, y + 1
        draw.polygon(
            [(bx + 4, by), (bx + 1, by + 5), (bx + 3, by + 5),
             (bx + 2, by + 9), (bx + 6, by + 4), (bx + 3, by + 4)],
            fill=255,
        )
    else:
        inner = w - 3
        filled = max(0, min(inner, round(inner * pct / 100)))
        if filled:
            draw.rectangle((x + 2, y + 2, x + 2 + filled - 1, y + h - 2), fill=0)


def cut_power(delay=None, bus_id=I2C_BUS, respect_config=True):
    """Start the board's power-off countdown. The rail drops `delay` seconds later.

    Order matters: 0x09 must be set before 0x02 is touched, or the board ignores
    the delay entirely and nothing happens.
    """
    settings = load_settings()
    if respect_config and not settings["pisugar_cut_power"]:
        logger.info("pisugar_cut_power is off - leaving the rail alone")
        return
    if delay is None:
        delay = settings["pisugar_cut_power_delay"]

    SMBus = _smbus()
    with SMBus(bus_id) as bus:
        bus.write_byte_data(I2C_ADDR, REG_WRITE_ENABLE, WRITE_UNLOCK)
        bus.write_byte_data(I2C_ADDR, REG_DELAY, max(0, min(255, int(delay))))
        ctrl1 = bus.read_byte_data(I2C_ADDR, REG_CTRL1)
        bus.write_byte_data(I2C_ADDR, REG_CTRL1, ctrl1 & ~BIT_OUTPUT_DELAY)
        bus.write_byte_data(I2C_ADDR, REG_WRITE_ENABLE, WRITE_LOCK)
    logger.info("PiSugar will cut power in ~%ss", delay)


def cancel_cut_power(bus_id=I2C_BUS):
    """Abort a countdown started by cut_power()."""
    SMBus = _smbus()
    with SMBus(bus_id) as bus:
        bus.write_byte_data(I2C_ADDR, REG_WRITE_ENABLE, WRITE_UNLOCK)
        ctrl1 = bus.read_byte_data(I2C_ADDR, REG_CTRL1)
        bus.write_byte_data(I2C_ADDR, REG_CTRL1, ctrl1 | BIT_OUTPUT_DELAY)
        bus.write_byte_data(I2C_ADDR, REG_WRITE_ENABLE, WRITE_LOCK)
    logger.info("PiSugar power-off countdown cancelled")


def guard(threshold=None, bus_id=I2C_BUS, force=False):
    """Halt the Pi when the battery is at or below the threshold and unplugged.

    Reads twice, ten seconds apart. A fuel gauge can report one bad sample, and a
    spurious shutdown in the middle of a scan is worse than one ten seconds late.
    """
    settings = load_settings()
    if not force and not settings["pisugar_autoshutdown"]:
        return 0
    if threshold is None:
        threshold = settings["pisugar_shutdown_percent"]

    sugar = PiSugar(bus_id=bus_id, refresh=0)
    sugar.read(force=True)
    if not sugar.available:
        logger.warning("PiSugar unreachable - not acting")
        return 1
    if sugar.charging or sugar.percent > threshold:
        return 0

    first = sugar.percent
    time.sleep(10)
    sugar.read(force=True)
    if not sugar.available or sugar.charging or sugar.percent > threshold:
        logger.info("Low reading of %d%% not confirmed - ignoring", first)
        return 0

    logger.warning("Battery at %d%% and unplugged - shutting down", sugar.percent)
    subprocess.run(["systemctl", "poweroff"], check=False)
    return 0


def status():
    sugar = PiSugar(refresh=0)
    sugar.read(force=True)
    if not sugar.available:
        print("PiSugar not reachable - is the board switched on?")
        return 1
    settings = load_settings()
    state = "charging" if sugar.charging else "on battery"
    print(f"battery {sugar.percent}%  ({state})")
    print(f"config:  {CONFIG_PATH}")
    for key in sorted(DEFAULTS):
        print(f"  {key} = {settings[key]}")
    return 0


def _main(argv):
    logging.basicConfig(format="%(message)s", level=logging.INFO)

    cmd = argv[1] if len(argv) > 1 else "--status"
    arg = argv[2] if len(argv) > 2 else None

    if cmd == "--status":
        return status()
    if cmd == "--cut-power":
        cut_power(int(arg) if arg else None, respect_config=arg is None)
        return 0
    if cmd == "--cancel":
        cancel_cut_power()
        return 0
    if cmd == "--guard":
        return guard(int(arg) if arg else None, force=arg is not None)

    print(f"usage: {os.path.basename(argv[0])} "
          "[--status | --cut-power [N] | --cancel | --guard [PCT]]")
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
