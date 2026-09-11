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
```

Say which PiSugar and which panel you have. [COMPATIBILITY.md](COMPATIBILITY.md) lists
what is confirmed; most rows are still unticked and a single report is enough to change
one.

## The display patch

`install.sh` rewrites four exact snippets of upstream `display.py`. If upstream changes
those lines the installer aborts instead of half-patching, which is the intended
behaviour. A fix means updating the snippets in `install.sh` and saying in the PR which
Bjorn commit you matched against.

## Adding a setting

Settings are plain keys in Bjorn's `shared_config.json`. To add one:

1. Add it to `DEFAULTS` in `pisugar.py`.
2. Add it to `additions` in `install.sh` and to `keys` in `uninstall.sh`.
3. Document it in the README table.

Bjorn's config page renders it automatically — booleans as switches, numbers as fields.
Do not hand-write HTML for it.

## Scope

This stays an add-on. It does not vendor Bjorn, and it does not grow into a fork. If a
change would be better upstream, send it to
[infinition/Bjorn](https://github.com/infinition/Bjorn) instead.
