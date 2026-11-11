"""Cluster basics (walter/cluster.py): signing + replay window, the registry and pairing code,
leader election with a fake clock (start-up grace, failover, the old lead stepping down,
promote, split-brain healing) and job placement. Never touches ~/.walter."""
import json
import time

import pytest

from walter import cluster as C


# ---------------------------------------------------------------- signing
KEY = "k" * 43
A, B, X = "n-aaaaaaaaaaaa", "n-bbbbbbbbbbbb", "n-cccccccccccc"


def test_signed_request_verifies():
    v = C.Verifier(clock=lambda: 1000)
    h = C.signed_headers(A, KEY, "POST", "/api/cluster/heartbeat", b'{"x":1}', now=1000)
    assert v.verify(h, "POST", "/api/cluster/heartbeat", b'{"x":1}', {A: KEY}.get) == A


@pytest.mark.parametrize("change", ["body", "path", "method", "key", "sig"])
def test_tampering_is_refused(change):
    v = C.Verifier(clock=lambda: 1000)
    h = C.signed_headers(A, KEY, "POST", "/api/cluster/write", b"{}", now=1000)
    method, path, body, keys = "POST", "/api/cluster/write", b"{}", {A: KEY}
    if change == "body":
        body = b'{"key":"x"}'
    elif change == "path":
        path = "/api/cluster/job"
    elif change == "method":
        method = "GET"
    elif change == "key":
        keys = {A: "z" * 43}
    else:
        h["X-Walter-Sig"] = "0" * 64
    with pytest.raises(PermissionError):
        v.verify(h, method, path, body, keys.get)


def test_replay_window_and_nonce():
    now = [1000]
    v = C.Verifier(clock=lambda: now[0])
    h = C.signed_headers(A, KEY, "POST", "/p", b"", now=1000)
    v.verify(h, "POST", "/p", b"", {A: KEY}.get)
    with pytest.raises(PermissionError, match="replayed"):
        v.verify(h, "POST", "/p", b"", {A: KEY}.get)          # same nonce again
    old = C.signed_headers(A, KEY, "POST", "/p", b"", now=1000 - 61)
    with pytest.raises(PermissionError, match="stale"):
        v.verify(old, "POST", "/p", b"", {A: KEY}.get)        # outside 60 s
    ok = C.signed_headers(A, KEY, "POST", "/p", b"", now=1000 - 59)
    assert v.verify(ok, "POST", "/p", b"", {A: KEY}.get) == A
    future = C.signed_headers(A, KEY, "POST", "/p", b"", now=1000 + 61)
    with pytest.raises(PermissionError, match="stale"):
        v.verify(future, "POST", "/p", b"", {A: KEY}.get)


def test_unsigned_and_unknown_nodes_refused():
    v = C.Verifier(clock=lambda: 1000)
    with pytest.raises(PermissionError, match="unsigned"):
        v.verify({}, "GET", "/p", b"", {A: KEY}.get)
    h = C.signed_headers(X, KEY, "GET", "/p", b"", now=1000)
    with pytest.raises(PermissionError, match="unknown node"):
        v.verify(h, "GET", "/p", b"", {A: KEY}.get)


def test_clean_url():
    assert C.clean_url("http://192.168.1.5:8765") == "http://192.168.1.5:8765"
    assert C.clean_url("http://192.168.1.5:8765/") == "http://192.168.1.5:8765"
    for bad in ("ftp://x", "http://u:p@x:1", "http://x/path", "http://x?q=1", "javascript:alert(1)", "", None,
                "http://x:99999"):
        assert C.clean_url(bad) == ""


# ---------------------------------------------------------------- registry
def test_store_identity_not_written_until_needed(tmp_path):
    st = C.ClusterStore(tmp_path / "cluster.json")
    assert st.peek_identity() is None and not st.exists()
    me = st.identity("Wall", 60, node_id=A)
    assert me["id"] == A and me["priority"] == 60
    assert oct((tmp_path / "cluster.json").stat().st_mode & 0o777) == "0o600"


def test_pairing_code_single_use_and_burned(tmp_path):
    st = C.ClusterStore(tmp_path / "cluster.json")
    st.identity("Wall")
    code = st.start_pairing()["code"]
    assert code not in (tmp_path / "cluster.json").read_text()      # only its hash is stored
    st.use_code(code)
    with pytest.raises(PermissionError):
        st.use_code(code)                                            # single use
    code = st.start_pairing()["code"]
    wrong = f"{(int(code) + 1) % 1000000:06d}"
    for _ in range(C.PAIR_MAX_TRIES):
        with pytest.raises(PermissionError):
            st.use_code(wrong)
    with pytest.raises(PermissionError, match="no cluster pairing"):
        st.use_code(code)                                            # burned after 5 wrong tries
    code = st.start_pairing(now=time.time() - 1000)["code"]
    with pytest.raises(PermissionError):
        st.use_code(code)                                            # expired


def test_peers_keys_and_leave(tmp_path):
    st = C.ClusterStore(tmp_path / "cluster.json")
    st.identity("Wall", node_id=A)
    st.add_peer({"id": B, "name": "Office", "url": "http://10.0.0.2:8765"}, KEY)
    assert st.key_for(B) == KEY and st.find("office")["id"] == B
    assert "key" not in json.dumps(st.public_peers())
    with pytest.raises(ValueError):
        st.add_peer({"id": "../etc", "name": "x"})
    with pytest.raises(ValueError):
        st.add_peer({"id": X, "name": "x"}, "short")
    k = st.ensure_pair_key(B, X)
    assert st.pair_key_for(X, B) == k
    st.remove_peer(B)
    assert st.key_for(B) is None and B in st.removed() and st.pair_key_for(B, X) is None
    st.add_peer({"id": B, "name": "Office", "url": "http://10.0.0.2:8765"}, KEY)
    st.leave()
    assert st.peers() == {} and st.peek_identity()["id"] == A


# ---------------------------------------------------------------- election
def hb(term, leader, prio=50):
    return {"term": term, "leader": leader, "priority": prio}


def test_solo_without_peers():
    e = C.Election(A, now=0)
    assert e.tick(100, has_peers=False) == "solo"


def test_startup_grace_then_highest_priority_then_lowest_id():
    ea, eb = C.Election(A, 50, now=0), C.Election(B, 50, now=0)
    ea.observe(B, hb(0, None), 1)
    eb.observe(A, hb(0, None), 1)
    assert ea.tick(5) == "follower"            # listening first (grace = 3 heartbeats)
    assert ea.tick(9.5) == "lead"              # same priority: lowest id (A) wins
    eb.observe(A, hb(ea.term, ea.leader), 9.6)
    assert eb.tick(9.7) == "follower" and eb.leader == A
    # a higher priority wins at election time
    ea2, eb2 = C.Election(A, 10, now=0), C.Election(B, 90, now=0)
    ea2.observe(B, hb(0, None, 90), 5)
    assert ea2.tick(10) == "follower" and ea2.leader == B


def test_failover_after_three_missed_heartbeats_and_old_lead_steps_down():
    lead, f1, f2 = C.Election(A, 80, now=0), C.Election(B, 50, now=0), C.Election(X, 40, now=0)
    lead.promote(A)
    t = 0.0
    for t in (1, 4, 7):
        for f in (f1, f2):
            f.observe(A, hb(lead.term, A, 80), t)
        f1.observe(X, hb(lead.term, A, 40), t)
        f2.observe(B, hb(lead.term, A, 50), t)
    assert f1.tick(8) == "follower" and f2.tick(8) == "follower"
    # the lead goes quiet; followers keep hearing each other
    for t in (10, 13, 16):
        f1.observe(X, hb(f2.term, f2.leader, 40), t)
        f2.observe(B, hb(f1.term, f1.leader, 50), t)
    assert f1.tick(15.9) == "follower"          # 8.9 s since A: not yet
    assert f1.tick(16.1) == "lead"              # 3 missed: B (priority 50) takes over
    f2.observe(B, hb(f1.term, f1.leader, 50), 16.2)
    assert f2.tick(16.3) == "follower" and f2.leader == B
    # the old lead (higher priority!) comes back: it follows the higher term
    lead.observe(B, hb(f1.term, B, 50), 30)
    assert lead.tick(30) == "follower" and lead.leader == B
    # and B, hearing A's new heartbeat, stays lead (sticky, no flapping)
    f1.observe(A, hb(lead.term, lead.leader, 80), 30)
    assert f1.tick(30) == "lead"


def test_restarted_old_lead_listens_first():
    e = C.Election(A, 90, term=4, now=100)       # restarts with its old term, no leader
    e.observe(B, hb(5, B, 50), 101)
    assert e.tick(102) == "follower" and e.leader == B


def test_promote_hands_over():
    ea, eb = C.Election(A, 50, now=0), C.Election(B, 50, now=0)
    ea.promote(A)
    eb.observe(A, hb(ea.term, A), 1)
    ea.promote(B)
    eb.observe(A, hb(ea.term, B), 2)
    assert eb.tick(2) == "lead"
    ea.observe(B, hb(eb.term, B), 2)
    assert ea.tick(2) == "follower"


def test_split_brain_heals_by_rank_in_same_term():
    ea, eb = C.Election(A, 50, term=3, now=0), C.Election(B, 50, term=3, now=0)
    ea.leader, eb.leader = A, B                   # both think they lead in term 3
    eb.observe(A, hb(3, A), 1)
    ea.observe(B, hb(3, B), 1)
    assert ea.tick(1) == "lead" and eb.tick(1) == "follower"


# ---------------------------------------------------------------- placement
def caps(cores=4, load=0.0, mem=4000, running=0, max_tasks=3, stt=False, jobs=True):
    return {"cores": cores, "load1": load, "mem_free_mb": mem, "running": running, "max_tasks": max_tasks,
            "stt": stt, "tts": False, "jobs": jobs}


def test_pick_node_by_free_cpu_slots_and_walls():
    nodes = [{"id": A, "caps": caps(4, 3.5), "self": True},
             {"id": B, "caps": caps(8, 1.0)},
             {"id": X, "caps": caps(16, 0.0, running=3)}]            # no free slot
    assert C.pick_node(nodes) == B
    assert C.pick_node(nodes, exclude=(B,)) == A
    # walled workspace: only nodes it's assigned to (or this node)
    assert C.pick_node(nodes, "vault", walled=True) == A
    nodes[1]["walled"] = ["vault"]
    assert C.pick_node(nodes, "vault", walled=True) == B
    nodes[1]["caps"]["jobs"] = False
    assert C.pick_node(nodes) == A
    assert C.pick_node([{"id": B, "caps": caps(stt=True)}, {"id": A, "caps": caps(16)}], need="stt") == B
    assert C.pick_node([]) is None
