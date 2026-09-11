"""PiSugar support for Bjorn.

Three jobs, one module:

  draw_battery()  level indicator in the top-right of the e-ink header
  --cut-power     tell the board to drop the rail once the Pi has halted
  --guard         clean shutdown when the battery runs low

Settings live in Bjorn's own config/shared_config.json, so they render as
switches and fields on the web UI's Config page without touching any HTML.

Supported boards, picked automatically:

  PiSugar 3 / 3 Plus   0x57   percentage straight from the chip, timed power cut
  PiSugar 1 / 2        0x75   IP5209 - voltage only, level from a curve
  PiSugar 2 Pro/Plus   0x75   IP5312 - same idea, different registers

Only the PiSugar 3 path is verified on hardware. The IP5209 and IP5312 paths are
written from the PiSugar datasheets and the official power manager's source; they
have never been run on a real board. See the README.
"""

import json
import logging
import os
import subprocess
import sys
import time

logger = logging.getLogger("pisugar")

BJORN_DIR = os.environ.get("BJORN_DIR", "/home/bjorn/Bjorn")
CONFIG_PATH = os.path.join(BJORN_DIR, "config", "shared_config.json")

DEFAULTS = {
    "pisugar_show_battery": True,
    "pisugar_autoshutdown": True,
    "pisugar_shutdown_percent": 10,
    "pisugar_cut_power": True,
    "pisugar_cut_power_delay": 30,
    "pisugar_model": "auto",
}

# Buses to probe, in order. Pi rev 1 boards wired the header to i2c-0.
CANDIDATE_BUSES = (1, 0)


def load_settings(config=None):
    """Merge Bjorn's config over the defaults.

    `config` is the live dict when running inside Bjorn; standalone runs read the
    file. A missing or broken file falls back to the defaults rather than
    disabling the guard — a typo should not cost you the battery.
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
        elif isinstance(fallback, str):
            values[key] = str(value).strip().lower() or fallback
        else:
            try:
                values[key] = int(value)
            except (TypeError, ValueError):
                values[key] = fallback
    return values


def _smbus():
    from smbus2 import SMBus
    return SMBus


def _interpolate(voltage, curve):
    """Battery percentage from a voltage curve, the same way PiSugar does it."""
    if voltage >= curve[0][0]:
        return 100.0
    if voltage <= curve[-1][0]:
        return 0.0
    for i in range(len(curve) - 1):
        hi_v, hi_p = curve[i]
        lo_v, lo_p = curve[i + 1]
        if lo_v <= voltage <= hi_v:
            span = hi_v - lo_v
            if span <= 0:
                return lo_p
            return lo_p + (voltage - lo_v) * (hi_p - lo_p) / span
    return 0.0


# --------------------------------------------------------------------------- #
# Backends                                                                     #
# --------------------------------------------------------------------------- #

class Backend:
    """One battery board. Subclasses know their own registers."""

    name = "unknown"
    addr = 0x00
    tested = False

    def __init__(self, bus_id):
        self.bus_id = bus_id

    @classmethod
    def probe(cls, bus, bus_id):
        raise NotImplementedError

    def read(self, bus):
        """Return (percent 0-100, external power present)."""
        raise NotImplementedError

    def cut_power(self, bus, delay):
        """Arrange for the rail to drop once the Pi is down. Returns a description."""
        raise NotImplementedError

    def cancel_cut_power(self, bus):
        raise NotImplementedError


class PiSugar3(Backend):
    """PiSugar 3 and 3 Plus.

    0x02 bit 7  external power connected (read only)
    0x02 bit 5  output switch with delay; clearing it starts the 0x09 countdown
    0x09        countdown length before the rail is cut, 0-255
    0x0B        write 0x29 to unlock the other registers, 0x00 to lock again
    0x2A        calculated battery percentage, 0-100
    """

    name = "PiSugar 3"
    addr = 0x57
    tested = True

    REG_CTRL1 = 0x02
    REG_DELAY = 0x09
    REG_WRITE_ENABLE = 0x0B
    REG_PERCENT = 0x2A
    WRITE_UNLOCK = 0x29
    WRITE_LOCK = 0x00
    BIT_EXTERNAL_POWER = 0x80
    BIT_OUTPUT_DELAY = 0x20

    @classmethod
    def probe(cls, bus, bus_id):
        percent = bus.read_byte_data(cls.addr, cls.REG_PERCENT)
        bus.read_byte_data(cls.addr, cls.REG_CTRL1)
        return percent <= 100

    def read(self, bus):
        percent = bus.read_byte_data(self.addr, self.REG_PERCENT)
        ctrl1 = bus.read_byte_data(self.addr, self.REG_CTRL1)
        return max(0, min(100, percent)), bool(ctrl1 & self.BIT_EXTERNAL_POWER)

    def cut_power(self, bus, delay):
        # Order matters: 0x09 must be set before 0x02 is touched, or the board
        # ignores the delay entirely and nothing happens.
        bus.write_byte_data(self.addr, self.REG_WRITE_ENABLE, self.WRITE_UNLOCK)
        bus.write_byte_data(self.addr, self.REG_DELAY, max(0, min(255, int(delay))))
        ctrl1 = bus.read_byte_data(self.addr, self.REG_CTRL1)
        bus.write_byte_data(self.addr, self.REG_CTRL1, ctrl1 & ~self.BIT_OUTPUT_DELAY)
        bus.write_byte_data(self.addr, self.REG_WRITE_ENABLE, self.WRITE_LOCK)
        return f"countdown started, rail drops in ~{delay}s"

    def cancel_cut_power(self, bus):
        bus.write_byte_data(self.addr, self.REG_WRITE_ENABLE, self.WRITE_UNLOCK)
        ctrl1 = bus.read_byte_data(self.addr, self.REG_CTRL1)
        bus.write_byte_data(self.addr, self.REG_CTRL1, ctrl1 | self.BIT_OUTPUT_DELAY)
        bus.write_byte_data(self.addr, self.REG_WRITE_ENABLE, self.WRITE_LOCK)
        return "countdown cancelled"


class IP5209(Backend):
    """PiSugar 1 and PiSugar 2 (the IP5209 chip). UNTESTED.

    0xa2/0xa3   battery voltage, low byte then high 6 bits with a sign bit
    0x55 bit 4  external power connected
    0x0c/0x04/0x02  light-load auto shutdown: threshold, timing, enable
    """

    name = "PiSugar 1/2 (IP5209)"
    addr = 0x75
    tested = False

    CURVE = [(4.16, 100.0), (4.05, 95.0), (4.00, 80.0), (3.92, 65.0), (3.86, 40.0),
             (3.79, 25.5), (3.66, 10.0), (3.52, 6.5), (3.49, 3.2), (3.10, 0.0)]
    IDLE_INTENSITY_A = 0.11          # Pi Zero idle draw the official manager assumes

    def voltage(self, bus):
        low = bus.read_byte_data(self.addr, 0xA2)
        high = bus.read_byte_data(self.addr, 0xA3)
        if high & 0x20:
            raw = (((high | 0b1100_0000) << 8) + low)
            if raw > 32767:
                raw -= 65536
            mv = 2600.0 - raw * 0.26855
        else:
            mv = 2600.0 + (((high & 0x1F) << 8) + low) * 0.26855
        return mv / 1000.0

    @classmethod
    def probe(cls, bus, bus_id):
        v = cls(bus_id).voltage(bus)
        return 2.5 <= v <= 4.6

    def read(self, bus):
        percent = int(round(_interpolate(self.voltage(bus), self.CURVE)))
        plugged = bool(bus.read_byte_data(self.addr, 0x55) & 0b0001_0000)
        return max(0, min(100, percent)), plugged

    def cut_power(self, bus, delay):
        # No timed cutoff on this chip. Instead arm light-load auto shutdown: once
        # the Pi halts, the draw drops below the threshold and the board powers
        # off by itself about eight seconds later. `delay` is not used.
        threshold = int(self.IDLE_INTENSITY_A * 1000.0 / 12.0)
        threshold = min(threshold, 0b0001_1111)
        v = bus.read_byte_data(self.addr, 0x0C) & 0b0000_0111
        bus.write_byte_data(self.addr, 0x0C, v | (threshold << 3))

        v = bus.read_byte_data(self.addr, 0x04) & 0b0011_1111   # 8 s
        bus.write_byte_data(self.addr, 0x04, v)

        v = bus.read_byte_data(self.addr, 0x02)
        bus.write_byte_data(self.addr, 0x02, v | 0b0000_0011)
        return "light-load auto shutdown armed, board powers off ~8s after the Pi halts"

    def cancel_cut_power(self, bus):
        v = bus.read_byte_data(self.addr, 0x02)
        bus.write_byte_data(self.addr, 0x02, v & 0b1111_1101)
        return "light-load auto shutdown disabled"


class IP5312(IP5209):
    """PiSugar 2 Pro and 2 Plus (the IP5312 chip). UNTESTED.

    0xd0/0xd1   battery voltage, low byte then high 6 bits
    0xdd        0x1f when external power is connected
    0xc9/0x06/0x03  light-load auto shutdown: threshold, timing, enable
    """

    name = "PiSugar 2 Pro/Plus (IP5312)"
    addr = 0x75
    tested = False

    CURVE = [(4.10, 100.0), (4.05, 95.0), (3.90, 88.0), (3.80, 77.0), (3.70, 65.0),
             (3.62, 55.0), (3.58, 49.0), (3.49, 25.6), (3.32, 4.5), (3.10, 0.0)]
    IDLE_INTENSITY_A = 0.20

    def voltage(self, bus):
        low = bus.read_byte_data(self.addr, 0xD0)
        high = bus.read_byte_data(self.addr, 0xD1)
        if low == 0 and high == 0:
            raise OSError("IP5312 voltage registers read zero")
        return (((high & 0b0011_1111) << 8) + low) * 0.26855 / 1000.0 + 2.6

    def read(self, bus):
        percent = int(round(_interpolate(self.voltage(bus), self.CURVE)))
        plugged = bus.read_byte_data(self.addr, 0xDD) == 0x1F
        return max(0, min(100, percent)), plugged

    def cut_power(self, bus, delay):
        threshold = int(self.IDLE_INTENSITY_A * 1000.0 / 4.3)
        threshold = min(threshold, 0b0011_1111)
        v = bus.read_byte_data(self.addr, 0xC9) & 0b1100_0000
        bus.write_byte_data(self.addr, 0xC9, v | threshold)

        v = bus.read_byte_data(self.addr, 0x06) & 0b0011_1111   # 8 s
        bus.write_byte_data(self.addr, 0x06, v)

        v = bus.read_byte_data(self.addr, 0x03)
        bus.write_byte_data(self.addr, 0x03, v | 0b0010_0000)
        return "light-load auto shutdown armed, board powers off ~8s after the Pi halts"

    def cancel_cut_power(self, bus):
        v = bus.read_byte_data(self.addr, 0x03)
        bus.write_byte_data(self.addr, 0x03, v & 0b1101_1111)
        return "light-load auto shutdown disabled"


# IP5312 is tried before IP5209: they share address 0x75, and the 5312 registers
# read back as zero on a 5209, which its probe rejects.
BACKENDS = (PiSugar3, IP5312, IP5209)
BY_NAME = {"3": PiSugar3, "pisugar3": PiSugar3,
           "ip5312": IP5312, "2pro": IP5312, "2plus": IP5312,
           "ip5209": IP5209, "2": IP5209, "1": IP5209}


def detect(model="auto"):
    """Find the board. Returns a Backend instance, or None if nothing answers."""
    SMBus = _smbus()
    wanted = None if model in ("", "auto") else BY_NAME.get(model)
    if model not in ("", "auto") and wanted is None:
        logger.warning("Unknown pisugar_model %r - falling back to auto", model)

    for bus_id in CANDIDATE_BUSES:
        if not os.path.exists(f"/dev/i2c-{bus_id}"):
            continue
        for cls in ((wanted,) if wanted else BACKENDS):
            try:
                with SMBus(bus_id) as bus:
                    if wanted or cls.probe(bus, bus_id):
                        return cls(bus_id)
            except Exception:
                continue
    return None


# --------------------------------------------------------------------------- #
# The bit Bjorn uses                                                           #
# --------------------------------------------------------------------------- #

class PiSugar:
    """Cached reader. read() only touches the bus every `refresh` seconds."""

    def __init__(self, refresh=30, model="auto"):
        self.refresh = refresh
        self.model = model
        self.percent = 0
        self.charging = False
        self.available = False
        self.backend = None
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
            if self.backend is None:
                self.backend = detect(self.model)
                if self.backend is None:
                    raise OSError("no PiSugar found on any I2C bus")
            with self._SMBus(self.backend.bus_id) as bus:
                self.percent, self.charging = self.backend.read(bus)
            self.available = True
            self._warned = False
        except Exception as e:
            self.available = False
            self.backend = None          # re-detect next time, the board may be off
            if not self._warned:
                logger.warning("PiSugar not answering: %s", e)
                self._warned = True


def draw_battery(image, draw, shared_data, sugar, sx=1.0, sy=1.0):
    """Percentage + battery glyph at the top right of the header.

    Anchored to the right edge of whatever panel is fitted rather than to fixed
    coordinates, so it lands correctly on a 122px 2.13" and a 176px 2.7" alike.
    Silent no-op when the board is off or the setting is disabled.
    """
    if not load_settings(getattr(shared_data, "config", None))["pisugar_show_battery"]:
        return

    sugar.read()
    if not sugar.available:
        return

    font = shared_data.font_arial9
    pct = sugar.percent

    w, h = 12, 10                               # glyph body, plus a 2px nub
    x = int(shared_data.width) - w - 4
    y = int(5 * sy)

    label = str(pct)
    tw = draw.textlength(label, font=font)
    draw.text((x - 1 - tw, y + 1), label, font=font, fill=0)

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


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #

def _with_board(fn):
    settings = load_settings()
    backend = detect(settings["pisugar_model"])
    if backend is None:
        logger.warning("No PiSugar found - is the board switched on?")
        return None, settings
    return backend, settings


def cut_power(delay=None, respect_config=True):
    backend, settings = _with_board(None)
    if backend is None:
        return 1
    if respect_config and not settings["pisugar_cut_power"]:
        logger.info("pisugar_cut_power is off - leaving the rail alone")
        return 0
    if delay is None:
        delay = settings["pisugar_cut_power_delay"]
    with _smbus()(backend.bus_id) as bus:
        logger.info("%s: %s", backend.name, backend.cut_power(bus, delay))
    return 0


def cancel_cut_power():
    backend, _ = _with_board(None)
    if backend is None:
        return 1
    with _smbus()(backend.bus_id) as bus:
        logger.info("%s: %s", backend.name, backend.cancel_cut_power(bus))
    return 0


def guard(threshold=None, force=False):
    """Halt the Pi when the battery is at or below the threshold and unplugged.

    Reads twice, ten seconds apart. A fuel gauge can report one bad sample, and a
    spurious shutdown in the middle of a scan is worse than one ten seconds late.
    """
    settings = load_settings()
    if not force and not settings["pisugar_autoshutdown"]:
        return 0
    if threshold is None:
        threshold = settings["pisugar_shutdown_percent"]

    sugar = PiSugar(refresh=0, model=settings["pisugar_model"])
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
    settings = load_settings()
    sugar = PiSugar(refresh=0, model=settings["pisugar_model"])
    sugar.read(force=True)
    if not sugar.available:
        print("No PiSugar reachable - is the board switched on?")
        print(f"config: {CONFIG_PATH}  (pisugar_model = {settings['pisugar_model']})")
        return 1

    board = sugar.backend
    state = "charging" if sugar.charging else "on battery"
    note = "" if board.tested else "   [UNTESTED - please report how it behaves]"
    print(f"board:   {board.name} on i2c-{board.bus_id} at 0x{board.addr:02x}{note}")
    print(f"battery: {sugar.percent}%  ({state})")
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
        return cut_power(int(arg) if arg else None, respect_config=arg is None)
    if cmd == "--cancel":
        return cancel_cut_power()
    if cmd == "--guard":
        return guard(int(arg) if arg else None, force=arg is not None)

    print(f"usage: {os.path.basename(argv[0])} "
          "[--status | --cut-power [N] | --cancel | --guard [PCT]]")
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
