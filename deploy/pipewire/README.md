# Echo cancellation for barge-in

Walter used to be deaf while he talked (so he wouldn't hear himself), which
meant nobody could interrupt him. With PipeWire's WebRTC echo canceller he
can keep listening while he talks.

Files:
- `60-walter-echo-cancel.conf`: goes in `~/.config/pipewire/pipewire.conf.d/`.
  Creates `walter_aec_sink` (he plays his voice here) and `walter_aec_source`
  (his echo-cancelled microphone). Both follow the default sound card; the
  system defaults are not changed.
- `asoundrc-walter-aec`: appended to `~/.asoundrc`. Gives the two PipeWire
  nodes ALSA names (`walter_aec_speaker`, `walter_aec_mic`) that Walter's
  Python audio (PortAudio) can open.

`deploy/install.sh` installs both. By hand:

    mkdir -p ~/.config/pipewire/pipewire.conf.d
    cp deploy/pipewire/60-walter-echo-cancel.conf ~/.config/pipewire/pipewire.conf.d/
    cat deploy/pipewire/asoundrc-walter-aec >> ~/.asoundrc
    systemctl --user restart pipewire wireplumber   # or reboot
    systemctl --user restart walter

Without restarting PipeWire you can run the canceller as its own little
PipeWire client until the next reboot (this is what `walter-aec-live` does):

    systemd-run --user --unit=walter-aec-live pipewire -c /path/to/standalone.conf

where the standalone config is the usual client modules (rt, protocol-native,
client-node, adapter) plus the module block from `60-walter-echo-cancel.conf`.

Check: `wpctl status` lists `walter_aec_source` / `walter_aec_sink` under Filters.
Walter's log says `barge-in mode: aec` at startup.

## Settings (`~/.walter/config.toml`, `[ears]`)

- `barge_in = "auto"`: full barge-in when the echo canceller is running,
  otherwise only his name (or "Walter, stop") interrupts him.
  `"aec"`, `"wake"`, `"off"` (old behaviour: deaf while talking).
- `barge_in_min_ms = 450`: speech needed while he talks before the ears check
  whether it's a real interruption.
- `aec_device` (`[ears]` and `[voice]`): the ALSA names above; used only when
  `device = ""`.

## How well it works here (measured 4 Nov 2026, laptop speaker + built-in mic)

Loopback test: 12 s of Walter's voice played through `walter_aec_sink` while
recording `walter_aec_source` and the raw mic at the same time.
- Median echo reduction ~20 to 25 dB (one run 45 dB), the room's hiss drops
  from ~750 to ~30 RMS (noise suppression).
- But the canceller leaks short bursts of his voice (some as loud as the raw
  echo), so loudness/VAD alone can't tell echo from a person.
- Walter's speech filters (Silero VAD + whisper confidence) rejected all of
  the echo, raw and cancelled, in 3 s windows.

So a barge-in needs words: the ears transcribe speech heard while he talks
(first look after `barge_in_min_ms`), and `walter/aec.py` only stops him for
2+ real words that are not an echo of what he's saying, "stop", or his name.
Typical reaction time is about 1 to 1.5 s after someone starts talking.
