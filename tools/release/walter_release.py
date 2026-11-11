#!/usr/bin/env python3
"""Walter release helper: signed tarballs, manifests and versioned installs.

Standard library + the `openssl` command only (root's system python3 has no
`cryptography`), so the updater can use it before any venv exists.

  walter_release.py genkey KEY PUB              new Ed25519 key (KEY mode 600, never printed)
  walter_release.py sign KEY FILE SIG           detached Ed25519 signature
  walter_release.py verify PUB FILE SIG [SHA256]  exit 0 only if signature (and sha256) match
  walter_release.py manifest ...                (used by make-release.sh)
  walter_release.py latest LATEST.json BASE_URL print version/tarball/sha256/sig as shell vars
  walter_release.py newer A B                   exit 0 if version A > version B
  walter_release.py extract TARBALL DEST        safe extract (no absolute paths, links out, devices)
  walter_release.py switch ROOT VERSION         atomic current -> VERSION, previous -> old current
  walter_release.py rollback ROOT               atomic swap of current and previous
  walter_release.py status ROOT                 current / previous / installed versions
  walter_release.py prune ROOT [KEEP]           delete releases other than current + previous

Release layout (ROOT is ~walter/.walter-releases):
  ROOT/<version>/      one unpacked release (Walter may edit his own copy)
  ROOT/venv/           shared Python venv (each release has .venv -> ../venv)
  ROOT/current  -> <version>     ~walter/walter -> .walter-releases/current
  ROOT/previous -> <version>     kept for `walterctl update rollback`
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path
from urllib.parse import urljoin

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
MANIFEST_FORMAT = 1


# ---------------------------------------------------------------- crypto
def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _openssl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["openssl", *args], capture_output=True, text=True)


def genkey(key: str | Path, pub: str | Path) -> None:
    key, pub = Path(key), Path(pub)
    if key.exists():
        raise SystemExit(f"{key} already exists; refusing to overwrite a signing key")
    key.parent.mkdir(parents=True, exist_ok=True)
    old = os.umask(0o077)
    try:
        r = _openssl("genpkey", "-algorithm", "ed25519", "-out", str(key))
    finally:
        os.umask(old)
    if r.returncode:
        raise SystemExit("openssl genpkey failed: " + r.stderr.strip())
    os.chmod(key, 0o600)
    export_public(key, pub)


def export_public(key: str | Path, pub: str | Path) -> None:
    r = _openssl("pkey", "-in", str(key), "-pubout", "-out", str(pub))
    if r.returncode:
        raise SystemExit("openssl pkey -pubout failed: " + r.stderr.strip())


def sign(key: str | Path, file: str | Path, sig: str | Path) -> None:
    r = _openssl("pkeyutl", "-sign", "-rawin", "-inkey", str(key), "-in", str(file), "-out", str(sig))
    if r.returncode:
        raise SystemExit("signing failed: " + r.stderr.strip())


def verify_sig(pub: str | Path, file: str | Path, sig: str | Path) -> bool:
    if not (Path(pub).is_file() and Path(file).is_file() and Path(sig).is_file()):
        return False
    r = _openssl("pkeyutl", "-verify", "-rawin", "-pubin", "-inkey", str(pub),
                 "-in", str(file), "-sigfile", str(sig))
    return r.returncode == 0


def verify(pub, file, sig, sha256: str | None = None) -> tuple[bool, str]:
    """(ok, reason). Checks the sha256 (if given) AND the signature."""
    if not Path(file).is_file():
        return False, "file missing"
    if sha256 is not None:
        if not re.fullmatch(r"[0-9a-f]{64}", sha256 or ""):
            return False, "bad sha256 in manifest"
        if sha256_file(file) != sha256:
            return False, "sha256 mismatch"
    if not verify_sig(pub, file, sig):
        return False, "signature does not verify"
    return True, "ok"


# ---------------------------------------------------------------- versions / manifests
def parse_version(v: str) -> tuple[int, int, int]:
    v = (v or "").strip().lstrip("v")
    if not VERSION_RE.match(v):
        raise ValueError(f"not a version: {v!r}")
    a, b, c = v.split(".")
    return int(a), int(b), int(c)


def newer(a: str, b: str | None) -> bool:
    if not b:
        return True
    try:
        bv = parse_version(b)
    except ValueError:
        return True          # unknown/legacy (e.g. a git sha): any real release is newer
    return parse_version(a) > bv


def build_manifest(version: str, tarball: str | Path, *, min_installer: int, changelog: str,
                   date: str | None = None, commit: str = "") -> dict:
    parse_version(version)
    t = Path(tarball)
    return {
        "format": MANIFEST_FORMAT,
        "version": version,
        "date": date or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "commit": commit,
        "tarball": t.name,
        "size": t.stat().st_size,
        "sha256": sha256_file(t),
        "signature": t.name + ".sig",
        "min_installer_version": int(min_installer),
        "changelog": changelog,
    }


def build_latest(manifest: dict, prefix: str = "") -> dict:
    """latest.json for the channel; URLs relative to latest.json unless prefix is absolute."""
    base = prefix.rstrip("/") + "/" if prefix else ""
    v = manifest["version"]
    return {
        "format": MANIFEST_FORMAT,
        "version": v,
        "date": manifest["date"],
        "tarball_url": f"{base}releases/{v}/{manifest['tarball']}",
        "sha256": manifest["sha256"],
        "signature_url": f"{base}releases/{v}/{manifest['signature']}",
        "manifest_url": f"{base}releases/{v}/manifest.json",
        "min_installer_version": manifest["min_installer_version"],
    }


def read_latest(path: str | Path, base_url: str) -> dict:
    d = json.loads(Path(path).read_text())
    out = {
        "version": str(d["version"]),
        "tarball_url": urljoin(base_url, str(d["tarball_url"])),
        "sha256": str(d["sha256"]).lower(),
        "signature_url": urljoin(base_url, str(d["signature_url"])),
        "min_installer_version": int(d.get("min_installer_version", 1)),
    }
    parse_version(out["version"])
    if not re.fullmatch(r"[0-9a-f]{64}", out["sha256"]):
        raise ValueError("bad sha256")
    for k in ("tarball_url", "signature_url"):
        if not out[k].startswith(("https://", "http://", "file://")):
            raise ValueError(f"bad {k}")
    return out


# ---------------------------------------------------------------- extract
def safe_extract(tarball: str | Path, dest: str | Path) -> Path:
    """Extract into DEST (must not exist). Returns the single top-level dir inside."""
    dest = Path(dest)
    dest.mkdir(parents=True)
    with tarfile.open(tarball, "r:gz") as tf:
        tops = set()
        for m in tf.getmembers():
            name = m.name
            if name.startswith("/") or ".." in Path(name).parts:
                raise ValueError(f"unsafe path in tarball: {name}")
            if not (m.isfile() or m.isdir() or m.issym()):
                raise ValueError(f"unsupported entry in tarball: {name}")
            if m.issym():
                target = (Path(name).parent / m.linkname)
                if m.linkname.startswith("/") or ".." in Path(os.path.normpath(target)).parts:
                    raise ValueError(f"symlink leaves the release: {name}")
            tops.add(Path(name).parts[0])
        if len(tops) != 1:
            raise ValueError("tarball must have exactly one top-level directory")
        tf.extractall(dest, filter="data")
    return dest / tops.pop()


# ---------------------------------------------------------------- versioned installs
def _target(link: Path) -> str | None:
    return os.readlink(link) if link.is_symlink() else None


def _atomic_link(root: Path, name: str, target: str) -> None:
    tmp = root / f".{name}.tmp-{os.getpid()}"
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    os.symlink(target, tmp)
    os.replace(tmp, root / name)       # rename(2): atomic


def status(root: str | Path) -> dict:
    root = Path(root)
    rels = sorted((p.name for p in root.iterdir() if p.is_dir() and not p.is_symlink()
                   and p.name not in ("venv",) and not p.name.startswith(".")),
                  key=_sort_key) if root.is_dir() else []
    return {"current": _target(root / "current"), "previous": _target(root / "previous"),
            "installed": rels}


def _sort_key(name: str):
    try:
        return (1, parse_version(name), name)
    except ValueError:
        return (0, (0, 0, 0), name)


def switch(root: str | Path, version: str) -> dict:
    root = Path(root)
    if not (root / version).is_dir():
        raise SystemExit(f"release {version} is not installed in {root}")
    cur = _target(root / "current")
    if cur == version:
        return status(root)
    if cur:
        _atomic_link(root, "previous", cur)
    _atomic_link(root, "current", version)
    return status(root)


def rollback(root: str | Path) -> dict:
    root = Path(root)
    cur, prev = _target(root / "current"), _target(root / "previous")
    if not prev or not (root / prev).is_dir():
        raise SystemExit("no previous release to roll back to")
    _atomic_link(root, "current", prev)
    if cur:
        _atomic_link(root, "previous", cur)
    return status(root)


def prune(root: str | Path, keep: int = 2) -> list[str]:
    """Delete releases that are neither current nor previous (keep >= 2)."""
    root = Path(root)
    st = status(root)
    protect = {st["current"], st["previous"]}
    removed = []
    for name in st["installed"]:
        if name in protect or name.endswith(".new"):
            continue
        shutil.rmtree(root / name)
        removed.append(name)
    return removed


# ---------------------------------------------------------------- CLI
def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd, a = argv[0], argv[1:]
    if cmd == "genkey":
        genkey(a[0], a[1])
        print(f"new signing key written to {a[0]} (mode 600); public key: {a[1]}")
    elif cmd == "pubkey":
        export_public(a[0], a[1])
    elif cmd == "sign":
        sign(*a[:3])
    elif cmd == "verify":
        ok, why = verify(a[0], a[1], a[2], a[3] if len(a) > 3 else None)
        print(why)
        return 0 if ok else 1
    elif cmd == "sha256":
        print(sha256_file(a[0]))
    elif cmd == "manifest":
        # manifest VERSION TARBALL MIN_INSTALLER CHANGELOG_FILE COMMIT OUT
        m = build_manifest(a[0], a[1], min_installer=int(a[2]),
                           changelog=Path(a[3]).read_text(), commit=a[4])
        Path(a[5]).write_text(json.dumps(m, indent=2) + "\n")
    elif cmd == "latest-from-manifest":
        m = json.loads(Path(a[0]).read_text())
        Path(a[1]).write_text(json.dumps(build_latest(m, a[2] if len(a) > 2 else ""), indent=2) + "\n")
    elif cmd == "latest":
        try:
            d = read_latest(a[0], a[1])
        except (ValueError, KeyError, json.JSONDecodeError) as e:
            print(f"bad latest.json: {e}", file=sys.stderr)
            return 1
        for k, v in d.items():
            print(f"REL_{k.upper()}={shlex.quote(str(v))}")
    elif cmd == "newer":
        return 0 if newer(a[0], a[1] if len(a) > 1 else None) else 1
    elif cmd == "extract":
        try:
            print(safe_extract(a[0], a[1]))
        except (ValueError, tarfile.TarError) as e:
            print(f"refusing to extract: {e}", file=sys.stderr)
            return 1
    elif cmd == "switch":
        print(json.dumps(switch(a[0], a[1])))
    elif cmd == "rollback":
        print(json.dumps(rollback(a[0])))
    elif cmd == "status":
        print(json.dumps(status(a[0]), indent=2))
    elif cmd == "prune":
        print(" ".join(prune(a[0], int(a[1]) if len(a) > 1 else 2)))
    else:
        print(f"unknown command {cmd}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
