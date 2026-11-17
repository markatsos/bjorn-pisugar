"""PiSugar support for Bjorn.

Three jobs, one module:

  draw_battery()  level indicator in the top-right of the e-ink header
  --cut-power     tell the board to drop the rail once the Pi has halted
  --guard         clean shutdown when the battery runs low

Settings live in Bjorn's own config/shared_config.json, so they render as
switches and fields on the web UI's Config page without touching any HTML.

Boards, by their pisugar_model value:

  pisugar3     PiSugar 3 / 3 Plus      0x57   level and charge state from the
                                              controller, low-battery shutdown,
                                              timed power cut
  pisugar2     PiSugar 2 (2 or 4 LEDs) 0x75   IP5209 - level from voltage, read-only
  pisugar2pro  PiSugar 2 Pro / 2 Plus  0x75   IP5312 - level from voltage, read-only

The default, "auto", finds the board for the indicator and does nothing else.
Nothing writes to a register or shuts the Pi down until pisugar_model names a
board with power control, and that board passes an identity check.

Only the PiSugar 3 path is verified on hardware. The PiSugar 2 boards are
experimental and are never written to: their charge-state detection differs
between variants and needs register set-up first, so they get the battery level
and nothing that acts on the hardware. PiSugar 1 has no I2C (IP5306) and cannot
be supported. See COMPATIBILITY.md.
"""

import contextlib
import json
import logging
import os
import subprocess
import sys
import time

VERSION = "0.3.0"

logger = logging.getLogger("pisugar")

# The installed copy lives in Bjorn's own directory, so that is the default. The
# systemd units pass BJORN_DIR explicitly as well, which also covers running a
# copy from somewhere else.
BJORN_DIR = os.environ.get("BJORN_DIR") or os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BJORN_DIR, "config", "shared_config.json")

DEFAULTS = {
    "pisugar_show_battery": True,
    "pisugar_autoshutdown": True,
    "pisugar_shutdown_percent": 10,
    "pisugar_cut_power": True,
    "pisugar_cut_power_delay": 30,
    "pisugar_model": "auto",
}

# Values for pisugar_model. Anything that writes to a register or shuts the Pi
# down needs the board named here; "auto" only ever reads. The names are not
# numbers on purpose: Bjorn's Config page saves a value like "3" as the number 3
# and from then on renders the field as a number input, which cannot hold "auto".
MODELS = ("auto", "pisugar3", "pisugar2", "pisugar2pro")

# Values written by 0.2.0 and earlier.
MODEL_ALIASES = {
    "3": "pisugar3",
    "2": "pisugar2", "ip5209": "pisugar2",
    "2pro": "pisugar2pro", "2plus": "pisugar2pro", "ip5312": "pisugar2pro",
    "pisugar2plus": "pisugar2pro",
}
UNSUPPORTED_MODELS = {
    "1": "PiSugar 1 has no I2C (IP5306) and cannot be read",
    "pisugar1": "PiSugar 1 has no I2C (IP5306) and cannot be read",
}

# Buses to probe, in order. Pi rev 1 boards wired the header to i2c-0.
CANDIDATE_BUSES = (1, 0)

_warned = set()


def _warn_once(message, *args):
    key = message % args
    if key not in _warned:
        _warned.add(key)
        logger.warning(message, *args)


def canonical_model(value):
    """The canonical name for a pisugar_model value, or None if it is not one."""
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    name = str(value).strip().lower()
    if name == "":
        return "auto"
    name = MODEL_ALIASES.get(name, name)
    return name if name in MODELS else None


def normalize_model(value):
    """Like canonical_model(), but falls back to "auto" - which never acts - with a warning."""
    name = canonical_model(value)
    if name is not None:
        return name
    reason = UNSUPPORTED_MODELS.get(str(value).strip().lower(), "not a known model")
    _warn_once("pisugar_model %r: %s - treating it as auto", value, reason)
    return "auto"


def _as_bool(value):
    # Bjorn's Config page stores real booleans; a hand-edited "false" must not
    # come out as True.
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "off")
    return bool(value)


def read_config(path=None):
    """Bjorn's config as a dict. Raises if it is missing, unreadable or malformed."""
    with open(path or CONFIG_PATH, "r", encoding="utf-8") as f:
        config = json.load(f)
    if not isinstance(config, dict):
        raise ValueError("top level is not a JSON object")
    return config


def settings_from_file():
    """Return (settings, None), or (None, reason) if the config cannot be read.

    The guard and the power cut act on the hardware, so they must not fall back to
    the defaults: those have both features enabled, and a wrong path would
    silently override whatever the user switched off in the web UI.
    """
    try:
        return load_settings(read_config()), None
    except Exception as e:
        return None, f"cannot read {CONFIG_PATH}: {e}"


def load_settings(config=None):
    """Merge Bjorn's config over the defaults.

    `config` is the live dict when running inside Bjorn. Without one the file is
    read on a best-effort basis, which is good enough for drawing the indicator;
    anything that acts on the hardware uses settings_from_file() instead.
    """
    values = dict(DEFAULTS)
    if config is None:
        try:
            config = read_config()
        except Exception as e:
            logger.warning("Could not read %s (%s) - using defaults", CONFIG_PATH, e)
            config = {}
    for key, fallback in DEFAULTS.items():
        value = config.get(key, fallback)
        if key == "pisugar_model":
            values[key] = normalize_model(value)
        elif isinstance(fallback, bool):
            values[key] = _as_bool(value)
        else:
            try:
                values[key] = int(value)
            except (TypeError, ValueError):
                values[key] = fallback
    values["pisugar_shutdown_percent"] = max(0, min(100, values["pisugar_shutdown_percent"]))
    values["pisugar_cut_power_delay"] = max(0, min(255, values["pisugar_cut_power_delay"]))
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

class NotSupported(Exception):
    """The board cannot do this, by design rather than by failure."""


class Backend:
    """One battery board. Subclasses know their own registers."""

    key = ""            # the pisugar_model value that selects this board
    name = "unknown"
    addr = 0x00
    tested = False      # run on real hardware
    writable = False    # power control: register writes and the low-battery shutdown

    def __init__(self, bus_id):
        self.bus_id = bus_id

    @classmethod
    def probe(cls, bus, bus_id):
        raise NotImplementedError

    def read(self, bus):
        """Return (percent 0-100, external power present - or None if not read)."""
        raise NotImplementedError

    def voltage(self, bus):
        """Battery voltage in volts."""
        raise NotImplementedError

    def check_identity(self, bus):
        """None if the board looks right for power control, else what is wrong."""
        return f"{self.name}: no power control on this board"

    def cut_power(self, bus, delay):
        """Arrange for the rail to drop once the Pi is down. Returns a description."""
        raise NotSupported(f"{self.name}: no power control on this board")

    def cancel_cut_power(self, bus):
        raise NotSupported(f"{self.name}: no power control on this board")


class PiSugar3(Backend):
    """PiSugar 3 and 3 Plus.

    0x02 bit 7  external power connected (read only)
    0x02 bit 5  output switch with delay; clearing it starts the 0x09 countdown
    0x09        countdown length before the rail is cut, 0-255
    0x0B        write 0x29 to unlock the other registers, 0x00 to lock again
    0x22/0x23   battery voltage in mV, high byte first
    0x2A        calculated battery percentage, 0-100
    """

    key = "pisugar3"
    name = "PiSugar 3"
    addr = 0x57
    tested = True
    writable = True

    REG_CTRL1 = 0x02
    REG_DELAY = 0x09
    REG_WRITE_ENABLE = 0x0B
    REG_VOLTAGE_HIGH = 0x22
    REG_VOLTAGE_LOW = 0x23
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

    def voltage(self, bus):
        high = bus.read_byte_data(self.addr, self.REG_VOLTAGE_HIGH)
        low = bus.read_byte_data(self.addr, self.REG_VOLTAGE_LOW)
        return ((high << 8) | low) / 1000.0

    def check_identity(self, bus):
        """A stricter check than probe(), run before anything is written.

        probe() only asks whether something at 0x57 returns a plausible
        percentage. Before the write-protection register is touched, the battery
        voltage has to be plausible for a single Li-ion cell as well.
        """
        percent = bus.read_byte_data(self.addr, self.REG_PERCENT)
        if percent > 100:
            return f"0x{self.REG_PERCENT:02x} reads {percent}, not a percentage"
        volts = self.voltage(bus)
        if not 2.5 <= volts <= 4.6:
            return f"battery voltage reads {volts:.3f} V, outside 2.5-4.6 V"
        return None

    @contextlib.contextmanager
    def _write_unlocked(self, bus):
        """Lift write protection for the duration of the block.

        Protection goes back on in a `finally`, so an I2C error halfway through
        cannot leave the registers writable. The unlock itself is inside the try:
        a write that fails on the wire may still have reached the board. If
        re-locking fails as well, that is logged; it is only raised when the block
        itself succeeded, so it never hides the error that caused the failure.
        """
        completed = False
        try:
            bus.write_byte_data(self.addr, self.REG_WRITE_ENABLE, self.WRITE_UNLOCK)
            yield
            completed = True
        finally:
            try:
                bus.write_byte_data(self.addr, self.REG_WRITE_ENABLE, self.WRITE_LOCK)
            except Exception as e:
                logger.error("PiSugar 3: could not restore write protection (0x0B): %s", e)
                if completed:
                    raise

    def cut_power(self, bus, delay):
        delay = max(0, min(255, int(delay)))
        with self._write_unlocked(bus):
            # Order matters: 0x09 must be set before 0x02 is touched, or the board
            # ignores the delay entirely and nothing happens.
            bus.write_byte_data(self.addr, self.REG_DELAY, delay)
            ctrl1 = bus.read_byte_data(self.addr, self.REG_CTRL1)
            bus.write_byte_data(self.addr, self.REG_CTRL1, ctrl1 & ~self.BIT_OUTPUT_DELAY & 0xFF)
        return f"countdown started, rail drops in ~{delay}s"

    def cancel_cut_power(self, bus):
        with self._write_unlocked(bus):
            ctrl1 = bus.read_byte_data(self.addr, self.REG_CTRL1)
            bus.write_byte_data(self.addr, self.REG_CTRL1, ctrl1 | self.BIT_OUTPUT_DELAY)
        return "countdown cancelled"


class IP5209(Backend):
    """PiSugar 2, 2-LED and 4-LED (the IP5209 chip). UNTESTED, read-only.

    0xa2/0xa3   battery voltage, low byte then high 6 bits with a sign bit

    Only the voltage is read. The two variants detect external power in different
    ways - the 2-LED board on 0x55 bit 4 once its GPIOs have been configured, the
    4-LED board not through a register at all - and configuring the GPIOs means
    writing to the chip. Until this runs on real hardware it stays read-only: a
    battery level, but no charge state, no low-battery shutdown and no power cut.
    """

    key = "pisugar2"
    name = "PiSugar 2 (IP5209)"
    addr = 0x75
    tested = False

    CURVE = [(4.16, 100.0), (4.05, 95.0), (4.00, 80.0), (3.92, 65.0), (3.86, 40.0),
             (3.79, 25.5), (3.66, 10.0), (3.52, 6.5), (3.49, 3.2), (3.10, 0.0)]

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
        return max(0, min(100, percent)), None


class IP5312(IP5209):
    """PiSugar 2 Pro, now sold as PiSugar 2 Plus (the IP5312 chip). UNTESTED, read-only.

    0xd0/0xd1   battery voltage, low byte then high 6 bits

    As with the IP5209, reading the charge state needs GPIO set-up writes first,
    so only the voltage is read.
    """

    key = "pisugar2pro"
    name = "PiSugar 2 Pro / 2 Plus (IP5312)"
    addr = 0x75
    tested = False

    CURVE = [(4.10, 100.0), (4.05, 95.0), (3.90, 88.0), (3.80, 77.0), (3.70, 65.0),
             (3.62, 55.0), (3.58, 49.0), (3.49, 25.6), (3.32, 4.5), (3.10, 0.0)]

    def voltage(self, bus):
        low = bus.read_byte_data(self.addr, 0xD0)
        high = bus.read_byte_data(self.addr, 0xD1)
        if low == 0 and high == 0:
            raise OSError("IP5312 voltage registers read zero")
        return (((high & 0b0011_1111) << 8) + low) * 0.26855 / 1000.0 + 2.6


# IP5312 is tried before IP5209: they share address 0x75, and the 5312 registers
# read back as zero on a 5209, which its probe rejects.
BACKENDS = (PiSugar3, IP5312, IP5209)
BY_MODEL = {cls.key: cls for cls in BACKENDS}


def _bus_exists(bus_id):
    return os.path.exists(f"/dev/i2c-{bus_id}")


def detect(model="auto"):
    """Find the board. Returns a Backend instance, or None if nothing answers.

    With "auto" every board is tried. With a model named, only that board is,
    and it still has to answer its probe - a wrong setting finds nothing rather
    than reading another chip's registers as if they were the named board's.
    """
    model = normalize_model(model)
    candidates = BACKENDS if model == "auto" else (BY_MODEL[model],)
    SMBus = _smbus()
    for bus_id in CANDIDATE_BUSES:
        if not _bus_exists(bus_id):
            continue
        for cls in candidates:
            try:
                with SMBus(bus_id) as bus:
                    if cls.probe(bus, bus_id):
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

    def set_model(self, model):
        """Follow a change of `pisugar_model` made on the Config page.

        A new value drops the board found so far and forces a fresh read, so the
        next frame uses the new setting rather than whatever was detected before.
        """
        if model == self.model:
            return
        self.model = model
        self.backend = None
        self.available = False
        self._last_read = 0.0

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
    settings = load_settings(getattr(shared_data, "config", None))
    if not settings["pisugar_show_battery"]:
        return

    # display.py creates the reader once, with the default model. Take the model
    # from the live config on every frame, so the override applies here too.
    sugar.set_model(settings["pisugar_model"])
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
# Power control                                                                #
# --------------------------------------------------------------------------- #

class NotConfigured(Exception):
    """Power control is off by configuration - the normal state until the board is named."""


class BoardError(Exception):
    """The named board is missing, or does not look like the board it was named as."""


def power_board(settings):
    """The board that the guard and the power cut may act on.

    Raises NotConfigured unless pisugar_model names a board with power control -
    auto-detection alone is never enough to write to a register or to shut the Pi
    down - and BoardError if that board does not answer or fails its identity check.
    """
    model = settings["pisugar_model"]
    if model == "auto":
        raise NotConfigured("pisugar_model is auto - set it to pisugar3 to enable power control")
    cls = BY_MODEL[model]
    if not cls.writable:
        raise NotConfigured(f"{cls.name} is read-only here - no power control on this board")
    board = detect(model)
    if board is None:
        raise BoardError(f"no {cls.name} answering - is the board switched on?")
    with _smbus()(board.bus_id) as bus:
        problem = board.check_identity(bus)
    if problem:
        raise BoardError(f"the device at 0x{board.addr:02x} failed the {cls.name} check: {problem}")
    return board


def cut_power(delay=None, respect_config=True):
    """Start the countdown that switches the board off once the Pi has halted.

    Run by pisugar-poweroff.service on the way down, where "nothing to do" -
    power control off, or pisugar_cut_power switched off - is not an error.
    """
    settings, error = settings_from_file()
    if error:
        logger.error("%s - leaving the rail alone", error)
        return 1
    if respect_config and not settings["pisugar_cut_power"]:
        logger.info("pisugar_cut_power is off - leaving the rail alone")
        return 0
    try:
        board = power_board(settings)
    except NotConfigured as e:
        logger.info("%s - leaving the rail alone", e)
        return 0 if respect_config else 3
    except BoardError as e:
        logger.error("%s - leaving the rail alone", e)
        return 1
    if delay is None:
        delay = settings["pisugar_cut_power_delay"]
    with _smbus()(board.bus_id) as bus:
        logger.info("%s: %s", board.name, board.cut_power(bus, delay))
    return 0


def cancel_cut_power():
    settings, error = settings_from_file()
    if error:
        logger.error("%s - not writing to the board", error)
        return 1
    try:
        board = power_board(settings)
    except NotConfigured as e:
        logger.error("%s - not writing to the board", e)
        return 3
    except BoardError as e:
        logger.error("%s - not writing to the board", e)
        return 1
    with _smbus()(board.bus_id) as bus:
        logger.info("%s: %s", board.name, board.cancel_cut_power(bus))
    return 0


def guard(threshold=None, force=False):
    """Halt the Pi when the battery is at or below the threshold and unplugged.

    Reads twice, ten seconds apart. A fuel gauge can report one bad sample, and a
    spurious shutdown in the middle of a scan is worse than one ten seconds late.
    """
    settings, error = settings_from_file()
    if error:
        logger.error("%s - not acting", error)
        return 1
    if not force and not settings["pisugar_autoshutdown"]:
        return 0
    if threshold is None:
        threshold = settings["pisugar_shutdown_percent"]

    try:
        board = power_board(settings)
    except NotConfigured as e:
        # The timer fires every two minutes; with power control off by choice,
        # say nothing. --status explains why the guard is idle.
        if force:
            logger.error("%s - not acting", e)
            return 3
        logger.debug("%s - not acting", e)
        return 0
    except BoardError as e:
        logger.warning("%s - not acting", e)
        return 1

    SMBus = _smbus()
    try:
        with SMBus(board.bus_id) as bus:
            percent, plugged = board.read(bus)
        if plugged or percent > threshold:
            return 0
        first = percent
        time.sleep(10)
        with SMBus(board.bus_id) as bus:
            percent, plugged = board.read(bus)
    except OSError as e:
        logger.warning("%s stopped answering (%s) - not acting", board.name, e)
        return 1
    if plugged or percent > threshold:
        logger.info("Low reading of %d%% not confirmed - ignoring", first)
        return 0

    logger.warning("Battery at %d%% and unplugged - shutting down", percent)
    subprocess.run(["systemctl", "poweroff"], check=False)
    return 0


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #

def status():
    rc = 0
    print(f"bjorn-pisugar {VERSION}")
    settings, error = settings_from_file()
    if error:
        print(f"config:  {error}")
        print("         the guard and the power cut do nothing until this is fixed")
        settings = load_settings({})
        rc = 1
    else:
        print(f"config:  {CONFIG_PATH}")

    model = settings["pisugar_model"]
    board = detect(model)
    if board is None:
        wanted = "PiSugar" if model == "auto" else BY_MODEL[model].name
        print(f"board:   no {wanted} answering - is the board switched on?")
        rc = 1
    else:
        if board.tested:
            note = ""
        elif not board.writable:
            note = "   [EXPERIMENTAL - battery level only, please report how it behaves]"
        else:
            note = "   [UNTESTED - please report how it behaves]"
        print(f"board:   {board.name} on i2c-{board.bus_id} at 0x{board.addr:02x}{note}")
        with _smbus()(board.bus_id) as bus:
            percent, plugged = board.read(bus)
            try:
                volts = f", {board.voltage(bus):.2f} V"
            except Exception:
                volts = ""
        if plugged is None:
            state = "charge state not read on this board"
        else:
            state = "external power connected" if plugged else "on battery"
        print(f"battery: {percent}%{volts}, {state}")

    try:
        power_board(settings)
        parts = []
        if settings["pisugar_autoshutdown"]:
            parts.append(f"shutdown at {settings['pisugar_shutdown_percent']}%")
        if settings["pisugar_cut_power"]:
            parts.append(f"power cut {settings['pisugar_cut_power_delay']}s after halt")
        print("power:   " + (", ".join(parts) if parts else "both switched off in Config"))
    except (NotConfigured, BoardError) as e:
        print(f"power:   off - {e}")

    print("settings:")
    for key in sorted(DEFAULTS):
        print(f"  {key} = {settings[key]}")
    return rc


def detect_command():
    """One line for install.sh: "<model> <bus> <name>", or "none". Read-only."""
    board = detect("auto")
    if board is None:
        print("none")
        return 1
    print(f"{board.key} {board.bus_id} {board.name}")
    return 0


USAGE = "[--status | --detect | --guard [PCT] | --cut-power [SECONDS] | --cancel]"


def _main(argv):
    logging.basicConfig(format="%(message)s", level=logging.INFO)

    cmd = argv[1] if len(argv) > 1 else "--status"
    arg = argv[2] if len(argv) > 2 else None
    try:
        number = int(arg) if arg is not None else None
    except ValueError:
        cmd = None

    try:
        if cmd == "--status":
            return status()
        if cmd == "--detect":
            return detect_command()
        if cmd == "--cut-power":
            return cut_power(number, respect_config=number is None)
        if cmd == "--cancel":
            return cancel_cut_power()
        if cmd == "--guard":
            return guard(number, force=number is not None)
    except ImportError as e:
        print(f"smbus2 is not installed ({e}) - run install.sh, or apt install python3-smbus2")
        return 1

    print(f"usage: {os.path.basename(argv[0])} {USAGE}")
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
