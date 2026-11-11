"""Admin rights for Walter: the one-time setup door, the full-admin opt-in,
`walter-admin grant|revoke|status` and the USB physical-presence path.

Everything runs unprivileged against a fake root directory with fake
chpasswd/visudo/udevadm/blkid/mount/umount on PATH: no real sudo, no real
system files.
"""
from __future__ import annotations

import asyncio
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
HELPER = REPO / "deploy" / "admin-password.sh"

pytestmark = pytest.mark.skipif(os.geteuid() == 0, reason="must run unprivileged (fake root)")

FAKES = {
    # records "user:password" so tests can check it
    "chpasswd": 'cat >> "$FAKE_LOG/chpasswd"\n',
    # accepts a sudoers file only if it has the expected line and mode 0440
    "visudo": '''f="${@: -1}"
[ -n "$FAKE_VISUDO_FAIL" ] && exit 1
grep -qx 'walter ALL=(ALL) NOPASSWD: ALL' "$f" || exit 1
[ "$(stat -c %a "$f")" = 440 ] || exit 1
''',
    "udevadm": 'echo "${FAKE_SYSPATH:-/devices/pci0000:00/0000:00:14.0/usb1/1-1/1-1:1.0/host6/block/sdb/sdb1}"\n',
    "blkid": 'echo "${FAKE_FSTYPE:-vfat}"\n',
    # "mount" the fake device (a directory) by symlinking it into place
    "mount": 'dev="${@: -2:1}"; dir="${@: -1}"; rmdir "$dir" && ln -s "$dev" "$dir"\n',
    "umount": 'rm -f "$1"\n',
    "logger": ':\n',
}


class FakeRoot(type(Path())):
    bins: Path
    logs: Path


@pytest.fixture
def root(tmp_path):
    r = FakeRoot(tmp_path / "root")
    for d in ("var/lib/walter-setup", "var/lib/walter", "etc/sudoers.d", "proc", "run"):
        (r / d).mkdir(parents=True)
    (r / "proc/uptime").write_text("600.12 1000.00\n")
    bins = tmp_path / "bin"
    bins.mkdir()
    for name, body in FAKES.items():
        p = bins / name
        p.write_text("#!/usr/bin/env bash\n" + body)
        p.chmod(0o755)
    logs = tmp_path / "log"
    logs.mkdir()
    r.bins, r.logs = bins, logs
    return r


def run(root, *args, **env):
    e = dict(os.environ, WALTER_ADMIN_ROOT=str(root), PATH=f"{root.bins}:{os.environ['PATH']}",
             FAKE_LOG=str(root.logs), WALTER_ADMIN_USB_MIN_UPTIME="120")
    e.update({k: str(v) for k, v in env.items()})
    return subprocess.run(["bash", str(HELPER), *args], env=e, capture_output=True, text=True)


def sudoers(root):
    return root / "etc/sudoers.d/90-walter"


def done(root):
    return root / "var/lib/walter/admin-password-handled"


def request(root, password="", full=False):
    d = root / "var/lib/walter-setup"
    if full:
        (d / "full-admin").write_text("yes\n")
    (d / "admin-password").write_text(password)


# ------------------------------------------------------------ one-time door
def test_refuses_without_root_or_fake_root():
    e = {k: v for k, v in os.environ.items() if k != "WALTER_ADMIN_ROOT"}
    p = subprocess.run(["bash", str(HELPER), "status"], env=e, capture_output=True, text=True)
    assert p.returncode != 0 and "root" in p.stderr


def test_no_request_does_nothing(root):
    assert run(root).returncode == 0
    assert not done(root).exists() and not sudoers(root).exists()


def test_skip_everything_closes_door_full_admin_off_by_default(root):
    request(root, "")
    p = run(root)
    assert p.returncode == 0, p.stderr
    assert done(root).read_text().splitlines() == ["skipped", "full admin: no"]
    assert not sudoers(root).exists()
    assert not (root.logs / "chpasswd").exists()
    assert not (root / "var/lib/walter-setup/admin-password").exists()


def test_password_and_full_admin(root):
    request(root, "correct horse", full=True)
    p = run(root)
    assert p.returncode == 0, p.stderr
    assert (root.logs / "chpasswd").read_text() == "walter:correct horse\n"
    f = sudoers(root)
    assert "walter ALL=(ALL) NOPASSWD: ALL" in f.read_text().splitlines()
    assert stat.S_IMODE(f.stat().st_mode) == 0o440
    assert done(root).read_text().splitlines() == ["admin password set", "full admin: yes"]
    assert not any(p.name.startswith(".90-walter") for p in f.parent.iterdir())  # no temp left
    log = (root / "var/lib/walter/admin.log").read_text()
    assert "full admin granted" in log and "correct horse" not in log


def test_full_admin_without_password(root):
    request(root, "", full=True)
    assert run(root).returncode == 0
    assert sudoers(root).exists()
    assert done(root).read_text().startswith("skipped")
    assert not (root.logs / "chpasswd").exists()


def test_door_only_opens_once(root):
    request(root, "")
    run(root)
    # later, Walter (the AI) tries to give himself rights through the same door
    request(root, "attacker-pass", full=True)
    assert run(root).returncode == 0
    assert not sudoers(root).exists()
    assert not (root.logs / "chpasswd").exists()
    assert not (root / "var/lib/walter-setup/admin-password").exists()
    assert not (root / "var/lib/walter-setup/full-admin").exists()


def test_short_password_is_skipped(root):
    request(root, "short")
    run(root)
    assert done(root).read_text().startswith("skipped")
    assert not (root.logs / "chpasswd").exists()


def test_bad_sudoers_is_never_installed(root):
    request(root, "", full=True)
    p = run(root, FAKE_VISUDO_FAIL=1)
    assert p.returncode != 0
    assert not sudoers(root).exists()
    assert not any(sudoers(root).parent.iterdir())
    assert done(root).exists()  # door still closed


# ---------------------------------------------------------- root commands
def test_grant_revoke_status(root):
    assert "full admin: no" in run(root, "status").stdout
    assert "setup door: open" in run(root, "status").stdout
    assert run(root, "grant").returncode == 0
    assert sudoers(root).exists()
    assert "full admin: yes" in run(root, "status").stdout
    assert run(root, "revoke").returncode == 0
    assert not sudoers(root).exists()
    assert "full admin revoked" in (root / "var/lib/walter/admin.log").read_text()


# -------------------------------------------------- USB physical presence
def stick(tmp_path, text):
    s = tmp_path / "stick"
    s.mkdir(exist_ok=True)
    (s / "walter-admin.txt").write_text(text)
    return s


def test_usb_grant_consumes_file(root, tmp_path):
    s = stick(tmp_path, "Full admin: yes\r\n")
    p = run(root, "usb", s)
    assert p.returncode == 0, p.stderr
    assert sudoers(root).exists()
    assert not (s / "walter-admin.txt").exists()  # one use per file
    assert not list((root / "run").iterdir())  # mount point cleaned up


def test_usb_revoke_and_password(root, tmp_path):
    run(root, "grant")
    s = stick(tmp_path, "# hello\nfull admin: no\npassword: a long enough one\n")
    assert run(root, "usb", s).returncode == 0
    assert not sudoers(root).exists()
    assert (root.logs / "chpasswd").read_text() == "walter:a long enough one\n"
    assert "a long enough one" not in (root / "var/lib/walter/admin.log").read_text()


def test_usb_works_even_after_door_closed(root, tmp_path):
    request(root, "")
    run(root)
    assert run(root, "usb", stick(tmp_path, "full admin: yes\n")).returncode == 0
    assert sudoers(root).exists()


def test_usb_stick_present_at_boot_is_ignored(root, tmp_path):
    (root / "proc/uptime").write_text("40.00 80.00\n")
    s = stick(tmp_path, "full admin: yes\n")
    p = run(root, "usb", s)
    assert "present at boot" in p.stdout
    assert not sudoers(root).exists()
    assert (s / "walter-admin.txt").exists()


@pytest.mark.parametrize("syspath", ["/devices/virtual/block/loop0", "/devices/pci0000:00/ata1/host0/block/sda/sda1"])
def test_usb_ignores_non_usb_devices(root, tmp_path, syspath):
    p = run(root, "usb", stick(tmp_path, "full admin: yes\n"), FAKE_SYSPATH=syspath)
    assert "not a USB device" in p.stdout
    assert not sudoers(root).exists()


def test_usb_ignores_other_filesystems(root, tmp_path):
    p = run(root, "usb", stick(tmp_path, "full admin: yes\n"), FAKE_FSTYPE="ext4")
    assert "filesystem" in p.stdout and not sudoers(root).exists()


def test_usb_without_file_or_with_nonsense(root, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert run(root, "usb", empty).returncode == 0
    assert run(root, "usb", stick(tmp_path, "full admin: maybe\npassword: short\n")).returncode == 0
    assert not sudoers(root).exists() and not (root.logs / "chpasswd").exists()


def test_usb_symlinked_file_is_ignored(root, tmp_path):
    s = tmp_path / "stick"
    s.mkdir()
    (tmp_path / "elsewhere.txt").write_text("full admin: yes\n")
    (s / "walter-admin.txt").symlink_to(tmp_path / "elsewhere.txt")
    run(root, "usb", s)
    assert not sudoers(root).exists()


# ---------------------------------------------------- setup.py side (phone)
def _setup(monkeypatch, tmp_path):
    import walter.setup as ws
    d = tmp_path / "walter-setup"
    d.mkdir()
    monkeypatch.setattr(ws, "ADMIN_REQUEST_DIR", d)
    s = ws.Setup.__new__(ws.Setup)
    s.needed, s.error = True, None
    return ws, s, d


def test_setup_request_files(monkeypatch, tmp_path):
    ws, s, d = _setup(monkeypatch, tmp_path)
    s.request_admin_password("pw123456", full_admin=True)
    assert (d / "admin-password").read_text() == "pw123456"
    assert (d / "full-admin").read_text() == "yes\n"
    assert stat.S_IMODE((d / "admin-password").stat().st_mode) == 0o600
    assert sorted(p.name for p in d.iterdir()) == ["admin-password", "full-admin"]  # no temp files
    # a later empty request never carries an old opt-in along
    s.request_admin_password("")
    assert not (d / "full-admin").exists()


def test_phone_code_passes_full_admin_only_when_true(monkeypatch, tmp_path):
    ws, s, d = _setup(monkeypatch, tmp_path)
    got = []

    async def ok():
        return True, "Signed in."

    async def finish(pw="", full_admin=False):
        got.append((pw, full_admin))

    s.verify, s.finish = ok, finish

    async def go():
        for body in ({"admin_password": "pw123456", "full_admin": True},
                     {"full_admin": "yes"},  # only a real JSON true counts
                     {}):
            r = await s.handle("verify", body)
            assert json.loads(r.body)["ok"]
        await asyncio.sleep(0)

    asyncio.run(go())
    assert got == [("pw123456", True), ("", False), ("", False)]


def test_phone_page_has_opt_in_default_off():
    html = (REPO / "face" / "setup-phone.html").read_text()
    assert 'id="fulladmin" type="checkbox">' in html  # no "checked"
    assert "passwordless sudo" in html and "/etc/sudoers.d/90-walter" in html
    assert "full_admin: fullAdmin" in html


def test_installer_installs_helpers():
    sh = (REPO / "deploy" / "install.sh").read_text()
    for f in ("walter-admin-usb@.service", "90-walter-admin-usb.rules", "/usr/local/sbin/walter-admin"):
        assert f in sh
    assert "NOPASSWD: ALL" not in sh  # never granted by the installer itself
