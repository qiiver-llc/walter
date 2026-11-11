"""Signed releases: manifest + Ed25519 signature verify (throwaway keys), tamper
detection, latest.json parsing, safe extraction, versioned switch/rollback/prune
on fake dirs, make-release.sh end to end in a temp clone, and safe beta defaults.
No network, no root, never touches the real signing key."""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import tarfile
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
HELPER = REPO / "tools" / "release" / "walter_release.py"
spec = importlib.util.spec_from_file_location("walter_release", HELPER)
wr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wr)

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="needs openssl")


@pytest.fixture
def keys(tmp_path):
    key, pub = tmp_path / "k" / "sign.key", tmp_path / "k" / "pub.pem"
    wr.genkey(key, pub)
    return key, pub


def _tarball(tmp_path, version="1.2.3", extra=None):
    src = tmp_path / "src" / f"walter-{version}"
    (src / "walter").mkdir(parents=True)
    (src / "walter" / "x.py").write_text("X = 1\n")
    (src / "VERSION").write_text(version + "\n")
    t = tmp_path / f"walter-{version}.tar.gz"
    with tarfile.open(t, "w:gz") as tf:
        tf.add(src, arcname=src.name)
        for name, data in (extra or {}).items():
            import io
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
    return t


def test_genkey_private_mode_and_no_overwrite(keys):
    key, pub = keys
    assert oct(key.stat().st_mode & 0o777) == "0o600"
    assert pub.read_text().startswith("-----BEGIN PUBLIC KEY-----")
    with pytest.raises(SystemExit):
        wr.genkey(key, pub)


def test_sign_verify_and_manifest(tmp_path, keys):
    key, pub = keys
    t = _tarball(tmp_path)
    wr.sign(key, t, str(t) + ".sig")
    m = wr.build_manifest("1.2.3", t, min_installer=2, changelog="- thing\n", commit="abc")
    assert m["sha256"] == wr.sha256_file(t) and m["version"] == "1.2.3"
    assert m["min_installer_version"] == 2 and m["changelog"] == "- thing\n"
    assert wr.verify(pub, t, str(t) + ".sig", m["sha256"]) == (True, "ok")
    latest = wr.build_latest(m)
    assert latest["tarball_url"] == "releases/1.2.3/walter-1.2.3.tar.gz"


def test_tamper_detection(tmp_path, keys):
    key, pub = keys
    t = _tarball(tmp_path)
    sig = Path(str(t) + ".sig")
    wr.sign(key, t, sig)
    good = wr.sha256_file(t)
    data = bytearray(t.read_bytes())
    data[len(data) // 2] ^= 0xFF
    t.write_bytes(bytes(data))
    assert wr.verify(pub, t, sig, good) == (False, "sha256 mismatch")
    # attacker also fixes the sha256 in the manifest: the signature still fails
    assert wr.verify(pub, t, sig, wr.sha256_file(t)) == (False, "signature does not verify")
    # signed by someone else's key
    other_key, other_pub = tmp_path / "o.key", tmp_path / "o.pem"
    wr.genkey(other_key, other_pub)
    t2 = _tarball(tmp_path / "b")
    wr.sign(other_key, t2, str(t2) + ".sig")
    assert not wr.verify(pub, t2, str(t2) + ".sig", wr.sha256_file(t2))[0]
    # missing signature
    assert not wr.verify(pub, t2, tmp_path / "nope.sig")[0]


def test_cli_verify_exit_codes(tmp_path, keys):
    key, pub = keys
    t = _tarball(tmp_path)
    wr.sign(key, t, str(t) + ".sig")
    run = lambda sha: subprocess.run(["python3", "-I", str(HELPER), "verify", str(pub), str(t),
                                      str(t) + ".sig", sha], capture_output=True, text=True)
    assert run(wr.sha256_file(t)).returncode == 0
    assert run("0" * 64).returncode == 1


def test_read_latest_resolves_relative_urls_and_rejects_junk(tmp_path):
    p = tmp_path / "latest.json"
    p.write_text(json.dumps({"version": "1.0.1", "tarball_url": "releases/1.0.1/w.tar.gz",
                             "sha256": "a" * 64, "signature_url": "releases/1.0.1/w.tar.gz.sig"}))
    d = wr.read_latest(p, "https://dl.example/walter/")
    assert d["tarball_url"] == "https://dl.example/walter/releases/1.0.1/w.tar.gz"
    assert d["min_installer_version"] == 1
    out = subprocess.run(["python3", "-I", str(HELPER), "latest", str(p), "https://dl.example/walter/"],
                         capture_output=True, text=True).stdout
    assert "REL_VERSION=1.0.1" in out
    for bad in ({"version": "1.0; rm -rf /"}, {"sha256": "zz"}, {"tarball_url": "javascript:x"}):
        d = {"version": "1.0.1", "tarball_url": "https://x/a", "sha256": "a" * 64, "signature_url": "https://x/b"}
        d.update(bad)
        p.write_text(json.dumps(d))
        with pytest.raises(ValueError):
            wr.read_latest(p, "ftp://nowhere/")


def test_version_ordering():
    assert wr.newer("1.10.0", "1.9.9")
    assert not wr.newer("1.0.0", "1.0.0")
    assert not wr.newer("0.9.0", "1.0.0")
    assert wr.newer("0.1.0", None)
    assert wr.newer("0.1.0", "abc1234")      # legacy git install -> any release is newer


def test_safe_extract(tmp_path):
    t = _tarball(tmp_path)
    top = wr.safe_extract(t, tmp_path / "out")
    assert top.name == "walter-1.2.3" and (top / "walter" / "x.py").is_file()
    evil = _tarball(tmp_path / "e", extra={"../escape.txt": b"x"})
    with pytest.raises(ValueError):
        wr.safe_extract(evil, tmp_path / "out2")
    assert not (tmp_path / "e" / "escape.txt").exists()


def _fake_releases(root: Path, *versions):
    root.mkdir(parents=True, exist_ok=True)
    for v in versions:
        (root / v / "walter").mkdir(parents=True)
        (root / v / "VERSION").write_text(v)
    return root


def test_switch_rollback_prune(tmp_path):
    root = _fake_releases(tmp_path / "rel", "1.0.0", "1.0.1", "1.0.2")
    st = wr.switch(root, "1.0.0")
    assert st["current"] == "1.0.0" and st["previous"] is None
    wr.switch(root, "1.0.1")
    st = wr.switch(root, "1.0.2")
    assert (st["current"], st["previous"]) == ("1.0.2", "1.0.1")
    assert (root / "current" / "VERSION").read_text() == "1.0.2"
    # a symlink to current (like ~walter/walter) follows the switch
    (tmp_path / "walter").symlink_to(root / "current")
    st = wr.rollback(root)
    assert (st["current"], st["previous"]) == ("1.0.1", "1.0.2")
    assert (tmp_path / "walter" / "VERSION").read_text() == "1.0.1"
    st = wr.rollback(root)                       # rollback again = roll forward
    assert st["current"] == "1.0.2"
    assert wr.prune(root) == ["1.0.0"]
    assert sorted(wr.status(root)["installed"]) == ["1.0.1", "1.0.2"]
    assert not list(root.glob(".*.tmp-*"))


def test_rollback_without_previous_refuses(tmp_path):
    root = _fake_releases(tmp_path / "rel", "1.0.0")
    wr.switch(root, "1.0.0")
    with pytest.raises(SystemExit):
        wr.rollback(root)
    with pytest.raises(SystemExit):
        wr.switch(root, "9.9.9")


def test_make_release_end_to_end(tmp_path):
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(REPO), str(clone)], check=True)
    env = dict(os.environ, HOME=str(tmp_path / "home"))      # no private-words file, no real key
    key = tmp_path / "throwaway.key"
    pub = clone / "deploy" / "release-public.pem"
    pub.unlink()
    wr.genkey(key, pub)
    subprocess.run(["git", "-C", str(clone), "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-qam", "test key"], check=True)
    out = tmp_path / "dist"
    r = subprocess.run(["bash", str(clone / "tools/release/make-release.sh"), "0.4.0", "--out", str(out),
                        "--key", str(key)], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    rel = out / "releases" / "0.4.0"
    t = rel / "walter-0.4.0.tar.gz"
    m = json.loads((rel / "manifest.json").read_text())
    assert m["sha256"] == wr.sha256_file(t) and m["changelog"].startswith("- ")
    assert wr.verify(pub, t, str(t) + ".sig", m["sha256"])[0]
    assert wr.verify_sig(pub, rel / "manifest.json", rel / "manifest.json.sig")
    latest = wr.read_latest(out / "latest.json", "https://dl.example/")
    assert latest["version"] == "0.4.0"
    with tarfile.open(t) as tf:
        names = tf.getnames()
    assert "walter-0.4.0/VERSION" in names and "walter-0.4.0/docs/BETA-INSTALL.md" in names
    assert not [n for n in names if n.startswith(("walter-0.4.0/tests", "walter-0.4.0/.git"))]
    # same version again is refused (immutable)
    r = subprocess.run(["bash", str(clone / "tools/release/make-release.sh"), "0.4.0", "--out", str(out),
                        "--key", str(key)], capture_output=True, text=True, env=env)
    assert r.returncode != 0
    # a key that doesn't match deploy/release-public.pem is refused
    k2 = tmp_path / "other.key"
    wr.genkey(k2, tmp_path / "other.pem")
    r = subprocess.run(["bash", str(clone / "tools/release/make-release.sh"), "0.4.1", "--out", str(out),
                        "--key", str(k2)], capture_output=True, text=True, env=env)
    assert r.returncode != 0 and "does not match" in r.stderr


def test_installer_embeds_the_committed_public_key():
    pem = (REPO / "deploy" / "release-public.pem").read_text().split()
    body = [line for line in pem if not line.startswith("-----") and line not in ("BEGIN", "END", "PUBLIC", "KEY-----")]
    install = (REPO / "deploy" / "install.sh").read_text()
    m = re.search(r"RELEASE_PUBLIC_PEM='-----BEGIN PUBLIC KEY-----\n(\S+)\n-----END PUBLIC KEY-----'", install)
    assert m and m.group(1) == "".join(body)


@pytest.mark.parametrize("script", ["deploy/install.sh", "deploy/update.sh", "tools/release/make-release.sh"])
def test_bash_syntax(script):
    assert subprocess.run(["bash", "-n", str(REPO / script)]).returncode == 0


def test_update_keeps_github_path_unless_channel_set():
    s = (REPO / "deploy" / "update.sh").read_text()
    assert 'if [[ -n "${WALTER_UPDATE_URL:-}" ]]; then' in s
    assert "WALTER_UPDATE_REPO" in s and "releases/tags/installer-latest" in s
    # verification happens before extraction
    assert s.index('"$HELPER" verify') < s.index('"$HELPER" extract')


def test_safe_beta_defaults():
    cfg = tomllib.loads((REPO / "config.example.toml").read_text())
    assert cfg["senses"]["camera"] is False
    assert cfg["senses"]["recognise_faces"] is False and cfg["senses"]["recognise_voices"] is False
    assert cfg["speech"]["tts_provider"] == "local" and cfg["speech"]["stt_provider"] == "local"
    assert cfg["server"]["host"] == "127.0.0.1"
    assert "acceptEdits" in (REPO / "config.example.toml").read_text()
    html = (REPO / "face" / "setup-phone.html").read_text()
    assert re.search(r'id="fulladmin" type="checkbox">', html)          # not pre-checked
    assert "--permission-mode" in (REPO / "deploy" / "install.sh").read_text()
