# Walter glossary

| Term | Meaning |
|---|---|
| **Walter** | The assistant on your wall screen. A product of Qiiver LLC. |
| **Wake word** | His name ("Walter", or the name you chose). Heard on the laptop, never sent anywhere. |
| **Follow-up window** | A few seconds after he answers when you can talk without his name. |
| **Barge-in / interrupting** | Talking over Walter to stop him. Setting `ears.barge_in`: `auto` (default), `wake` (only his name), `off` (mic deaf while he talks). |
| **Ears** | Speech recognition. Local (whisper, on the laptop) by default; cloud (OpenAI) optional. |
| **Voice** | Text-to-speech. Local (piper) by default; cloud (OpenAI) optional. |
| **Brain** | Claude, which understands and answers. Fast model for chat, deep model for hard turns (`walterctl brain`). |
| **Worker** | A background job for long tasks (research, writing) so Walter stays free to talk. |
| **Workers card** | The panel showing running workers and what they're doing. |
| **Loop chip** | "Loop: ..." on the screen: what Walter is working on by himself; an amber dot means he's waiting on you. |
| **Board** | The info area on a tall screen: clock, weather, calendar, list, scores, now playing. |
| **Dashboard / sleep mode** | What the screen shows while Walter sleeps (Z key or "go to sleep"). Space wakes him. |
| **Canvas** | A big panel that slides in beside Walter for plans, tables and charts. |
| **Card** | A short message shown big on his face for a few seconds. |
| **While you were away** | A feed of things he couldn't say out loud (muted, asleep, nobody home). |
| **Inbox / review** | Things mentioned in passing, sorted later with do / defer / delegate / drop. |
| **Focus mode** | A timed work session with a ring that fills as time runs out. |
| **Routine** | Something Walter does on a schedule (e.g. weekday morning briefing). |
| **Companion app** | The web app for your phone/computer, served by Walter at `/app`. No app store. |
| **Pairing** | Linking a phone to Walter with a 6-digit code/QR; gives that phone a private token. |
| **Revoke** | Unpair a device; it stops working within seconds. |
| **Remote** | The app screen that controls the wall browser. |
| **Player** | The app's controls for podcasts and YouTube on the wall. |
| **Ducking** | Podcasts/videos get quieter while you talk and pause while Walter answers. |
| **Cloud Link** | Optional remote access to the app from outside your home through a private, encrypted tunnel. Exposes only the app. |
| **Tunnel** | The encrypted connection Cloud Link uses (Cloudflare). |
| **QR drop page** | A private page on your phone for typing keys or passwords, so they're never spoken to Walter. |
| **Enrolment / recognition** | Opt-in learning of a person's face or voice; stores only numeric fingerprints (embeddings). |
| **Presence** | Camera-based "someone's in the room". Off by default. |
| **Diagnostics bundle** | A redacted technical summary for support (`walterctl diagnostics`); never sent without you. |
| **Feedback** | Bug / idea / praise from the app's Settings; saved on the box until you tap Send or download it. |
| **Release / rollback** | Signed versions installed nightly; `walterctl update rollback` returns to the previous one. |
| **Kiosk** | The full-screen browser that shows Walter's face. |
| **walterctl** | Walter's command-line control panel (`walterctl --help`). |
| **Permission mode** | How freely Walter's brain may act: `acceptEdits` (recommended for the beta) or `bypassPermissions` (full power). |
| **Admin rights** | Optional passwordless sudo for Walter. Off by default. |
| **Untrusted text** | Web pages, emails, invites: data Walter reads but never takes orders from. |
