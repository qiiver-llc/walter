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
