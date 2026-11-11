"""Release channel publisher (tools/release/publish_release.py) with fakes: no network,
no Cloudflare, throwaway keys. Covers version gating, site assembly (flat names, keep N,
headers, signed latest.json + install.sh), tamper refusal and the internet check."""
from __future__ import annotations

import importlib.util
import io
import json
import shutil
import tarfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("publish_release", REPO / "tools" / "release" / "publish_release.py")
pr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pr)
wr = pr.wr

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="needs openssl")


@pytest.fixture
def keys(tmp_path):
    key, pub = tmp_path / "k" / "sign.key", tmp_path / "k" / "pub.pem"
    wr.genkey(key, pub)
    return key, pub


def fake_build(tmp_path, key, version):
    """What make-release.sh writes: DIST/releases/V/{walter-V.tar.gz,manifest.json}(+.sig)."""
    dist = tmp_path / f"dist-{version}"
    rel = dist / "releases" / version
    rel.mkdir(parents=True)
    tar = rel / f"walter-{version}.tar.gz"
    with tarfile.open(tar, "w:gz") as tf:
        for name, data in ((f"walter-{version}/VERSION", version.encode() + b"\n"),
                           (f"walter-{version}/deploy/install.sh", f"#!/usr/bin/env bash\n# {version}\n".encode())):
            ti = tarfile.TarInfo(name); ti.size = len(data); tf.addfile(ti, io.BytesIO(data))
    m = wr.build_manifest(version, tar, min_installer=2, changelog="- x\n", commit="abc")
    (rel / "manifest.json").write_text(json.dumps(m))
    wr.sign(key, tar, str(tar) + ".sig")
    wr.sign(key, rel / "manifest.json", str(rel / "manifest.json") + ".sig")
    return dist


def test_check_newer():
    assert pr.check_newer("0.1.0", "")
    assert pr.check_newer("0.1.1", "0.1.0")
    assert not pr.check_newer("0.1.0", "0.1.0")
    assert not pr.check_newer("0.0.9", "0.1.0")
    with pytest.raises(ValueError):
        pr.check_newer("1.0", "")


def test_live_version_fakes():
    assert pr.live_version("https://x", fetcher=lambda u: None) == ""
    assert pr.live_version("https://x", fetcher=lambda u: b'{"version": "0.2.0"}') == "0.2.0"

    def down(u):
        raise OSError("no dns")
    assert pr.live_version("https://x", fetcher=down) == ""
    with pytest.raises(SystemExit):
        pr.live_version("https://x", fetcher=lambda u: b"<html>")


def test_assemble_layout_headers_and_signatures(tmp_path, keys):
    key, pub = keys
    mirror, site = tmp_path / "mirror", tmp_path / "site"
    deployed = pr.assemble(fake_build(tmp_path, key, "0.1.0"), "0.1.0", mirror, site, key, pub)
    assert deployed == ["0.1.0"]
    names = {p.name for p in site.iterdir()}
    for n in ("latest.json", "latest.json.sig", "manifest-0.1.0.json", "manifest-0.1.0.json.sig",
              "walter-0.1.0.tar.gz", "walter-0.1.0.tar.gz.sig", "install.sh", "install.sh.sig",
              "release-public.pem", "_headers", "404.html"):
        assert n in names, n
    latest = json.loads((site / "latest.json").read_text())
    assert latest["tarball_url"] == "walter-0.1.0.tar.gz"
    # the updater's own parser resolves the flat relative URLs
    rel = wr.read_latest(site / "latest.json", "https://releases.wallter.app/")
    assert rel["tarball_url"] == "https://releases.wallter.app/walter-0.1.0.tar.gz"
    assert wr.verify_sig(pub, site / "latest.json", site / "latest.json.sig")
    assert wr.verify_sig(pub, site / "install.sh", site / "install.sh.sig")
    assert (site / "install.sh").read_text().startswith("#!")
    h = (site / "_headers").read_text()
    assert "nosniff" in h and "max-age=60" in h and "immutable" in h
    assert "Access-Control-Allow-Origin: *" not in h


def test_keep_last_n_and_latest_follows_newest(tmp_path, keys):
    key, pub = keys
    mirror, site = tmp_path / "mirror", tmp_path / "site"
    for v in ("0.1.0", "0.1.1", "0.1.2", "0.2.0"):
        deployed = pr.assemble(fake_build(tmp_path, key, v), v, mirror, site, key, pub, keep=2)
    assert deployed == ["0.1.2", "0.2.0"]
    assert not (site / "walter-0.1.0.tar.gz").exists() and (mirror / "walter-0.1.0.tar.gz").exists()
    assert json.loads((site / "latest.json").read_text())["version"] == "0.2.0"
    assert "# 0.2.0" in (site / "install.sh").read_text()


def test_refuses_tampered_or_foreign_signed_build(tmp_path, keys):
    key, pub = keys
    dist = fake_build(tmp_path, key, "0.1.0")
    tar = dist / "releases" / "0.1.0" / "walter-0.1.0.tar.gz"
    tar.write_bytes(tar.read_bytes() + b"x")
    with pytest.raises(SystemExit, match="REFUSED"):
        pr.assemble(dist, "0.1.0", tmp_path / "m", tmp_path / "s", key, pub)
    other_key, other_pub = tmp_path / "o" / "k", tmp_path / "o" / "p.pem"
    wr.genkey(other_key, other_pub)
    with pytest.raises(SystemExit, match="REFUSED"):
        pr.assemble(fake_build(tmp_path / "b", other_key, "0.1.0"), "0.1.0", tmp_path / "m", tmp_path / "s", key, pub)
    assert not (tmp_path / "m" / "latest.json").exists()


def test_published_versions_are_immutable(tmp_path, keys):
    key, pub = keys
    mirror = tmp_path / "mirror"
    pr.assemble(fake_build(tmp_path, key, "0.1.0"), "0.1.0", mirror, tmp_path / "s", key, pub)
    again = fake_build(tmp_path / "again", key, "0.1.0")
    man = again / "releases" / "0.1.0" / "manifest.json"
    man.write_text(man.read_text().replace('"- x\\n"', '"- different\\n"'))
    wr.sign(key, man, str(man) + ".sig")
    with pytest.raises(SystemExit, match="immutable"):
        pr.assemble(again, "0.1.0", mirror, tmp_path / "s", key, pub)


def _site_fetcher(site: Path, base: str):
    def fetch(url):
        assert url.startswith(base)
        p = site / url[len(base):]
        return p.read_bytes() if p.is_file() else None
    return fetch


def test_verify_remote_ok_and_tamper(tmp_path, keys):
    key, pub = keys
    site = tmp_path / "site"
    pr.assemble(fake_build(tmp_path, key, "0.1.0"), "0.1.0", tmp_path / "m", site, key, pub)
    base = "https://releases.example/"
    r = pr.verify_remote(base, pub, "0.1.0", fetcher=_site_fetcher(site, base))
    assert r["version"] == "0.1.0" and len(r["sha256"]) == 64
    with pytest.raises(SystemExit, match="expected"):
        pr.verify_remote(base, pub, "0.1.1", fetcher=_site_fetcher(site, base))
    t = site / "walter-0.1.0.tar.gz"
    b = t.read_bytes(); t.write_bytes(b[:-1] + bytes([b[-1] ^ 1]))
    with pytest.raises(SystemExit, match="sha256 mismatch"):
        pr.verify_remote(base, pub, fetcher=_site_fetcher(site, base))
    (site / "latest.json").unlink()
    with pytest.raises(SystemExit, match="404"):
        pr.verify_remote(base, pub, fetcher=_site_fetcher(site, base))


def test_install_defaults_to_cloudflare_channel_in_bootstrap_only():
    s = (REPO / "deploy" / "install.sh").read_text()
    assert 'DEFAULT_UPDATE_URL="https://releases.wallter.app"' in s
    boot = s.index("if (( BOOTSTRAP )); then")
    assert s.index('UPDATE_URL="${UPDATE_URL:-$DEFAULT_UPDATE_URL}"') > boot
    # outside bootstrap, the channel stays whatever was passed (the dev box gets none)
    assert 'UPDATE_URL="${WALTER_UPDATE_URL:-}"' in s
    assert "releases.wallter.app/install.sh" in (REPO / "docs" / "BETA-INSTALL.md").read_text()


def test_publish_script_syntax_and_safety():
    import subprocess
    sh = REPO / "tools" / "release" / "publish-release.sh"
    assert subprocess.run(["bash", "-n", str(sh)]).returncode == 0
    s = sh.read_text()
    assert "check-newer" in s and "verify-remote" in s and "git push" not in s
    assert s.index('"$PY" check-newer') < s.index("make-release.sh\" ")
    assert s.index("pages deploy") < s.index("verify-remote \"$BASE_URL\"")
