import zipfile
from pathlib import Path

import vendorcheck as vc


def make_pkg(root: Path):
    (root / "foo").mkdir(parents=True)
    (root / "foo" / "__init__.py").write_text("x = 1\n")
    di = root / "foo-1.2.3.dist-info"
    di.mkdir()
    (di / "METADATA").write_text("Metadata-Version: 2.1\nName: Foo-Bar\nVersion: 1.2.3\n\nbody Version: 9\n")
    (di / "top_level.txt").write_text("foo\n")


def test_discover_dist_info(tmp_path):
    make_pkg(tmp_path)
    (e,) = vc.discover(tmp_path)
    assert (e.name, e.version, e.tops, e.source) == ("Foo-Bar", "1.2.3", ["foo"], "dist-info")


def test_discover_version_py(tmp_path):
    (tmp_path / "bar").mkdir()
    (tmp_path / "bar" / "__init__.py").write_text("")
    (tmp_path / "bar" / "version.py").write_text("__version__ = '4.5'\n")
    (tmp_path / "bar" / "sub").mkdir()
    (tmp_path / "bar" / "sub" / "__init__.py").write_text("")
    (tmp_path / "bar" / "sub" / "version.py").write_text("__version__ = '9'\n")
    (e,) = vc.discover(tmp_path)  # subpackage version.py must be ignored
    assert (e.name, e.version, e.source) == ("bar", "4.5", "version.py")


def test_same_content_crlf(tmp_path):
    a, b, c = tmp_path / "a", tmp_path / "b", tmp_path / "c"
    a.write_bytes(b"x\r\ny\r\n")
    b.write_bytes(b"x\ny\n")
    c.write_bytes(b"x\nz\n")
    assert vc.same_content(a, b)
    assert not vc.same_content(a, c)


def test_zip_slip_rejected(tmp_path):
    z = tmp_path / "evil.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("../escape.txt", "x")
    try:
        with zipfile.ZipFile(z) as zf:
            vc.safe_extract_zip(zf, tmp_path / "out")
    except RuntimeError:
        return
    raise AssertionError("zip-slip not rejected")


def test_empty_dir_fails_unless_allowed(tmp_path, capsys):
    assert vc.main([str(tmp_path)]) == 2
    assert vc.main([str(tmp_path), "--allow-empty"]) == 0


def test_summary_languages():
    r = vc.Result("yt-dlp", "2026.8.19", "dist-info", ".", same=1049, modified=["a"], extra=["b", "c"])
    assert vc.summarize(r) == "yt_dlp 2026.8.19: 1049 files identical, 1 modified, 2 extra."
    assert vc.summarize(r, "tr") == "yt_dlp 2026.8.19: 1049 dosya aynı, 1 değişmiş, 2 fazladan."


def test_singular_file_word():
    r = vc.Result("six", "1.16.0", "dist-info", ".", same=1)
    assert vc.summarize(r) == "six 1.16.0: 1 file identical, 0 modified, 0 extra."


def test_cached_sdist_descends(tmp_path):
    (tmp_path / "pkg-1.0").mkdir()
    assert vc._descend_sdist(tmp_path, "pkg-1.0.tar.gz") == tmp_path / "pkg-1.0"
    assert vc._descend_sdist(tmp_path, "pkg-1.0-py3-none-any.whl") == tmp_path


# ---------------------------------------------------------------- offline e2e
def _fake_official(tmp_path, monkeypatch):
    off = tmp_path / "official"
    (off / "foo").mkdir(parents=True)
    (off / "foo" / "__init__.py").write_text("x = 1\n")
    (off / "foo" / "util.py").write_text("y = 2\n")
    monkeypatch.setattr(vc, "fetch_official", lambda n, v, c: (off, "foo-1.2.3-py3-none-any.whl"))


def test_e2e_detects_modified_extra_missing_and_diff(tmp_path, monkeypatch, capsys):
    _fake_official(tmp_path, monkeypatch)
    emb = tmp_path / "app"
    make_pkg(emb)
    (emb / "foo" / "__init__.py").write_text("x = 999\n")   # modified
    (emb / "foo" / "evil.py").write_text("import os\n")      # extra
    # util.py absent -> missing
    assert vc.main([str(emb), "-vv", "--no-nested"]) == 1
    out = capsys.readouterr().out
    assert "0 files identical, 1 modified, 1 extra, 1 missing" in out
    assert "-x = 1" in out and "+x = 999" in out


def test_e2e_ignore_globs_make_it_clean(tmp_path, monkeypatch):
    _fake_official(tmp_path, monkeypatch)
    emb = tmp_path / "app"
    make_pkg(emb)
    (emb / "foo" / "__init__.py").write_text("x = 999\n")
    (emb / "foo" / "evil.py").write_text("import os\n")
    args = [str(emb), "--ignore", "foo/__init__.py", "--ignore", "foo/evil.py",
            "--ignore", "foo/util.py"]
    assert vc.main(args) == 0


def test_nested_archive_is_expanded(tmp_path, monkeypatch, capsys):
    _fake_official(tmp_path, monkeypatch)
    inner_src = tmp_path / "inner"
    make_pkg(inner_src)
    (inner_src / "foo" / "util.py").write_text("y = 2\n")
    (inner_src / "foo" / "__init__.py").write_text("x = 1\n")
    outer = tmp_path / "outer"
    outer.mkdir()
    with zipfile.ZipFile(outer / "bundle.xpi", "w") as zf:
        for f in inner_src.rglob("*"):
            if f.is_file():
                zf.write(f, f.relative_to(inner_src).as_posix())
    assert vc.main([str(outer)]) == 0
    assert "foo 1.2.3" in capsys.readouterr().out.replace("Foo_Bar", "foo")


def test_step_summary_written(tmp_path, monkeypatch):
    f = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(f))
    vc.write_step_summary([vc.Result("six", "1.16.0", "dist-info", ".", same=3, modified=["a.py"])])
    text = f.read_text(encoding="utf-8")
    assert "| `six` | 1.16.0 | 3 | 1 | 0 | 0 | DIFFERS |" in text
