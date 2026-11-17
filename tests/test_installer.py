"""lib/installer.py on a throwaway Bjorn tree. No root, no systemd needed.

    python3 -m unittest discover -s tests -v
"""

import contextlib
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))

import installer  # noqa: E402

# The lines of Bjorn v1.0.0 alpha 2's display.py that the patch touches, with
# their original indentation, inside just enough code to parse.
STOCK_DISPLAY = (
    "import os\n"
    "import subprocess  \n"
    "\n"
    "\n"
    "class Display:\n"
    "    def __init__(self, shared_data):\n"
    "        self.shared_data = shared_data\n"
    "        self.web_screen_reversed = self.shared_data.web_screen_reversed\n"
    "\n"
    "    def run(self, image, draw):\n"
    "        while True:\n"
    "            if self.shared_data:\n"
    '                draw.text((int(37 * self.scale_factor_x), int(5 * self.scale_factor_y)), "BJORN", '
    "font=self.shared_data.font_viking, fill=0)\n"
    "                if self.shared_data.pan_connected:\n"
    "                    image.paste(self.shared_data.connected, (int(104 * self.scale_factor_x), "
    "int(3 * self.scale_factor_y)))\n"
    "                if self.shared_data.usb_active:\n"
    "                    image.paste(self.shared_data.usb, (int(90 * self.scale_factor_x), "
    "int(4 * self.scale_factor_y)))\n"
    "            break"                 # upstream has no newline at the end either
)

STOCK_CONFIG = json.dumps({"__title_Bjorn__": "Settings", "manual_mode": False,
                           "epd_type": "epd2in13_V4"}, indent=4) + "\n"


def sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


class PatchTest(unittest.TestCase):

    def test_round_trip_for_both_layouts(self):
        for layout in ("narrow", "wide"):
            patched = installer.patch_text(STOCK_DISPLAY, layout)
            self.assertIn(installer.MARKER, patched)
            self.assertEqual(installer.unpatch_text(patched), (STOCK_DISPLAY, layout))

    def test_narrow_moves_the_logo_and_shares_the_icon_slot(self):
        patched = installer.patch_text(STOCK_DISPLAY, "narrow")
        self.assertIn("int(23 * self.scale_factor_x)", patched)
        self.assertIn("elif self.shared_data.usb_active:", patched)

    def test_unexpected_version_is_refused(self):
        changed = STOCK_DISPLAY.replace("int(37 *", "int(38 *")
        with self.assertRaisesRegex(installer.Refused, "not the version"):
            installer.patch_text(changed, "narrow")

    def test_patching_twice_is_refused(self):
        with self.assertRaises(installer.Refused):
            installer.patch_text(installer.patch_text(STOCK_DISPLAY, "narrow"), "narrow")

    def test_unrecognised_pisugar_lines_are_refused(self):
        odd = STOCK_DISPLAY.replace("import subprocess  \n", "import subprocess  \n" + installer.MARKER + "\n")
        with self.assertRaises(installer.Refused):
            installer.unpatch_text(odd)


class InstallTest(unittest.TestCase):
    """PiSugar/pisugar-doc#4, point 5."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.bjorn = os.path.join(self.tmp, "Bjorn")
        os.makedirs(os.path.join(self.bjorn, "config"))
        self.display = os.path.join(self.bjorn, "display.py")
        self.config = os.path.join(self.bjorn, "config", "shared_config.json")
        self.write(self.display, STOCK_DISPLAY)
        self.write(self.config, STOCK_CONFIG)
        os.chmod(self.display, 0o664)
        os.chmod(self.config, 0o640)
        state, units = os.path.join(self.tmp, "state"), os.path.join(self.tmp, "units")
        os.makedirs(units)
        for name, value in (("STATE_DIR", state), ("UNIT_DIR", units)):
            patcher = mock.patch.object(installer, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.state, self.units = state, units

    @staticmethod
    def write(path, text):
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(text)

    def read_config(self):
        with open(self.config, encoding="utf-8") as f:
            return json.load(f)

    def run_quietly(self, fn, *args, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return fn(*args, **kwargs)

    def install(self, model="pisugar3", **kwargs):
        self.run_quietly(installer.install, self.bjorn, ROOT, "narrow", model, **kwargs)

    def uninstall(self, force=False):
        return self.run_quietly(installer.uninstall, self.bjorn, force)

    def check(self, force=False):
        return self.run_quietly(installer.check, self.bjorn, force)

    def test_round_trip_leaves_everything_byte_identical(self):
        before = sha(self.display), sha(self.config)
        self.install(packages=["python3-smbus2"], i2c_enabled=True)
        self.assertEqual(self.read_config()["pisugar_model"], "pisugar3")
        self.assertEqual(self.check(), 0)
        self.assertEqual(self.uninstall(), 0)
        self.assertEqual((sha(self.display), sha(self.config)), before)
        self.assertFalse(os.path.exists(os.path.join(self.bjorn, "pisugar.py")))
        self.assertEqual(os.listdir(self.units), [])
        self.assertFalse(os.path.exists(self.state))

    def test_modes_are_kept(self):
        self.install()
        self.assertEqual(os.stat(self.display).st_mode & 0o777, 0o664)
        self.assertEqual(os.stat(self.config).st_mode & 0o777, 0o640)
        self.uninstall()
        self.assertEqual(os.stat(self.display).st_mode & 0o777, 0o664)
        self.assertEqual(os.stat(self.config).st_mode & 0o777, 0o640)

    def test_units_carry_bjorn_dir(self):
        self.install()
        with open(os.path.join(self.units, "pisugar-guard.service"), encoding="utf-8") as f:
            unit = f.read()
        self.assertIn(f"Environment=BJORN_DIR={self.bjorn}\n", unit)
        self.assertIn(f"ExecStart=/usr/bin/python3 {self.bjorn}/pisugar.py --guard", unit)

    def test_keys_that_existed_before_survive_uninstall(self):
        config = self.read_config()
        config["pisugar_shutdown_percent"] = 15
        self.write(self.config, json.dumps(config, indent=4))
        self.install()
        self.uninstall()
        config = self.read_config()
        self.assertEqual(config["pisugar_shutdown_percent"], 15)
        self.assertNotIn("pisugar_model", config)
        self.assertNotIn("__title_pisugar__", config)

    def test_a_changed_module_stops_uninstall_unless_forced(self):
        self.install()
        module = os.path.join(self.bjorn, "pisugar.py")
        with open(module, "a", encoding="utf-8") as f:
            f.write("# local change\n")
        patched = sha(self.display)
        self.assertEqual(self.check(), 2)
        with self.assertRaises(installer.Refused):
            self.uninstall()
        self.assertTrue(os.path.exists(module))
        self.assertEqual(sha(self.display), patched)
        self.assertEqual(self.check(force=True), 0)
        self.assertEqual(self.uninstall(force=True), 0)
        self.assertFalse(os.path.exists(module))

    def test_a_changed_display_keeps_its_changes_when_forced(self):
        self.install()
        with open(self.display, encoding="utf-8") as f:
            text = f.read()
        self.write(self.display, "# local change\n" + text)
        self.assertEqual(self.check(), 2)
        self.assertEqual(self.uninstall(force=True), 0)
        with open(self.display, encoding="utf-8") as f:
            self.assertEqual(f.read(), "# local change\n" + STOCK_DISPLAY)

    def test_reinstall_after_a_bjorn_update_never_restores_the_old_version(self):
        self.install()
        updated = "# Bjorn update\n" + STOCK_DISPLAY
        self.write(self.display, updated)          # git checkout + git pull
        self.install()
        self.assertEqual(self.uninstall(), 0)
        with open(self.display, encoding="utf-8") as f:
            self.assertEqual(f.read(), updated)

    def test_install_is_idempotent(self):
        self.install()
        snapshot = sha(self.display), sha(self.config)
        self.install()
        self.assertEqual((sha(self.display), sha(self.config)), snapshot)

    def test_preflight_refuses_an_unexpected_version_before_any_change(self):
        self.write(self.display, STOCK_DISPLAY.replace("int(37 *", "int(38 *"))
        with self.assertRaises(installer.Refused):
            self.run_quietly(installer.preflight, self.bjorn, "narrow")
        self.assertFalse(os.path.exists(self.state))

    def test_a_0_2_0_install_is_converted_and_then_fully_removed(self):
        # What 0.2.0 left behind: a patched display.py, display.py.stock, its
        # keys with a numeric model value, units, and no record.
        self.write(self.display + ".stock", STOCK_DISPLAY)
        self.write(self.display, installer.patch_text(STOCK_DISPLAY, "narrow"))
        config = self.read_config()
        config.update({k: v for k, v in installer.CONFIG_KEYS})
        config["pisugar_model"] = "3"
        config["pisugar_shutdown_percent"] = 12
        self.write(self.config, json.dumps(config, indent=4) + "\n")

        self.install(model=None)
        self.assertFalse(os.path.exists(self.display + ".stock"))
        config = self.read_config()
        self.assertEqual(config["pisugar_model"], "pisugar3")
        self.assertEqual(config["pisugar_shutdown_percent"], 12)

        self.assertEqual(self.uninstall(), 0)
        with open(self.display, encoding="utf-8") as f:
            self.assertEqual(f.read(), STOCK_DISPLAY)
        self.assertFalse(any(k.startswith("pisugar_") for k in self.read_config()))

    def test_a_0_2_0_install_can_be_removed_without_converting(self):
        self.write(self.display + ".stock", STOCK_DISPLAY)
        self.write(self.display, installer.patch_text(STOCK_DISPLAY, "narrow"))
        self.write(os.path.join(self.bjorn, "pisugar.py"), "# 0.2.0\n")
        self.write(os.path.join(self.units, "pisugar-guard.service"),
                   f"[Service]\nExecStart=/usr/bin/python3 {self.bjorn}/pisugar.py --guard\n")
        # No BJORN_DIR given: the directory is read back from the unit.
        self.assertEqual(self.run_quietly(installer.check, "/nonexistent"), 0)
        self.assertEqual(self.run_quietly(installer.uninstall, "/nonexistent"), 0)
        with open(self.display, encoding="utf-8") as f:
            self.assertEqual(f.read(), STOCK_DISPLAY)
        self.assertFalse(os.path.exists(self.display + ".stock"))
        self.assertEqual(os.listdir(self.units), [])

    def test_nothing_installed(self):
        self.assertEqual(self.check(), 4)


if __name__ == "__main__":
    unittest.main()
