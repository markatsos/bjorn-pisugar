"""pisugar.py against a simulated I2C bus. No hardware, no smbus2 needed.

    python3 -m unittest discover -s tests -v
"""

import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.modules.setdefault("smbus2", types.SimpleNamespace(SMBus=None))

import pisugar  # noqa: E402


class FakeBus:
    """Registers keyed by (address, register). Anything else NACKs, as on a real bus.

    `fail` maps (address, register) or (address, register, value) to the
    exception a write should raise. Calling the instance stands in for
    SMBus(bus_id), so it can be returned from pisugar._smbus().
    """

    def __init__(self, registers, fail=None):
        self.registers = dict(registers)
        self.writes = []
        self.fail = dict(fail or {})

    def __call__(self, bus_id):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read_byte_data(self, addr, reg):
        try:
            return self.registers[(addr, reg)]
        except KeyError:
            raise OSError(121, "Remote I/O error") from None

    def write_byte_data(self, addr, reg, value):
        self.writes.append((addr, reg, value))
        error = self.fail.get((addr, reg, value)) or self.fail.get((addr, reg))
        if error:
            raise error
        self.registers[(addr, reg)] = value


def pisugar3_registers(percent=80, plugged=False, millivolts=3900):
    return {
        (0x57, 0x02): 0x6C | (0x80 if plugged else 0),
        (0x57, 0x09): 0,
        (0x57, 0x0B): 0,
        (0x57, 0x22): millivolts >> 8,
        (0x57, 0x23): millivolts & 0xFF,
        (0x57, 0x2A): percent,
    }


def ip5209_registers(volts=3.8):
    raw = int((volts * 1000 - 2600) / 0.26855)
    return {
        (0x75, 0xA2): raw & 0xFF,
        (0x75, 0xA3): (raw >> 8) & 0x1F,
        (0x75, 0x55): 0x10,             # would have meant "plugged" in 0.2.0
        (0x75, 0xD0): 0,
        (0x75, 0xD1): 0,
    }


class BoardTestCase(unittest.TestCase):
    """Runs each test with a temporary config and one simulated bus on i2c-1."""

    registers = {}

    def setUp(self):
        self.bus = FakeBus(self.registers)
        self.tmp = tempfile.TemporaryDirectory()
        self.config_path = os.path.join(self.tmp.name, "shared_config.json")
        self.poweroff = mock.Mock()
        for patcher in (
            mock.patch.object(pisugar, "_smbus", return_value=self.bus),
            mock.patch.object(pisugar, "_bus_exists", side_effect=lambda bus_id: bus_id == 1),
            mock.patch.object(pisugar, "CONFIG_PATH", self.config_path),
            mock.patch.object(pisugar.subprocess, "run", self.poweroff),
            mock.patch.object(pisugar.time, "sleep"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        pisugar.logger.disabled = True
        self.addCleanup(setattr, pisugar.logger, "disabled", False)

    def configure(self, **settings):
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(settings, f)

    def assertNothingWritten(self):
        self.assertEqual(self.bus.writes, [])

    def assertNoPoweroff(self):
        self.poweroff.assert_not_called()


class WriteProtectionTest(BoardTestCase):
    """PiSugar/pisugar-doc#4, point 6."""

    registers = pisugar3_registers()

    def test_cut_power_writes_the_documented_sequence(self):
        pisugar.PiSugar3(1).cut_power(self.bus, 30)
        self.assertEqual(self.bus.writes, [
            (0x57, 0x0B, 0x29),         # unlock
            (0x57, 0x09, 30),           # delay first
            (0x57, 0x02, 0x4C),         # then clear bit 5 of 0x6C
            (0x57, 0x0B, 0x00),         # lock
        ])

    def test_protection_is_restored_when_a_write_fails(self):
        self.bus.fail[(0x57, 0x09)] = OSError("delay write failed")
        with self.assertRaisesRegex(OSError, "delay write failed"):
            pisugar.PiSugar3(1).cut_power(self.bus, 30)
        self.assertEqual(self.bus.writes[-1], (0x57, 0x0B, 0x00))
        self.assertEqual(self.bus.registers[(0x57, 0x0B)], 0x00)

    def test_protection_is_restored_when_cancel_fails(self):
        self.bus.fail[(0x57, 0x02)] = OSError("ctrl write failed")
        with self.assertRaises(OSError):
            pisugar.PiSugar3(1).cancel_cut_power(self.bus)
        self.assertEqual(self.bus.writes[-1], (0x57, 0x0B, 0x00))

    def test_a_failed_relock_does_not_hide_the_original_error(self):
        self.bus.fail[(0x57, 0x09)] = OSError("original")
        self.bus.fail[(0x57, 0x0B, 0x00)] = OSError("relock")
        with self.assertRaisesRegex(OSError, "original"):
            pisugar.PiSugar3(1).cut_power(self.bus, 30)

    def test_a_failed_relock_after_success_is_reported(self):
        self.bus.fail[(0x57, 0x0B, 0x00)] = OSError("relock")
        with self.assertRaisesRegex(OSError, "relock"):
            pisugar.PiSugar3(1).cut_power(self.bus, 30)


class ExplicitModelTest(BoardTestCase):
    """PiSugar/pisugar-doc#4, point 3: nothing acts until the board is named."""

    registers = pisugar3_registers(percent=5)

    def test_auto_never_writes_or_shuts_down(self):
        self.configure(pisugar_model="auto")
        self.assertEqual(pisugar.guard(), 0)
        self.assertEqual(pisugar.guard(100, force=True), 3)
        self.assertEqual(pisugar.cut_power(), 0)
        self.assertEqual(pisugar.cut_power(30, respect_config=False), 3)
        self.assertEqual(pisugar.cancel_cut_power(), 3)
        self.assertNothingWritten()
        self.assertNoPoweroff()

    def test_missing_model_key_means_auto(self):
        self.configure()
        self.assertEqual(pisugar.cut_power(), 0)
        self.assertNothingWritten()

    def test_named_pisugar3_cuts_power(self):
        self.configure(pisugar_model="pisugar3", pisugar_cut_power_delay=45)
        self.assertEqual(pisugar.cut_power(), 0)
        self.assertIn((0x57, 0x09, 45), self.bus.writes)
        self.assertEqual(self.bus.writes[-1], (0x57, 0x0B, 0x00))

    def test_named_pisugar3_shuts_down_on_a_confirmed_low_reading(self):
        self.configure(pisugar_model="pisugar3", pisugar_shutdown_percent=10)
        self.assertEqual(pisugar.guard(), 0)
        self.poweroff.assert_called_once_with(["systemctl", "poweroff"], check=False)
        self.assertNothingWritten()

    def test_guard_leaves_a_plugged_in_board_alone(self):
        self.bus.registers[(0x57, 0x02)] |= 0x80
        self.configure(pisugar_model="pisugar3")
        self.assertEqual(pisugar.guard(), 0)
        self.assertNoPoweroff()

    def test_a_forced_run_says_why_it_does_nothing(self):
        self.bus.registers[(0x57, 0x02)] |= 0x80
        self.configure(pisugar_model="pisugar3")
        pisugar.logger.disabled = False
        with self.assertLogs("pisugar", level="INFO") as logs:
            self.assertEqual(pisugar.guard(100, force=True), 0)
        self.assertIn("external power connected - not shutting down", logs.output[0])
        self.assertNoPoweroff()

    def test_a_timer_run_stays_quiet_when_there_is_nothing_to_do(self):
        self.bus.registers[(0x57, 0x02)] |= 0x80
        self.configure(pisugar_model="pisugar3")
        pisugar.logger.disabled = False
        with mock.patch.object(pisugar.logger, "info") as info:
            self.assertEqual(pisugar.guard(), 0)
        info.assert_not_called()

    def test_cut_power_switch_still_applies(self):
        self.configure(pisugar_model="pisugar3", pisugar_cut_power=False)
        self.assertEqual(pisugar.cut_power(), 0)
        self.assertNothingWritten()

    def test_identity_check_blocks_writes(self):
        self.bus.registers[(0x57, 0x22)] = 0
        self.bus.registers[(0x57, 0x23)] = 0
        self.configure(pisugar_model="pisugar3")
        self.assertEqual(pisugar.cut_power(), 1)
        self.assertEqual(pisugar.guard(), 1)
        self.assertNothingWritten()
        self.assertNoPoweroff()

    def test_values_written_by_0_2_0_still_select_the_board(self):
        for value in ("3", 3, 3.0, "PiSugar3"):
            self.assertEqual(pisugar.canonical_model(value), "pisugar3", value)
        self.assertEqual(pisugar.canonical_model("ip5209"), "pisugar2")
        self.assertEqual(pisugar.canonical_model("2plus"), "pisugar2pro")
        self.assertEqual(pisugar.canonical_model(""), "auto")

    def test_unknown_and_pisugar1_are_treated_as_auto(self):
        self.assertIsNone(pisugar.canonical_model("1"))
        self.assertEqual(pisugar.normalize_model("1"), "auto")
        self.assertEqual(pisugar.normalize_model("pisugar9"), "auto")
        self.assertEqual(pisugar.normalize_model(True), "auto")


class WrongBoardTest(BoardTestCase):
    """A named model only matches that board."""

    registers = ip5209_registers()

    def test_pisugar3_named_but_absent_finds_nothing(self):
        self.configure(pisugar_model="pisugar3")
        self.assertIsNone(pisugar.detect("pisugar3"))
        self.assertEqual(pisugar.cut_power(), 1)
        self.assertNothingWritten()


class PiSugar2Test(BoardTestCase):
    """PiSugar/pisugar-doc#4, points 1 and 2: the 0x75 boards are read-only."""

    registers = ip5209_registers(volts=3.8)

    def test_auto_detects_the_ip5209_without_a_charge_state(self):
        board = pisugar.detect("auto")
        self.assertIsInstance(board, pisugar.IP5209)
        percent, plugged = board.read(self.bus)
        self.assertEqual(percent, 28)
        self.assertIsNone(plugged)

    def test_named_pisugar2_never_writes_or_shuts_down(self):
        self.configure(pisugar_model="pisugar2")
        self.assertEqual(pisugar.guard(100, force=True), 3)
        self.assertEqual(pisugar.cut_power(), 0)
        self.assertEqual(pisugar.cancel_cut_power(), 3)
        self.assertNothingWritten()
        self.assertNoPoweroff()

    def test_no_backend_can_be_selected_for_pisugar1(self):
        self.assertNotIn("pisugar1", pisugar.BY_MODEL)
        self.assertFalse(any(cls.writable for cls in (pisugar.IP5209, pisugar.IP5312)))


class ConfigTest(BoardTestCase):
    """PiSugar/pisugar-doc#4, point 4: no acting on defaults."""

    registers = pisugar3_registers(percent=5)

    def test_unreadable_config_means_no_action(self):
        self.assertFalse(os.path.exists(self.config_path))
        self.assertEqual(pisugar.guard(), 1)
        self.assertEqual(pisugar.cut_power(), 1)
        self.assertEqual(pisugar.cancel_cut_power(), 1)
        self.assertNothingWritten()
        self.assertNoPoweroff()

    def test_malformed_config_means_no_action(self):
        with open(self.config_path, "w") as f:
            f.write("[1, 2")
        self.assertEqual(pisugar.cut_power(), 1)
        self.assertNothingWritten()

    def test_string_booleans_and_ranges(self):
        values = pisugar.load_settings({
            "pisugar_autoshutdown": "false",
            "pisugar_cut_power": "off",
            "pisugar_show_battery": "true",
            "pisugar_shutdown_percent": 150,
            "pisugar_cut_power_delay": -5,
        })
        self.assertFalse(values["pisugar_autoshutdown"])
        self.assertFalse(values["pisugar_cut_power"])
        self.assertTrue(values["pisugar_show_battery"])
        self.assertEqual(values["pisugar_shutdown_percent"], 100)
        self.assertEqual(values["pisugar_cut_power_delay"], 0)


class BjornDirTest(unittest.TestCase):
    """PiSugar/pisugar-doc#4, point 4: the units and the module agree on BJORN_DIR."""

    def bjorn_dir(self, **env):
        code = "import pisugar; print(pisugar.BJORN_DIR)"
        base = {k: v for k, v in os.environ.items() if k != "BJORN_DIR"}
        return subprocess.run([sys.executable, "-c", code], cwd=ROOT, env={**base, **env},
                              capture_output=True, text=True, check=True).stdout.strip()

    def test_defaults_to_the_module_directory(self):
        self.assertEqual(self.bjorn_dir(), ROOT)

    def test_environment_wins(self):
        self.assertEqual(self.bjorn_dir(BJORN_DIR="/opt/Bjorn"), "/opt/Bjorn")

    def test_units_pass_bjorn_dir(self):
        for unit in ("pisugar-guard.service", "pisugar-poweroff.service"):
            with open(os.path.join(ROOT, "systemd", unit), encoding="utf-8") as f:
                self.assertIn("Environment=BJORN_DIR=@BJORN_DIR@\n", f.read(), unit)


class FakeDraw:
    def __init__(self):
        self.calls = []

    def textlength(self, text, font=None):
        return 6 * len(text)

    def __getattr__(self, name):
        return lambda *args, **kwargs: self.calls.append(name)


class DisplayTest(BoardTestCase):
    """PiSugar/pisugar-doc#4, point 7: the override reaches the indicator."""

    registers = ip5209_registers()

    def shared_data(self, **config):
        return types.SimpleNamespace(config=config, width=122, font_arial9=None)

    def test_configured_model_reaches_the_reader(self):
        sugar = pisugar.PiSugar()
        self.assertEqual(sugar.model, "auto")
        pisugar.draw_battery(None, FakeDraw(), self.shared_data(pisugar_model="pisugar2"), sugar)
        self.assertEqual(sugar.model, "pisugar2")
        self.assertIsInstance(sugar.backend, pisugar.IP5209)

        # Saved on the Config page as the number 3: the reader follows, and the
        # IP5209 is no longer accepted as the board.
        pisugar.draw_battery(None, FakeDraw(), self.shared_data(pisugar_model=3), sugar)
        self.assertEqual(sugar.model, "pisugar3")
        self.assertFalse(sugar.available)

    def test_no_charging_bolt_without_a_charge_state(self):
        draw = FakeDraw()
        pisugar.draw_battery(None, draw, self.shared_data(), pisugar.PiSugar())
        self.assertIn("rectangle", draw.calls)
        self.assertNotIn("polygon", draw.calls)

    def test_hidden_when_switched_off(self):
        draw = FakeDraw()
        pisugar.draw_battery(None, draw, self.shared_data(pisugar_show_battery=False), pisugar.PiSugar())
        self.assertEqual(draw.calls, [])


if __name__ == "__main__":
    unittest.main()
