# Walter quickstart (beta): 10 steps

Full details: BETA-INSTALL.md. Help: TROUBLESHOOTING.md.

1. **Get a laptop ready**: 64-bit, about 2015 or newer, 8 GB RAM (4 GB works slowly), 32 GB
   free disk, a mic and speaker (a USB speakerphone is best), Wi-Fi or Ethernet.
2. **Install Debian 12 or 13** (minimal "netinst", no desktop). The whole laptop is Walter's.
3. **Run the line from your invite email** as a user with sudo:
   `curl -fsSL https://releases.wallter.app/install.sh | sudo bash -s -- --invite YOUR-CODE`
   It checks the download's fingerprint and signature, then installs (15 to 40 minutes).
4. **Reboot.** The setup wizard appears on Walter's screen. Scan its QR code with your phone
   (same Wi-Fi); your phone drives the rest.
5. **Name your Walter**, set your town (for weather) and the time zone.
6. **Test the mic and speaker**: play the test sound, say "Hey Walter" at the meter.
7. **Connect his brain**: sign in with Claude on Walter's own screen (keyboard needed), or use
   your own Anthropic API key. Walter never sees your Claude password.
8. **Choose privacy settings**: camera, cloud speech, recognition and admin rights are all
   **off** unless you tick them. Optional: connect Google, follow podcasts, pick your teams,
   request Cloud Link.
9. **Pair your phone**: scan the 6-digit pairing QR. On iPhone, add it to the Home Screen.
10. **Say hello**: "Hey Walter, hello." Then try "What's the weather?", "Add milk to the
    shopping list" and "Remind me in 10 minutes to stretch."

Every step can be skipped and redone later: app > Settings > Setup > Revisit setup steps.
Something wrong? App > Settings > Feedback, or reply to your invite email.
