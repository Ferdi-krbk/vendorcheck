# vendorcheck

[![CI](https://github.com/Ferdi-krbk/vendorcheck/actions/workflows/ci.yml/badge.svg)](https://github.com/Ferdi-krbk/vendorcheck/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

**Is the Python library bundled inside your app, add-on or extension byte-for-byte the official PyPI release?**

Point it at a folder or an archive (`.zip`, `.xpi`, `.vsix`, `.fda`, `.whl`, any app bundle that is a zip). It finds the embedded Python packages, downloads the official release from PyPI, verifies the download against PyPI's published SHA-256, and compares **file by file**.

```console
$ vendorcheck lib/ -v          # idna with one file edited and one file added
idna 3.7: 8 files identical, 1 modified, 1 extra.
    [modified] idna/core.py
    [extra] idna/evil.py
```

Real run on a clean `pip install --target` of yt-dlp:

```console
$ vendorcheck lib/
yt_dlp 2026.8.19: 1049 files identical, 0 modified, 0 extra.
```

## Why

Software supply-chain attacks often hide in *copies* of popular libraries: a vendored package that was quietly patched, or has an extra file dropped in. Lockfiles, `pip-audit` and signature checks look at what PyPI serves, not at the copy shipped inside your artifact. `vendorcheck` closes that gap.

## Install

```bash
pip install git+https://github.com/Ferdi-krbk/vendorcheck
# or just download vendorcheck.py - it is a single file, stdlib only, Python 3.9+
```

## Usage

```bash
vendorcheck PATH [-v] [--json] [--lang en|tr] [--only NAME ...] [--cache DIR] [--allow-empty]
```

| Option | Meaning |
|---|---|
| `-v` | list every modified / extra / missing file |
| `--json` | machine-readable output |
| `--only NAME ...` | check just these packages |
| `--cache DIR` | reuse downloaded official releases |
| `--allow-empty` | exit 0 if no embedded package is found (default: exit 2, so CI can't pass by checking nothing) |
| `--lang tr` | Turkish output |

**Exit codes:** `0` identical · `1` differences found · `2` error / nothing checked.

## GitHub Action

```yaml
- uses: actions/checkout@v4
- uses: Ferdi-krbk/vendorcheck@v1
  with:
    path: build/addon      # folder or .xpi/.vsix/.zip
    # only: "yt-dlp requests"
```

The job fails when embedded code differs from the official release. The repo's own [CI](.github/workflows/ci.yml) proves it: a clean vendored tree must pass and a tampered one must fail.

## How it works

1. **Detect** packages through `*.dist-info/METADATA`, `*.egg-info/PKG-INFO`, or `<pkg>/version.py` (`__version__ = "..."`).
2. **Fetch** `https://pypi.org/pypi/<name>/<version>/json`, prefer a pure `py3-none-any` wheel, fall back to the sdist, and verify its SHA-256 against PyPI's digest.
3. **Compare** each file's SHA-256. Line-ending-only differences (CRLF vs LF) are tolerated; `__pycache__` and `.pyc` are ignored.
4. **Report** identical / modified / extra (and missing) files.

Archives are extracted with zip-slip protection; nothing from the audited artifact is ever executed.

## Limits (v1)

- Only packages with version metadata (see above). Guessing name/version for bare copies is out of scope.
- If PyPI only has platform wheels or an sdist, generated or compiled files show up as "extra".
- Nested archives (a zip inside a zip) are not opened.
- It proves your copy equals what PyPI serves, not that the PyPI release itself is trustworthy. Pair it with `pip-audit` and provenance tools like `trustcheck`.

## Related

- [trustcheck](https://pypi.org/project/trustcheck/) verifies provenance/signatures of PyPI releases, not embedded code.
- [vendoring](https://pypi.org/project/vendoring/) automates *creating* vendored trees; `vendorcheck` *audits* them.

## License

MIT
