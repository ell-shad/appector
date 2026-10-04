# Contributing

Appector's source repository is currently private. This guide describes the
expected workflow for authorized contributors.

## Development setup

Use a Debian-based development machine or disposable VM with Python 3,
PyGObject, GTK 4, libadwaita, and GdkPixbuf introspection packages installed.
Run the app with `python3 run.py`.

## Before submitting changes

Run the automated tests and packaging checks:

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q appector
sh -n scripts/build-deb.sh
./scripts/build-deb.sh
lintian --pedantic dist/appector_*_all.deb
```

Do not run destructive package operations on a daily-use machine. Use a
disposable VM for installation/removal integration tests. Do not commit
credentials, local logs, installed-app exports, build products, or user data.

## Releases

The app version in `appector/__init__.py` is the source version. Use a matching
`v<version>` tag only after release approval and after the public release
repository, publishing credential, maintainer contact, and release-integrity
policy have been configured.
