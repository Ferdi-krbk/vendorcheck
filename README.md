# vendorcheck

Is the library embedded in your app/extension identical to the official PyPI release?

```
python vendorcheck.py my_addon.xpi -v
yt_dlp 2026.8.19: 1049 dosya aynı, 1 değişmiş, 2 fazladan.
    [değişmiş] yt_dlp/extractor/youtube.py
    [fazladan] yt_dlp/evil.py
```

- Input: directory or zip-like archive (`.zip .fda .xpi .vsix .whl`).
- Finds packages via `*.dist-info/METADATA`, `*.egg-info/PKG-INFO`, or `<pkg>/version.py` (v1 scope).
- Downloads the official pure wheel (else sdist) from PyPI, verifies its SHA-256 against PyPI's digest, then compares file by file (CRLF/LF differences tolerated; `__pycache__`/`.pyc` ignored).
- Exit code: `0` identical, `1` differences, `2` error. Options: `--json`, `-v`, `--only NAME`, `--cache DIR`.
- Stdlib only, Python 3.9+.

## GitHub Action

```yaml
- uses: actions/checkout@v4
- uses: <owner>/vendorcheck@v1
  with:
    path: build/addon
```

## Limits

- sdist fallback lacks generated files, so they appear as "fazladan" (extra).
- Packages without version metadata are not detected.
- This checks the code matches PyPI's artifact; it does not prove PyPI's artifact itself is benign.
