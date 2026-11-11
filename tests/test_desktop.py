"""Desktop face launcher (desktop/): per-OS config paths, browser commands, autostart files
(rendered text only, every OS), doctor checks with fakes, launcher settings. Nothing here
opens a browser, touches systemd/launchd/the registry, or uses the real home folder."""
import json
import sys
import urllib.error
from pathlib import PurePosixPath, PureWindowsPath

import pytest

from desktop import autostart as AS
from desktop import browser as B
from desktop import doctor as D
from desktop import launcher as L
from desktop import paths as P


# ------------------------------------------------------------------ paths
def test_os_name():
    assert P.os_name("linux") == "linux"
    assert P.os_name("darwin") == "mac"
    assert P.os_name("win32") == "windows"
    assert P.os_name("cygwin") == "windows"
    assert P.os_name("freebsd13") == "linux"


def test_config_dir_per_os():
    assert str(P.config_dir("linux", {}, "/home/u")) == "/home/u/.config/walter-desktop"
    assert str(P.config_dir("linux", {"XDG_CONFIG_HOME": "/x/cfg"}, "/home/u")) == "/x/cfg/walter-desktop"
    # a relative XDG_CONFIG_HOME is ignored (XDG spec)
    assert str(P.config_dir("linux", {"XDG_CONFIG_HOME": "rel"}, "/home/u")) == "/home/u/.config/walter-desktop"
    assert str(P.config_dir("mac", {}, "/Users/u")) == "/Users/u/Library/Application Support/walter-desktop"
    w = P.config_dir("windows", {"APPDATA": r"C:\Users\u\AppData\Roaming"}, r"C:\Users\u")
    assert str(w) == r"C:\Users\u\AppData\Roaming\walter-desktop"
    w2 = P.config_dir("windows", {}, r"C:\Users\u")
    assert str(w2) == r"C:\Users\u\AppData\Roaming\walter-desktop"


def test_config_dir_override_and_private(tmp_path):
    d = P.config_dir(P.os_name(), {"WALTER_DESKTOP_HOME": str(tmp_path / "wd")}, "/nope")
    assert str(d) == str(tmp_path / "wd")
    P.ensure_dir(d)
    if sys.platform != "win32":
        assert oct((tmp_path / "wd").stat().st_mode & 0o777) == "0o700"
    assert P.config_file(d).name == "desktop.json"
    assert P.hands_file(d).name == "hands.json"


# ------------------------------------------------------------------ browser
def test_browser_find_linux_prefers_chromium():
    have = {"google-chrome": "/usr/bin/google-chrome", "firefox": "/usr/bin/firefox"}
    assert B.find("linux", which=have.get) == ("/usr/bin/google-chrome", "chrome")
    assert B.find("linux", which={"firefox": "/usr/bin/firefox"}.get) == ("/usr/bin/firefox", "firefox")
    assert B.find("linux", which=lambda n: None) is None


def test_browser_find_mac_and_windows():
    assert B.find("mac", exists=lambda p: p == "/Applications/Safari.app") == ("Safari", "safari")
    assert B.find("mac", exists=lambda p: p.endswith("Google Chrome.app"))[1] == "chrome"
    env = {"ProgramFiles(x86)": r"C:\PF86"}
    got = B.find("windows", which=lambda n: None, exists=lambda p: "msedge" in p, env=env)
    assert got[0].endswith(r"msedge.exe") and got[1] == "chrome"


def test_browser_commands():
    url = "http://10.0.0.2:8765/face/?token=abc"
    c = B.command("linux", ("/usr/bin/chromium", "chrome"), url, "/p", "kiosk")
    assert c[0] == "/usr/bin/chromium" and "--kiosk" in c and c[-1] == url and "--user-data-dir=/p" in c
    c = B.command("linux", ("/usr/bin/chromium", "chrome"), url, "/p", "app")
    assert f"--app={url}" in c and "--kiosk" not in c
    c = B.command("linux", ("/usr/bin/chromium", "chrome"), url, "/p", "fullscreen")
    assert "--start-fullscreen" in c
    c = B.command("mac", ("Google Chrome", "chrome"), url, "/p", "app")
    assert c[:5] == ["open", "-n", "-a", "Google Chrome", "--args"]
    assert B.command("mac", ("Safari", "safari"), url, "/p", "kiosk") == ["open", "-a", "Safari", url]
    assert B.command("linux", ("/usr/bin/firefox", "firefox"), url, "/p", "kiosk") == ["/usr/bin/firefox", "--kiosk", url]
    assert B.command("linux", None, url, "/p") is None


def test_open_face_uses_own_profile(tmp_path):
    seen = {}

    def spawn(cmd, **kw):
        seen["cmd"] = cmd
        return "proc"
    r = B.open_face("linux", "http://x/face/", tmp_path / "prof", "app", spawn=spawn,
                    which={"chromium": "/usr/bin/chromium"}.get)
    assert r == "proc" and (tmp_path / "prof").is_dir()
    assert f"--user-data-dir={tmp_path / 'prof'}" in seen["cmd"]


# ------------------------------------------------------------------ autostart
ARGV = ["/opt/walter desktop/venv/bin/python", "-m", "desktop", "run"]


def test_systemd_unit():
    t = AS.render("linux", ARGV, systemd=True, env={"PYTHONPATH": "/opt/walter desktop"})
    assert "[Service]" in t and "WantedBy=graphical-session.target" in t
    assert 'ExecStart="/opt/walter desktop/venv/bin/python" -m desktop run' in t
    assert 'Environment="PYTHONPATH=/opt/walter desktop"' in t
    assert "Restart=on-failure" in t


def test_xdg_desktop_fallback():
    t = AS.render("linux", ARGV, systemd=False)
    assert t.startswith("[Desktop Entry]") and "Exec=\"/opt/walter desktop/venv/bin/python\" -m desktop run" in t


def test_launchd_plist_escapes():
    t = AS.render("mac", ["/Apps/W&W/python", "-m", "desktop", "run"], "/Users/u/Library/Logs/w")
    assert "<string>/Apps/W&amp;W/python</string>" in t
    assert f"<string>{AS.LABEL}</string>" in t and "<key>RunAtLoad</key>" in t
    import xml.dom.minidom
    xml.dom.minidom.parseString(t.encode())   # well-formed


def test_windows_startup_and_reg():
    argv = [r"C:\Program Files\Walter\walter-desktop.exe", "run"]
    t = AS.render("windows", argv)
    assert t.startswith("@echo off\r\n") and r'start "" /min "C:\Program Files\Walter\walter-desktop.exe" run' in t
    reg = AS.render_windows_reg(argv)
    assert "CurrentVersion\\Run]" in reg and '"WalterDesktop"=' in reg
    assert r'\"C:\\Program Files\\Walter\\walter-desktop.exe\" run' in reg


def test_autostart_targets():
    assert AS.target("linux", "/home/u", {}) == PurePosixPath("/home/u/.config/systemd/user/walter-desktop.service")
    assert AS.target("linux", "/home/u", {}, systemd=False) == PurePosixPath("/home/u/.config/autostart/walter-desktop.desktop")
    assert AS.target("mac", "/Users/u", {}) == PurePosixPath(f"/Users/u/Library/LaunchAgents/{AS.LABEL}.plist")
    assert AS.target("windows", r"C:\Users\u", {"APPDATA": r"C:\Users\u\AppData\Roaming"}) == PureWindowsPath(
        r"C:\Users\u\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Startup\walter-desktop.cmd")


def test_has_systemd_user_fakes():
    class R:
        def __init__(self, out):
            self.stdout = out
    assert AS.has_systemd_user(which=lambda n: "/bin/systemctl", run=lambda *a, **k: R("running\n"))
    assert AS.has_systemd_user(which=lambda n: "/bin/systemctl", run=lambda *a, **k: R("degraded\n"))
    assert not AS.has_systemd_user(which=lambda n: "/bin/systemctl", run=lambda *a, **k: R("offline\n"))
    assert not AS.has_systemd_user(which=lambda n: None)


# ------------------------------------------------------------------ doctor
def test_doctor_python():
    assert D.check_python((3, 13, 0)).ok is True
    c = D.check_python((3, 8, 1))
    assert c.ok is False and "3.10" in c.fix


def test_doctor_audio_with_devices():
    devs = [{"max_input_channels": 1, "max_output_channels": 0}, {"max_input_channels": 0, "max_output_channels": 2}]
    mic, spk = D.check_audio("linux", devices=lambda: devs)
    assert mic.ok and spk.ok
    mic, spk = D.check_audio("linux", devices=lambda: [{"max_input_channels": 0, "max_output_channels": 2}])
    assert mic.ok is False and "mic" in mic.fix.lower()


def test_doctor_audio_without_sounddevice():
    def boom():
        raise ImportError("no sounddevice")
    mic, spk = D.check_audio("mac", devices=boom)
    assert mic.ok is None and "sounddevice" in mic.fix
    assert spk.ok is None


def test_doctor_paired_and_brain():
    assert D.check_paired({}).ok is False
    st = {"brain": "http://10.0.0.2:8765", "token": "t", "face": {"name": "Office"}}
    assert D.check_paired(st).ok is True

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"{}"
    seen = {}

    def ok(req, timeout):
        seen["auth"] = req.headers.get("Authorization")
        return Resp()
    assert D.check_brain(st, opener=ok).ok is True and seen["auth"] == "Bearer t"

    def http(code):
        def f(req, timeout):
            raise urllib.error.HTTPError(req.full_url, code, "x", {}, None)
        return f
    assert "pair again" in D.check_brain(st, opener=http(401)).fix
    assert "home network" in D.check_brain(st, opener=http(403)).fix

    def down(req, timeout):
        raise urllib.error.URLError("refused")
    c = D.check_brain(st, opener=down)
    assert c.ok is False and "Wi-Fi" in c.fix
    assert D.check_brain({}).ok is None


def test_doctor_mdns_browser_hands():
    assert D.check_mdns(importer=lambda n: None).ok is True

    def no(n):
        raise ImportError(n)
    assert D.check_mdns(importer=no).ok is None
    assert D.check_browser("linux", which={"chromium": "/usr/bin/chromium"}.get).ok is True
    assert D.check_browser("linux", which=lambda n: None).ok is None
    h = D.check_hands("linux", which=lambda n: "/usr/bin/" + n if n in ("xdotool", "scrot") else None,
                      env={"XDG_SESSION_TYPE": "x11"})
    assert h.ok is True
    h = D.check_hands("linux", which=lambda n: None, env={})
    assert h.ok is None and "xdotool" in h.fix


def test_doctor_report_counts_failures():
    text, rc = D.report([D.Check("A", True, "fine"), D.Check("B", None, "meh", "do x")])
    assert rc == 0 and "All good" in text and "fix: do x" in text
    text, rc = D.report([D.Check("A", False, "broken", "fix it")])
    assert rc == 1 and "[FAIL] A: broken" in text


def test_doctor_run_all_with_fakes():
    checks = D.run_all("linux", {}, version=(3, 12, 0), devices=lambda: [], which=lambda n: None,
                       importer=lambda n: None, env={})
    names = [c.name for c in checks]
    assert names[:2] == ["Python", "Microphone"] and "Paired" in names and "Hands tools" in names


# ------------------------------------------------------------------ launcher
def test_settings_roundtrip(tmp_path):
    assert L.load_settings(tmp_path) == L.DEFAULTS
    L.save_settings(tmp_path, {**L.DEFAULTS, "mode": "kiosk", "junk": 1})
    s = L.load_settings(tmp_path)
    assert s["mode"] == "kiosk" and "junk" not in s
    assert "junk" not in json.loads((tmp_path / "desktop.json").read_text())


def test_self_argv_not_frozen():
    a = L.self_argv("run")
    assert a[1:] == ["-m", "desktop", "run"]


def test_hands_cmd_edits_local_policy(tmp_path, capsys):
    from walter import hands as H
    proj = tmp_path / "proj"
    proj.mkdir()
    assert L.hands_cmd(tmp_path, "linux", ["allow-folder", str(proj)]) == 0
    assert L.hands_cmd(tmp_path, "linux", ["allow-folder", "/"]) == 1           # too broad
    assert L.hands_cmd(tmp_path, "linux", ["allow-app", "Firefox", "firefox"]) == 0
    assert L.hands_cmd(tmp_path, "linux", ["commands", "on"]) == 0
    assert L.hands_cmd(tmp_path, "linux", ["allow-command", "git", "status"]) == 0
    pol = H.load_policy(tmp_path / "hands.json")
    assert pol.allowed_folders == [str(proj.resolve())] and pol.apps == {"firefox": "firefox"}
    assert pol.run_command and pol.commands == ["git status"] and not pol.enabled
    assert oct((tmp_path / "hands.json").stat().st_mode & 0o777) == "0o600"
    L.hands_cmd(tmp_path, "linux", ["stop"])
    assert (tmp_path / "hands.stop").exists()
    L.hands_cmd(tmp_path, "linux", ["start"])
    assert not (tmp_path / "hands.stop").exists()


def test_main_requires_pairing(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WALTER_DESKTOP_HOME", str(tmp_path / "wd"))
    assert L.main(["run", "--no-screen"]) == 1
    assert "pair first" in capsys.readouterr().err


@pytest.mark.parametrize("osn", ["linux", "mac", "windows"])
def test_every_os_renders(osn):
    assert AS.render(osn, ["walter-desktop", "run"], "/logs")
