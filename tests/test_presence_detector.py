"""Detector: busy-background mask, brightness changes, person-sized blobs.

Synthetic frames only (numpy arrays in memory); no camera, nothing on disk
except a tmp_path busy map in the persistence test.
"""
import math

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")
from walter.presence import Detector  # noqa: E402

H, W = 240, 320
rng = np.random.default_rng(1)


def background():
    img = np.full((H, W), 90, np.uint8)
    img[160:, :] = 60                       # floor
    img[20:90, 230:310] = 150               # window frame
    return img


def scene(t, fan=True, branches=True, person_x=None, gain=0):
    img = background().astype(np.int16)
    if fan:  # ceiling fan, top-left: a bright bar at a random (aliased) angle each frame
        cx, cy, r = 50, 40, 32
        a = rng.uniform(0, math.pi)
        p1 = (int(cx - r * math.cos(a)), int(cy - r * math.sin(a)))
        p2 = (int(cx + r * math.cos(a)), int(cy + r * math.sin(a)))
        bar = np.zeros((H, W), np.uint8)
        cv2.line(bar, p1, p2, 255, 7)
        img[bar > 0] = 230
    if branches:  # branches in the window: random blotchy noise
        patch = rng.integers(0, 2, (7, 8)).astype(np.uint8) * 120
        patch = cv2.resize(patch, (80, 70), interpolation=cv2.INTER_NEAREST)
        img[20:90, 230:310] = 110 + patch
    if person_x is not None:  # a person-sized dark blob (about 45x130 px)
        x0 = int(person_x)
        img[70:200, max(0, x0):max(0, min(W, x0 + 45))] = 25
    img += gain
    img += rng.normal(0, 2, img.shape).astype(np.int16)   # sensor noise
    return np.clip(img, 0, 255).astype(np.uint8)


class NoFaces:
    """Stand-in classifier: these tests are about motion, and the real
    cascades are slow on flat synthetic images."""
    def detectMultiScale(self, *a, **k):
        return ()


class NoPeople:
    def detectMultiScale(self, *a, **k):
        return (), ()


def detector(path=None):
    det = Detector(cv2, path)
    det.face = det.profile = NoFaces()
    det.hog = NoPeople()
    return det


def run(det, frames, t0=0.0):
    return [det.look(f, now=t0 + i) for i, f in enumerate(frames)]


def learned():
    det = detector()
    run(det, [scene(i) for i in range(200)])
    return det


def test_fan_and_branches_are_masked_out():
    det = detector()
    out = run(det, [scene(i) for i in range(300)])
    late = out[150:]
    assert not any(s.seen for s in late), [s.why for s in late if s.seen][:3]
    m = det.mask()
    sy, sx = det.H / H, det.W / W
    assert m[int(40 * sy), int(50 * sx)]            # fan centre is masked
    assert m[int(55 * sy), int(270 * sx)]           # window is masked
    assert not m[int(130 * sy), int(160 * sx)]      # the middle of the room is not
    assert det.busy_pct() < 25


def test_person_walking_in_is_seen():
    det = learned()
    out = run(det, [scene(0, person_x=x) for x in range(-40, 200, 12)], t0=1000)
    hits = [s for s in out if s.seen]
    assert hits, [s.why for s in out]
    assert all(s.kind in ("motion", "person") for s in hits)
    assert 0.0 < hits[0].x < 1.0


def test_person_in_front_of_the_window_still_counts_partly():
    det = learned()
    out = run(det, [scene(0, person_x=x) for x in range(140, 300, 12)], t0=1000)
    assert any(s.seen for s in out)


def test_brightness_change_is_ignored():
    det = learned()
    out = run(det, [scene(0, gain=g) for g in (0, 40, 40, -30, -30, 60, 0)], t0=1000)
    assert not any(s.seen for s in out), [s.why for s in out]


def test_whole_scene_change_is_skipped():
    det = learned()
    det.look(scene(0), now=1000)
    flipped = 255 - scene(0)
    s1 = det.look(flipped, now=1001)
    s2 = det.look(scene(0), now=1002)
    assert not s1.seen and not s2.seen
    assert "skipped" in s1.why


def test_small_motion_is_not_a_person():
    det = learned()
    frames = []
    for i in range(10):            # a small thing (cat, curtain corner) wandering about
        f = scene(0)
        x = 100 + i * 8
        f[180:192, x:x + 14] = 20
        frames.append(f)
    out = run(det, frames, t0=1000)
    assert not any(s.seen for s in out), [s.why for s in out if s.seen]


def test_motion_needs_two_frames():
    det = learned()
    det.look(scene(0), now=1000)
    s = det.look(scene(0, person_x=150), now=1001)   # one sudden blob
    assert not (s.seen and s.kind == "motion")


def test_static_detection_becomes_a_fixture():
    det = detector()
    det.restart(0.0)
    box = (140, 60, 40, 40)                       # image coords
    det.look(scene(0, fan=False, branches=False), now=0)
    assert not det._static(box, W, H, now=10)
    for t in range(1, 5):
        det.look(scene(0, fan=False, branches=False), now=t)
    assert det._static(box, W, H, now=det.STATIC_S + 10)   # nothing moved there for ages
    det.look(scene(0, fan=False, branches=False), now=det.STATIC_S + 20)
    det.look(scene(0, fan=False, branches=False, person_x=140), now=det.STATIC_S + 21)
    assert not det._static(box, W, H, now=det.STATIC_S + 22)   # it moved: not a fixture


def test_busy_map_persists_without_images(tmp_path):
    p = tmp_path / "busy.npy"
    det = detector(p)
    run(det, [scene(i) for i in range(150)])
    det.save()
    saved = np.load(p, allow_pickle=False)
    assert saved.shape == (Detector.H, Detector.W) and saved.dtype == np.uint8
    det2 = detector(p)
    assert det2.frames >= Detector.WARMUP
    assert det2.mask()[int(40 * Detector.H / H), int(50 * Detector.W / W)]


def test_real_classifiers_find_nobody_in_an_empty_room():
    det = Detector(cv2)                    # the real cascades + HOG, a few frames only
    out = run(det, [cv2.GaussianBlur(scene(i), (5, 5), 0) for i in range(4)])
    assert not any(s.seen for s in out), [s.why for s in out]
