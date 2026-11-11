# Music on Walter (no DRM needed)

Amazon Music and other DRM services can't play on the wall (this Chromium has no
Widevine; `walterctl media amazon check`). Walter has two legal, DRM-free paths instead.

## 1. Internet radio (free)

Live stations from the public, community-run [radio-browser.info](https://www.radio-browser.info)
directory, played with mpv on the speakers.

| Say | Or run |
|---|---|
| "Play country radio" / "put on some jazz radio" | `walterctl radio play country` |
| "Play WNYC" (a call sign) / "play radio WNYC" | `walterctl radio play WNYC` |
| "Play [a favourite station]" | `walterctl radio play "station name"` |
| "Play the radio" (last station) | `walterctl radio play` |
| "Stop the music" / "turn off the radio" | `walterctl radio stop` |
| "Pause the radio" / "resume the radio" | `walterctl radio pause` / `resume` |
| "Turn the radio up/down" | `walterctl radio volume up` / `down` / `N` |
| "What's playing?" / "what station is this?" | `walterctl radio status` |

- `walterctl radio list [words]` searches the directory; no words lists favourites and recent stations.
- `walterctl radio favorites add "name"` (searched), `add "name=https://stream/url"` (direct),
  `add this` (the station playing now), `remove "name"`.
- Presets: config.toml `[radio] favorites = ["Station Name", "Other=https://..."]` (empty by
  default), plus `volume` (80) and `duck_volume` (25 percent).
- Like podcasts, the radio ducks to 25 percent while Walter listens or thinks and pauses while he
  talks, then carries on. Starting the radio stops a podcast (one thing at a time).
- The face's now-playing line/card shows the station, the song title if the stream sends one, and
  a red "live" tag (face/dashboard-podcast.json with `"source": "radio"`).
- Safety: the directory is found by a DNS lookup of all.api.radio-browser.info (fallback mirror
  list), requests use a generic User-Agent, only http(s) stream URLs are played, mpv never hands a
  URL to yt-dlp, and station names / stream titles are untrusted text: shown, never obeyed
  (docs/SECURITY.md).
- Voice matching is conservative: genres and "... radio/station", favourites by name, and call signs
  only when the transcript has them in capitals (K or W plus 2-3 letters). Anything vaguer goes to
  Walter's brain. Bare "stop"/"pause" only count while the radio is on and nothing else is playing.

## 2. Spotify Connect ("Walter" as a speaker)

**Spotify Premium required.** Walter becomes a speaker you choose in your own Spotify app,
like a smart speaker.

- `walterctl spotify enable`: installs librespot if needed and starts the systemd user service
  `walter-spotify` (speaker name from config `[spotify] name`, default "Walter").
- Then: phone on the same Wi-Fi, open Spotify, tap the speaker/devices icon, pick **Walter**.
- `walterctl spotify status` (installed? running? playing?), `walterctl spotify disable`.
- Login is zeroconf only: your Spotify app hands the speaker a session. Walter never sees or
  stores Spotify credentials (`--disable-credential-cache`, no username/password, no audio cache).
- Install: `sudo -n apt-get install librespot` if Debian ships it (Debian 13 doesn't today), else
  the pinned raspotify 0.48.4 package (librespot v0.8.0, amd64) is downloaded, checked against its
  sha256 and only extracted into ~/.walter/tools/librespot/ (nothing installed system-wide).
  To bump it, change `PIN` in walter/spotify.py (version, URL and sha256 together).
- Audio goes to the default sink (PipeWire via its PulseAudio backend). While Walter listens or
  thinks, Spotify's stream is turned down to 25 percent (`[spotify] duck_volume`); while he talks
  it is muted (paused via MPRIS instead, if librespot ever exposes it), then restored.
- Ducking takes effect after Walter's next restart; the speaker itself works right away.

### What Walter can NOT do with Spotify (yet)

- Search or start Spotify music by voice ("play Taylor Swift on Spotify"): that needs the Spotify
  Web API and an OAuth login. Later.
- Tell you what Spotify is playing, skip or pause it by voice: control it from the Spotify app.
- Work with a free Spotify account (Connect to third-party speakers is Premium only).
