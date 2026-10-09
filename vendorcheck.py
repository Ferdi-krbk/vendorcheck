#!/usr/bin/env python3
"""vendorcheck: is the embedded Python library identical to the official PyPI release?

Usage:
    python vendorcheck.py PATH [--json] [-v] [--lang en|tr] [--cache DIR]
                          [--only NAME ...] [--allow-empty]

PATH is a directory or a zip-like archive (.zip, .fda, .xpi, .vsix, .whl, ...).
v1 scope: only packages whose name+version can be read from metadata
(*.dist-info/METADATA, *.egg-info/PKG-INFO) or from <pkg>/version.py.

Exit code: 0 = everything identical, 1 = differences found,
           2 = error or nothing to check.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

__version__ = "0.1.1"
IGNORED_DIRS = {"__pycache__"}
IGNORED_SUFFIXES = (".pyc", ".pyo")
UA = {"User-Agent": f"vendorcheck/{__version__} (+https://github.com/Ferdi-krbk/vendorcheck)"}


# ----------------------------------------------------------------- helpers
def norm_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_extract_zip(zf: zipfile.ZipFile, dest: Path) -> None:
    dest = dest.resolve()
    for info in zf.infolist():
        target = (dest / info.filename).resolve()
        if dest != target and dest not in target.parents:
            raise RuntimeError(f"unsafe path in archive: {info.filename}")
    zf.extractall(dest)


def safe_extract_tar(tf: tarfile.TarFile, dest: Path) -> None:
    dest = dest.resolve()
    members = []
    for m in tf.getmembers():
        target = (dest / m.name).resolve()
        if dest != target and dest not in target.parents:
            raise RuntimeError(f"unsafe path in archive: {m.name}")
        if m.isfile() or m.isdir():  # skip symlinks/devices
            members.append(m)
    tf.extractall(dest, members=members)


def is_ignored(rel: Path) -> bool:
    return any(p in IGNORED_DIRS for p in rel.parts) or rel.name.endswith(IGNORED_SUFFIXES)


def read_text(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace")


# --------------------------------------------------------------- discovery
@dataclass
class Embedded:
    name: str               # distribution name (best guess for version.py case)
    version: str
    container: Path         # directory that holds the package dirs
    tops: list[str]         # top-level package dir names / module names
    source: str             # "dist-info" | "egg-info" | "version.py"


def parse_metadata(text: str) -> tuple[str | None, str | None]:
    name = version = None
    for line in text.splitlines():
        if not line.strip():
            break  # end of headers
        if line.startswith("Name:") and name is None:
            name = line.split(":", 1)[1].strip()
        elif line.startswith("Version:") and version is None:
            version = line.split(":", 1)[1].strip()
    return name, version


def tops_from_info(info_dir: Path, fallback: str) -> list[str]:
    container = info_dir.parent
    tl = info_dir / "top_level.txt"
    cands: list[str] = []
    if tl.is_file():
        cands = [l.strip() for l in read_text(tl).splitlines() if l.strip()]
    elif (info_dir / "RECORD").is_file():
        for line in read_text(info_dir / "RECORD").splitlines():
            first = line.split(",", 1)[0].replace("\\", "/").split("/")[0]
            if first and first not in ("..", ) and not first.endswith((".dist-info", ".data")):
                if first.endswith(".py"):
                    first = first[:-3]
                if first not in cands:
                    cands.append(first)
    if not cands:
        cands = [fallback.replace("-", "_")]
    # keep only those that exist here
    return [c for c in cands if (container / c).is_dir() or (container / f"{c}.py").is_file()]


def discover(root: Path) -> list[Embedded]:
    found: list[Embedded] = []
    covered: set[Path] = set()
    for dirpath, dirnames, _ in os.walk(root):
        d = Path(dirpath)
        for sub in list(dirnames):
            if sub.endswith((".dist-info", ".egg-info")):
                info = d / sub
                meta = info / ("METADATA" if sub.endswith(".dist-info") else "PKG-INFO")
                if not meta.is_file():
                    continue
                name, version = parse_metadata(read_text(meta))
                if not name or not version:
                    continue
                tops = tops_from_info(info, name)
                if not tops:
                    continue
                found.append(Embedded(name, version, d, tops,
                                      "dist-info" if sub.endswith(".dist-info") else "egg-info"))
                for t in tops:
                    covered.add((d / t).resolve())
    # version.py fallback: top-level packages only
    for dirpath, dirnames, filenames in os.walk(root):
        d = Path(dirpath)
        if "version.py" in filenames and "__init__.py" in filenames:
            if (d.parent / "__init__.py").exists() or d.resolve() in covered:
                continue
            if any(c in covered for c in [d.resolve(), *d.resolve().parents]):
                continue
            m = re.search(r"""^__version__\s*=\s*['"]([^'"]+)['"]""",
                          read_text(d / "version.py"), re.M)
            if m:
                found.append(Embedded(d.name.replace("_", "-"), m.group(1), d.parent,
                                      [d.name], "version.py"))
    return found


# -------------------------------------------------------------------- PyPI
def http_get(url: str, attempts: int = 3) -> bytes:
    """GET with retry + exponential backoff on 429/5xx and connection errors."""
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as ex:
            if ex.code not in (429, 500, 502, 503, 504) or i == attempts - 1:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if i == attempts - 1:
                raise
        time.sleep(2 ** i)
    raise RuntimeError("unreachable")


def pick_artifact(urls: list[dict]) -> dict | None:
    wheels = [u for u in urls if u["packagetype"] == "bdist_wheel"
              and u["filename"].endswith("-none-any.whl")]
    if wheels:
        return wheels[0]
    sd = [u for u in urls if u["packagetype"] == "sdist"]
    return sd[0] if sd else None


def _descend_sdist(out: Path, filename: str) -> Path:
    """An sdist unpacks into a single top directory; wheels do not."""
    if filename.endswith((".whl", ".zip")):
        return out
    subs = [p for p in out.iterdir() if p.is_dir()]
    return subs[0] if len(subs) == 1 else out


def fetch_official(name: str, version: str, cache: Path) -> tuple[Path, str]:
    """Download + hash-verify official artifact, return (extracted dir, artifact filename)."""
    meta = json.loads(http_get(f"https://pypi.org/pypi/{norm_name(name)}/{version}/json"))
    art = pick_artifact(meta["urls"])
    if not art:
        raise RuntimeError("no pure wheel or sdist on PyPI")
    out = cache / f"{norm_name(name)}-{version}"
    if (out / ".ok").exists():
        return _descend_sdist(out, art["filename"]), art["filename"]
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    data = http_get(art["url"])
    if sha256_bytes(data) != art["digests"]["sha256"]:
        raise RuntimeError("downloaded artifact sha256 does not match PyPI-published digest")
    if art["filename"].endswith((".whl", ".zip")):
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            safe_extract_zip(zf, out)
    else:
        with tarfile.open(fileobj=io.BytesIO(data)) as tf:
            safe_extract_tar(tf, out)
    (out / ".ok").touch()
    return _descend_sdist(out, art["filename"]), art["filename"]


# ----------------------------------------------------------------- compare
def collect(base: Path, container: Path, tops: list[str]) -> dict[str, Path]:
    files: dict[str, Path] = {}
    for t in tops:
        for root in (container / t, container / f"{t}.py"):
            if root.is_file():
                files[root.relative_to(container).as_posix()] = root
            elif root.is_dir():
                for p in root.rglob("*"):
                    if p.is_file() and not is_ignored(p.relative_to(container)):
                        files[p.relative_to(container).as_posix()] = p
    return files


def same_content(a: Path, b: Path) -> bool:
    if sha256_file(a) == sha256_file(b):
        return True
    # tolerate CRLF/LF differences only (git autocrlf etc.)
    try:
        da, db = a.read_bytes(), b.read_bytes()
    except OSError:
        return False
    if b"\0" in da[:8192] or b"\0" in db[:8192]:
        return False
    return da.replace(b"\r\n", b"\n") == db.replace(b"\r\n", b"\n")


@dataclass
class Result:
    name: str
    version: str
    source: str
    location: str
    status: str = "ok"          # ok | error
    error: str = ""
    artifact: str = ""
    same: int = 0
    modified: list[str] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return self.status == "ok" and not (self.modified or self.extra or self.missing)


def find_official_container(off: Path, tops: list[str]) -> Path:
    for cand in (off, off / "src", off / "lib"):
        if any((cand / t).is_dir() or (cand / f"{t}.py").is_file() for t in tops):
            return cand
    return off


def check_one(e: Embedded, cache: Path, root: Path) -> Result:
    res = Result(e.name, e.version, e.source, e.container.relative_to(root).as_posix() or ".")
    try:
        off, res.artifact = fetch_official(e.name, e.version, cache)
    except urllib.error.HTTPError as ex:
        res.status, res.error = "error", f"PyPI HTTP {ex.code} (not on PyPI under this name/version?)"
        return res
    except Exception as ex:  # noqa: BLE001
        res.status, res.error = "error", str(ex)
        return res
    off_container = find_official_container(off, e.tops)
    mine = collect(root, e.container, e.tops)
    theirs = collect(off, off_container, e.tops)
    for rel, p in sorted(mine.items()):
        if rel not in theirs:
            res.extra.append(rel)
        elif same_content(p, theirs[rel]):
            res.same += 1
        else:
            res.modified.append(rel)
    res.missing = sorted(set(theirs) - set(mine))
    return res


STRINGS = {
    "en": {"err": "COULD NOT CHECK", "same": "files identical", "same1": "file identical",
           "mod": "modified", "extra": "extra", "miss": "missing",
           "tags": ("modified", "extra", "missing"),
           "none": "No embedded packages found (v1 needs .dist-info / .egg-info / version.py)."},
    "tr": {"err": "KONTROL EDİLEMEDİ", "same": "dosya aynı", "same1": "dosya aynı",
           "mod": "değişmiş", "extra": "fazladan", "miss": "eksik",
           "tags": ("değişmiş", "fazladan", "eksik"),
           "none": "Gömülü paket bulunamadı (v1: .dist-info / .egg-info / version.py gerekir)."},
}


def summarize(r: Result, lang: str = "en") -> str:
    s = STRINGS[lang]
    label = f"{r.name.replace('-', '_')} {r.version}"
    if r.status == "error":
        return f"{label}: {s['err']} ({r.error})"
    same_word = s["same1"] if r.same == 1 else s["same"]
    parts = [f"{r.same} {same_word}", f"{len(r.modified)} {s['mod']}", f"{len(r.extra)} {s['extra']}"]
    if r.missing:
        parts.append(f"{len(r.missing)} {s['miss']}")
    return f"{label}: " + ", ".join(parts) + "."


# -------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="vendorcheck", description=__doc__.split("\n\n")[0])
    ap.add_argument("path", help="directory or archive (.zip/.fda/.xpi/.vsix/...)")
    ap.add_argument("--version", action="version", version=f"vendorcheck {__version__}")
    ap.add_argument("--json", action="store_true", help="machine readable output")
    ap.add_argument("-v", "--verbose", action="store_true", help="list differing files")
    ap.add_argument("--cache", help="download cache dir (default: temp)")
    ap.add_argument("--only", nargs="*", help="check only these package names")
    ap.add_argument("--lang", choices=sorted(STRINGS), default="en", help="output language")
    ap.add_argument("--allow-empty", action="store_true",
                    help="exit 0 even if no embedded packages were found")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    src = Path(args.path)
    if not src.exists():
        print(f"not found: {src}", file=sys.stderr)
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="vendorcheck-"))
    try:
        if src.is_dir():
            root = src.resolve()
        else:
            if not zipfile.is_zipfile(src):
                print("not a directory or zip-compatible archive", file=sys.stderr)
                return 2
            root = tmp / "pkg"
            with zipfile.ZipFile(src) as zf:
                safe_extract_zip(zf, root)
        cache = Path(args.cache) if args.cache else tmp / "cache"
        cache.mkdir(parents=True, exist_ok=True)

        embedded = discover(root)
        if args.only:
            wanted = {norm_name(n) for n in args.only}
            embedded = [e for e in embedded if norm_name(e.name) in wanted]
        # dedupe identical (name, version, container)
        seen, uniq = set(), []
        for e in embedded:
            k = (norm_name(e.name), e.version, e.container)
            if k not in seen:
                seen.add(k)
                uniq.append(e)
        results = [check_one(e, cache, root) for e in uniq]

        if args.json:
            print(json.dumps([r.__dict__ | {"clean": r.clean} for r in results],
                             indent=2, ensure_ascii=False))
        else:
            if not results:
                print(STRINGS[args.lang]["none"])
            for r in results:
                print(summarize(r, args.lang))
                if args.verbose:
                    tags = STRINGS[args.lang]["tags"]
                    for tag, items in zip(tags, (r.modified, r.extra, r.missing)):
                        for it in items:
                            print(f"    [{tag}] {it}")
        if not results:
            return 0 if args.allow_empty else 2
        if any(r.status == "error" for r in results):
            return 2 if not any(not r.clean for r in results if r.status == "ok") else 1
        return 0 if all(r.clean for r in results) else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
