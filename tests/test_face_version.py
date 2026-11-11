import os

from walter.server import face_snapshot, face_version


def _tree(tmp_path):
    (tmp_path / "walter.js").write_text("// face")
    (tmp_path / "index.html").write_text("<html>")
    (tmp_path / "backgrounds" / "custom").mkdir(parents=True)
    return tmp_path


def test_version_is_stable_across_restarts(tmp_path):
    d = _tree(tmp_path)
    assert face_version(face_snapshot(d)) == face_version(face_snapshot(d))


def test_code_edit_changes_version(tmp_path):
    d = _tree(tmp_path)
    v = face_version(face_snapshot(d))
    os.utime(d / "walter.js", (1e9, 1e9))
    assert face_version(face_snapshot(d)) != v


def test_data_files_do_not_change_version(tmp_path):
    d = _tree(tmp_path)
    v = face_version(face_snapshot(d))
    (d / "usage.json").write_text("{}")
    (d / "usage.json.tmp").write_text("{}")
    (d / ".canvas-x.tmp").write_text("{}")
    (d / "backgrounds" / "custom" / "mine.jpg").write_bytes(b"jpg")
    assert face_version(face_snapshot(d)) == v
