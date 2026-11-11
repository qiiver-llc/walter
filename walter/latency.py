"""Reply latency: how long from the end of someone's speech to Walter's voice.

One TurnTiming follows a spoken turn through the pipeline; each stage marks
the moment it finished (time.monotonic()). When the first audio of the reply
starts playing, one INFO line sums it up, e.g.

    latency 5.9s: turn end +1.4, text +0.0, brain +0.0, first token +2.6,
    first sentence +0.4, audio +1.5 (synth 1.4s)

The "+" numbers are the time each stage added after the previous one.
"""
from __future__ import annotations

import logging
import time

log = logging.getLogger("walter.latency")

# (key, label) in pipeline order
STAGES = (
    ("speech_end", "speech end"),      # the last voiced frame (estimated from the silence count)
    ("turn_end", "turn end"),          # end-of-turn decision
    ("text", "text"),                  # final transcript ready
    ("brain", "brain"),                # written to the brain's stdin
    ("first_token", "first token"),    # first streamed text from the brain
    ("first_say", "first sentence"),   # first speakable sentence/clause handed to the voice
    ("audio", "audio"),                # its audio starts playing
)


class TurnTiming:
    def __init__(self, **marks: float):
        self.t: dict[str, float] = {k: v for k, v in marks.items() if v is not None}
        self.synth_s: float | None = None
        self.tool_before_speech = False
        self.logged = False

    def mark(self, key: str, at: float | None = None):
        """Record a stage the first time it happens (later calls are ignored)."""
        if key not in self.t:
            self.t[key] = time.monotonic() if at is None else at

    def summary(self) -> str:
        start = self.t.get("speech_end")
        prev = start
        parts = []
        for key, label in STAGES[1:]:
            if key not in self.t:
                continue
            at = self.t[key]
            if prev is not None:
                parts.append(f"{label} +{max(0.0, at - prev):.1f}")
            prev = at
            if start is None:
                start = at
        total = (prev - start) if (prev is not None and start is not None) else 0.0
        extra = []
        ref = self.t.get("turn_end", self.t.get("speech_end"))
        if "ack" in self.t and ref is not None:   # the instant "One sec." (audit item 2)
            extra.append(f"ack {max(0.0, self.t['ack'] - ref):.1f}s after turn end")
        if self.synth_s is not None:
            extra.append(f"synth {self.synth_s:.1f}s")
        if self.tool_before_speech:
            extra.append("tool call before speaking")
        tail = f" ({', '.join(extra)})" if extra else ""
        head = "latency" if "audio" in self.t else "latency (no audio)"
        return f"{head} {total:.1f}s: " + ", ".join(parts) + tail

    def log(self):
        if not self.logged:
            self.logged = True
            log.info("%s", self.summary())
