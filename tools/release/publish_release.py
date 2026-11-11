#!/usr/bin/env python3
"""Walter release channel: assemble, check and verify the Cloudflare Pages site.

Used by tools/release/publish-release.sh; standard library + openssl only.

  publish_release.py live-version BASE_URL          print the channel's current version ("" if none)
  publish_release.py check-newer VERSION CURRENT     exit 0 only if VERSION > CURRENT (or no CURRENT)
  publish_release.py assemble DIST VERSION MIRROR SITE KEY [KEEP]
        copy the built release from DIST into the persistent MIRROR (flat names), sign
        install.sh (taken from the signed tarball) and latest.json, then write the
        deployable SITE dir with only the newest KEEP (default 5) releases + _headers.
  publish_release.py verify-remote BASE_URL PUB [EXPECT_VERSION]
        download latest.json + tarball + signatures over https and check sha256 and
        Ed25519 signatures with the same code the updater uses.

Channel layout (all at the root of https://releases.wallter.app/):
  latest.json (+ .sig)        short cache (60 s); points at the newest release
  manifest-X.Y.Z.json (+ .sig), walter-X.Y.Z.tar.gz (+ .sig)   immutable, long cache
  install.sh (+ .sig)         bootstrap installer from the newest signed release
  release-public.pem          public key, for humans to verify by hand
"""
from __future__ import annotations

import importlib.util
import json
import re
import shutil
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("walter_release", HERE / "walter_release.py")
wr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wr)

PAGES_FILE_LIMIT = 25 * 1024 * 1024   # Cloudflare Pages: 25 MiB per file
KEEP_DEFAULT = 5
UA = "walter-release-publisher/1"

HEADERS = """\
/*
  X-Content-Type-Options: nosniff
  X-Frame-Options: DENY
  Referrer-Policy: no-referrer
  Content-Security-Policy: default-src 'none'; sandbox
  Cross-Origin-Resource-Policy: same-site
  Access-Control-Allow-Origin: https://wallter.app
  Access-Control-Allow-Methods: GET, HEAD
  Vary: Origin

/latest.json
  Content-Type: application/json; charset=utf-8
  Cache-Control: public, max-age=60, must-revalidate

/latest.json.sig
  Content-Type: application/octet-stream
  Cache-Control: public, max-age=60, must-revalidate

/install.sh
  Content-Type: text/plain; charset=utf-8
  Cache-Control: public, max-age=60, must-revalidate

/install.sh.sig
  Content-Type: application/octet-stream
  Cache-Control: public, max-age=60, must-revalidate

/release-public.pem
  Content-Type: text/plain; charset=utf-8
  Cache-Control: public, max-age=300

/manifest-*
  Cache-Control: public, max-age=31536000, immutable

/walter-*
  Content-Type: application/octet-stream
  Cache-Control: public, max-age=31536000, immutable
"""

# A real 404 page: without one, Pages serves index.html for unknown paths (SPA mode),
# and a missing latest.json would come back as HTML with status 200.
NOT_FOUND = "<!doctype html><title>Not found</title><p>Not found.</p>\n"
INDEX = ("<!doctype html><meta charset=utf-8><title>Walter releases</title>"
         "<p>Walter signed release channel. Nothing to browse here; "
         "see <a href=\"/latest.json\">latest.json</a>.</p>\n")

VERSIONED = re.compile(r"^(?:walter-(\d+\.\d+\.\d+)\.tar\.gz|manifest-(\d+\.\d+\.\d+)\.json)(?:\.sig)?$")


# ---------------------------------------------------------------- channel state
def fetch(url: str, timeout: float = 30) -> bytes | None:
    """GET url; None on 404. Other errors raise."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Cache-Control": "no-cache"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def live_version(base_url: str, fetcher=fetch) -> str:
    """The version the channel serves now, or "" if it has none yet."""
    try:
        raw = fetcher(base_url.rstrip("/") + "/latest.json")
    except (urllib.error.URLError, OSError):
        return ""          # channel not reachable yet (e.g. DNS not created): first publish
    if not raw:
        return ""
    try:
        v = str(json.loads(raw)["version"])
        wr.parse_version(v)
        return v
    except (ValueError, KeyError, TypeError):
        raise SystemExit("the channel serves an unreadable latest.json; fix it by hand first")


def check_newer(version: str, current: str) -> bool:
    wr.parse_version(version)
    if not current:
        return True
    return wr.parse_version(version) > wr.parse_version(current)


def mirror_versions(mirror: Path) -> list[str]:
    vs = set()
    for p in mirror.iterdir() if mirror.is_dir() else ():
        m = VERSIONED.match(p.name)
        if m:
            vs.add(m.group(1) or m.group(2))
    return sorted(vs, key=wr.parse_version)


# ---------------------------------------------------------------- assemble
def build_latest(manifest: dict) -> dict:
    v = manifest["version"]
    return {
        "format": wr.MANIFEST_FORMAT,
        "version": v,
        "date": manifest["date"],
        "tarball_url": f"walter-{v}.tar.gz",
        "sha256": manifest["sha256"],
        "signature_url": f"walter-{v}.tar.gz.sig",
        "manifest_url": f"manifest-{v}.json",
        "min_installer_version": manifest["min_installer_version"],
    }


def _installer_from_tarball(tarball: Path, version: str) -> bytes:
    with tarfile.open(tarball, "r:gz") as tf:
        f = tf.extractfile(f"walter-{version}/deploy/install.sh")
        if f is None:
            raise SystemExit("the release has no deploy/install.sh")
        return f.read()


def assemble(dist: Path, version: str, mirror: Path, site: Path, key: Path, pub: Path,
             keep: int = KEEP_DEFAULT, signer=None) -> list[str]:
    """Add release VERSION from DIST (make-release output) to MIRROR and write SITE.
    Returns the versions deployed (newest last)."""
    sign = signer or wr.sign
    dist, mirror, site = Path(dist), Path(mirror), Path(site)
    rel = dist / "releases" / version
    tar = rel / f"walter-{version}.tar.gz"
    man = rel / "manifest.json"
    for p in (tar, Path(str(tar) + ".sig"), man, Path(str(man) + ".sig")):
        if not p.is_file():
            raise SystemExit(f"missing build output: {p}")
    manifest = json.loads(man.read_text())
    if manifest.get("version") != version:
        raise SystemExit("manifest version mismatch")
    if tar.stat().st_size > PAGES_FILE_LIMIT:
        raise SystemExit(f"{tar.name} is over the 25 MiB Pages limit")
    # The publisher re-checks the signature with the repo's public key before anything moves.
    ok, why = wr.verify(pub, tar, str(tar) + ".sig", manifest["sha256"])
    if not ok:
        raise SystemExit(f"REFUSED: tarball {why}")
    if not wr.verify_sig(pub, man, str(man) + ".sig"):
        raise SystemExit("REFUSED: manifest signature does not verify")

    mirror.mkdir(parents=True, exist_ok=True)
    names = {tar: f"walter-{version}.tar.gz", man: f"manifest-{version}.json"}
    for src, name in names.items():
        for suffix in ("", ".sig"):
            dst = mirror / (name + suffix)
            if dst.exists() and dst.read_bytes() != Path(str(src) + suffix).read_bytes():
                raise SystemExit(f"{dst.name} already published with different bytes (versions are immutable)")
            shutil.copyfile(str(src) + suffix, dst)

    newest = mirror_versions(mirror)[-1]
    if newest == version:      # only the newest release decides latest.json and install.sh
        (mirror / "latest.json").write_text(json.dumps(build_latest(manifest), indent=2) + "\n")
        sign(key, mirror / "latest.json", mirror / "latest.json.sig")
        (mirror / "install.sh").write_bytes(_installer_from_tarball(tar, version))
        sign(key, mirror / "install.sh", mirror / "install.sh.sig")
    shutil.copyfile(pub, mirror / "release-public.pem")

    # SITE = newest KEEP releases + channel files
    if site.exists():
        shutil.rmtree(site)
    site.mkdir(parents=True)
    deployed = mirror_versions(mirror)[-max(1, keep):]
    for p in sorted(mirror.iterdir()):
        m = VERSIONED.match(p.name)
        if m and (m.group(1) or m.group(2)) not in deployed:
            continue
        if p.is_file():
            if p.stat().st_size > PAGES_FILE_LIMIT:
                raise SystemExit(f"{p.name} is over the 25 MiB Pages limit")
            shutil.copyfile(p, site / p.name)
    (site / "_headers").write_text(HEADERS)
    (site / "404.html").write_text(NOT_FOUND)
    (site / "index.html").write_text(INDEX)
    return deployed


# ---------------------------------------------------------------- verify from the internet
def verify_remote(base_url: str, pub: Path, expect: str = "", fetcher=fetch) -> dict:
    """Download the channel like a device does and check everything. Returns a summary."""
    base = base_url.rstrip("/") + "/"
    with tempfile.TemporaryDirectory() as td:
        t = Path(td)

        def get(url: str, name: str) -> Path:
            try:
                data = fetcher(url)
            except (urllib.error.URLError, OSError) as e:
                raise SystemExit(f"REMOTE CHECK FAILED: {url}: {e}")
            if data is None:
                raise SystemExit(f"REMOTE CHECK FAILED: 404 for {url}")
            (t / name).write_bytes(data)
            return t / name

        latest = get(base + "latest.json", "latest.json")
        try:
            rel = wr.read_latest(latest, base)          # same parser as deploy/update.sh
        except (ValueError, KeyError, json.JSONDecodeError) as e:
            raise SystemExit(f"REMOTE CHECK FAILED: bad latest.json ({e})")
        if expect and rel["version"] != expect:
            raise SystemExit(f"REMOTE CHECK FAILED: channel says {rel['version']}, expected {expect}")
        if not rel["tarball_url"].startswith("https://") and base.startswith("https://"):
            raise SystemExit("REMOTE CHECK FAILED: tarball URL is not https")
        get(base + "latest.json.sig", "latest.json.sig")
        if not wr.verify_sig(pub, latest, t / "latest.json.sig"):
            raise SystemExit("REMOTE CHECK FAILED: latest.json signature")
        tar = get(rel["tarball_url"], "release.tar.gz")
        get(rel["signature_url"], "release.tar.gz.sig")
        ok, why = wr.verify(pub, tar, t / "release.tar.gz.sig", rel["sha256"])   # same as the updater
        if not ok:
            raise SystemExit(f"REMOTE CHECK FAILED: tarball {why}")
        inst = get(base + "install.sh", "install.sh")
        get(base + "install.sh.sig", "install.sh.sig")
        if not wr.verify_sig(pub, inst, t / "install.sh.sig"):
            raise SystemExit("REMOTE CHECK FAILED: install.sh signature")
        if not inst.read_bytes().startswith(b"#!"):
            raise SystemExit("REMOTE CHECK FAILED: install.sh is not a script (HTML fallback?)")
        return {"version": rel["version"], "sha256": rel["sha256"], "size": tar.stat().st_size}


# ---------------------------------------------------------------- CLI
def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd, a = argv[0], argv[1:]
    if cmd == "live-version":
        print(live_version(a[0]))
    elif cmd == "check-newer":
        return 0 if check_newer(a[0], a[1] if len(a) > 1 else "") else 1
    elif cmd == "assemble":
        dist, version, mirror, site, key, pub = a[:6]
        keep = int(a[6]) if len(a) > 6 else KEEP_DEFAULT
        print(" ".join(assemble(Path(dist), version, Path(mirror), Path(site), Path(key), Path(pub), keep)))
    elif cmd == "verify-remote":
        r = verify_remote(a[0], Path(a[1]), a[2] if len(a) > 2 else "")
        print(f"verified {r['version']} from the internet: sha256 {r['sha256'][:16]}..., "
              f"{r['size']} bytes, signatures ok")
    else:
        print(f"unknown command {cmd}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
