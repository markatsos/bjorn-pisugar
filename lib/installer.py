"""File-level install and uninstall steps for bjorn-pisugar.

install.sh and uninstall.sh install packages, enable I2C and drive systemctl.
Everything that has to be undone exactly is done here instead, and recorded:

  pisugar.py      copied into Bjorn's directory
  display.py      patched; the original is kept outside Bjorn's tree
  config keys     added to shared_config.json - only the ones that were missing
  systemd units   written to /etc/systemd/system

The record is /var/lib/bjorn-pisugar/install.json, next to the original
display.py. Uninstalling reverts what the record lists and nothing else, and it
refuses to delete or overwrite a file that changed after installation unless it
is forced to.

Files are rewritten by writing a temporary copy and renaming it over the
original, with the original's owner, group and mode, so nothing about a file
changes except its content. This runs as root inside Bjorn's directory, which
the unprivileged bjorn user can write to, so no name in that directory is
trusted: see write_file().

Run by the shell scripts as root; not meant to be imported by Bjorn.
"""

import argparse
import ast
import contextlib
import datetime
import glob
import hashlib
import json
import os
import secrets
import shutil
import stat
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pisugar  # noqa: E402  (needs ROOT on the path)

STATE_DIR = os.environ.get("BJORN_PISUGAR_STATE_DIR", "/var/lib/bjorn-pisugar")
UNIT_DIR = os.environ.get("BJORN_PISUGAR_UNIT_DIR", "/etc/systemd/system")
UNITS = ("pisugar-poweroff.service", "pisugar-guard.service", "pisugar-guard.timer")
RECORD_FORMAT = 1

# Changing the order or content of these keys changes what users see on the
# Config page; the title key makes Bjorn draw a "PiSugar" heading above them.
CONFIG_KEYS = [("__title_pisugar__", "PiSugar")] + list(pisugar.DEFAULTS.items())


# --------------------------------------------------------------------------- #
# The display.py patch                                                         #
# --------------------------------------------------------------------------- #

MARKER = "from pisugar import PiSugar, draw_battery"

COMMON = [
    ("import subprocess  \n",
     "import subprocess  \n" + MARKER + "\n"),
    ("        self.web_screen_reversed = self.shared_data.web_screen_reversed\n",
     "        self.web_screen_reversed = self.shared_data.web_screen_reversed\n"
     "        self.pisugar = PiSugar()\n"),
]

# On a 122px panel the header is full: shift the logo left and let the two
# network icons share one slot, otherwise the indicator has nowhere to go.
NARROW = [
    ('draw.text((int(37 * self.scale_factor_x), int(5 * self.scale_factor_y)), "BJORN"',
     'draw.text((int(23 * self.scale_factor_x), int(5 * self.scale_factor_y)), "BJORN"'),
    ("                if self.shared_data.pan_connected:\n"
     "                    image.paste(self.shared_data.connected, (int(104 * self.scale_factor_x), int(3 * self.scale_factor_y)))\n"
     "                if self.shared_data.usb_active:\n"
     "                    image.paste(self.shared_data.usb, (int(90 * self.scale_factor_x), int(4 * self.scale_factor_y)))\n",
     "                if self.shared_data.pan_connected:\n"
     "                    image.paste(self.shared_data.connected, (int(76 * self.scale_factor_x), int(3 * self.scale_factor_y)))\n"
     "                elif self.shared_data.usb_active:\n"
     "                    image.paste(self.shared_data.usb, (int(76 * self.scale_factor_x), int(4 * self.scale_factor_y)))\n"
     "                draw_battery(image, draw, self.shared_data, self.pisugar, self.scale_factor_x, self.scale_factor_y)\n"),
]

# A 2.7" panel has room to spare, so it keeps the stock layout.
WIDE = [
    ("                if self.shared_data.usb_active:\n"
     "                    image.paste(self.shared_data.usb, (int(90 * self.scale_factor_x), int(4 * self.scale_factor_y)))\n",
     "                if self.shared_data.usb_active:\n"
     "                    image.paste(self.shared_data.usb, (int(90 * self.scale_factor_x), int(4 * self.scale_factor_y)))\n"
     "                draw_battery(image, draw, self.shared_data, self.pisugar, self.scale_factor_x, self.scale_factor_y)\n"),
]

LAYOUTS = {"narrow": COMMON + NARROW, "wide": COMMON + WIDE}


class Refused(Exception):
    """A step would not be safe. Nothing was changed by it."""


def patch_text(text, layout):
    """display.py with the indicator patched in. Refused on an unexpected version."""
    for old, _new in LAYOUTS[layout]:
        found = text.count(old)
        if found != 1:
            snippet = old.strip().splitlines()[0][:60]
            raise Refused(f"display.py is not the version this add-on was written for: "
                          f"expected one match for {snippet!r}, found {found}")
    for old, new in LAYOUTS[layout]:
        text = text.replace(old, new, 1)
    ast.parse(text)
    return text


def unpatch_text(text):
    """display.py with exactly the add-on's lines taken out, and their layout."""
    for layout, edits in LAYOUTS.items():
        if all(text.count(new) == 1 for _old, new in edits):
            for old, new in reversed(edits):
                text = text.replace(new, old, 1)
            ast.parse(text)
            return text, layout
    raise Refused("display.py contains PiSugar lines that this version does not recognise")


# --------------------------------------------------------------------------- #
# Files                                                                        #
# --------------------------------------------------------------------------- #

def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path):
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except FileNotFoundError:
        return None


def read_text(path):
    with open(path, "rb") as f:
        return f.read().decode("utf-8")


def write_file(path, text, mode=None, owner=None):
    """Write `text` to `path` atomically, without following symbolic links.

    install.sh runs this as root, and Bjorn's directory belongs to the
    unprivileged bjorn user, who could have prepared any name in it - for
    example a symbolic link to /etc/shadow where a temporary file is expected.
    So nothing here goes through a name that someone else controls:

    - the directory is opened once, refusing a symbolic link, and every step
      below works relative to that handle;
    - a destination that is a symbolic link, or not a regular file, is refused;
    - the temporary file gets an unpredictable name and is created with
      O_CREAT | O_EXCL | O_NOFOLLOW, so anything already at that name makes
      the creation fail instead of being written through;
    - mode and owner are set on the open descriptor, and the data is fsynced,
      before the file is renamed over the destination;
    - on failure, only the temporary file this call created is removed.

    `mode` and `owner` ((uid, gid)) default to those of the file being replaced,
    so rewriting display.py or the config changes nothing but the content. A new
    file defaults to 0644, owned by whoever runs this.
    """
    directory, name = os.path.split(os.path.abspath(path))
    try:
        dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as e:
        if os.path.islink(directory):
            raise Refused(f"{directory} is a symbolic link - refusing to write into it") from e
        raise
    try:
        try:
            current = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            current = None
        if current is not None:
            if stat.S_ISLNK(current.st_mode):
                raise Refused(f"{path} is a symbolic link - refusing to replace it")
            if not stat.S_ISREG(current.st_mode):
                raise Refused(f"{path} is not a regular file - refusing to replace it")
            mode = stat.S_IMODE(current.st_mode) if mode is None else mode
            owner = (current.st_uid, current.st_gid) if owner is None else owner
        mode = 0o644 if mode is None else mode

        tmp, fd = _create_temporary(dir_fd, name)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(text.encode("utf-8"))
                f.flush()
                os.fchmod(f.fileno(), mode)
                if owner is not None:
                    st = os.fstat(f.fileno())
                    if (st.st_uid, st.st_gid) != owner:
                        os.fchown(f.fileno(), *owner)
                os.fsync(f.fileno())
            os.replace(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp, dir_fd=dir_fd)
            raise
        os.fsync(dir_fd)                # make the rename itself durable
    finally:
        os.close(dir_fd)


def _create_temporary(dir_fd, name):
    """A new, empty file next to `name`, created by this call and nobody else.

    O_EXCL fails on any existing entry, a symbolic link included, and
    O_NOFOLLOW makes sure a link is never followed. Mode 0600 until the content
    is complete.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    for _ in range(100):
        tmp = f".{name}.{secrets.token_hex(8)}.pisugar-tmp"
        try:
            return tmp, os.open(tmp, flags, 0o600, dir_fd=dir_fd)
        except FileExistsError:
            continue
    raise Refused(f"could not create a temporary file next to {name}")


def regular_file_or_absent(path):
    """Refuse a path that is a symbolic link or anything but a regular file."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(st.st_mode):
        raise Refused(f"{path} is a symbolic link - point BJORN_DIR at the real files")
    if not stat.S_ISREG(st.st_mode):
        raise Refused(f"{path} is not a regular file")


def real_directory(path):
    """Refuse a directory that is a symbolic link, or missing."""
    if os.path.islink(path.rstrip("/") or "/"):
        raise Refused(f"{path} is a symbolic link - point BJORN_DIR at the real directory")
    if not os.path.isdir(path):
        raise Refused(f"{path} is not a directory")


def read_config(path):
    try:
        config = json.loads(read_text(path))
    except (OSError, ValueError) as e:
        raise Refused(f"cannot read {path}: {e}")
    if not isinstance(config, dict):
        raise Refused(f"{path} is not a JSON object")
    return config


def write_config(path, config):
    # Same formatting as Bjorn's own save_config(). The file shipped in Bjorn's
    # repository ends with a newline and the ones Bjorn writes do not; keep
    # whichever this one has, so an install and uninstall leave it byte-identical.
    text = json.dumps(config, indent=4)
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        if f.tell() > 0:
            f.seek(-1, os.SEEK_END)
            if f.read(1) == b"\n":
                text += "\n"
    write_file(path, text)


# --------------------------------------------------------------------------- #
# The install record                                                           #
# --------------------------------------------------------------------------- #

def record_path():
    return os.path.join(STATE_DIR, "install.json")


def original_path():
    return os.path.join(STATE_DIR, "display.py.orig")


def load_record():
    try:
        record = json.loads(read_text(record_path()))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        raise Refused(f"cannot read the install record {record_path()}: {e}")
    if record.get("format") != RECORD_FORMAT:
        raise Refused(f"{record_path()} was written by a newer version of this add-on")
    return record


def save_record(record):
    os.makedirs(STATE_DIR, mode=0o755, exist_ok=True)
    record["updated"] = _now()
    write_file(record_path(), json.dumps(record, indent=2) + "\n")


def new_record(bjorn_dir):
    return {
        "format": RECORD_FORMAT,
        "version": pisugar.VERSION,
        "bjorn_dir": bjorn_dir,
        "installed": _now(),
        "files": {},
        "display": None,
        "config": {"path": config_path(bjorn_dir), "keys_added": []},
        "units": {},
        "packages": [],
        "i2c_enabled": False,
    }


def _now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()


def display_path(bjorn_dir):
    return os.path.join(bjorn_dir, "display.py")


def config_path(bjorn_dir):
    return os.path.join(bjorn_dir, "config", "shared_config.json")


# --------------------------------------------------------------------------- #
# Output                                                                       #
# --------------------------------------------------------------------------- #

_COLOUR = sys.stdout.isatty()


def say(kind, message):
    colours = {"ok": "\033[0;32m", "skip": "\033[1;33m", "warn": "\033[1;33m"}
    label = f"{kind:<4}"
    if _COLOUR and kind in colours:
        label = f"{colours[kind]}{label}\033[0m"
    print(f"  {label} {message}")


# --------------------------------------------------------------------------- #
# Install                                                                      #
# --------------------------------------------------------------------------- #

def preflight(bjorn_dir, layout):
    """Check that install() can run, without changing anything."""
    record = load_record()
    if record and record["bjorn_dir"] != bjorn_dir:
        raise Refused(f"already installed into {record['bjorn_dir']} - "
                      "run uninstall.sh first, or point BJORN_DIR there")
    # write_file() refuses links; finding one here stops the install before
    # anything has changed rather than halfway through.
    real_directory(bjorn_dir)
    real_directory(os.path.dirname(config_path(bjorn_dir)))
    for path in (display_path(bjorn_dir), config_path(bjorn_dir),
                 os.path.join(bjorn_dir, "pisugar.py")):
        regular_file_or_absent(path)
    read_config(config_path(bjorn_dir))

    text = read_text(display_path(bjorn_dir))
    if pisugar_lines_in(text):
        if record and record.get("display"):
            if sha256_text(text) == record["display"]["patched_sha256"]:
                say("ok", "display.py: patched by this add-on, unchanged since")
            else:
                say("warn", "display.py: patched, but changed since installation - it will be left as it is")
        else:
            unpatch_text(text)          # raises if it cannot be undone
            say("ok", "display.py: patched by 0.2.0 - will be converted")
    else:
        patch_text(text, layout)        # raises on an unexpected version
        say("ok", f"display.py: matches the expected Bjorn version - ready to patch ({layout} panel)")


def pisugar_lines_in(text):
    return MARKER in text


def install(bjorn_dir, src_dir, layout, model, packages=(), i2c_enabled=False):
    record = load_record()
    if record and record["bjorn_dir"] != bjorn_dir:
        raise Refused(f"already installed into {record['bjorn_dir']} - run uninstall.sh first")
    record = record or new_record(bjorn_dir)
    record["version"] = pisugar.VERSION
    display = display_path(bjorn_dir)
    text = read_text(display)

    # A 0.2.0 install left the patch in place but kept no record. Take the patch
    # out first, so the original is known and the rest is a normal install.
    converted = False
    if pisugar_lines_in(text) and not record.get("display"):
        text, _ = unpatch_text(text)
        write_file(display, text)
        record["converted_from"] = "0.2.0"
        converted = True
        say("ok", "display.py: took out the 0.2.0 patch")
        legacy = display + ".stock"
        if os.path.exists(legacy):
            if read_text(legacy) == text:
                os.remove(legacy)
                say("ok", "removed display.py.stock left by 0.2.0 (identical to the original)")
            else:
                say("warn", "display.py.stock left by 0.2.0 does not match the current display.py")
                say("warn", "(Bjorn was probably updated since) - left in place, delete it if unwanted")
        save_record(record)

    # The module.
    module = os.path.join(bjorn_dir, "pisugar.py")
    bjorn = os.stat(bjorn_dir)
    write_file(module, read_text(os.path.join(src_dir, "pisugar.py")),
               mode=0o644, owner=(bjorn.st_uid, bjorn.st_gid))
    record["files"][module] = sha256_file(module)
    save_record(record)
    say("ok", f"pisugar.py -> {module}")

    # display.py.
    if pisugar_lines_in(text):
        if sha256_text(text) == record["display"]["patched_sha256"]:
            say("skip", "display.py already patched")
        else:
            say("warn", "display.py was changed after installation - left as it is")
    else:
        patched = patch_text(text, layout)
        os.makedirs(STATE_DIR, mode=0o755, exist_ok=True)
        # Always the current original: after a Bjorn update this replaces the
        # copy of the old version, which must never be restored over the new one.
        write_file(original_path(), text, mode=0o644)
        write_file(display, patched)
        record["display"] = {
            "path": display,
            "layout": layout,
            "original_sha256": sha256_text(text),
            "patched_sha256": sha256_text(patched),
        }
        save_record(record)
        say("ok", f"display.py patched for a {layout} panel (original kept in {original_path()})")

    # Config keys - only the missing ones are added, and only those are recorded.
    cfg_file = config_path(bjorn_dir)
    config = read_config(cfg_file)
    recorded = record["config"]["keys_added"]
    added, adopted = [], []
    for key, value in CONFIG_KEYS:
        if key not in config:
            config[key] = value
            added.append(key)
        elif converted and key not in recorded:
            adopted.append(key)         # added by 0.2.0, which kept no record
    recorded.extend(k for k in added + adopted if k not in recorded)
    before = config.get("pisugar_model")
    after = model or pisugar.canonical_model(before) or before
    if after != before:
        config["pisugar_model"] = after
    if added or after != before:
        write_config(cfg_file, config)
    save_record(record)
    visible = lambda keys: ", ".join(k for k in keys if not k.startswith("__"))  # noqa: E731
    if added:
        say("ok", "config keys added: " + visible(added))
    if adopted:
        say("ok", "config keys from 0.2.0 taken over: " + visible(adopted))
    if not added and not adopted:
        say("skip", "config keys already present")
    say("ok", f"pisugar_model = {config['pisugar_model']}")

    # systemd units.
    for unit in UNITS:
        content = read_text(os.path.join(src_dir, "systemd", unit)).replace("@BJORN_DIR@", bjorn_dir)
        path = os.path.join(UNIT_DIR, unit)
        write_file(path, content, mode=0o644, owner=(0, 0) if os.geteuid() == 0 else None)
        record["units"][path] = sha256_text(content)
    say("ok", f"units written to {UNIT_DIR}")

    record["packages"] = sorted(set(record["packages"]) | set(packages))
    record["i2c_enabled"] = bool(record["i2c_enabled"] or i2c_enabled)
    save_record(record)
    say("ok", f"install record: {record_path()}")


# --------------------------------------------------------------------------- #
# Uninstall                                                                    #
# --------------------------------------------------------------------------- #

def changed_since_install(record):
    """Files the add-on installed that have been modified since."""
    changed = []
    for path, digest in list(record["files"].items()) + list(record["units"].items()):
        current = sha256_file(path)
        if current is not None and current != digest:
            changed.append(path)
    display = record.get("display")
    if display:
        text = read_text(display["path"])
        if pisugar_lines_in(text) and sha256_text(text) != display["patched_sha256"]:
            changed.append(display["path"])
    return changed


def check(bjorn_dir, force=False):
    """0 if uninstall can go ahead, 2 if changed files stop it, 4 if nothing is installed."""
    record = load_record()
    if record is None:
        bjorn_dir = legacy_bjorn_dir(bjorn_dir)
        if not legacy_install_present(bjorn_dir):
            say("skip", "nothing installed - no record in " + STATE_DIR)
            return 4
        display = display_path(bjorn_dir)
        if not os.path.exists(display):
            raise Refused(f"found a 0.2.0 install, but no Bjorn at {bjorn_dir} - "
                          "set BJORN_DIR to where Bjorn is")
        text = read_text(display)
        if pisugar_lines_in(text):
            unpatch_text(text)          # raises if the lines cannot be taken out
        read_config(config_path(bjorn_dir))
        say("ok", f"a 0.2.0 install in {bjorn_dir} with no record - "
                  "removing what 0.2.0 itself installed")
        return 0
    changed = changed_since_install(record)
    for path in changed:
        say("warn", f"{path} was changed after installation")
    if changed and not force:
        say("warn", "re-run with --force to remove the add-on's files and lines anyway")
        return 2
    say("ok", f"install record for {record['bjorn_dir']} ({record['version']})")
    return 0


def uninstall(bjorn_dir, force=False):
    record = load_record()
    if record is None:
        return uninstall_legacy(legacy_bjorn_dir(bjorn_dir))
    changed = changed_since_install(record)
    if changed and not force:
        raise Refused("files changed after installation: " + ", ".join(changed))
    status = 0

    for path in record["units"]:
        if os.path.exists(path):
            os.remove(path)
    say("ok", "units removed")

    for path in record["files"]:
        if os.path.exists(path):
            os.remove(path)
        for cached in glob.glob(os.path.join(os.path.dirname(path), "__pycache__", "pisugar.*.pyc")):
            os.remove(cached)
    say("ok", "pisugar.py removed")

    display = record.get("display")
    if display:
        status = max(status, restore_display(display))

    cfg_file = record["config"]["path"]
    try:
        config = read_config(cfg_file)
    except Refused as e:
        say("warn", f"{e} - config keys left in place")
        status = 1
    else:
        removed = [k for k in record["config"]["keys_added"] if k in config]
        for key in removed:
            del config[key]
        if not removed:
            say("skip", "no config keys to remove")
        else:
            try:
                write_config(cfg_file, config)
            except Refused as e:
                say("warn", f"{e} - config keys left in place")
                status = 1
            else:
                say("ok", "config keys removed: " + ", ".join(k for k in removed if not k.startswith("__")))

    report_leftovers(record)
    if status == 0:
        shutil.rmtree(STATE_DIR, ignore_errors=True)
    else:
        say("warn", f"install record kept in {STATE_DIR}")
    return status


def restore_display(display):
    path = display["path"]
    text = read_text(path)
    if not pisugar_lines_in(text):
        say("skip", "display.py no longer contains the patch - left as it is")
        return 0
    original = original_path()
    try:
        if (sha256_text(text) == display["patched_sha256"]
                and sha256_file(original) == display["original_sha256"]):
            write_file(path, read_text(original))
            say("ok", "display.py restored to the original")
            return 0
        stock, _ = unpatch_text(text)
        write_file(path, stock)
    except Refused as e:
        say("warn", f"{e} - display.py left as it is; remove the PiSugar lines by hand")
        return 1
    if sha256_text(stock) == display["original_sha256"]:
        say("ok", "display.py restored to the original")
    else:
        say("ok", "PiSugar lines removed from display.py; changes made since installation kept")
    return 0


def report_leftovers(record):
    apt = [p for p in record["packages"] if not p.startswith("pip:")]
    pip = [p[4:] for p in record["packages"] if p.startswith("pip:")]
    if apt:
        say("note", "packages installed by install.sh are left in place, in case anything else uses them:")
        say("", "sudo apt remove " + " ".join(apt))
    if pip:
        say("note", "pip packages installed by install.sh are left in place:")
        say("", "sudo pip3 uninstall --break-system-packages " + " ".join(pip))
    if record["i2c_enabled"]:
        say("note", "I2C was enabled by install.sh and is still on. To turn it off:")
        say("", "sudo raspi-config nonint do_i2c 1")


def legacy_bjorn_dir(fallback):
    """Where an install without a record put the module, read back from its units."""
    for unit in UNITS:
        try:
            lines = read_text(os.path.join(UNIT_DIR, unit)).splitlines()
        except FileNotFoundError:
            continue
        for line in lines:
            if line.startswith("ExecStart="):
                for word in line.split():
                    if word.endswith("/pisugar.py"):
                        return os.path.dirname(word)
    return fallback


def legacy_install_present(bjorn_dir):
    display = display_path(bjorn_dir)
    return (os.path.exists(os.path.join(bjorn_dir, "pisugar.py"))
            or (os.path.exists(display) and pisugar_lines_in(read_text(display)))
            or any(os.path.exists(os.path.join(UNIT_DIR, u)) for u in UNITS))


def uninstall_legacy(bjorn_dir):
    """A 0.2.0 install kept no record, so remove what 0.2.0 itself would have."""
    if not legacy_install_present(bjorn_dir):
        say("skip", "nothing installed")
        return 0
    status = 0
    for unit in UNITS:
        path = os.path.join(UNIT_DIR, unit)
        if os.path.exists(path):
            os.remove(path)
    say("ok", "units removed")
    module = os.path.join(bjorn_dir, "pisugar.py")
    if os.path.exists(module):
        os.remove(module)
    for cached in glob.glob(os.path.join(bjorn_dir, "__pycache__", "pisugar.*.pyc")):
        os.remove(cached)
    say("ok", "pisugar.py removed")

    display = display_path(bjorn_dir)
    text = read_text(display) if os.path.exists(display) else ""
    if pisugar_lines_in(text):
        try:
            stock, _ = unpatch_text(text)
        except Refused as e:
            say("warn", f"{e} - display.py left as it is")
            status = 1
        else:
            write_file(display, stock)
            say("ok", "PiSugar lines removed from display.py")
            legacy = display + ".stock"
            if os.path.exists(legacy) and read_text(legacy) == stock:
                os.remove(legacy)
                say("ok", "removed display.py.stock (identical to the restored file)")
            elif os.path.exists(legacy):
                say("warn", "display.py.stock does not match the restored display.py - left in place")

    cfg_file = config_path(bjorn_dir)
    try:
        config = read_config(cfg_file)
    except Refused as e:
        say("warn", f"{e} - config keys left in place")
        return 1
    # Without a record there is no telling which keys existed before 0.2.0, so
    # this removes all of the add-on's own keys, as 0.2.0's uninstaller did.
    removed = [k for k, _ in CONFIG_KEYS if k in config]
    for key in removed:
        del config[key]
    if removed:
        write_config(cfg_file, config)
        say("ok", "config keys removed: " + ", ".join(k for k in removed if not k.startswith("__")))
    return status


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #

def _main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("preflight")
    p.add_argument("--bjorn-dir", required=True)
    p.add_argument("--layout", choices=sorted(LAYOUTS), required=True)

    p = sub.add_parser("install")
    p.add_argument("--bjorn-dir", required=True)
    p.add_argument("--src", default=ROOT)
    p.add_argument("--layout", choices=sorted(LAYOUTS), required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--package", action="append", default=[])
    p.add_argument("--i2c-enabled", action="store_true")

    for name in ("check", "uninstall"):
        p = sub.add_parser(name)
        p.add_argument("--bjorn-dir", required=True)
        p.add_argument("--force", action="store_true")

    p = sub.add_parser("model", help="print the canonical name of a pisugar_model value")
    p.add_argument("value")

    p = sub.add_parser("config-model", help="print the pisugar_model currently in Bjorn's config")
    p.add_argument("--bjorn-dir", required=True)

    args = parser.parse_args(argv)
    try:
        if args.command == "preflight":
            preflight(args.bjorn_dir, args.layout)
            return 0
        if args.command == "install":
            model = pisugar.canonical_model(args.model)
            if model is None:
                raise Refused(f"unknown model {args.model!r}")
            install(args.bjorn_dir, args.src, args.layout, model, args.package, args.i2c_enabled)
            return 0
        if args.command == "check":
            return check(args.bjorn_dir, args.force)
        if args.command == "uninstall":
            return uninstall(args.bjorn_dir, args.force)
        if args.command == "model":
            model = pisugar.canonical_model(args.value)
            if model is None:
                return 1
            print(model)
            return 0
        if args.command == "config-model":
            try:
                value = read_config(config_path(args.bjorn_dir)).get("pisugar_model", "auto")
            except Refused:
                value = "auto"
            print(pisugar.canonical_model(value) or "auto")
            return 0
    except (Refused, OSError) as e:
        say("warn", str(e))
        return 3
    return 2


if __name__ == "__main__":
    sys.exit(_main())
