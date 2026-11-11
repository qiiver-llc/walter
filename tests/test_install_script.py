"""Static checks on deploy/install.sh found by the clean-VM test (tools/vmtest)."""
import re
import subprocess
from pathlib import Path

INSTALL = Path(__file__).resolve().parent.parent / "deploy" / "install.sh"


def _src():
    return INSTALL.read_text()


def test_syntax():
    subprocess.run(["bash", "-n", str(INSTALL)], check=True)


def test_firmware_not_in_core_apt_line():
    # Non-free firmware must never share an apt-get line with the core packages:
    # on a machine without non-free-firmware the whole line failed and the
    # fallback skipped cage, chromium, python3-venv and PipeWire.
    for m in re.finditer(r"apt-get install -y[^\n]*(?:\\\n[^\n]*)*", _src()):
        cmd = m.group(0)
        if "python3-venv" in cmd or "cage" in cmd:
            assert "firmware-" not in cmd, cmd


def test_firmware_is_best_effort():
    s = _src()
    assert re.search(r'for fw in firmware-iwlwifi', s)
    assert 'apt-cache show "$fw"' in s


def test_install_marks_installed_for_updater():
    # walter-update.service has ConditionPathExists=/var/lib/walter/installed;
    # the curl bootstrap must create it or beta homes never update.
    unit = (INSTALL.parent / "walter-update.service").read_text()
    assert "ConditionPathExists=/var/lib/walter/installed" in unit
    s = _src()
    assert "> /var/lib/walter/installed" in s
    assert s.index("/var/lib/walter/installed") < s.index('say "Done!"')


# ------------------------------------------------ USB stick -> signed update channel
ROOT = INSTALL.parent.parent


def _run_install(*args):
    import os
    if os.geteuid() == 0:   # never run the real installer from a test
        import pytest
        pytest.skip("runs install.sh up to its root check; not as root")
    return subprocess.run(["bash", str(INSTALL), *args], capture_output=True, text=True, timeout=30)


def test_channel_default_and_none_are_accepted_and_bad_ones_refused():
    # "default"/"none"/https pass option checks and stop at the root check (exit 1);
    # anything else is refused before that (exit 2).
    for ok in ("default", "none", "https://releases.example/"):
        r = _run_install("--channel", ok)
        assert r.returncode == 1 and "sudo" in r.stderr, (ok, r.stderr)
    for bad in ("http://releases.example/", "releases.wallter.app"):
        r = _run_install("--channel", bad)
        assert r.returncode == 2 and "https://" in r.stderr, (bad, r.stderr)


def test_channel_default_maps_to_the_bootstrap_channel():
    s = _src()
    assert 'default) UPDATE_URL="$DEFAULT_UPDATE_URL"' in s
    # the same writer the curl bootstrap relies on, root-owned
    w = s.index("printf 'WALTER_UPDATE_URL=%q\\n' \"$UPDATE_URL\" >> /etc/walter/update.conf")
    assert s.index("chown root:root /etc/walter/update.conf && chmod 644 /etc/walter/update.conf") > w


def test_usb_stick_first_boot_installs_with_the_channel():
    fb = (ROOT / "deploy" / "firstboot.sh").read_text()
    subprocess.run(["bash", "-n", str(ROOT / "deploy" / "firstboot.sh")], check=True)
    assert 'CHANNEL=default' in fb and '"$SRC/UPDATE_CHANNEL"' in fb
    assert 'deploy/install.sh" --channel "$CHANNEL"' in fb
    iso = (ROOT / "iso" / "build-iso.sh").read_text()
    subprocess.run(["bash", "-n", str(ROOT / "iso" / "build-iso.sh")], check=True)
    assert 'CHANNEL="${WALTER_ISO_CHANNEL:-default}"' in iso
    assert '> "$WORK/walter/UPDATE_CHANNEL"' in iso
    # the preseed copies the whole payload (incl. UPDATE_CHANNEL) next to firstboot.sh
    assert "cp -a /cdrom/walter /target/opt/walter-src" in (ROOT / "iso" / "preseed.cfg").read_text()


def test_vm_test_expects_a_real_channel_answer():
    q = (ROOT / "iso" / "test-in-qemu.sh").read_text()
    subprocess.run(["bash", "-n", str(ROOT / "iso" / "test-in-qemu.sh")], check=True)
    assert 'grep -E "up to date|no update source"' not in q
    assert "WALTER_UPDATE_URL=https://releases.wallter.app" in q
    assert "up to date|new release on the channel" in q
