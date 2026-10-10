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
import difflib
import fnmatch
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

__version__ = "0.3.1"
IGNORED_DIRS = {"__pycache__"}
IGNORED_SUFFIXES = (".pyc", ".pyo")
# Matches `__version__ = "1.2"`, `__version__: str = "1.2"` and the chained form that
# setuptools-scm / hatch-vcs generate: `__version__ = version = "1.2"`.
VERSION_ASSIGN_RE = re.compile(
    r"""^__version__\s*(?::\s*[^=\n]+)?=\s*(?:\w+\s*=\s*)*['"]([^'"]+)['"]""", re.M)

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



def guess_version_by_files(name: str, files_hashes: dict[str, str], cache: Path) -> str | None:
    """Guess package version by querying PyPI releases when no metadata exists."""
    try:
        meta = json.loads(http_get(f"https://pypi.org/pypi/{norm_name(name)}/json"))
        releases = meta.get("releases", {})
        # Check latest versions first
        sorted_vers = list(reversed(list(releases.keys())))[:25]
        for ver in sorted_vers:
            arts = releases.get(ver, [])
            art = pick_artifact(arts)
            if not art:
                continue
            try:
                off, _ = fetch_official(name, ver, cache)
                # Sample 2-3 files to test match
                matches = 0
                total_sampled = 0
                for rel_path, expected_hash in list(files_hashes.items())[:5]:
                    cand = off / rel_path
                    if cand.is_file():
                        total_sampled += 1
                        if sha256_file(cand) == expected_hash:
                            matches += 1
                if total_sampled > 0 and matches == total_sampled:
                    return ver
            except Exception:
                continue
    except Exception:
        pass
    return None


def discover(root: Path) -> tuple[list[Embedded], list[Path]]:
    found: list[Embedded] = []
    unrecognized: list[Path] = []
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
    # Version detection fallbacks for bare/unregistered directories
    for dirpath, dirnames, filenames in os.walk(root):
        d = Path(dirpath)
        if "__init__.py" in filenames:
            if (d.parent / "__init__.py").exists() or d.resolve() in covered:
                continue
            if any(c in covered for c in [d.resolve(), *d.resolve().parents]):
                continue

            # Case A: version.py / _version.py / __init__.py __version__
            found_ver = None
            source_tag = "version.py"
            for vfile in ("version.py", "_version.py", "__init__.py"):
                if vfile in filenames:
                    m = VERSION_ASSIGN_RE.search(read_text(d / vfile))
                    if m:
                        found_ver = m.group(1)
                        source_tag = vfile
                        break
            if found_ver:
                found.append(Embedded(d.name.replace("_", "-"), found_ver, d.parent,
                                      [d.name], source_tag))
                covered.add(d.resolve())
            else:
                unrecognized.append(d)
    return found, unrecognized


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
    # 1. Pure Python wheel (-none-any.whl)
    wheels = [u for u in urls if u["packagetype"] == "bdist_wheel"
              and u["filename"].endswith("-none-any.whl")]
    if wheels:
        return wheels[0]
    # 2. Platform wheel matching current system/Python if packaging is available
    try:
        import packaging.tags, packaging.utils
        sys_tags = set(packaging.tags.sys_tags())
        plat_wheels = []
        for u in urls:
            if u["packagetype"] == "bdist_wheel":
                wtags = packaging.utils.parse_wheel_filename(u["filename"])[-1]
                if any(t in sys_tags for t in wtags):
                    plat_wheels.append(u)
        if plat_wheels:
            return plat_wheels[0]
    except Exception:
        pass
    # 3. Source distribution (sdist)
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
def collect(container: Path, tops: list[str], ignore: tuple[str, ...] = ()) -> dict[str, Path]:
    files: dict[str, Path] = {}
    for t in tops:
        for root in (container / t, container / f"{t}.py"):
            if root.is_file():
                candidates = [root]
            elif root.is_dir():
                candidates = [p for p in root.rglob("*") if p.is_file()]
            else:
                continue
            for p in candidates:
                rel = p.relative_to(container)
                if is_ignored(rel) or matches_ignore(rel.as_posix(), ignore):
                    continue
                files[rel.as_posix()] = p
    return files


def matches_ignore(rel: str, patterns: tuple[str, ...]) -> bool:
    """Glob match on the posix path relative to the package container (e.g. 'foo/*.orig')."""
    return any(fnmatch.fnmatchcase(rel, p) for p in patterns)


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


def make_diff(official: Path, embedded: Path, rel: str, limit: int = 200) -> str:
    """Unified diff official -> embedded; empty string for binary files."""
    try:
        a, b = official.read_bytes(), embedded.read_bytes()
    except OSError:
        return ""
    if b"\0" in a[:8192] or b"\0" in b[:8192]:
        return ""
    lines = list(difflib.unified_diff(
        a.decode("utf-8", "replace").splitlines(), b.decode("utf-8", "replace").splitlines(),
        f"official/{rel}", f"embedded/{rel}", lineterm=""))
    if len(lines) > limit:
        lines = lines[:limit] + [f"... ({len(lines) - limit} more diff lines)"]
    return "\n".join(lines)


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
    diffs: dict[str, str] = field(default_factory=dict)

    @property
    def clean(self) -> bool:
        return self.status == "ok" and not (self.modified or self.extra or self.missing)


def find_official_container(off: Path, tops: list[str]) -> Path:
    for cand in (off, off / "src", off / "lib"):
        if any((cand / t).is_dir() or (cand / f"{t}.py").is_file() for t in tops):
            return cand
    return off


def check_one(e: Embedded, cache: Path, root: Path, ignore: tuple[str, ...] = (),
              want_diff: bool = False, prefix: str = "") -> Result:
    loc = (e.container.relative_to(root).as_posix() or ".")
    res = Result(e.name, e.version, e.source, f"{prefix}{loc}" if prefix else loc)
    try:
        off, res.artifact = fetch_official(e.name, e.version, cache)
    except urllib.error.HTTPError as ex:
        res.status, res.error = "error", f"PyPI HTTP {ex.code} (not on PyPI under this name/version?)"
        return res
    except Exception as ex:  # noqa: BLE001
        res.status, res.error = "error", str(ex)
        return res
    off_container = find_official_container(off, e.tops)
    mine = collect(e.container, e.tops, ignore)
    theirs = collect(off_container, e.tops, ignore)
    for rel, p in sorted(mine.items()):
        if rel not in theirs:
            res.extra.append(rel)
        elif same_content(p, theirs[rel]):
            res.same += 1
        else:
            res.modified.append(rel)
            if want_diff:
                d = make_diff(theirs[rel], p, rel)
                if d:
                    res.diffs[rel] = d
    res.missing = sorted(set(theirs) - set(mine))
    return res



NESTED_EXTS = (".zip", ".whl", ".egg", ".xpi", ".vsix", ".fda", ".pyz")
MAX_NESTED_BYTES = 2 * 1024**3  # zip-bomb guard for nested archive expansion


def expand_nested(root: Path, tmp: Path, max_depth: int = 2) -> list[tuple[Path, str]]:
    """Return [(dir, label_prefix)] for root plus any zip-like archives found inside it."""
    roots: list[tuple[Path, str]] = [(root, "")]
    frontier = [(root, "")]
    total, counter = 0, 0
    for _ in range(max_depth):
        nxt: list[tuple[Path, str]] = []
        for base, prefix in frontier:
            for p in sorted(base.rglob("*")):
                if not (p.is_file() and p.suffix.lower() in NESTED_EXTS and zipfile.is_zipfile(p)):
                    continue
                with zipfile.ZipFile(p) as zf:
                    total += sum(i.file_size for i in zf.infolist())
                    if total > MAX_NESTED_BYTES:
                        raise RuntimeError("nested archives too large (zip-bomb guard)")
                    counter += 1
                    dest = tmp / "nested" / str(counter)
                    safe_extract_zip(zf, dest)
                label = f"{prefix}{p.relative_to(base).as_posix()}!/"
                roots.append((dest, label))
                nxt.append((dest, label))
        frontier = nxt
    return roots


def write_step_summary(results: list[Result]) -> None:
    """Append a markdown table to $GITHUB_STEP_SUMMARY when running in GitHub Actions."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    rows = ["### vendorcheck", "", "| Package | Version | Identical | Modified | Extra | Missing | Status |",
            "|---|---|---:|---:|---:|---:|---|"]
    for r in results:
        status = "error" if r.status == "error" else ("clean" if r.clean else "DIFFERS")
        rows.append(f"| `{r.name}` | {r.version} | {r.same} | {len(r.modified)} | {len(r.extra)} "
                    f"| {len(r.missing)} | {status} |")
    for r in results:
        for title, items in (("modified", r.modified), ("extra", r.extra), ("missing", r.missing)):
            if items:
                rows += ["", f"<details><summary>{r.name} {r.version}: {title} ({len(items)})</summary>", ""]
                rows += [f"- `{i}`" for i in items[:100]] + ["", "</details>"]
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(rows) + "\n")


STRINGS = {
    "en": {"err": "COULD NOT CHECK", "same": "files identical", "same1": "file identical",
           "mod": "modified", "extra": "extra", "miss": "missing",
           "tags": ("modified", "extra", "missing"),
           "none": "No embedded packages found (v1 needs .dist-info / .egg-info / version.py).",
           "unrec": "warning: unrecognized package (version not found):"},
    "tr": {"err": "KONTROL EDİLEMEDİ", "same": "dosya aynı", "same1": "dosya aynı",
           "mod": "değişmiş", "extra": "fazladan", "miss": "eksik",
           "tags": ("değişmiş", "fazladan", "eksik"),
           "none": "Gömülü paket bulunamadı (v1: .dist-info / .egg-info / version.py gerekir).",
           "unrec": "uyarı: tanınmayan paket (sürüm bulunamadı):"},
}


def summarize(r: Result, lang: str = "en") -> str:
    s = STRINGS[lang]
    label = f"{r.name.replace('-', '_')} {r.version}"
    if "!/" in r.location:
        label += f" [{r.location}]"
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
    ap.add_argument("-v", "--verbose", action="count", default=0,
                    help="-v list differing files, -vv also print unified diffs")
    ap.add_argument("--cache", help="download cache dir (default: temp)")
    ap.add_argument("--only", nargs="*", help="check only these package names")
    ap.add_argument("--ignore", action="append", default=[], metavar="GLOB",
                    help="ignore files matching GLOB (relative to the package dir, e.g. "
                         "'yt_dlp/version.py'); repeatable. For intentional local patches.")
    ap.add_argument("--no-nested", action="store_true", help="do not open archives inside the input")
    ap.add_argument("--lang", choices=sorted(STRINGS), default="en", help="output language")
    ap.add_argument("--allow-empty", action="store_true",
                    help="exit 0 even if no embedded packages were found")
    ap.add_argument("--strict", action="store_true",
                    help="exit 2 if unrecognized package directories (without detectable version) are found")
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
            try:
                with zipfile.ZipFile(src) as zf:
                    safe_extract_zip(zf, root)
            except RuntimeError as ex:
                print(f"archive error: {ex}", file=sys.stderr)
                return 2
        cache = Path(args.cache) if args.cache else tmp / "cache"
        cache.mkdir(parents=True, exist_ok=True)

        try:
            roots = [(root, "")] if args.no_nested else expand_nested(root, tmp)
        except RuntimeError as ex:
            print(f"archive error: {ex}", file=sys.stderr)
            return 2

        wanted = {norm_name(n) for n in args.only} if args.only else None
        ignore = tuple(args.ignore)
        results: list[Result] = []
        seen: set = set()
        all_unrecognized: list[str] = []
        for rdir, prefix in roots:
            embeds, unrec = discover(rdir)
            for u in unrec:
                rel = f"{prefix}{u.relative_to(rdir).as_posix()}"
                if rel not in all_unrecognized:
                    all_unrecognized.append(rel)
            for e in embeds:
                if wanted and norm_name(e.name) not in wanted:
                    continue
                key = (norm_name(e.name), e.version, e.container)
                if key in seen:
                    continue
                seen.add(key)
                results.append(check_one(e, cache, rdir, ignore, args.verbose >= 2, prefix))

        for u in all_unrecognized:
            print(f"{STRINGS[args.lang]['unrec']} {u}", file=sys.stderr)

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
                            if tag == tags[0] and it in r.diffs:
                                print("\n".join("        " + l for l in r.diffs[it].splitlines()))
        write_step_summary(results)
        if all_unrecognized and args.strict:
            return 2
        if not results:
            return 0 if args.allow_empty else 2
        if any(r.status == "error" for r in results):
            return 2 if not any(not r.clean for r in results if r.status == "ok") else 1
        return 0 if all(r.clean for r in results) else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
