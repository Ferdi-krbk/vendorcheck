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
