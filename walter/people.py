"""People: *who* is there? Opt-in, fully local face and voice recognition.

PRIVACY. Nobody is recognised unless they enrolled on purpose
(`walterctl enroll face|voice NAME`). Enrolment and recognition keep only
embeddings: short lists of numbers a model computes from a face or a voice
(128 numbers per face, 192 per voice sample). No image or audio is ever
saved; frames and sound are looked at in memory and dropped. Everything runs
on this computer; nothing is sent anywhere. The embeddings live in
~/.walter/people/<name>/{face,voice}.npy (never in the git repo) and
`walterctl forget NAME` deletes them. Switches: [senses] recognise_faces /
recognise_voices.

Models (downloaded once into ~/.walter/models by `python -m walter.people fetch`):
  face:  OpenCV Zoo YuNet (face detector, 0.2 MB, ~8 ms per 320x240 frame) and
         SFace (face embedding, 38 MB, ~40 ms per face) on this i7-6600U;
  voice: NVIDIA NeMo TitaNet-small speaker embeddings (sherpa-onnx export,
         40 MB, ~6M parameters, 192 numbers; ~80 ms per 4 s of speech on one
         core). Picked over CAM++ (similar size and speed) because in a test
         with four synthetic voices TitaNet kept same-voice and different-voice
         scores apart (0.77+ vs <0.40) where CAM++ mixed them up.

Matching: cosine similarity. A person's score is the mean of the 3 best
similarities to their enrolment samples (robust to one odd sample, and it
copes with head turns). A match needs the score to reach that person's
threshold *and* beat the next enrolled person by a small gap; everything
else stays "someone" / "unknown". Each threshold is tuned at enrolment from
how alike the person's own samples are (leave-one-out), then clamped into a
conservative band per kind (see TUNING).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import numpy as np

log = logging.getLogger("walter.people")

KINDS = ("face", "voice")

FACE_DET_MODEL = "face_detection_yunet_2023mar.onnx"
FACE_REC_MODEL = "face_recognition_sface_2021dec.onnx"
VOICE_MODEL = "nemo_en_titanet_small.onnx"
MODELS = {
    FACE_DET_MODEL: ("https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/"
                     "face_detection_yunet_2023mar.onnx",
                     "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"),
    FACE_REC_MODEL: ("https://github.com/opencv/opencv_zoo/raw/main/models/face_recognition_sface/"
                     "face_recognition_sface_2021dec.onnx",
                     "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79"),
    VOICE_MODEL: ("https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/"
                  "nemo_en_titanet_small.onnx",
                  "ad4a1802485d8b34c722d2a9d04249662f2ece5d28a7a039063ca22f515a789e"),
}


@dataclass(frozen=True)
class Tuning:
    floor: float        # never accept a match below this (impostor scores stay under it)
    cap: float          # never demand more than this (or real-life looks/voices would fail)
    margin: float       # threshold = 5th percentile of own-sample scores minus this
    top_k: int = 3      # person score = mean of the k best sample similarities
    rival_gap: float = 0.05   # the best person must beat the next one by this much


# SFace's published same-person cosine threshold is 0.363 (LFW); we sit well
# above it. TitaNet: different speakers mostly < 0.4, the same speaker ~0.6-0.9.
TUNING = {"face": Tuning(floor=0.42, cap=0.55, margin=0.30),
          "voice": Tuning(floor=0.50, cap=0.70, margin=0.20)}

NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9 '\-]{0,39}$")


# ----------------------------------------------------------------- maths
def normalise(x) -> np.ndarray:
    """Rows scaled to unit length (float32, 2-D)."""
    a = np.asarray(x, np.float32)
    if a.ndim == 1:
        a = a[None, :]
    n = np.linalg.norm(a, axis=1, keepdims=True)
    return a / np.maximum(n, 1e-9)


def person_score(q: np.ndarray, embs: np.ndarray, top_k: int = 3) -> float:
    """Mean of the `top_k` best cosine similarities of unit vector q to unit rows embs."""
    sims = embs @ q
    k = max(1, min(top_k, len(sims)))
    return float(np.mean(np.sort(sims)[-k:]))


def tune_threshold(embs, kind: str, groups=None) -> tuple[float, dict]:
    """Threshold for one person from their enrolment samples alone.

    Leave-one-out: score each sample against the others (excluding samples
    from the same `group`, e.g. overlapping windows of one sentence), take
    the 5th percentile of those genuine scores and step down by the margin,
    then clamp into [floor, cap]. A very consistent enrolment gets a
    stricter threshold; a varied one never drops below the floor."""
    t = TUNING[kind]
    e = normalise(embs)
    n = len(e)
    stats = {"samples": n}
    if n < 2:
        return t.floor, {**stats, "threshold": t.floor}
    sims = e @ e.T
    mask = np.eye(n, dtype=bool)
    if groups is not None:
        g = np.asarray(groups)
        mask |= g[:, None] == g[None, :]
    genuine = []
    for i in range(n):
        others = sims[i][~mask[i]]
        if len(others):
            k = min(t.top_k, len(others))
            genuine.append(float(np.mean(np.sort(others)[-k:])))
    if not genuine:
        return t.floor, {**stats, "threshold": t.floor}
    g = np.asarray(genuine)
    p5 = float(np.percentile(g, 5))
    thr = float(np.clip(p5 - t.margin, t.floor, t.cap))
    return round(thr, 3), {**stats, "own_p5": round(p5, 3), "own_median": round(float(np.median(g)), 3),
                           "threshold": round(thr, 3)}


@dataclass
class Match:
    name: str | None        # who it is, or None (unknown / someone)
    score: float            # best person's score (0 if nobody enrolled)
    candidate: str | None   # the best-scoring enrolled person, even if not accepted
    threshold: float

    def as_dict(self) -> dict:
        return {"name": self.name, "score": round(self.score, 3), "candidate": self.candidate,
                "threshold": round(self.threshold, 3)}


class Matcher:
    """Enrolled people of one kind -> who does this embedding belong to?"""

    def __init__(self, people: dict, kind: str, override: float = 0.0):
        """people: {name: (embeddings (N, D), threshold)}; override > 0 replaces every threshold."""
        self.kind = kind
        self.tuning = TUNING[kind]
        self.people = {n: (normalise(e), float(override or thr)) for n, (e, thr) in people.items() if len(e)}

    def __len__(self):
        return len(self.people)

    def match(self, emb, extra: float = 0.0) -> Match:
        """`extra` raises the bar (e.g. for very short utterances)."""
        if not self.people:
            return Match(None, 0.0, None, 0.0)
        q = normalise(emb)[0]
        dims = {e.shape[1] for e, _ in self.people.values()}
        if q.shape[0] not in dims:
            return Match(None, 0.0, None, 0.0)
        scores = sorted(((person_score(q, e, self.tuning.top_k), n, thr)
                         for n, (e, thr) in self.people.items() if e.shape[1] == q.shape[0]), reverse=True)
        best, name, thr = scores[0]
        second = scores[1][0] if len(scores) > 1 else -1.0
        thr = thr + extra
        ok = best >= thr and best - second >= self.tuning.rival_gap
        return Match(name if ok else None, best, name, thr)


# --------------------------------------------------------------- storage
def check_name(name: str) -> str:
    name = re.sub(r"\s+", " ", (name or "").strip())
    if not NAME_RE.match(name):
        raise ValueError("name must start with a letter: letters, digits, spaces, ' and - (max 40)")
    return name


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", check_name(name).lower()).strip("-")


class PeopleStore:
    """~/.walter/people/<slug>/{meta.json, face.npy, voice.npy}: embeddings only."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def _dir(self, name: str) -> Path:
        return self.root / slug(name)

    def _meta(self, d: Path) -> dict:
        try:
            return json.loads((d / "meta.json").read_text())
        except (OSError, ValueError):
            return {}

    def _write(self, path: Path, write):
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "wb") as f:
            write(f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)

    def save(self, name: str, kind: str, embs, info: dict | None = None) -> dict:
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}")
        name = check_name(name)
        e = normalise(embs)
        if not len(e):
            raise ValueError("no samples")
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        d = self._dir(name)
        d.mkdir(exist_ok=True)
        os.chmod(d, 0o700)
        self._write(d / f"{kind}.npy", lambda f: np.save(f, e, allow_pickle=False))
        meta = self._meta(d)
        meta["name"] = name
        meta[kind] = {"samples": len(e), "dim": int(e.shape[1]),
                      "enrolled": time.strftime("%Y-%m-%d %H:%M"), **(info or {})}
        self._write(d / "meta.json", lambda f: f.write(json.dumps(meta, indent=2).encode()))
        return meta

    def people(self) -> list[dict]:
        """[{name, face: {...} | None, voice: {...} | None}] (only what is really on disk)."""
        out = []
        if not self.root.is_dir():
            return out
        for d in sorted(p for p in self.root.iterdir() if p.is_dir()):
            meta = self._meta(d)
            name = meta.get("name") or d.name
            row = {"name": name}
            for k in KINDS:
                f = d / f"{k}.npy"
                row[k] = ({**(meta.get(k) or {}), "file": str(f), "bytes": f.stat().st_size}
                          if f.exists() else None)
            if row["face"] or row["voice"]:
                out.append(row)
        return out

    def load(self, kind: str) -> dict:
        """{name: (embeddings, threshold)} for everyone enrolled with `kind`."""
        out = {}
        for row in self.people():
            info = row.get(kind)
            if not info:
                continue
            try:
                e = np.load(info["file"], allow_pickle=False)
            except (OSError, ValueError) as err:
                log.warning("people: can't read %s: %s", info["file"], err)
                continue
            thr = float(info.get("threshold") or TUNING[kind].floor)
            out[row["name"]] = (e, thr)
        return out

    def forget(self, name: str, kind: str | None = None) -> list[str]:
        """Delete someone's face and/or voice embeddings. Returns the kinds removed."""
        d = self._dir(name)
        if not d.is_dir():
            return []
        kinds = [kind] if kind else list(KINDS)
        removed = []
        meta = self._meta(d)
        for k in kinds:
            f = d / f"{k}.npy"
            if f.exists():
                f.unlink()
                removed.append(k)
            meta.pop(k, None)
        if not any((d / f"{k}.npy").exists() for k in KINDS):
            shutil.rmtree(d, ignore_errors=True)
        elif removed:
            self._write(d / "meta.json", lambda f: f.write(json.dumps(meta, indent=2).encode()))
        return removed


class People:
    """Shared by presence (faces), ears (voices), the conductor and the server."""

    def __init__(self, cfg, root: Path | None = None, models_dir: Path | None = None):
        self.cfg = cfg
        self.store = PeopleStore(root or cfg.data_dir / "people")
        self.models_dir = Path(models_dir or cfg.data_dir / "models")
        self.face = Matcher({}, "face")
        self.voice = Matcher({}, "voice")
        self.reload()

    def reload(self):
        s = self.cfg.senses
        self.face = Matcher(self.store.load("face"), "face", s.face_threshold)
        self.voice = Matcher(self.store.load("voice"), "voice", s.voice_threshold)
        log.info("people: %d face(s), %d voice(s) enrolled", len(self.face), len(self.voice))

    def save(self, name, kind, embs, groups=None) -> dict:
        thr, stats = tune_threshold(embs, kind, groups)
        meta = self.store.save(name, kind, embs, {**stats, "threshold": thr})
        self.reload()
        return meta

    def forget(self, name, kind=None) -> list[str]:
        removed = self.store.forget(name, kind)
        self.reload()
        return removed

    def list(self) -> list[dict]:
        return self.store.people()

    def model(self, name: str) -> Path:
        return self.models_dir / name


# ------------------------------------------------------------ face model
@dataclass
class FaceSample:
    emb: np.ndarray
    x: float            # face centre, 0 = left edge of the image
    size: int           # face width in pixels
    score: float        # detector confidence
    sharp: float        # blur check: Laplacian variance / contrast of the aligned crop, x100


class FaceModel:
    """YuNet (find faces + landmarks) and SFace (aligned 112x112 crop -> 128 numbers).
    Crops exist only inside `faces()` and are dropped straight away."""

    MIN_CONF = 0.85

    def __init__(self, cv2, models_dir: Path):
        self.cv2 = cv2
        det, rec = Path(models_dir) / FACE_DET_MODEL, Path(models_dir) / FACE_REC_MODEL
        for p in (det, rec):
            if not p.exists():
                raise FileNotFoundError(f"{p} missing (run: .venv/bin/python -m walter.people fetch)")
        self.det = cv2.FaceDetectorYN.create(str(det), "", (320, 240), 0.8, 0.3, 50)
        self.rec = cv2.FaceRecognizerSF.create(str(rec), "")
        self._size = (320, 240)

    def faces(self, frame, min_size: int = 32, min_sharp: float = 0.5, max_faces: int = 3) -> list[FaceSample]:
        cv2 = self.cv2
        if frame.ndim == 2:
            frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
        h, w = frame.shape[:2]
        if (w, h) != self._size:
            self.det.setInputSize((w, h))
            self._size = (w, h)
        _, found = self.det.detect(frame)
        if found is None:
            return []
        out = []
        for row in sorted(found, key=lambda r: -r[2])[:max_faces]:
            if row[2] < min_size or row[3] < min_size or row[14] < self.MIN_CONF:
                continue
            crop = self.rec.alignCrop(frame, row)
            g = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY).astype(np.float64)
            # edge energy relative to contrast: a dim face isn't "blurry", a smeared one is
            sharp = float(100.0 * cv2.Laplacian(g, cv2.CV_64F).var() / max(float(g.var()), 1.0))
            if sharp < min_sharp:
                continue
            emb = np.asarray(self.rec.feature(crop), np.float32).ravel().copy()
            del crop
            out.append(FaceSample(emb, float((row[0] + row[2] / 2) / w), int(row[2]), float(row[14]), sharp))
        return out


class FaceEnrolment:
    """Collects good, varied face embeddings of exactly one person (camera thread feeds it)."""

    MIN_SIZE = 48        # px wide (in a 320x240 frame): about an arm's length away
    MIN_SHARP = 1.2      # reject blurry frames (moving head, out of focus); sharp faces score 2-4
    SPACING_S = 0.3      # at most ~3 samples a second
    SAME = 0.985         # near-identical to the previous sample: skip (adds nothing)
    STRANGER = 0.30      # very unlike the rest so far: someone else walked past

    def __init__(self, target: int = 20, per_phase: int = 8):
        self.target, self.per_phase = target, per_phase
        self.phase = 0
        self.cap: int | None = per_phase
        self.embs: list[np.ndarray] = []
        self.phase_n: dict[int, int] = {}
        self.note = "waiting for a face"
        self.rejected = 0
        self._last_t = -1e9
        self._lock = threading.Lock()

    def feed(self, faces: list[FaceSample], now: float):
        with self._lock:
            if len(faces) != 1:
                self.note = "no clear face" if not faces else "more than one face: just you, please"
                self.rejected += bool(faces)
                return
            if now - self._last_t < self.SPACING_S:
                return
            if self.cap is not None and self.phase_n.get(self.phase, 0) >= self.cap:
                self.note = "got this angle"
                return
            e = normalise(faces[0].emb)[0]
            if self.embs:
                if float(self.embs[-1] @ e) > self.SAME:
                    return
                if len(self.embs) >= 3:
                    c = normalise(np.mean(self.embs, axis=0))[0]
                    if float(c @ e) < self.STRANGER:
                        self.note = "that looked like someone else"
                        self.rejected += 1
                        return
            self.embs.append(e)
            self.phase_n[self.phase] = self.phase_n.get(self.phase, 0) + 1
            self._last_t = now
            self.note = "ok"

    @property
    def n(self) -> int:
        return len(self.embs)

    def set_phase(self, p: int, cap: int | None = None):
        """A new instruction (look straight / left / right...): up to `cap` samples (None = no limit)."""
        with self._lock:
            self.phase, self.cap = p, cap

    def result(self) -> np.ndarray:
        with self._lock:
            return np.array(self.embs, np.float32)


# ----------------------------------------------------------- voice model
class VoiceModel:
    """TitaNet-small speaker embeddings through sherpa-onnx (features + ONNX)."""

    SR = 16000
    MIN_S = 1.0

    def __init__(self, models_dir: Path, threads: int = 1):
        import sherpa_onnx
        p = Path(models_dir) / VOICE_MODEL
        if not p.exists():
            raise FileNotFoundError(f"{p} missing (run: .venv/bin/python -m walter.people fetch)")
        cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(p), num_threads=threads)
        self.ex = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)
        self.dim = self.ex.dim
        self._lock = threading.Lock()

    @staticmethod
    def as_float(pcm) -> np.ndarray:
        if isinstance(pcm, (bytes, bytearray, memoryview)):
            return np.frombuffer(pcm, np.int16).astype(np.float32) / 32768.0
        a = np.asarray(pcm)
        return a.astype(np.float32) / 32768.0 if a.dtype == np.int16 else a.astype(np.float32)

    def embed(self, audio) -> np.ndarray | None:
        a = self.as_float(audio)
        if len(a) < self.MIN_S * self.SR:
            return None
        with self._lock:
            s = self.ex.create_stream()
            s.accept_waveform(self.SR, a)
            s.input_finished()
            if not self.ex.is_ready(s):
                return None
            return np.asarray(self.ex.compute(s), np.float32)

    def windows(self, audio, win_s: float = 2.0, hop_s: float = 1.0) -> list[np.ndarray]:
        """Embeddings of overlapping windows plus the whole thing (enrolment samples)."""
        a = self.as_float(audio)
        out = []
        win, hop = int(win_s * self.SR), int(hop_s * self.SR)
        if len(a) >= win + hop // 2:
            for start in range(0, len(a) - win + 1, hop):
                e = self.embed(a[start:start + win])
                if e is not None:
                    out.append(e)
        e = self.embed(a)
        if e is not None:
            out.append(e)
        return out


ENROL_SENTENCES = [
    "The quick brown fox jumps over the lazy dog by the river.",
    "Please remind me to water the tomatoes on Thursday morning.",
    "A gentle breeze carried the smell of fresh bread down the street.",
    "Six thick thistle sticks, and a big bowl of warm vegetable soup.",
    "Could you play some quiet music while I cook dinner tonight?",
]


def enrol_page(name: str, heard: int, sentences=ENROL_SENTENCES) -> str:
    """The canvas during voice enrolment (Markdown)."""
    lines = [f"# Read these out loud, {name}", "",
             "Your normal voice, a short pause after each one.", ""]
    for i, t in enumerate(sentences):
        if i < heard:
            lines.append(f"{i + 1}. ~~{t}~~ (heard)")
        elif i == heard:
            lines.append(f"{i + 1}. **{t}**")
        else:
            lines.append(f"{i + 1}. {t}")
    lines += ["", f"**{min(heard, len(sentences))} of {len(sentences)} heard**",
              "", "Only voice numbers are kept, never a recording."]
    return "\n".join(lines)


class VoiceEnrolment:
    """Embeddings from the sentences someone reads aloud (ears feed it)."""

    def __init__(self, name: str, sentences: int = 5):
        self.name = name
        self.sentences = sentences
        self.embs: list[np.ndarray] = []
        self.groups: list[int] = []
        self.heard = 0
        self.texts: list[str] = []
        self._lock = threading.Lock()

    def feed(self, embs: list[np.ndarray], text: str):
        if not embs:
            return
        with self._lock:
            self.embs.extend(embs)
            self.groups.extend([self.heard] * len(embs))
            self.heard += 1
            self.texts.append(text)

    @property
    def done(self) -> bool:
        return self.heard >= self.sentences


class VoiceId:
    """The ears' side: speaker embedding for each finished utterance, and enrolment."""

    MAX_S = 10.0        # recognition looks at the last 10 s of a turn at most
    SHORT_S = 2.0       # shorter than this: the bar goes up a little
    SHORT_EXTRA = 0.05

    def __init__(self, people: People, model: VoiceModel | None = None):
        self.people = people
        self.model = model
        self.enrol: VoiceEnrolment | None = None

    def load(self) -> bool:
        if self.model is None:
            try:
                self.model = VoiceModel(self.people.models_dir)
            except Exception as e:
                log.warning("voice recognition unavailable: %s", e)
                return False
        return True

    def active(self) -> bool:
        return self.model is not None and (len(self.people.voice) > 0 or self.enrol is not None)

    def analyse(self, pcm: bytes) -> dict | None:
        """(Runs in a worker thread.) -> {"match": Match dict or None, "enrol": [embs] or None}."""
        if self.model is None:
            return None
        sr = VoiceModel.SR
        a = VoiceModel.as_float(pcm)
        dur = len(a) / sr
        out = {"dur": round(dur, 1), "match": None, "enrol": None}
        if self.enrol is not None:
            out["enrol"] = self.model.windows(a[-int(30 * sr):])
        if len(self.people.voice):
            e = self.model.embed(a[-int(self.MAX_S * sr):])
            if e is not None:
                m = self.people.voice.match(e, self.SHORT_EXTRA if dur < self.SHORT_S else 0.0)
                out["match"] = m.as_dict()
        return out


# ---------------------------------------------------------------- screen
class Screen:
    """Instructions on the face during enrolment (bin/walter-show card, bin/walter-canvas)."""

    def __init__(self):
        from .config import REPO_DIR
        self.bin = REPO_DIR / "bin"

    async def _run(self, *args, stdin: bytes | None = None):
        import asyncio
        try:
            p = await asyncio.create_subprocess_exec(
                sys.executable, *map(str, args), stdin=asyncio.subprocess.PIPE if stdin else None,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await asyncio.wait_for(p.communicate(stdin), 10)
        except Exception as e:
            log.debug("screen: %s failed: %s", args[:1], e)

    async def card(self, title: str, body: str, seconds: float = 60):
        await self._run(self.bin / "walter-show", title, body, seconds)

    async def clear(self):
        await self._run(self.bin / "walter-show", "--clear")

    async def canvas(self, markdown: str, title: str):
        await self._run(self.bin / "walter-canvas", "show", "-", "--md", "--title", title,
                        "--seconds", 0, stdin=markdown.encode())

    async def canvas_hide(self):
        await self._run(self.bin / "walter-canvas", "hide")


# ------------------------------------------------------------- downloads
def fetch_models(models_dir: Path) -> list[str]:
    """Download the recognition models (checked against their sha256)."""
    models_dir = Path(models_dir)
    models_dir.mkdir(parents=True, exist_ok=True)
    done = []
    for name, (url, sha) in MODELS.items():
        p = models_dir / name
        if p.exists() and hashlib.sha256(p.read_bytes()).hexdigest() == sha:
            done.append(f"{name}: ok")
            continue
        tmp = p.with_name(p.name + ".part")
        with urllib.request.urlopen(url, timeout=120) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f)
        got = hashlib.sha256(tmp.read_bytes()).hexdigest()
        if got != sha:
            tmp.unlink()
            raise RuntimeError(f"{name}: checksum mismatch ({got})")
        os.replace(tmp, p)
        done.append(f"{name}: downloaded")
    return done


if __name__ == "__main__":
    if sys.argv[1:] == ["fetch"]:
        from .config import DATA_DIR
        for line in fetch_models(DATA_DIR / "models"):
            print(line)
    else:
        print("usage: python -m walter.people fetch")
