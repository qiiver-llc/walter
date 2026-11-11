"""Presence: a sense of whether someone is in the room, from the webcam.

PRIVACY. Frames are grabbed at low resolution (320x240) about once a
second, looked at in memory and thrown away straight after. Nothing is ever
saved, logged, uploaded or shown; only a yes/no ("someone is there"), a
rough left/right position and (with [senses] recognise_faces) the names of
*enrolled* people leave the camera thread. Face recognition (people.py) runs
at most every 2 s and only when someone is seen: YuNet finds the face, SFace
turns it into 128 numbers, which are compared with the enrolled embeddings
and dropped; unknown faces stay "someone". The camera is released
completely during quiet hours and when [senses] camera is false.

Sound-triggered sleep (CameraGate): once nobody has been seen for a few
minutes and the microphone is quiet, the camera device is closed (webcam
light off). A sound that clearly stands out from the mic's adaptive
background, a voice, or his name wakes it for a short look; if nobody is
confirmed it goes back to sleep. The mic side only ever passes a loudness
number (relative to the background), never audio.

Two parts:
  PresenceTracker: a pure state machine (testable, no camera), fed
                   observations, turns them into arrivals / empty room.
  Presence:        runs the camera in a background thread and tells the
                   conductor about arrivals and the room going empty.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from dataclasses import dataclass

log = logging.getLogger("walter.presence")


@dataclass
class Observation:
    t: float
    seen: bool              # a face/person found, or sustained motion
    x: float | None = None  # where (0 = left edge of the camera image, 1 = right), if known
    faces: list | None = None  # recognition ran on this frame: a name (or None = unknown) per face


class PresenceTracker:
    """Turns a stream of yes/no observations into presence events.

    - present:  someone confirmed (`confirm_n` sightings within `confirm_window_s`)
    - away:     nobody seen for `absent_after_s` (short: they stepped out)
    - empty:    nobody seen for `empty_after_s` (the room is empty: doze)
    - arrival:  confirmed presence after the room was unseen for `arrive_after_s`
    """

    def __init__(self, arrive_after_s=600.0, empty_after_s=900.0, absent_after_s=120.0,
                 confirm_n=2, confirm_window_s=6.0, greet_cooldown_s=1800.0):
        self.arrive_after_s = arrive_after_s
        self.empty_after_s = empty_after_s
        self.absent_after_s = absent_after_s
        self.confirm_n = confirm_n
        self.confirm_window_s = confirm_window_s
        self.greet_cooldown_s = greet_cooldown_s
        self.present = False
        self.empty = False
        self.last_seen: float | None = None
        self.watch_since: float | None = None   # when the camera (re)started watching
        self.last_greet = -1e18
        self._hits: list[float] = []

    def reset(self, now: float):
        """The camera (re)started: we know nothing about the last while."""
        self.present = False
        self.empty = False
        self.last_seen = None
        self.watch_since = now
        self._hits = []

    def unseen_for(self, now: float) -> float:
        ref = self.last_seen if self.last_seen is not None else self.watch_since
        return 0.0 if ref is None else max(0.0, now - ref)

    def observe(self, ob: Observation) -> list[dict]:
        """Feed one observation; returns events: {"e": "arrival"|"present"|"away"|"empty", ...}."""
        now = ob.t
        if self.watch_since is None:
            self.reset(now)
        events: list[dict] = []
        if ob.seen:
            self._hits = [h for h in self._hits if now - h <= self.confirm_window_s] + [now]
            confirmed = self.present or len(self._hits) >= self.confirm_n
            if confirmed:
                if not self.present:
                    gap = self.unseen_for(now)
                    first = self.last_seen is None   # nobody seen since the camera came on
                    self.present = True
                    self.empty = False
                    if gap >= self.arrive_after_s:
                        greet = now - self.last_greet >= self.greet_cooldown_s
                        if greet:
                            self.last_greet = now
                        events.append({"e": "arrival", "away_s": gap, "since_start": first,
                                       "greet": greet, "x": ob.x})
                    else:
                        events.append({"e": "present", "x": ob.x})
                elif self.empty:
                    self.empty = False
                    events.append({"e": "present", "x": ob.x})
                self.last_seen = now
        else:
            gone = self.unseen_for(now)
            if self.present and gone >= self.absent_after_s:
                self.present = False
                events.append({"e": "away"})
            if not self.empty and gone >= self.empty_after_s:
                self.empty = True
                self.present = False
                events.append({"e": "empty", "for_s": gone})
        return events

    def status(self, now: float) -> dict:
        return {"present": self.present, "room_empty": self.empty,
                "last_seen_ago_s": None if self.last_seen is None else round(now - self.last_seen),
                "unseen_for_s": round(self.unseen_for(now))}


class CameraGate:
    """When should the camera be on, and how often should it look?
    A pure state machine (no camera, no clock): fed sound levels, voices and
    the tracker's verdicts, it decides between

      off:       not allowed (disabled, quiet hours); device released
      watching:  camera on (slower while someone is clearly there)
      sleeping:  nobody seen for `sleep_after_s` and the mic quiet: device released
      verifying: a sound woke it; look for a person for `verify_s`, then
                 either watch (someone's there) or go back to sleep

    Sound levels are ratios to the mic's adaptive background (1.0 = the
    room's usual hum). Debounce: a wake needs two loud moments within
    `pair_s` (or one very loud bang), and after a check that found nobody,
    sound and voice activity are ignored for a cooldown that doubles each
    time (a TV, a radio), up to `max_cooldown_s`. His name always wakes it.
    """

    OFF, WATCHING, SLEEPING, VERIFYING = "off", "watching", "sleeping", "verifying"

    def __init__(self, sleep_after_s=300.0, quiet_s=60.0, force_sleep_after_s=900.0,
                 verify_s=45.0, wake_ratio=3.0, quiet_ratio=2.0, bang_ratio=8.0, pair_s=2.0,
                 cooldown_s=60.0, max_cooldown_s=600.0, base_period=1.0, talk_period=2.0,
                 still_period=5.0, still_after_s=600.0, sleep_enabled=True):
        self.sleep_after_s = sleep_after_s
        self.quiet_s = quiet_s
        self.force_sleep_after_s = max(force_sleep_after_s, sleep_after_s)
        self.verify_s = verify_s
        self.wake_ratio = wake_ratio
        self.quiet_ratio = min(quiet_ratio, wake_ratio)
        self.bang_ratio = max(bang_ratio, wake_ratio)
        self.pair_s = pair_s
        self.cooldown_s = cooldown_s
        self.max_cooldown_s = max_cooldown_s
        self.base_period = base_period
        self.talk_period = max(talk_period, base_period)
        self.still_period = max(still_period, base_period)
        self.still_after_s = still_after_s
        self.sleep_enabled = sleep_enabled
        self.state = self.OFF
        self.reason = "not started"
        self.since = 0.0
        self.last_loud = -1e18      # last sound above quiet_ratio
        self.last_voice = -1e18     # last voice activity (utterance start or transcript)
        self._loud_t = -1e18        # last sound above wake_ratio (for the pair rule)
        self.verify_until = 0.0
        self.cooldown_until = -1e18
        self.fails = 0              # checks in a row that found nobody

    # ------------------------------------------------------------ helpers
    @property
    def camera_wanted(self) -> bool:
        return self.state in (self.WATCHING, self.VERIFYING)

    def _go(self, state: str, now: float, reason: str) -> bool:
        if state == self.state:
            return False
        self.state, self.reason, self.since = state, reason, now
        if state == self.VERIFYING:
            self.verify_until = now + self.verify_s
        return True

    def _wake(self, now: float, reason: str, strong: bool = False) -> bool:
        if self.state != self.SLEEPING:
            return False
        if not strong and now < self.cooldown_until:
            return False
        return self._go(self.VERIFYING, now, reason)

    # ------------------------------------------------------------- inputs
    def allow(self, now: float, allowed: bool, why: str = "") -> bool:
        """Camera permitted at all (enabled and outside quiet hours)?"""
        if not allowed:
            self.fails, self.cooldown_until = 0, -1e18
            return self._go(self.OFF, now, why or "not allowed")
        if self.state == self.OFF:
            return self._go(self.WATCHING, now, why or "allowed: watching")
        return False

    def sound(self, now: float, ratio: float) -> bool:
        """Mic loudness (peak of a short window) relative to its background."""
        if ratio >= self.quiet_ratio:
            self.last_loud = now
        if ratio < self.wake_ratio:
            return False
        paired = now - self._loud_t <= self.pair_s
        self._loud_t = now
        if ratio >= self.bang_ratio:
            return self._wake(now, f"sudden loud sound ({ratio:.1f}x background)")
        if paired:
            return self._wake(now, f"sound standing out from the background ({ratio:.1f}x)")
        return False

    def voice(self, now: float, strong: bool = False) -> bool:
        """Voice activity (strong = his name, which ignores the cooldown)."""
        self.last_voice = self.last_loud = now
        return self._wake(now, "his name was said" if strong else "a voice", strong=strong)

    def tick(self, now: float, present: bool, unseen_s: float) -> bool:
        """Time passing; `present`/`unseen_s` from the PresenceTracker."""
        if self.state == self.VERIFYING:
            if present:
                self.fails, self.cooldown_until = 0, -1e18
                return self._go(self.WATCHING, now, "verified: someone is there")
            if now >= self.verify_until:
                self.fails += 1
                cool = min(self.max_cooldown_s, self.cooldown_s * 2 ** (self.fails - 1))
                self.cooldown_until = now + cool
                return self._go(self.SLEEPING, now, f"nobody confirmed in {self.verify_s:.0f}s "
                                                    f"(sounds ignored for {cool:.0f}s)")
        elif self.state == self.WATCHING and self.sleep_enabled and not present:
            if unseen_s >= self.sleep_after_s:
                quiet = now - max(self.last_loud, self.last_voice)
                if quiet >= self.quiet_s:
                    return self._go(self.SLEEPING, now, f"nobody seen for {unseen_s / 60:.0f} min "
                                                        f"and the room quiet for {quiet:.0f}s")
                if unseen_s >= self.force_sleep_after_s:
                    return self._go(self.SLEEPING, now, f"nobody seen for {unseen_s / 60:.0f} min "
                                                        "(only background noise: TV, radio?)")
        return False

    def wake(self, now: float, reason: str) -> bool:
        """Force a check (e.g. from the control panel)."""
        return self._wake(now, reason, strong=True)

    # ------------------------------------------------------------ outputs
    def period(self, now: float, present: bool) -> float:
        """Seconds between frames."""
        if self.state == self.WATCHING and present:
            if now - max(self.last_voice, self.last_loud) >= self.still_after_s:
                return self.still_period    # there and quiet for ages: a glance every few seconds
            return self.talk_period         # there and around: a relaxed rate
        return self.base_period             # looking for someone: full rate

    def status(self, now: float) -> dict:
        d = {"camera_state": self.state, "camera_reason": self.reason,
             "camera_state_for_s": round(now - self.since)}
        if self.state == self.VERIFYING:
            d["verify_left_s"] = max(0, round(self.verify_until - now))
        if now < self.cooldown_until:
            d["wake_cooldown_s"] = round(self.cooldown_until - now)
        return d


@dataclass
class Sighting:
    """What one frame showed (text only; never any pixels)."""
    seen: bool
    x: float | None = None
    why: str = "nothing"      # "face ...", "person ...", "motion blob ...", or why nothing counted
    kind: str = "none"        # face | profile | person | motion | none | skip


class Detector:
    """Decides "is a person in this frame?" from three signals, strongest first:

      1. a face (OpenCV's Haar frontal cascade, every frame, ~3 ms);
      2. a person (HOG pedestrian detector + profile-face cascade, the
         "deep look": every few frames or whenever something moves, ~15-30 ms);
      3. a person-sized motion blob: large, compact, outside the learned
         busy background and lasting two frames.

    Busy background: a slowly decaying per-pixel motion frequency map on the
    160x120 motion image. Pixels that move a large fraction of the time
    (ceiling fan, branches in a window, flicker) are masked out of the motion
    check. Global brightness changes (auto-exposure, clouds) are normalised
    away, and frames where most pixels change at once are skipped.

    Fixtures: a face/person detection at a spot where nothing has moved for
    `STATIC_S` (a poster, a coat on a door) is ignored until it moves.

    Finds *a* face or body; recognising *who* is separate (Camera + people.py).
    Nothing is kept but the low-res busy map (motion frequencies, not an image).
    """

    W, H = 160, 120          # motion image size
    DIFF_T = 25              # grey-level change that counts as "moved"
    GLOBAL_FRAC = 0.35       # more than this changed at once = lighting / camera adjusting: skip
    BUSY_TAU = 100.0         # frames: time constant of the motion-frequency map (~100 s at 1 fps)
    BUSY_ON = 0.2            # moving in more than 20% of frames = busy background
    WARMUP = 40              # frames before motion blobs are trusted (busy map still learning)
    MIN_BLOB = 0.02          # person-sized: at least 2% of the motion image (384 px)
    MIN_BLOB_H = 0.15        # ...and at least 15% of the image tall
    MAX_BLOB = 0.5
    MIN_FILL = 0.25          # blob area / its bounding box: compact, not scattered speckle
    DEEP_EVERY = 3           # deep look (HOG + profile) at least every this many frames
    HOG_MIN_WEIGHT = 0.5
    STATIC_S = 900.0         # a detection where nothing moved for this long is a fixture
    SAVE_EVERY_S = 600.0

    def __init__(self, cv2, busy_path=None):
        import numpy as np
        self.cv2, self.np = cv2, np
        cv2.setNumThreads(1)  # never fight the speech recogniser for cores
        self.face = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        if self.face.empty():
            raise RuntimeError("face cascade missing from OpenCV")
        self.profile = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_profileface.xml")
        if self.profile.empty():
            self.profile = None
        self.hog = cv2.HOGDescriptor()
        self.hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())
        self.busy_path = busy_path
        self.busy = np.zeros((self.H, self.W), np.float32)   # motion frequency per pixel
        self.frames = 0                                       # busy-map updates so far
        self._saved_t = time.monotonic()
        self._kernel3 = np.ones((3, 3), np.uint8)
        self._kernel7 = np.ones((7, 7), np.uint8)
        self._load()
        self.restart(time.time())

    # --------------------------------------------------------- busy map I/O
    def _load(self):
        if not self.busy_path:
            return
        try:
            b = self.np.load(self.busy_path, allow_pickle=False)
            if b.shape == (self.H, self.W) and b.dtype == self.np.uint8:
                self.busy = b.astype(self.np.float32) / 255.0
                self.frames = self.WARMUP   # a remembered map: no need to wait
        except (OSError, ValueError):
            pass

    def save(self):
        """Persist the low-res busy map (motion frequencies only, no image)."""
        if not self.busy_path:
            return
        try:
            tmp = str(self.busy_path) + ".tmp"
            with open(tmp, "wb") as f:
                self.np.save(f, (self.np.clip(self.busy, 0, 1) * 255).astype(self.np.uint8))
            os.replace(tmp, self.busy_path)
        except OSError as e:
            log.debug("camera: couldn't save busy map: %s", e)
        self._saved_t = time.monotonic()

    def restart(self, now: float, resume: bool = False):
        """The camera (re)opened: forget the previous frame. After a short
        sleep (`resume`), keep the fixture memory with the time the camera was
        off left out (a poster stays a poster); otherwise forget it."""
        self.prev = None          # previous tiny blurred frame (memory only)
        self.moving = 0           # consecutive frames with a person-sized blob
        self.n = 0                # frames since restart
        self.t0 = now
        paused = getattr(self, "_paused_t", None)
        if resume and paused is not None and getattr(self, "last_moved", None) is not None:
            self.last_moved += max(0.0, now - paused)
        else:
            self.last_moved = self.np.full((self.H, self.W), now, self.np.float64)
        self._paused_t = None

    def pause(self, now: float):
        """The camera closed (memory of the scene is kept, no image)."""
        self.prev = None
        self._paused_t = now

    def mask(self):
        """Busy-background pixels (True = ignored by the motion check)."""
        m = (self.busy > self.BUSY_ON).astype(self.np.uint8)
        return self.cv2.dilate(m, self._kernel3) > 0

    def busy_pct(self) -> float:
        return round(100.0 * float(self.mask().mean()), 1)

    # ------------------------------------------------------------- signals
    def motion(self, small, now: float):
        """Update the busy map from this 160x120 frame; return
        (blob or None, candidate, info). blob = (area_frac, cx, cy, w_frac, h_frac);
        candidate = something sizeable moved (worth a deep look)."""
        cv2, np = self.cv2, self.np
        prev, self.prev = self.prev, small
        if prev is None:
            return None, False, "first frame"
        cur = small.astype(np.int16)
        d = cur - prev.astype(np.int16)
        d -= int(np.median(d))                      # global brightness shift (exposure, clouds)
        moved = np.abs(d) > self.DIFF_T
        frac = float(moved.mean())
        if frac > self.GLOBAL_FRAC:
            self.moving = 0
            return None, False, f"global change {frac:.0%} (skipped)"
        a = 1.0 / self.BUSY_TAU
        self.busy *= (1.0 - a)
        self.busy[moved] += a
        self.frames += 1
        busy = self.mask()
        live = (moved & ~busy).astype(np.uint8)
        self.last_moved[live > 0] = now
        live = cv2.morphologyEx(live, cv2.MORPH_OPEN, self._kernel3)   # drop speckle
        live = cv2.dilate(live, self._kernel7)                          # join a body's edges
        info = f"motion {frac:.1%}, masked {float((moved & busy).mean()):.1%}"
        if self.frames < self.WARMUP:
            return None, False, info + " (learning background)"
        contours, _ = cv2.findContours(live, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            return None, False, info
        c = max(contours, key=cv2.contourArea)
        area = cv2.contourArea(c) / (self.W * self.H)
        bx, by, bw, bh = cv2.boundingRect(c)
        fill = cv2.contourArea(c) / max(1, bw * bh)
        blob = (area, (bx + bw / 2) / self.W, (by + bh / 2) / self.H, bw / self.W, bh / self.H)
        desc = f"blob {area:.1%} fill {fill:.2f} {bw}x{bh} at x={blob[1]:.2f},y={blob[2]:.2f}"
        candidate = area >= self.MIN_BLOB / 4
        if not (self.MIN_BLOB <= area <= self.MAX_BLOB and bh / self.H >= self.MIN_BLOB_H
                and fill >= self.MIN_FILL):
            return None, candidate, info + "; " + desc + " (not person-like)"
        return blob, True, info + "; " + desc

    def _static(self, box, w, h, now: float) -> bool:
        """Has nothing moved around this detection box (image coords) for STATIC_S?"""
        x, y, bw, bh = box
        sx, sy = self.W / w, self.H / h
        x0, y0 = max(0, int((x - bw * 0.25) * sx)), max(0, int((y - bh * 0.25) * sy))
        x1, y1 = min(self.W, int((x + bw * 1.25) * sx) + 1), min(self.H, int((y + bh * 1.25) * sy) + 1)
        region = self.last_moved[y0:y1, x0:x1]
        if region.size == 0:
            return False
        return now - float(region.max()) > self.STATIC_S

    def _pick(self, boxes, w, h, now, kind):
        """Largest non-fixture box -> (box, n_fixtures)."""
        live, fixed = [], 0
        for b in boxes:
            if self._static(b, w, h, now):
                fixed += 1
            else:
                live.append(b)
        if not live:
            return None, fixed
        return max(live, key=lambda b: b[2] * b[3]), fixed

    def look(self, frame, now: float | None = None) -> Sighting:
        cv2 = self.cv2
        now = time.time() if now is None else now
        gray = frame if frame.ndim == 2 else cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        h, w = gray.shape[:2]
        small = cv2.GaussianBlur(cv2.resize(gray, (self.W, self.H), interpolation=cv2.INTER_AREA), (5, 5), 0)
        self.n += 1
        blob, candidate, minfo = self.motion(small, now)
        self.moving = self.moving + 1 if blob else 0
        notes = [minfo]
        if self.busy_path and time.monotonic() - self._saved_t > self.SAVE_EVERY_S:
            self.save()

        eq = cv2.equalizeHist(gray)
        faces = self.face.detectMultiScale(eq, scaleFactor=1.2, minNeighbors=5, minSize=(20, 20))
        box, fixed = self._pick([tuple(int(v) for v in f) for f in faces], w, h, now, "face")
        if box:
            return Sighting(True, (box[0] + box[2] / 2) / w, f"face {box[2]}x{box[3]} at x={(box[0] + box[2] / 2) / w:.2f}", "face")
        if fixed:
            notes.append(f"{fixed} static face(s) ignored")

        if candidate or self.n % self.DEEP_EVERY == 0:
            found = self._deep(gray, eq, w, h, now, notes)
            if found:
                return found

        # person-sized motion counts once it lasts two frames (one flicker isn't a person)
        if blob and self.moving >= 2:
            return Sighting(True, blob[1], "motion " + minfo.split("; ", 1)[-1], "motion")
        return Sighting(False, None, "; ".join(notes), "skip" if "skipped" in minfo else "none")

    def _deep(self, gray, eq, w, h, now, notes) -> Sighting | None:
        """HOG people detector, then profile faces (left/right on alternate looks)."""
        cv2 = self.cv2
        rects, weights = self.hog.detectMultiScale(gray, winStride=(8, 8), padding=(8, 8), scale=1.05)
        people = [tuple(int(v) for v in r) for r, wt in zip(rects, self.np.ravel(weights))
                  if float(wt) >= self.HOG_MIN_WEIGHT]
        box, fixed = self._pick(people, w, h, now, "person")
        if box:
            cx = (box[0] + box[2] / 2) / w
            return Sighting(True, cx, f"person {box[2]}x{box[3]} at x={cx:.2f}", "person")
        if fixed:
            notes.append(f"{fixed} static person(s) ignored")
        if self.profile is not None:
            for flip in ((self.n // self.DEEP_EVERY) % 2 == 1,):   # one direction per deep look, alternating
                img = cv2.flip(eq, 1) if flip else eq
                found = self.profile.detectMultiScale(img, scaleFactor=1.2, minNeighbors=5, minSize=(20, 20))
                boxes = [tuple(int(v) for v in f) for f in found]
                if flip:
                    boxes = [(w - x - fw, y, fw, fh) for x, y, fw, fh in boxes]
                box, fixed = self._pick(boxes, w, h, now, "profile")
                if box:
                    cx = (box[0] + box[2] / 2) / w
                    return Sighting(True, cx, f"profile face {box[2]}x{box[3]} at x={cx:.2f}", "profile")
        notes.append("deep look: nobody")
        return None


class Camera:
    """Background thread: grab a frame ~`fps` times a second while `want`
    is set, run the detector, report an Observation. Frames never leave
    this thread and are dropped immediately."""

    RETRY_S = 60.0

    DEBUG_EVERY_S = 5.0   # debug log: at most this often (sooner when the signal kind changes)
    REC_EVERY_S = 2.0     # face recognition: at most this often, and only when someone is seen

    def __init__(self, device, width: int, height: int, fps: float, report, on_active,
                 busy_path=None, debug: bool = False):
        self.device = device
        self.width, self.height = width, height
        self.period = 1.0 / max(0.2, min(fps, 5.0))
        self.report = report          # called (in this thread) with each Observation
        self.on_active = on_active    # called (in this thread) with True/False when the camera opens/closes
        self.want = threading.Event()
        self.resume = False           # next open is a wake from a short sleep (keep fixture memory)
        self._quit = threading.Event()
        self._nudge = threading.Event()   # cuts a wait short (want/period changed)
        self.problem: str | None = None
        self.active = False
        self.busy_path = busy_path
        self.debug = debug                 # log (text only) which signal fired
        self.last: Sighting | None = None  # the latest verdict (text only)
        self.busy_pct: float | None = None
        self.look_ms: float | None = None  # average detector time per frame
        self._dbg_t, self._dbg_kind = 0.0, None
        self._thread: threading.Thread | None = None
        # who is there (people.py): set by Presence
        self.people = None            # People (enrolled embeddings + matchers)
        self.recognise = False        # [senses] recognise_faces
        self.enrol = None             # FaceEnrolment while someone is enrolling
        self.rec_ms: float | None = None
        self._face_model = None
        self._face_err: str | None = None
        self._rec_t = -1e9

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="walter-camera", daemon=True)
            self._thread.start()

    def close(self):
        self._quit.set()
        self.want.set()
        self._nudge.set()

    def set_want(self, on: bool, resume: bool = False):
        if on and not self.want.is_set():
            self.resume = resume
            self.want.set()
        elif not on and self.want.is_set():
            self.want.clear()
            self._nudge.set()          # release the device now, not after the current wait

    def set_period(self, seconds: float):
        seconds = max(0.2, min(float(seconds), 30.0))
        if abs(seconds - self.period) > 1e-6:
            faster = seconds < self.period
            self.period = seconds
            if faster:
                self._nudge.set()

    def _set_active(self, on: bool):
        if on != self.active:
            self.active = on
            self.on_active(on)

    def _fail(self, msg: str):
        if msg != self.problem:  # log each distinct problem once
            log.warning("camera: %s (will retry every %d s)", msg, self.RETRY_S)
        self.problem = msg

    def _debug(self, sight: "Sighting", ms: float):
        """Rate-limited text log of what fired (never any image data)."""
        now = time.monotonic()
        if sight.kind == self._dbg_kind and now - self._dbg_t < self.DEBUG_EVERY_S:
            return
        self._dbg_t, self._dbg_kind = now, sight.kind
        log.info("camera debug: %s: %s | busy mask %.1f%% | %.0f ms",
                 "SEEN" if sight.seen else "nobody", sight.why, self.busy_pct or 0.0, ms)

    def _who(self, cv2, frame, seen: bool):
        """Face recognition on this frame, if wanted: a list with a name (or None
        for an unknown face) per face found, or None if it didn't run. While
        enrolling, good samples go to the enrolment instead (no names)."""
        enrol, people = self.enrol, self.people
        if people is None or (enrol is None and not self.recognise):
            return None
        now = time.monotonic()
        if enrol is None and (not seen or now - self._rec_t < self.REC_EVERY_S or not len(people.face)):
            return None
        self._rec_t = now
        if self._face_model is None:
            if self._face_err:
                return None
            try:
                from .people import FaceModel
                self._face_model = FaceModel(cv2, people.models_dir)
            except Exception as e:
                self._face_err = str(e)
                log.warning("face recognition unavailable: %s", e)
                return None
        t = time.perf_counter()
        try:
            if enrol is not None:
                from .people import FaceEnrolment
                enrol.feed(self._face_model.faces(frame, FaceEnrolment.MIN_SIZE, FaceEnrolment.MIN_SHARP, 2), now)
                return None
            matcher = people.face
            names = [matcher.match(f.emb).name for f in self._face_model.faces(frame)]
        except Exception as e:
            log.warning("face recognition failed: %s", e)
            return None
        finally:
            ms = (time.perf_counter() - t) * 1000
            self.rec_ms = ms if self.rec_ms is None else 0.8 * self.rec_ms + 0.2 * ms
        return names

    def _run(self):
        try:  # lower this thread's priority: speech recognition comes first
            os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 10)
        except (AttributeError, OSError):
            pass
        try:
            import cv2
        except ImportError:
            self._fail("OpenCV isn't installed (pip install opencv-python-headless)")
            return
        try:
            detector = Detector(cv2, self.busy_path)
        except Exception as e:
            self._fail(f"detector unavailable: {e}")
            return
        while not self._quit.is_set():
            self.want.wait()
            if self._quit.is_set():
                break
            dev = self.device
            if isinstance(dev, str) and dev.isdigit():
                dev = int(dev)
            cap = cv2.VideoCapture(dev, cv2.CAP_V4L2) if isinstance(dev, int) else cv2.VideoCapture(dev)
            if not cap.isOpened():
                cap.release()
                self._fail(f"can't open {self.device} (missing or busy)")
                self._quit.wait(self.RETRY_S)
                continue
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            cap.set(cv2.CAP_PROP_FPS, 5)
            fails = 0
            detector.restart(time.time(), resume=self.resume)
            self.resume = False
            try:
                while self.want.is_set() and not self._quit.is_set():
                    t0 = time.monotonic()
                    cap.grab()                  # drop a stale buffered frame
                    ok, frame = cap.read()
                    if not ok or frame is None:
                        fails += 1
                        if fails >= 5:
                            self._fail(f"{self.device} stopped giving frames (busy or unplugged)")
                            break
                        self._quit.wait(1.0)
                        continue
                    if fails or self.problem:
                        if self.problem:
                            log.info("camera: working again")
                        fails, self.problem = 0, None
                    self._set_active(True)
                    if frame.shape[1] > self.width * 1.5:  # driver ignored our size: shrink it
                        frame = cv2.resize(frame, (self.width, self.height))
                    t1 = time.perf_counter()
                    sight = detector.look(frame)
                    ms = (time.perf_counter() - t1) * 1000
                    faces = self._who(cv2, frame, sight.seen)
                    del frame                   # the image is gone; only yes/no, position, names remain
                    self.look_ms = ms if self.look_ms is None else 0.95 * self.look_ms + 0.05 * ms
                    self.last = sight
                    self.busy_pct = detector.busy_pct()
                    if self.debug:
                        self._debug(sight, ms)
                    self.report(Observation(time.time(), sight.seen, sight.x, faces))
                    self._nudge.clear()
                    while (self.want.is_set() and not self._quit.is_set()
                           and time.monotonic() - t0 < self.period):
                        self._nudge.wait(max(0.05, self.period - (time.monotonic() - t0)))
                        self._nudge.clear()
            except Exception as e:
                self._fail(f"error: {e}")
            finally:
                cap.release()             # device closed: the webcam light goes off
                detector.pause(time.time())
                detector.save()
                self._set_active(False)
            if self.want.is_set() and not self._quit.is_set():
                self._quit.wait(self.RETRY_S)  # it failed: try again in a while


class Presence:
    """Glue between the camera, the tracker, the sound gate and the conductor."""

    TICK_S = 2.0
    ARRIVAL_WAIT_S = 5.0   # an arrival waits this long for a face to be recognised
    WHO_WINDOW_S = 30.0    # "who is there": recognised within this long

    def __init__(self, cfg, conductor, send):
        self.cfg = cfg
        self.conductor = conductor
        self.send = send
        s = cfg.senses
        self.tracker = PresenceTracker(arrive_after_s=s.arrive_after_min * 60,
                                       empty_after_s=s.empty_after_min * 60,
                                       greet_cooldown_s=s.greet_cooldown_min * 60)
        base = 1.0 / max(0.2, min(float(s.fps), 5.0))
        self.gate = CameraGate(sleep_after_s=s.sleep_after_min * 60, verify_s=s.verify_s,
                               wake_ratio=s.wake_ratio, cooldown_s=s.wake_cooldown_s,
                               base_period=base, sleep_enabled=bool(s.camera_sleep))
        self.camera: Camera | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.camera_on = False   # frames are actually flowing
        self._resume = False     # the camera slept on purpose: its next start keeps what we knew
        self._period = None
        self.people = None       # walter.people.People, set by main (None = no recognition)
        self.enrolling = False   # a face enrolment is running: camera on, fast
        self.seen_people: dict[str, float] = {}   # name -> last time recognised
        self.unknown_face_t = -1e18               # last time a face matched nobody
        self._pending_arrival: dict | None = None

    @property
    def recognise(self) -> bool:
        return bool(self.cfg.senses.recognise_faces) and self.people is not None

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.senses.camera)

    def set_enabled(self, on: bool):
        self.cfg.senses.camera = bool(on)
        self._apply()

    def set_debug(self, on: bool):
        """Log (text only) which signal makes the camera think someone's there,
        and why the camera sleeps / wakes."""
        self.cfg.senses.debug = bool(on)
        if self.camera is not None:
            self.camera.debug = bool(on)

    # ------------------------------------------------------------ the gate
    def _changed(self, before: str):
        """The gate changed state: log why (text only) and apply it."""
        g = self.gate
        msg = "camera %s -> %s: %s"
        if self.cfg.senses.debug:
            log.info("camera debug: " + msg, before, g.state, g.reason)
        else:
            log.debug(msg, before, g.state, g.reason)
        if g.state == CameraGate.SLEEPING:
            self._resume = True
        elif g.state == CameraGate.OFF:
            self._resume = False
        self.send({"t": "camera", "on": self.camera_on, "state": g.state})
        self._apply()

    def _gate(self, fn, *a):
        before = self.gate.state
        if fn(*a):
            self._changed(before)

    def on_sound(self, ratio: float):
        """Mic loudness relative to its adaptive background (from the ears)."""
        self._gate(self.gate.sound, time.time(), ratio)

    def on_voice(self, strong: bool = False):
        """Voice activity (strong: his name was said)."""
        self._gate(self.gate.voice, time.time(), strong)

    def tick(self, now: float | None = None):
        now = time.time() if now is None else now
        allowed = self.enabled and not self.conductor.in_quiet_hours()
        self._gate(self.gate.allow, now, allowed,
                   "camera allowed: watching" if allowed
                   else ("quiet hours" if self.enabled else "camera disabled"))
        if self.gate.state == CameraGate.SLEEPING:
            # The camera is off and the mic quiet: count it as nobody seen, so
            # "stepped out" and "room empty" (doze) still happen on time.
            for ev in self.tracker.observe(Observation(now, False)):
                self._event(ev)
        self._gate(self.gate.tick, now, self.tracker.present, self.tracker.unseen_for(now))
        self._apply()

    def _apply(self):
        """Camera open only while the gate wants it (released otherwise: LED off)."""
        g = self.gate
        want = self.enabled and (self.enrolling or (not self.conductor.in_quiet_hours() and g.camera_wanted))
        if want and self.camera is None:
            s = self.cfg.senses
            self.camera = Camera(s.camera_device, s.width, s.height, s.fps,
                                 self._from_thread(self._observed), self._from_thread(self._active),
                                 busy_path=self.cfg.data_dir / "presence-busy.npy", debug=s.debug)
            self.camera.start()
        if self.camera is not None:
            self.camera.people = self.people
            self.camera.recognise = self.recognise
            self.camera.set_want(want, resume=self._resume)
            period = 0.25 if self.enrolling else g.period(time.time(), self.tracker.present)
            if period != self._period:
                if self._period is not None and self.cfg.senses.debug and want:
                    log.info("camera debug: now looking every %.0f s%s", period,
                             " (someone's there and it's quiet)" if period >= g.still_period
                             else " (someone's there)" if period > g.base_period else "")
                self._period = period
            self.camera.set_period(period)

    def _from_thread(self, fn):
        def call(*a):
            if self.loop and not self.loop.is_closed():
                self.loop.call_soon_threadsafe(fn, *a)
        return call

    def _active(self, on: bool):
        self.camera_on = on
        slept = self.gate.state == CameraGate.SLEEPING
        if on:
            if not self._resume:
                self.tracker.reset(time.time())
            self._resume = False  # a wake from sleep keeps "last seen" (so arrivals still count)
        elif not slept:
            self.conductor.undoze()  # no camera, no evidence the room is empty
        self.send({"t": "camera", "on": on, "state": self.gate.state})
        log.info("camera %s%s", "on" if on else "off", f" ({self.gate.state})")

    def _observed(self, ob: Observation):
        if not self.camera_on:
            return
        if ob.faces is not None:
            self._saw_faces(ob.faces, ob.t)
        # Voices count as someone being there too (keeps him from dozing on a sitting talker).
        # Only real transcribed speech: noise that merely opened the mic doesn't count.
        # Not while verifying a wake-up, though: then only the camera's own eyes count
        # (a TV talks too).
        if (not ob.seen and self.gate.state == CameraGate.WATCHING
                and time.time() - getattr(self.conductor, "last_speech", 0.0) < 60):
            ob = Observation(ob.t, True, None)
        for ev in self.tracker.observe(ob):
            self._event(ev)
        before = self.gate.state
        if self.gate.tick(ob.t, self.tracker.present, self.tracker.unseen_for(ob.t)):
            self._changed(before)

    # ------------------------------------------------------------- who
    def _saw_faces(self, names: list, t: float):
        for n in names:
            if n is None:
                self.unknown_face_t = t
                continue
            if t - self.seen_people.get(n, -1e18) > 120:
                log.info("presence: recognised %s by face", n)
            self.seen_people[n] = t
        if self._pending_arrival is not None and any(names):
            self._flush_arrival()

    def who_now(self, window: float | None = None, now: float | None = None) -> list[str]:
        """Who is (probably) there: recognised names, most recent first, plus
        "someone" for anyone unrecognised. [] if nobody is there."""
        now = time.time() if now is None else now
        window = self.WHO_WINDOW_S if window is None else window
        names = [n for n, t in sorted(self.seen_people.items(), key=lambda kv: -kv[1]) if now - t <= window]
        unknown = now - self.unknown_face_t <= window
        if not names and (self.tracker.present or unknown):
            return ["someone"]
        return names + (["someone"] if unknown else [])

    def _flush_arrival(self):
        ev, self._pending_arrival = self._pending_arrival, None
        if ev is None:
            return
        ev["who"] = self.who_now()
        log.info("presence: arrival is %s", ", ".join(ev["who"]))
        self.conductor.on_presence_arrival(ev)

    async def enroll_face(self, name: str, screen) -> dict:
        """Guided ~15 s capture: instructions on the face card, ~20 good samples,
        embeddings saved (never images)."""
        from .people import FaceEnrolment, check_name
        name = check_name(name)
        if self.people is None:
            return {"ok": False, "error": "recognition isn't set up"}
        if not self.enabled:
            return {"ok": False, "error": "the camera is off ([senses] camera = false or walterctl camera off)"}
        if self.enrolling:
            return {"ok": False, "error": "an enrolment is already running"}
        sess = FaceEnrolment()
        self.enrolling = True
        title = f"Learning your face, {name}"
        await screen.card(title, "Stand about an arm's length from me, just you in view.\n\nStarting the camera...", 60)
        try:
            self._apply()
            for _ in range(50):
                if self.camera_on:
                    break
                await asyncio.sleep(0.2)
            if not self.camera_on or self.camera is None:
                why = (self.camera.problem if self.camera and self.camera.problem else "it didn't start")
                await screen.card("Camera trouble", f"I couldn't open my camera: {why}", 10)
                return {"ok": False, "error": f"camera unavailable: {why}"}
            self.camera.enrol = sess
            steps = ["Look straight at me", "Now turn your head slightly to your left",
                     "And slightly to your right", "Back to the middle: look at me again"]
            t0, phase, shown = time.monotonic(), -1, None
            while True:
                el = time.monotonic() - t0
                p = min(3, int(el // 5))
                if p != phase:
                    phase = p
                    sess.set_phase(p, cap=None if p == 3 else sess.per_phase)
                if (el >= 15 and sess.n >= sess.target) or el >= 30:
                    break
                bar = "#" * min(sess.n, sess.target) + "." * max(0, sess.target - sess.n)
                body = f"{steps[phase]}...\n\n{bar}  {sess.n} / {sess.target}"
                if sess.note not in ("ok", "got this angle"):
                    body += f"\n({sess.note})"
                if body != shown:
                    shown = body
                    await screen.card(title, body, 30)
                await asyncio.sleep(0.3)
        finally:
            if self.camera is not None:
                self.camera.enrol = None
            self.enrolling = False
            self._apply()
        embs = sess.result()
        if len(embs) < 8:
            await screen.card("Let's try that again", f"I only got {len(embs)} clear looks (need at least 8). "
                              "More light, a bit closer, and just you in view helps.", 15)
            return {"ok": False, "samples": int(len(embs)), "error": "not enough clear face samples"}
        meta = self.people.save(name, "face", embs)
        info = meta["face"]
        await screen.card(f"Got it, {name}!", f"{info['samples']} face samples saved (numbers only, no photos).", 8)
        return {"ok": True, "name": name, "kind": "face", **info}

    def _event(self, ev: dict):
        log.info("presence: %s", {k: (round(v) if isinstance(v, float) and k.endswith("_s") else v)
                                   for k, v in ev.items() if k != "x"})
        if ev["e"] == "arrival" and self.recognise and len(self.people.face):
            # Give the face recogniser a moment to say who it is (it runs every 2 s).
            if not [n for n in self.who_now(10, ev.get("t")) if n != "someone"] and self.loop is not None:
                self._pending_arrival = ev
                self.loop.call_later(self.ARRIVAL_WAIT_S, self._flush_arrival)
                return
        if ev["e"] == "arrival":
            ev["who"] = self.who_now()
        if ev["e"] in ("arrival", "present"):
            self.conductor.on_presence_arrival(ev) if ev["e"] == "arrival" else self.conductor.undoze()
        elif ev["e"] == "empty":
            self.conductor.doze()

    async def run(self):
        self.loop = asyncio.get_running_loop()
        while True:
            try:
                self.tick()
            except Exception:
                log.exception("presence tick failed")
            await asyncio.sleep(self.TICK_S)

    def close(self):
        if self.camera:
            self.camera.close()

    def status(self) -> dict:
        now = time.time()
        g = self.gate
        if not self.enabled:
            cam = "disabled"
        elif self.conductor.in_quiet_hours():
            cam = "off (quiet hours)"
        elif g.state == CameraGate.SLEEPING:
            cam = "sleeping (released; a sound wakes it)"
        elif self.camera_on:
            cam = "on"
        else:
            cam = "unavailable: " + (self.camera.problem if self.camera and self.camera.problem else "starting")
        d = {"camera": cam, "dozing": self.conductor.dozing}
        if self.enabled:
            d.update(g.status(now))
            if g.camera_wanted:
                d["look_every_s"] = round(g.period(now, self.tracker.present), 1)
        if self.camera_on or g.state == CameraGate.SLEEPING:
            d.update(self.tracker.status(now))
        cam = self.camera
        if self.camera_on and cam is not None:
            d["busy_mask_pct"] = cam.busy_pct
            d["look_ms"] = None if cam.look_ms is None else round(cam.look_ms)
            d["last_signal"] = cam.last.why if cam.last and cam.last.seen else (cam.last.kind if cam.last else None)
        d["who"] = self.who_now(now=now) if (self.camera_on or self.tracker.present) else []
        if self.people is not None:
            d["recognise_faces"] = self.recognise
            d["faces_enrolled"] = len(self.people.face)
            if self.camera is not None and self.camera.rec_ms is not None:
                d["recognise_ms"] = round(self.camera.rec_ms)
            if self.camera is not None and self.camera._face_err:
                d["recognise_problem"] = self.camera._face_err
        if self.enrolling:
            d["enrolling"] = True
        d["debug"] = bool(self.cfg.senses.debug)
        return d
