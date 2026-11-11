# Home Assistant bridge (optional)

Walter can drive an **existing** Home Assistant on your network: lights, switches,
fans, thermostats, scenes and blinds. It talks to Home Assistant's REST API with a
long-lived access token. It is **off by default** and does nothing until you turn it on.

Code: `walter/homeassistant.py`. Tests: `tests/test_homeassistant.py` (a fake Home
Assistant built with Python's `http.server`; no real home needed).

## Set it up

1. **Tell Walter the address**: `walterctl ha set-url http://homeassistant.local:8123`
   (or type it in the first-run wizard's Extras step).
2. **Make a long-lived access token** in Home Assistant: click your profile (bottom
   left) > **Security** tab > **Long-lived access tokens** > **Create token**, name it
   "Walter", copy it. (It is shown only once.)
3. **Hand it over privately**: `walterctl ha connect` puts a QR code on the wall.
   Open it on your phone and paste the token. Walter checks it with one call
   (`GET /api/`) and saves it to `~/.walter/secrets/homeassistant.token` (mode 600).
   The token is never spoken, shown, logged or printed. Never read a token out loud
   or type it into chat. (Headless alternative: `walterctl ha token --stdin < file`.)
4. **Turn it on**: `walterctl ha enable`, then `walterctl ha status` and
   `walterctl ha list`.

To stop: `walterctl ha disable`. To revoke: delete the token in Home Assistant
(profile > Security) and remove `~/.walter/secrets/homeassistant.token`.

Config (`~/.walter/config.toml`, applies live with `walterctl config reload`):

```toml
[homeassistant]
enabled = false
url = ""          # http://homeassistant.local:8123
timeout_s = 6.0
fastpath = true   # simple voice commands handled instantly
```

## What Walter may do

| Freely | Needs your spoken yes first | Read only |
|---|---|---|
| lights (on/off, brightness, colour) | locks (lock or unlock) | sensors |
| switches and plugs | garage doors, gates, doors (by device class or name) | binary sensors |
| fans | alarm panels (arm or disarm) | weather, people |
| thermostats (set temperature) | valves | |
| scenes | anything named garage / gate / door / lock / alarm / opener | |
| blinds and shades | every other domain (scripts, automations, buttons, sirens...) | |

The module itself enforces this: it **refuses** the middle column unless called with
`--confirmed`, which Walter only adds after you have said yes out loud. The voice fast
path never touches them; Walter asks first. A switch named "garage opener" counts as a
garage door.

## Commands

```
walterctl ha status | list [light|kitchen] | areas | state NAME | temperature [NAME]
walterctl ha on NAME | off NAME | brightness NAME 40 | colour NAME blue | temp NAME 68
walterctl ha scene "movie night" | cover open|close|stop NAME
walterctl ha call DOMAIN SERVICE ENTITY [--data JSON] [--confirmed]
walterctl ha alias add "telly corner" light.living_room_lamp | alias rm NAME | aliases
walterctl ha refresh
```

Names are matched loosely: aliases first (`~/.walter/ha-aliases.json`, spoken name to
entity id), then the device's name or id ("the kitchen lights" finds
`light.kitchen`), then a careful fuzzy match. If two devices fit equally, Walter asks
instead of guessing.

## Voice (instant, no thinking pause)

When enabled, these are handled straight away and confirmed out loud:

- "turn on / turn off the kitchen lights", "switch the coffee maker on"
- "set the living room lamp to 30 percent"
- "set the thermostat to 68"
- "what's the temperature inside" / "what's the thermostat at"

Only devices Walter already knows (from the last `list` or `refresh`; the cache is
`~/.walter/ha-cache.json`) are handled this way. Anything compound ("... and ..."),
timed ("in 10 minutes"), unknown, or needing a yes goes to Walter's brain instead.
Plain "what's the temperature" is left to the brain (you might mean the weather).

## Security notes

- Device names and states come from Home Assistant and are treated as **data**:
  cleaned, and wrapped in `<untrusted>` tags in `list` / `areas` output
  (see docs/SECURITY.md).
- The token gives full control of your Home Assistant as that user. Consider a
  dedicated non-admin Home Assistant user for Walter.
- Walter only talks to the address you configure; nothing goes to the cloud.
