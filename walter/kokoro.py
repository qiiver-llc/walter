"""Kokoro-82M: an optional, more natural local voice (trial; fully offline).

Runs the sherpa-onnx export of Kokoro v1.0 (model.int8.onnx + voices.bin +
lexicon-us-en.txt in ~/.walter/voices/kokoro/<dir>) straight through
onnxruntime, so there are no extra packages. Words are phonemised with the
Kokoro (misaki) US lexicon; words it lacks ("degrees" and other plurals,
names) go through espeak-ng, the one piper already ships. sherpa-onnx's own
Kokoro front end silently drops such words, which is why it isn't used here.

Config: [voice] engine = "kokoro" (default "piper"), kokoro_voice = "am_michael",
kokoro_dir, kokoro_threads, kokoro_speed. Any load or synthesis error falls
back to piper (voice.py keeps one voice per reply).
"""
from __future__ import annotations

import logging
import re
import threading
from pathlib import Path

import numpy as np

log = logging.getLogger("walter.kokoro")

MAX_TOKENS = 500          # the model's style table has 510 rows; leave room for the pads
SAMPLE_RATE = 24000
# American male voices worth comparing (names from the model's speaker table)
AMERICAN_MALE = ("am_michael", "am_puck", "am_fenrir", "am_adam", "am_echo", "am_eric",
                 "am_liam", "am_onyx")
DEMO_VOICES = ("am_michael", "am_puck", "am_fenrir")

_WORD = re.compile(r"[A-Za-z][A-Za-z']*|[0-9]+|[^\sA-Za-z0-9]")
# espeak IPA that Kokoro's table spells differently
_ESPEAK_FIX = str.maketrans({"r": "ɹ", "ɐ": "ə", "ɚ": "əɹ", "ʲ": "j", "x": "k", "ɬ": "l", "ɾ": "T",
                             "g": "ɡ"})
_ESPEAK_LOCK = threading.Lock()
# function words the lexicon spells as their stressed/letter form (misaki's choices)
_OVERRIDES = {"a": "ɐ", "A": "ɐ", "the": "ðə", "The": "ðə", "to": "tə", "of": "ʌv", "and": "ænd"}


def load_tokens(path: Path) -> dict[str, int]:
    vocab: dict[str, int] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line:
            continue
        sym, _, idx = line.rpartition(" ")
        if sym == "":           # the line for the space symbol is "  16"
            sym = " "
        vocab[sym] = int(idx)
    return vocab


def load_lexicon(path: Path) -> dict[str, str]:
    lex: dict[str, str] = {}
    with path.open(encoding="utf-8") as f:
        for line in f:
            word, _, phones = line.rstrip("\n").partition(" ")
            if word and phones and word not in lex:
                lex[word] = phones.replace(" ", "")
    return lex


def speaker_names(model_path: Path) -> list[str]:
    import onnxruntime as ort
    opts = ort.SessionOptions()
    s = ort.InferenceSession(str(model_path), opts, providers=["CPUExecutionProvider"])
    return s.get_modelmeta().custom_metadata_map.get("speaker_names", "").split(",")


class Kokoro:
    """One Kokoro voice. synth(text) -> (float32 audio in -1..1, 24000)."""

    def __init__(self, root: str | Path, voice: str = "am_michael", threads: int = 2,
                 speed: float = 1.0):
        import onnxruntime as ort
        self.root = Path(root).expanduser()
        model = self.root / "model.int8.onnx"
        if not model.exists():
            model = self.root / "model.onnx"
        if not model.exists():
            raise FileNotFoundError(f"no Kokoro model in {self.root}")
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = max(1, int(threads))
        opts.inter_op_num_threads = 1
        self.session = ort.InferenceSession(str(model), opts, providers=["CPUExecutionProvider"])
        meta = self.session.get_modelmeta().custom_metadata_map
        self.names = meta.get("speaker_names", "").split(",")
        self.sample_rate = int(meta.get("sample_rate", SAMPLE_RATE))
        n = len(self.names)
        styles = np.fromfile(self.root / "voices.bin", dtype=np.float32)
        self.styles = styles.reshape(n, -1, 256)
        self.vocab = load_tokens(self.root / "tokens.txt")
        self.lexicon = load_lexicon(self.root / "lexicon-us-en.txt")
        self.speed = float(speed) if speed and speed > 0 else 1.0
        self.voice = ""
        self.sid = 0
        self.set_voice(voice)
        self._espeak = None

    def set_voice(self, voice: str):
        if voice not in self.names:
            raise ValueError(f"unknown Kokoro voice {voice!r}")
        self.voice, self.sid = voice, self.names.index(voice)

    # -- text to phonemes ------------------------------------------------
    def _espeak_word(self, word: str) -> str:
        if self._espeak is None:
            from piper.phonemize_espeak import EspeakPhonemizer
            self._espeak = EspeakPhonemizer()
        with _ESPEAK_LOCK:
            sents = self._espeak.phonemize("en-us", word)
        ph = "".join("".join(s) for s in sents)
        ph = re.sub(r"[.,!?;:]+$", "", ph.strip())
        import unicodedata
        return unicodedata.normalize("NFC", ph).translate(_ESPEAK_FIX)

    def word_phonemes(self, word: str) -> str:
        if word in _OVERRIDES:
            return _OVERRIDES[word]
        for cand in (word, word.lower(), word.capitalize()):
            if cand in self.lexicon:
                return self.lexicon[cand]
        return self._espeak_word(word)

    def phonemes(self, text: str) -> str:
        out: list[str] = []
        for tok in _WORD.findall(text):
            if tok[0].isalnum():
                ph = self.word_phonemes(tok)
                if ph:
                    if out and not out[-1].endswith(" "):
                        out.append(" ")
                    out.append(ph)
            elif tok in self.vocab:      # punctuation the model knows (pauses, intonation)
                out.append(tok)
            elif tok == "-" and out:
                out.append(" ")
        return "".join(out).strip()

    def tokens(self, phonemes: str) -> list[int]:
        return [self.vocab[c] for c in phonemes if c in self.vocab]

    # -- synthesis ---------------------------------------------------------
    def _chunks(self, ids: list[int]) -> list[list[int]]:
        if len(ids) <= MAX_TOKENS:
            return [ids]
        out, cur = [], []
        space = self.vocab.get(" ")
        for i in ids:
            cur.append(i)
            if len(cur) >= MAX_TOKENS - 40 and i == space or len(cur) >= MAX_TOKENS:
                out.append(cur)
                cur = []
        if cur:
            out.append(cur)
        return out

    def synth(self, text: str, speed: float | None = None,
              voice: str | None = None) -> tuple[np.ndarray, int]:
        """voice: another speaker of the same model for this call only."""
        sid = self.sid if not voice else self.names.index(voice)
        ids = self.tokens(self.phonemes(text))
        if not ids:
            return np.zeros(0, np.float32), self.sample_rate
        parts = []
        sp = np.array([speed or self.speed], dtype=np.float32)
        for chunk in self._chunks(ids):
            style = self.styles[sid, min(len(chunk), self.styles.shape[1] - 1)][None, :]
            toks = np.array([[0, *chunk, 0]], dtype=np.int64)
            audio = self.session.run(None, {"tokens": toks, "style": style, "speed": sp})[0]
            parts.append(np.asarray(audio, np.float32).reshape(-1))
        return np.concatenate(parts), self.sample_rate
