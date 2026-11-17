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


class SymlinkSafetyTest(unittest.TestCase):
    """PiSugar/pisugar-doc#5: the installer runs as root in a directory the
    unprivileged bjorn user can write to, so it must never write through a name
    that user could have prepared."""

    VICTIM = "root-owned file, do not touch\n"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.bjorn = os.path.join(self.tmp, "Bjorn")
        os.makedirs(os.path.join(self.bjorn, "config"))
        self.display = os.path.join(self.bjorn, "display.py")
        self.config = os.path.join(self.bjorn, "config", "shared_config.json")
        self.module = os.path.join(self.bjorn, "pisugar.py")
        InstallTest.write(self.display, STOCK_DISPLAY)
        InstallTest.write(self.config, STOCK_CONFIG)
        self.victim = os.path.join(self.tmp, "victim")
        InstallTest.write(self.victim, self.VICTIM)
        units = os.path.join(self.tmp, "units")
        os.makedirs(units)
        for name, value in (("STATE_DIR", os.path.join(self.tmp, "state")), ("UNIT_DIR", units)):
            patcher = mock.patch.object(installer, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def assertVictimUntouched(self):
        with open(self.victim, encoding="utf-8") as f:
            self.assertEqual(f.read(), self.VICTIM)

    def quietly(self, fn, *args, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return fn(*args, **kwargs)

    def test_links_at_the_old_temporary_names_are_never_followed(self):
        # 0.3.0 wrote every file through "<file>.pisugar-tmp": the reported attack.
        planted = [p + ".pisugar-tmp" for p in (self.display, self.config, self.module)]
        for link in planted:
            os.symlink(self.victim, link)
        self.quietly(installer.install, self.bjorn, ROOT, "narrow", "pisugar3")
        self.assertVictimUntouched()
        for link in planted:
            self.assertTrue(os.path.islink(link), "a name the installer did not create was touched")
        with open(self.display, encoding="utf-8") as f:
            self.assertIn(installer.MARKER, f.read())
        self.assertEqual(self.quietly(installer.uninstall, self.bjorn), 0)
        self.assertVictimUntouched()

    def test_a_link_at_the_chosen_temporary_name_is_not_followed(self):
        # Make the "random" name known in advance and plant a link at it.
        os.symlink(self.victim, os.path.join(self.bjorn, ".display.py.aaaa.pisugar-tmp"))
        with mock.patch.object(installer.secrets, "token_hex", side_effect=["aaaa", "bbbb"]):
            installer.write_file(self.display, "new content")
        self.assertVictimUntouched()
        with open(self.display, encoding="utf-8") as f:
            self.assertEqual(f.read(), "new content")
        self.assertTrue(os.path.islink(os.path.join(self.bjorn, ".display.py.aaaa.pisugar-tmp")))

    def test_a_destination_that_is_a_link_is_refused(self):
        os.remove(self.display)
        os.symlink(self.victim, self.display)
        with self.assertRaisesRegex(installer.Refused, "symbolic link"):
            installer.write_file(self.display, "new content")
        self.assertVictimUntouched()
        self.assertTrue(os.path.islink(self.display))

    def test_a_directory_that_is_a_link_is_refused(self):
        elsewhere = os.path.join(self.tmp, "elsewhere")
        os.makedirs(elsewhere)
        linked = os.path.join(self.tmp, "linked")
        os.symlink(elsewhere, linked)
        with self.assertRaisesRegex(installer.Refused, "symbolic link"):
            installer.write_file(os.path.join(linked, "pisugar.py"), "x")
        self.assertEqual(os.listdir(elsewhere), [])

    def test_preflight_refuses_links_before_anything_changes(self):
        copy = os.path.join(self.tmp, "display-copy.py")
        InstallTest.write(copy, STOCK_DISPLAY)
        os.remove(self.display)
        os.symlink(copy, self.display)
        with self.assertRaisesRegex(installer.Refused, "symbolic link"):
            self.quietly(installer.preflight, self.bjorn, "narrow")
        self.assertFalse(os.path.exists(installer.STATE_DIR))

    def test_a_failed_write_removes_only_its_own_temporary_file(self):
        not_ours = self.display + ".pisugar-tmp"
        InstallTest.write(not_ours, "someone else's file\n")
        with mock.patch.object(installer.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                installer.write_file(self.display, "new content")
        with open(not_ours, encoding="utf-8") as f:
            self.assertEqual(f.read(), "someone else's file\n")
        leftovers = [n for n in os.listdir(self.bjorn)
                     if n.endswith(".pisugar-tmp") and n != os.path.basename(not_ours)]
        self.assertEqual(leftovers, [])
        with open(self.display, encoding="utf-8") as f:
            self.assertEqual(f.read(), STOCK_DISPLAY)

    def test_mode_is_applied_to_a_new_file(self):
        target = os.path.join(self.bjorn, "new-file")
        installer.write_file(target, "x", mode=0o640)
        self.assertEqual(os.stat(target).st_mode & 0o777, 0o640)


if __name__ == "__main__":
    unittest.main()
