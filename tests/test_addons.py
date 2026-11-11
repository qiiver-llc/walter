"""Add-ons (walter/addons.py): manifest v1, pinned installs from a fake GitHub (catalog + any repo),
sha256 check, safe unpacking, walled rule, SKILL.md cap, kill switch, secrets, settings, MCP."""
import hashlib
import io
import json
import os
import tarfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from walter import addons as A

COMMIT = "a" * 40
COMMIT2 = "b" * 40

TEAK_TOML = """\
id = "boxes"
name = "Cloud boxes"
version = "0.1.0"
description = "Run background workers in cloud boxes."
author = "Qiiver LLC"
homepage = "https://example.com"
min_walter = "0.0.1"
permissions = ["network", "run_commands", "code_leaves_machine"]
[[settings]]
key = "default_on"
label = "Use it by default"
type = "bool"
default = false
[[settings]]
key = "size"
label = "Box size"
type = "choice"
choices = ["small", "large"]
default = "small"
[[secrets]]
key = "token"
label = "Service token"
required = true
[cli]
status = "bin/status.sh"
[mcp]
command = "bin/server.py"
args = ["--dir", "${ADDON_DIR}"]
"""

LOCAL_TOML = """\
id = "notes"
name = "Notes helper"
version = "1.0.0"
description = "Keeps notes tidy."
permissions = []
"""


def addon_files(toml=TEAK_TOML, skill="Use the boxes for repos that don't need the wall.\n"):
    return {"addon.toml": toml, "SKILL.md": skill, "README.md": "# docs\n",
            "bin/status.sh": ("#!/bin/sh\necho status:$WALTER_ADDON_ID:$1\n", 0o755),
            "bin/server.py": ("print('mcp')\n", 0o755)}


def write_tree(root: Path, files: dict):
    for rel, v in files.items():
        data, mode = (v if isinstance(v, tuple) else (v, 0o644))
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(data)
        os.chmod(p, mode)


def targz(top: str, files: dict, extra=None) -> bytes:
    """A GitHub-style archive: everything under '<top>/'. `extra(tf)` may add hostile entries."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        d = tarfile.TarInfo(top)
        d.type = tarfile.DIRTYPE
        d.mode = 0o755
        tf.addfile(d)
        for rel, v in sorted(files.items()):
            data, mode = (v if isinstance(v, tuple) else (v, 0o644))
            b = data.encode() if isinstance(data, str) else data
            ti = tarfile.TarInfo(f"{top}/{rel}")
            ti.size, ti.mode, ti.mtime = len(b), mode, int(time.time())
            tf.addfile(ti, io.BytesIO(b))
        if extra:
            extra(tf)
    return buf.getvalue()


class FakeGitHub:
    """repos: {"o/r": {"refs": {"main": sha, "HEAD": sha}, "commits": {sha: (files, extra)}}}"""

    def __init__(self):
        self.repos = {}
        self.hits = []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def send(self, code, body: bytes, ctype="application/octet-stream"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                fake.hits.append(self.path)
                parts = self.path.split("/")[1:]
                try:
                    if parts[0] == "api" and parts[1] == "repos" and parts[4] == "commits":
                        repo = fake.repos[parts[2] + "/" + parts[3]]
                        return self.send(200, repo["refs"][parts[5]].encode(), "text/plain")
                    if parts[0] == "raw":
                        repo = fake.repos[parts[1] + "/" + parts[2]]
                        files, _ = repo["commits"][parts[3]]
                        v = files["/".join(parts[4:])]
                        return self.send(200, (v[0] if isinstance(v, tuple) else v).encode())
                    if parts[0] == "codeload":
                        name = parts[1] + "/" + parts[2]
                        files, extra = fake.repos[name]["commits"][parts[4]]
                        return self.send(200, targz(f"{parts[2]}-{parts[4]}", files, extra))
                    if parts[0] == "index.json":
                        repo = fake.repos["qiiver-llc/walter-addons"]
                        files, _ = repo["commits"][repo["refs"]["main"]]
                        return self.send(200, files["index.json"].encode(), "application/json")
                except (KeyError, IndexError):
                    pass
                self.send(404, b"not found")

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    def hub(self):
        return A.Hub(api_base=self.base + "/api", raw_base=self.base + "/raw", codeload_base=self.base + "/codeload")

    def close(self):
        self.srv.shutdown()


def sha_of(files: dict, tmp: Path) -> str:
    d = tmp / f"sha-{len(list(tmp.iterdir()))}"
    write_tree(d, files)
    return A.pack_sha256(d)


def catalog_repo(tmp: Path, entries_files: dict, sha_override=None, commit=COMMIT, extra=None):
    """The catalog repo at one commit: addons/<id>/... + index.json with real sha256s."""
    files, index = {}, {"format": 1, "addons": []}
    for aid, af in entries_files.items():
        for rel, v in af.items():
            files[f"addons/{aid}/{rel}"] = v
        import tomllib
        m = tomllib.loads(af["addon.toml"])
        index["addons"].append({"id": aid, "name": m["name"], "version": m["version"],
                                "description": m["description"], "permissions": m["permissions"],
                                "path": f"addons/{aid}",
                                "sha256": (sha_override or {}).get(aid) or sha_of(af, tmp)})
    files["index.json"] = json.dumps(index)
    return files, extra


@pytest.fixture
def gh():
    g = FakeGitHub()
    yield g
    g.close()


@pytest.fixture
def store(tmp_path, gh):
    data = tmp_path / "data"
    data.mkdir()
    return A.Addons(data, catalog_url=gh.base + "/index.json", repo="qiiver-llc/walter-addons",
                    branch="main", hub=gh.hub())


def put_catalog(gh, tmp_path, entries, commit=COMMIT, **kw):
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    files, extra = catalog_repo(work, entries, commit=commit, **kw)
    r = gh.repos.setdefault("qiiver-llc/walter-addons", {"refs": {}, "commits": {}})
    r["refs"]["main"] = commit
    r["commits"][commit] = (files, extra)


# ------------------------------------------------------------------ manifest
def test_manifest_valid_and_normalised(tmp_path):
    import tomllib
    m, errs = A.validate_manifest(tomllib.loads(TEAK_TOML))
    assert errs == []
    assert m["id"] == "boxes" and m["permissions"] == ["code_leaves_machine", "network", "run_commands"]
    assert [s["key"] for s in m["settings"]] == ["default_on", "size"]
    assert m["secrets"] == [{"key": "token", "label": "Service token", "required": True}]
    assert m["cli"] == {"status": "bin/status.sh"} and m["mcp"]["command"] == "bin/server.py"


@pytest.mark.parametrize("patch,needle", [
    ({"id": "Bad_ID"}, "id must be"), ({"id": "x"}, "id must be"), ({"id": "../etc"}, "id must be"),
    ({"permissions": ["network", "root"]}, "unknown permission"),
    ({"version": "one"}, "version"), ({"name": ""}, "name"),
    ({"settings": [{"key": "a", "type": "colour"}]}, "type must be"),
    ({"settings": [{"key": "a", "type": "choice"}]}, "choice needs"),
    ({"settings": [{"key": "a", "type": "number", "default": "x"}]}, "default"),
    ({"cli": {"evil": "../../bin/sh"}}, "[cli]"), ({"cli": {"evil": "/bin/sh"}}, "[cli]"),
    ({"homepage": "javascript:alert(1)"}, "homepage"),
])
def test_manifest_rejects(patch, needle):
    raw = {"id": "ok-id", "name": "Ok", "version": "1.0.0", "description": "d", "permissions": []}
    raw.update(patch)
    _, errs = A.validate_manifest(raw)
    assert any(needle in e for e in errs), errs


def test_manifest_folder_needs_skill_and_scripts(tmp_path):
    write_tree(tmp_path, {"addon.toml": TEAK_TOML})
    _, errs = A.read_manifest(tmp_path)
    assert "SKILL.md is missing" in errs and any("[cli] status" in e for e in errs)


def test_settings_coerce():
    b = {"type": "bool"}
    assert A.coerce(b, "on") is True and A.coerce(b, "0") is False
    n = {"type": "number", "min": 1, "max": 5}
    assert A.coerce(n, "3") == 3 and A.coerce(n, "2.5") == 2.5
    for bad in ("9", "x", "nan"):
        with pytest.raises(A.AddonError):
            A.coerce(n, bad)
    with pytest.raises(A.AddonError):
        A.coerce({"type": "choice", "choices": ["a"]}, "b")


# ------------------------------------------------------------------ pack (the catalog sha256)
def test_pack_is_deterministic(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    write_tree(a, addon_files())
    time.sleep(0.01)
    write_tree(b, dict(reversed(list(addon_files().items()))))
    os.utime(b / "SKILL.md", (1, 1))
    assert A.pack_sha256(a) == A.pack_sha256(b)
    (b / A.SOURCE).write_text("{}")          # the install record is not part of it
    (b / "bin" / "__pycache__").mkdir()
    (b / "bin" / "__pycache__" / "x.pyc").write_text("junk")
    (b / "bin" / "y.pyc").write_text("junk")
    (b / ".DS_Store").write_text("junk")
    assert A.pack_sha256(a) == A.pack_sha256(b)
    (b / "SKILL.md").write_text("changed")
    assert A.pack_sha256(a) != A.pack_sha256(b)
    os.symlink("/etc/passwd", a / "link")
    with pytest.raises(A.AddonError):
        A.pack(a)


# ------------------------------------------------------------------ catalog install
def test_catalog_lists_and_installs_pinned(store, gh, tmp_path):
    put_catalog(gh, tmp_path, {"boxes": addon_files()})
    cat = store.catalog()["addons"]
    assert [e["id"] for e in cat] == ["boxes"] and not cat[0]["installed"]
    assert any(p["danger"] for p in cat[0]["permissions"] if p["id"] == "code_leaves_machine")
    d = store.install("boxes")
    assert d["on"] is False and d["official"] is True
    src = json.loads((store.dir / "boxes" / A.SOURCE).read_text())
    assert src["commit"] == COMMIT and src["repo"] == "qiiver-llc/walter-addons" and len(src["sha256"]) == 64
    assert os.access(store.dir / "boxes" / "bin" / "status.sh", os.X_OK)
    assert any(f"/codeload/qiiver-llc/walter-addons/tar.gz/{COMMIT}" == h for h in gh.hits)
    assert not store.state()["enabled"]                  # OFF by default
    with pytest.raises(A.AddonError) as e:
        store.install("boxes")
    assert e.value.code == "exists"
    # the catalog moves on: update available, never installed by itself
    files = addon_files(TEAK_TOML.replace('version = "0.1.0"', 'version = "0.2.0"'))
    put_catalog(gh, tmp_path, {"boxes": files}, commit=COMMIT2)
    assert store.catalog()["addons"][0]["update"] is True
    assert store.installed("boxes")["version"] == "0.1.0"
    assert store.check_updates() == [{"id": "boxes", "from": "0.1.0", "to": "0.2.0"}]
    store.enable("boxes")
    d = store.update("boxes")
    assert d["version"] == "0.2.0" and d["updated"] and store.is_on("boxes")      # same permissions: stays on
    assert json.loads((store.dir / "boxes" / A.SOURCE).read_text())["commit"] == COMMIT2


def test_sha_mismatch_is_refused(store, gh, tmp_path):
    put_catalog(gh, tmp_path, {"boxes": addon_files()}, sha_override={"boxes": "0" * 64})
    with pytest.raises(A.AddonError) as e:
        store.install("boxes")
    assert e.value.code == "sha_mismatch"
    assert store.installed("boxes") is None
    assert not [p for p in store.dir.iterdir()]          # no staging left behind


def test_catalog_permissions_must_match(store, gh, tmp_path):
    put_catalog(gh, tmp_path, {"boxes": addon_files()})
    files, extra = gh.repos["qiiver-llc/walter-addons"]["commits"][COMMIT]
    idx = json.loads(files["index.json"])
    idx["addons"][0]["permissions"] = ["network"]       # the catalog hides one
    files["index.json"] = json.dumps(idx)
    with pytest.raises(A.AddonError) as e:
        store.install("boxes")
    assert "permissions" in str(e.value)


# ------------------------------------------------------------------ safe unpacking
def _hostile(kind):
    def add(tf):
        top = tf.getmembers()[0].name
        if kind == "dotdot":
            b = b"pwned"
            ti = tarfile.TarInfo(f"{top}/addons/boxes/../../../../evil.txt")
            ti.size = len(b)
            tf.addfile(ti, io.BytesIO(b))
        elif kind == "symlink":
            ti = tarfile.TarInfo(f"{top}/addons/boxes/escape")
            ti.type, ti.linkname = tarfile.SYMTYPE, "/etc/passwd"
            tf.addfile(ti)
        elif kind == "hardlink":
            ti = tarfile.TarInfo(f"{top}/addons/boxes/hard")
            ti.type, ti.linkname = tarfile.LNKTYPE, "/etc/passwd"
            tf.addfile(ti)
        elif kind == "absolute":
            b = b"x"
            ti = tarfile.TarInfo("/tmp/walter-addon-abs")
            ti.size = 1
            tf.addfile(ti, io.BytesIO(b))
        elif kind == "huge":
            b = b"0" * (A.ADDON_FILE_CAP + 1)
            ti = tarfile.TarInfo(f"{top}/addons/boxes/big.bin")
            ti.size = len(b)
            tf.addfile(ti, io.BytesIO(b))
    return add


@pytest.mark.parametrize("kind,code", [("dotdot", "traversal"), ("symlink", "link"), ("hardlink", "link"),
                                       ("absolute", "traversal"), ("huge", "too_big")])
def test_hostile_archives_are_refused(store, gh, tmp_path, kind, code):
    put_catalog(gh, tmp_path, {"boxes": addon_files()}, extra=_hostile(kind))
    with pytest.raises(A.AddonError) as e:
        store.install("boxes")
    assert e.value.code == code
    assert store.installed("boxes") is None
    assert not (tmp_path / "evil.txt").exists() and not (tmp_path / "data" / "evil.txt").exists()
    assert not [p for p in store.dir.iterdir()]


def test_safe_extract_subfolder_only(tmp_path):
    arc = targz("repo-x", {"addons/a/x.txt": "a", "addons/b/y.txt": "b", "README.md": "r"})
    n = A.safe_extract(arc, "addons/a", tmp_path / "out")
    assert n == 1 and (tmp_path / "out" / "x.txt").read_text() == "a"
    with pytest.raises(A.AddonError):
        A.safe_extract(arc, "../x", tmp_path / "out2")


@pytest.mark.parametrize("url,expect", [
    ("https://github.com/someone/my-addon", ("someone/my-addon", "", "")),
    ("someone/my-addon", ("someone/my-addon", "", "")),
    ("https://github.com/someone/repo.git", ("someone/repo", "", "")),
    ("https://github.com/someone/repo/tree/main/addons/x", ("someone/repo", "main", "addons/x")),
    ("https://github.com/someone/repo/blob/dev/x/addon.toml", ("someone/repo", "dev", "x")),
])
def test_parse_repo_url(url, expect):
    assert A.parse_repo_url(url) == expect


@pytest.mark.parametrize("url", ["https://gitlab.com/a/b", "https://github.com/a", "file:///etc",
                                 "https://github.com/a/b/tree/main/../../x", "https://github.com/a/b/issues",
                                 "", "x" * 500])
def test_parse_repo_url_rejects(url):
    with pytest.raises(A.AddonError):
        A.parse_repo_url(url)


# ------------------------------------------------------------------ any public repo
def test_add_from_repo_subfolder(store, gh, tmp_path):
    files = {f"addons/notes/{k}": v for k, v in addon_files(LOCAL_TOML).items() if k != "bin/server.py"}
    files["addons/notes/addon.toml"] = LOCAL_TOML
    gh.repos["someone/stuff"] = {"refs": {"main": COMMIT2}, "commits": {COMMIT2: (files, None)}}
    p = store.preview_url("https://github.com/someone/stuff/tree/main/addons/notes")
    assert p["id"] == "notes" and p["commit"] == COMMIT2 and p["official"] is False
    assert "Not made by Qiiver" in p["warning"]
    d = store.install_url("https://github.com/someone/stuff/tree/main/addons/notes", p["commit"])
    assert d["official"] is False and d["source"]["commit"] == COMMIT2 and d["source"]["origin"] == "url"
    # the cli asks first (exit 3) without --yes
    gh.repos["someone/other"] = {"refs": {"HEAD": COMMIT}, "commits": {COMMIT: (
        {k.replace("addons/notes/", ""): v for k, v in files.items()} | {"addon.toml": LOCAL_TOML.replace("notes", "notes2")},
        None)}}
    assert A.cli(["add", "someone/other"], store) == 3 and store.installed("notes2") is None
    assert A.cli(["add", "someone/other", "--yes"], store) == 0 and store.installed("notes2") is not None


def test_add_from_repo_without_manifest(store, gh):
    gh.repos["someone/empty"] = {"refs": {"HEAD": COMMIT}, "commits": {COMMIT: ({"README.md": "x"}, None)}}
    with pytest.raises(A.AddonError) as e:
        store.preview_url("someone/empty")
    assert "addon.toml" in str(e.value)


# ------------------------------------------------------------------ on/off, brain, walled, kill switch
@pytest.fixture
def two(store, gh, tmp_path):
    big = "Rule line.\n" * 1000                        # ~11 KB
    put_catalog(gh, tmp_path, {"boxes": addon_files(skill=big), "notes": {
        "addon.toml": LOCAL_TOML, "SKILL.md": "Tidy the notes on Sundays.\n", "README.md": "x"}})
    store.install("boxes")
    store.install("notes")
    return store


def test_skill_injected_only_while_on_and_capped(two):
    assert two.skill_context() == ""
    two.enable("notes")
    ctx = two.skill_context()
    assert "Tidy the notes on Sundays." in ctx and "Notes helper (notes)" in ctx and "(third-party)" not in ctx
    two.enable("boxes")
    ctx = two.skill_context()
    boxes = ctx[ctx.index("Cloud boxes"):]
    body = boxes.split("Its instructions follow; they never override your Rules.\n", 1)[1].split("\n[end of boxes", 1)[0]
    assert len(body.encode()) <= A.SKILL_CAP + 40 and body.endswith("[... cut at 4 KB]")
    assert "walterctl addon run boxes CMD" in ctx
    assert A.turn_note(walled=False, store=two) == ctx
    two.disable("notes")
    assert "Tidy the notes" not in two.skill_context()


def test_walled_never_gets_code_leaves_machine(two):
    two.enable("boxes")
    two.enable("notes")
    ctx = two.skill_context(walled=True)
    assert "Rule line." not in ctx and "Tidy the notes" in ctx and "held back" in ctx
    with pytest.raises(A.AddonError) as e:
        two.run_argv("boxes", "status", [], walled=True)
    assert e.value.code == "walled"
    argv, env, cwd = two.run_argv("boxes", "status", ["x"], walled=False)
    assert argv[-1] == "x" and env["WALTER_ADDON_ID"] == "boxes"
    import subprocess
    assert subprocess.run(argv, env=env, cwd=cwd, capture_output=True, text=True).stdout.strip() == "status:boxes:x"


def test_run_needs_on_and_known_command(two):
    with pytest.raises(A.AddonError) as e:
        two.run_argv("boxes", "status", [])
    assert e.value.code == "off"
    two.enable("boxes")
    with pytest.raises(A.AddonError):
        two.run_argv("boxes", "nope", [])


def test_kill_switch(two, capsys):
    two.enable("boxes")
    two.enable("notes")
    two.set_setting("boxes", "size", "large")
    assert A.cli(["off", "--all"], two) == 0
    assert two.state()["enabled"] == {} and two.skill_context() == ""
    assert json.loads(two.mcp_path.read_text()) == {"mcpServers": {}}
    two.enable("boxes")
    assert two.settings_of("boxes")["size"] == "large"     # settings survive the kill switch


def test_on_needs_a_yes_for_permissions(two):
    assert A.cli(["on", "boxes"], two) == 3 and not two.is_on("boxes")
    assert A.cli(["on", "boxes", "--yes"], two) == 0 and two.is_on("boxes")
    assert A.cli(["on", "notes"], two) == 0                # no permissions: no question


def test_mcp_servers_only_while_on(two):
    assert A.mcp_config_arg(two) == []
    two.enable("boxes")
    arg = A.mcp_config_arg(two)
    assert arg[0] == "--mcp-config"
    cfg = json.loads(Path(arg[1]).read_text())["mcpServers"]["addon-boxes"]
    base = str(two.dir / "boxes")
    assert cfg["command"] == base + "/bin/server.py" and cfg["args"] == ["--dir", base]
    assert cfg["env"]["WALTER_ADDON_SECRETS"].endswith("addons-secrets/boxes")
    assert oct(Path(arg[1]).stat().st_mode & 0o777) == "0o600"


def test_settings_and_secrets(two, capsys):
    assert two.set_setting("boxes", "default_on", "yes") is True
    with pytest.raises(A.AddonError):
        two.set_setting("boxes", "size", "medium")
    with pytest.raises(A.AddonError):
        two.set_setting("boxes", "nope", "1")
    assert two.secrets_set("boxes") == {"token": False}
    two.save_secret("boxes", "token", "  s3cr3t-value  ")
    p = two.secrets_dir / "boxes" / "token"
    assert p.read_text() == "s3cr3t-value\n" and oct(p.stat().st_mode & 0o777) == "0o600"
    assert oct((two.secrets_dir / "boxes").stat().st_mode & 0o777) == "0o700"
    assert "s3cr3t" not in json.dumps(two.list())
    A.cli(["list"], two)
    A.cli(["settings", "boxes"], two)
    assert "s3cr3t" not in capsys.readouterr().out
    with pytest.raises(A.AddonError):
        two.save_secret("boxes", "other", "x")
    with pytest.raises(A.AddonError):
        two.save_secret("boxes", "token", "a\nb")
    assert A.cli(["secret", "boxes", "token", "s3cr3t"], two) == 2       # never in argv
    two.remove("boxes")
    assert not (two.secrets_dir / "boxes").exists()


def test_secret_drop_page_saves(two):
    import urllib.parse
    import urllib.request
    from walter import addon_secret_drop as D
    saved = []
    srv, url, spec, done = D.make_server(two, "boxes", "token", ports=(0,), on_saved=lambda: saved.append(1))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        local = "http://127.0.0.1:%d/%s" % (srv.server_address[1], url.split("/")[3])
        assert b"Service token" in urllib.request.urlopen(local + "/").read()
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen("http://127.0.0.1:%d/wrong/" % srv.server_address[1])
        body = urllib.parse.urlencode({"k": "tok-123"}).encode()
        assert b"Done" in urllib.request.urlopen(local + "/", data=body).read()
        assert (two.secrets_dir / "boxes" / "token").read_text() == "tok-123\n" and saved == [1]
    finally:
        srv.shutdown()


def test_update_with_more_permissions_turns_it_off(two, gh, tmp_path):
    two.enable("notes")
    more = LOCAL_TOML.replace('version = "1.0.0"', 'version = "1.1.0"').replace("permissions = []",
                                                                             'permissions = ["network"]')
    put_catalog(gh, tmp_path, {"notes": {"addon.toml": more, "SKILL.md": "x", "README.md": "x"}}, commit=COMMIT2)
    d = two.update("notes")
    assert d["permissions_grew"] and not two.is_on("notes")


def test_broken_and_foreign_folders(store):
    (store.dir / "junk").mkdir(parents=True)
    (store.dir / "junk" / "addon.toml").write_text(LOCAL_TOML)          # id 'notes' in folder 'junk'
    (store.dir / "junk" / "SKILL.md").write_text("x")
    items = store.list()
    assert items[0]["broken"] and any("folder" in e for e in items[0]["errors"])
    with pytest.raises(A.AddonError):
        store.enable("junk")
    with pytest.raises(A.AddonError):
        store.folder("../x")


def test_turn_note_is_empty_without_state(tmp_path):
    s = A.Addons(tmp_path, catalog_url="http://x/index.json", repo="a/b", branch="main")
    assert A.turn_note(store=s) == "" and A.mcp_config_arg(s) == []


# ------------------------------------------------------------------ app API (walter/app_addons.py)
def test_app_api(two, gh, tmp_path):
    import asyncio
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from walter import app_addons

    class Hub:
        def auth(self, request):
            if request.headers.get("Authorization") != "Bearer ok":
                raise web.HTTPUnauthorized()
            return {"name": "Phone"}

    async def json_body(request, limit):
        return await request.json()

    drops = []
    routes = web.RouteTableDef()
    app_addons.add_routes(routes, Hub(), json_body, make=lambda: two,
                          start_drop=lambda s, aid, key: drops.append((aid, key)) or "http://10.0.0.2:8768/x/")
    app = web.Application()
    app.add_routes(routes)
    H = {"Authorization": "Bearer ok"}
    gh.repos["someone/thing"] = {"refs": {"HEAD": COMMIT}, "commits": {COMMIT: (
        {"addon.toml": LOCAL_TOML.replace('"notes"', '"thing"'), "SKILL.md": "x", "README.md": "x"}, None)}}

    async def go():
        async with TestClient(TestServer(app)) as c:
            assert (await c.get("/api/app/addons")).status == 401
            j = await (await c.get("/api/app/addons", headers=H)).json()
            assert {a["id"] for a in j["addons"]} == {"boxes", "notes"} and j["walled"] is False
            r = await c.post("/api/app/addons", json={"action": "on", "id": "boxes"}, headers=H)
            j = await r.json()
            assert r.status == 409 and j["needs_confirm"] and any(p["danger"] for p in j["permissions"])
            assert not two.is_on("boxes")
            r = await c.post("/api/app/addons", json={"action": "on", "id": "boxes", "confirm": True}, headers=H)
            assert r.status == 200 and two.is_on("boxes") and (await r.json())["mcp_note"]
            r = await c.post("/api/app/addons", json={"action": "setting", "id": "boxes", "key": "size",
                                                      "value": "large"}, headers=H)
            assert r.status == 200 and two.settings_of("boxes")["size"] == "large"
            r = await c.post("/api/app/addons", json={"action": "setting", "id": "boxes", "key": "size",
                                                      "value": "huge"}, headers=H)
            assert r.status == 400
            r = await c.post("/api/app/addons", json={"action": "secret", "id": "boxes", "key": "token"}, headers=H)
            assert (await r.json())["url"].startswith("http") and drops == [("boxes", "token")]
            cat = await (await c.get("/api/app/addons/catalog", headers=H)).json()
            assert all(e["installed"] for e in cat["addons"])
            r = await c.post("/api/app/addons", json={"action": "preview", "url": "someone/thing"}, headers=H)
            p = (await r.json())["preview"]
            assert p["id"] == "thing" and "Not made by Qiiver" in p["warning"]
            r = await c.post("/api/app/addons", json={"action": "add", "url": "someone/thing"}, headers=H)
            assert r.status == 409 and two.installed("thing") is None
            r = await c.post("/api/app/addons", json={"action": "add", "url": "someone/thing", "commit": p["commit"],
                                                      "confirm": True}, headers=H)
            assert r.status == 200 and two.installed("thing")["source"]["commit"] == COMMIT
            r = await c.post("/api/app/addons", json={"action": "add", "url": "https://evil.example/a/b",
                                                      "confirm": True}, headers=H)
            assert r.status == 400
            assert (await c.post("/api/app/addons", json={"action": "off_all"}, headers=H)).status == 409
            r = await c.post("/api/app/addons", json={"action": "off_all", "confirm": True}, headers=H)
            assert (await r.json())["turned_off"] == ["boxes"] and not two.state()["enabled"]
            assert (await c.post("/api/app/addons", json={"action": "remove", "id": "thing"}, headers=H)).status == 409
            r = await c.post("/api/app/addons", json={"action": "remove", "id": "thing", "confirm": True}, headers=H)
            assert r.status == 200 and two.installed("thing") is None
            r = await c.post("/api/app/addons", json={"action": "on", "id": "../x"}, headers=H)
            assert r.status == 400

    asyncio.run(go())


# ------------------------------------------------------------------ [[routines]] while ON
ROUTINE_TOML = """\
id = "daily-thing"
name = "Daily thing"
version = "1.0.0"
description = "Does a thing every morning."
permissions = []
[[settings]]
key = "time"
label = "Time of day"
type = "string"
default = "06:30"
[[settings]]
key = "flavour"
label = "Flavour"
type = "choice"
choices = ["plain", "spicy"]
default = "plain"
[[routines]]
when = "daily ${setting.time}"
prompt = "SILENT: do the ${setting.flavour} thing."
[[routines]]
when = "weekdays 18:00"
prompt = "Evening check."
"""


@pytest.fixture
def routined(tmp_path):
    data = tmp_path / "data"
    write_tree(data / "addons" / "daily-thing", {"addon.toml": ROUTINE_TOML, "SKILL.md": "x\n"})
    mine = [{"id": 7, "due": time.time() + 3600, "text": "my own routine", "kind": "do",
             "repeat": {"days": [0, 1, 2, 3, 4, 5, 6], "time": "08:00"}}]
    (data / "reminders.json").write_text(json.dumps(mine))
    return A.Addons(data, catalog_url="http://127.0.0.1:9/index.json", repo="o/r", branch="main")


def _rem(store):
    return json.loads(store.reminders_path.read_text())


def test_routine_when_parsing_and_validation():
    assert A.parse_routine_when("daily 6:30") == {"days": list(range(7)), "time": "06:30"}
    assert A.parse_routine_when("mon,wed 18:05") == {"days": [0, 2], "time": "18:05"}
    assert A.parse_routine_when("weekends 07:00")["days"] == [5, 6]
    for bad in ("daily", "someday 06:30", "daily 25:00", "daily 6pm"):
        with pytest.raises(A.AddonError):
            A.parse_routine_when(bad)
    import tomllib
    m, errs = A.validate_manifest(tomllib.loads(ROUTINE_TOML))
    assert errs == [] and len(m["routines"]) == 2
    _, errs = A.validate_manifest(tomllib.loads(ROUTINE_TOML.replace("${setting.flavour}", "${setting.nope}")))
    assert any("nope" in e for e in errs)
    _, errs = A.validate_manifest(tomllib.loads(ROUTINE_TOML.replace("weekdays 18:00", "fortnightly 18:00")))
    assert any("routines" in e for e in errs)


def test_routines_exist_only_while_on_and_never_touch_mine(routined):
    s = routined
    s.enable("daily-thing")
    items = _rem(s)
    mine = [i for i in items if not i.get("addon")]
    ours = sorted((i for i in items if i.get("addon")), key=lambda i: i["addon_routine"])
    assert [i["id"] for i in mine] == [7]
    assert [i["id"] for i in ours] == [8, 9] and all(i["addon"] == "daily-thing" and i["kind"] == "do" for i in ours)
    assert ours[0]["repeat"] == {"days": list(range(7)), "time": "06:30"}
    assert ours[0]["text"] == "SILENT: do the plain thing." and ours[0]["due"] > time.time()
    # enabling again (or any other save) changes nothing, and a paused one stays paused
    items[[i["id"] for i in items].index(8)]["enabled"] = False
    s.reminders_path.write_text(json.dumps(items))
    s.enable("daily-thing")
    assert next(i for i in _rem(s) if i["id"] == 8).get("enabled") is False
    s.disable("daily-thing")
    assert [i["id"] for i in _rem(s)] == [7]


def test_routines_follow_settings_and_kill_switch(routined):
    s = routined
    s.enable("daily-thing")
    s.set_setting("daily-thing", "time", "05:45")
    s.set_setting("daily-thing", "flavour", "spicy")
    ours = [i for i in _rem(s) if i.get("addon_routine") == 0]
    assert len(ours) == 1 and ours[0]["repeat"]["time"] == "05:45" and "spicy" in ours[0]["text"]
    s.set_setting("daily-thing", "time", "not a time")     # a bad value: that routine is skipped, no crash
    assert [i.get("addon_routine") for i in _rem(s) if i.get("addon")] == [1]
    s.disable_all()
    assert [i["id"] for i in _rem(s)] == [7]
    s.enable("daily-thing")
    s.remove("daily-thing")
    assert [i["id"] for i in _rem(s)] == [7]


def test_routines_no_reminders_file_and_env(routined):
    s = routined
    s.reminders_path.unlink()
    s.disable("daily-thing")                                # nothing on: no file is created
    assert not s.reminders_path.exists()
    s.enable("daily-thing")
    assert len(_rem(s)) == 2
    env = s.addon_env("daily-thing", s.installed("daily-thing"))
    assert env["WALTER_ADDON_DATA"].endswith("addons-data/daily-thing")


# ------------------------------------------------------------------ verse migration (local catalog checkout)
VERSE_TOML = ROUTINE_TOML.replace('id = "daily-thing"', 'id = "verse-of-the-day"').replace(
    "[[settings]]\nkey = \"flavour\"", "[[settings]]\nkey = \"translation\"").replace(
    'choices = ["plain", "spicy"]\ndefault = "plain"', 'choices = ["KJV or WEB", "KJV"]\ndefault = "KJV or WEB"').replace(
    "${setting.flavour}", "${setting.translation}").replace(
    '[[routines]]\nwhen = "weekdays 18:00"\nprompt = "Evening check."\n',
    '[cli]\nimport = "bin/import.sh"\n').replace('permissions = []', 'permissions = ["run_commands"]')


@pytest.fixture
def verse_world(tmp_path):
    cat = tmp_path / "catalog"
    folder = cat / "addons" / "verse-of-the-day"
    write_tree(folder, {"addon.toml": VERSE_TOML, "SKILL.md": "verse\n",
                        "bin/import.sh": ('#!/bin/sh\nmkdir -p "$WALTER_ADDON_DATA"\ncp "$1" "$WALTER_ADDON_DATA/used.tsv"\n'
                                          'echo imported\n', 0o755)})
    (cat / "index.json").write_text(json.dumps({"format": 1, "addons": [
        {"id": "verse-of-the-day", "permissions": ["run_commands"], "sha256": A.pack_sha256(folder)}]}))
    data = tmp_path / "data"
    write_tree(data / "home" / "journal", {"verses-used.txt": "Joshua 1:9\n"})
    old = {"id": 34, "due": time.time() + 600, "kind": "do", "workspace": "home",
           "text": "SILENT daily verse (never speak). ... list in ~/.walter/home/journal/verses-used.txt ...",
           "repeat": {"days": list(range(7)), "time": "06:30"}}
    mine = {"id": 7, "due": time.time() + 3600, "text": "morning briefing", "kind": "do",
            "repeat": {"days": [0, 1, 2, 3, 4], "time": "08:00"}}
    (data / "reminders.json").write_text(json.dumps([old, mine]))
    store = A.Addons(data, catalog_url="http://127.0.0.1:9/index.json", repo="o/r", branch="main")
    return store, folder, data


def test_migrate_verse_dry_run_changes_nothing(verse_world):
    store, folder, data = verse_world
    before = (data / "reminders.json").read_text()
    lines = []
    assert A.migrate_verse(store, yes=False, src=folder, after=[], out=lines.append) == 3
    assert any("#34" in l for l in lines) and any("06:30" in l for l in lines)
    assert (data / "reminders.json").read_text() == before and store.installed("verse-of-the-day") is None


def test_migrate_verse_does_it(verse_world):
    store, folder, data = verse_world
    lines = []
    assert A.migrate_verse(store, yes=True, src=folder, after=[], out=lines.append) == 0
    m = store.installed("verse-of-the-day")
    assert m and m["source"]["origin"] == "catalog" and store.is_on("verse-of-the-day") and A.verse_on(store)
    items = json.loads((data / "reminders.json").read_text())
    assert [i["id"] for i in items if not i.get("addon")] == [7]          # old one gone, mine untouched
    ours = [i for i in items if i.get("addon") == "verse-of-the-day"]
    assert len(ours) == 1 and ours[0]["repeat"]["time"] == "06:30" and "KJV or WEB" in ours[0]["text"]
    assert (data / "addons-data" / "verse-of-the-day" / "used.tsv").read_text() == "Joshua 1:9\n"
    assert (data / "home" / "journal" / "verses-used.txt").exists()       # the old file stays
    assert any("its text was" in l for l in lines)
    # again: nothing left to migrate, nothing duplicated
    assert A.migrate_verse(store, yes=True, src=folder, after=[], out=lines.append) == 0
    assert len([i for i in json.loads((data / "reminders.json").read_text()) if i.get("addon")]) == 1
    store.disable("verse-of-the-day")
    assert not A.verse_on(store)
    assert [i["id"] for i in json.loads((data / "reminders.json").read_text())] == [7]


def test_install_local_needs_matching_catalog_sha(verse_world):
    store, folder, data = verse_world
    (folder / "SKILL.md").write_text("changed after the index was built\n")
    with pytest.raises(A.AddonError) as e:
        A.install_local(store, folder)
    assert e.value.code == "sha_mismatch"
