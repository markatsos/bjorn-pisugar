# Contributing

Issues and pull requests are welcome. A few things that make them easier to act on.

## Reporting a problem

Include the output of:

```bash
python3 /home/bjorn/Bjorn/pisugar.py
i2cdetect -y 1
cat /home/bjorn/Bjorn/version.txt
uname -m
systemctl status pisugar-guard.timer --no-pager
sudo cat /var/lib/bjorn-pisugar/install.json     # for install or uninstall problems
```

Say which PiSugar and which panel you have. [COMPATIBILITY.md](COMPATIBILITY.md) lists
what is confirmed; most rows are still unticked and a single report is enough to change
one.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

They run on any Linux machine with Python 3, with no hardware and no root. Changes to
`pisugar.py` or `lib/installer.py` should come with a test. Anything that touches the
hardware also needs a run on a real board: say which one in the PR.

## Safety rules

The add-on writes to a battery controller and can shut the Pi down, so a few rules are
not up for negotiation:

- Nothing writes to a register or shuts the Pi down unless `pisugar_model` names a board
  with power control, and that board passes its identity check. Auto-detection is only
  ever used for reading.
- A board whose write path has not been run on real hardware stays read-only.
- Never fall back to default settings for anything that acts on the hardware.
- Every change install makes to Bjorn's files goes through `lib/installer.py` and into
  the install record, so that uninstall can revert exactly that.

## The display patch

`lib/installer.py` rewrites exact snippets of upstream `display.py` (`COMMON`, `NARROW`
and `WIDE`). Uninstalling, or converting a 0.2.0 install, takes them out again by
reversing the same edits. If upstream changes those lines, the preflight check stops the
installer before it changes anything, which is the intended behaviour. A fix means
updating the snippets and saying in the PR which Bjorn commit you matched against.

## Adding a setting

Settings are plain keys in Bjorn's `shared_config.json`. To add one:

1. Add it to `DEFAULTS` in `pisugar.py`. The installer adds every key in `DEFAULTS`
   that is missing from the config, and records it so that uninstall removes it again.
2. Document it in the README table.

Bjorn's config page renders it automatically — booleans as switches, numbers as fields.
Do not hand-write HTML for it. A text value that looks like a number is saved as a
number and then rendered as a number field, so give text settings non-numeric values.

## Scope

This stays an add-on. It does not vendor Bjorn, and it does not grow into a fork. If a
change would be better upstream, send it to
[infinition/Bjorn](https://github.com/infinition/Bjorn) instead.
