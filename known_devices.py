"""Ported verbatim from ../dreeve-zepp-connector (src/dreeve_zepp_connector/known_devices.py).

Static `deviceSource` -> model-name table.

Zepp's workout API exposes no device-model field at all (see the
`ZEPP_DEVICE_NAMES` quirk in CLAUDE.md) - `devicesource` is just an opaque
per-physical-device ID. This table lets common devices resolve to a real
name without the user having to set `ZEPP_DEVICE_NAMES` themselves.

Scraped (2026-08-31) from Zepp's own developer docs, which is the only
public source that documents this mapping:
https://docs.zepp.com/docs/1.0/reference/related-resources/device-list/
("Devices with Zepp OS" + "Devices without Zepp OS" tables, both keyed by
`deviceSource`) - parsed from the page's raw server-rendered HTML, not an
AI paraphrase. Verbatim except for three entries ("Active 2 (Round)",
"Active 3 Premium", "Cheetah 2 Pro") where the doc's own "Equipment name"
column omits the "Amazfit" brand prefix inconsistently with every other
row in the same table - prefixed here for consistency. The doc also marks
some IDs with a trailing `*` (e.g. `9568512*`) footnoted as "represents the
device version for Mainland China" - that's an annotation on the ID, not
part of the number or a wildcard, so it's stripped before use here; the
China-variant ID is included as a normal key like every other regional ID
for the same model, just without the asterisk. Confirmed live: hitting
huami-token's plaintext `GET https://api-mifit.zepp.com/users/{user_id}/devices`
endpoint (see zepp-mcp's `huami_client.HuamiClient.devices()` /
`huami_token.zepp.ZeppClient.get_devices()`) against a real account returns
each device's `deviceSource` plus `productId`/`deviceId`/`sn` - but no
human-readable model name field either, so calling that endpoint at runtime
wouldn't add anything over this static table; not wired up here.

Not exhaustive - new devices ship after this was scraped, and non-Zepp-OS
devices are only partially documented there. `ZEPP_DEVICE_NAMES` still
exists to override any entry here or add a `deviceSource` this table
doesn't know about.
"""

from __future__ import annotations

KNOWN_DEVICE_NAMES: dict[str, str] = {
    "57": "Zepp E (Round)",
    "61": "Zepp E (Square)",
    "63": "Amazfit GTR 2",
    "64": "Amazfit GTR 2",
    "68": "Amazfit Pop",
    "81": "Zepp E (Round)",
    "82": "Zepp E (Square)",
    "83": "Amazfit T-Rex Pro",
    "91": "Amazfit GTS 2 mini",
    "92": "Amazfit GTS 2 mini",
    "200": "Amazfit T-Rex Pro",
    "206": "Amazfit GTR 2e",
    "209": "Amazfit GTR 2e",
    "224": "Amazfit GTS 3",
    "225": "Amazfit GTS 3",
    "226": "Amazfit GTR 3",
    "227": "Amazfit GTR 3",
    "229": "Amazfit GTR 3 Pro",
    "230": "Amazfit GTR 3 Pro",
    "242": "Amazfit GTR 3 Pro",
    "244": "Amazfit GTR 2",
    "246": "Amazfit GTS 4 mini",
    "247": "Amazfit GTS 4 mini",
    "250": "Amazfit GTR Mini",
    "251": "Amazfit GTR Mini",
    "252": "Amazfit Band 7",
    "253": "Amazfit Band 7",
    "254": "Amazfit Band 7",
    "414": "Amazfit Falcon",
    "415": "Amazfit Falcon",
    "418": "Amazfit T-Rex 2",
    "419": "Amazfit T-Rex 2",
    "6095106": "Amazfit GTR 3 Pro",
    "6553856": "Amazfit T-Rex Ultra",
    "6553857": "Amazfit T-Rex Ultra",
    "7864577": "Amazfit GTR 4",
    "7930112": "Amazfit GTR 4",
    "7930113": "Amazfit GTR 4",
    "7995648": "Amazfit GTS 4",
    "7995649": "Amazfit GTS 4",
    "8126720": "Amazfit Cheetah Pro",
    "8126721": "Amazfit Cheetah Pro",
    "8192256": "Amazfit Cheetah (Round)",
    "8192257": "Amazfit Cheetah (Round)",
    "8257793": "Amazfit Cheetah (Square)",
    "8323328": "Amazfit Active",
    "8323329": "Amazfit Active",
    "8388864": "Amazfit Active Edge",
    "8388865": "Amazfit Active Edge",
    "8454400": "Amazfit Bip 5",
    "8454401": "Amazfit Bip 5",
    "8519936": "Amazfit Balance",
    "8519937": "Amazfit Balance",
    "8519939": "Amazfit Balance",
    "8716544": "Amazfit T-Rex 3",
    "8716545": "Amazfit T-Rex 3",
    "8716547": "Amazfit T-Rex 3",
    "8782081": "Amazfit Bip 5 Unity",
    "8782088": "Amazfit Bip 5 Unity",
    "8782089": "Amazfit Bip 5 Unity",
    "8913152": "Amazfit Active 2 (Round)",
    "8913153": "Amazfit Active 2 (Round)",
    "8913155": "Amazfit Active 2 (Round)",
    "8913159": "Amazfit Active 2 (Round)",
    "9568512": "Amazfit Balance 2",
    "9568513": "Amazfit Balance 2",
    "9568515": "Amazfit Balance 2",
    "9765120": "Amazfit Bip 6",
    "9765121": "Amazfit Bip 6",
    "9961728": "Amazfit Cheetah 2 Ultra",
    "9961729": "Amazfit Cheetah 2 Ultra",
    "10092800": "Amazfit Active 2 (Round)",
    "10092801": "Amazfit Active 2 (Round)",
    "10092803": "Amazfit Active 2 (Round)",
    "10092807": "Amazfit Active 2 (Round)",
    "10158337": "Amazfit Bip 6",
    "10223872": "Amazfit Active 2 (Square)",
    "10223873": "Amazfit Active 2 (Square)",
    "10223875": "Amazfit Active 2 (Square)",
    "10551552": "Amazfit T-Rex 3 Pro (48mm)",
    "10551553": "Amazfit T-Rex 3 Pro (48mm)",
    "10551555": "Amazfit T-Rex 3 Pro (48mm)",
    "10682624": "Amazfit T-Rex 3 Pro (44mm)",
    "10682625": "Amazfit T-Rex 3 Pro (44mm)",
    "10682627": "Amazfit T-Rex 3 Pro (44mm)",
    "10813697": "Amazfit Active Max",
    "10813699": "Amazfit Active Max",
    "10879232": "Amazfit T-Rex Ultra 2",
    "10879233": "Amazfit T-Rex Ultra 2",
    "10879235": "Amazfit T-Rex Ultra 2",
    "10944768": "Amazfit Active 3 Premium",
    "10944769": "Amazfit Active 3 Premium",
    "10944771": "Amazfit Active 3 Premium",
    "10948867": "Amazfit Active 3 Premium",
    "11010304": "Amazfit Cheetah 2 Pro",
    "11010305": "Amazfit Cheetah 2 Pro",
    "11010307": "Amazfit Cheetah 2 Pro",
    "11075840": "Amazfit Balance Ultra",
    "11075841": "Amazfit Balance Ultra",
    "11141376": "Amazfit Balance 3",
    "11141377": "Amazfit Balance 3",
    "11141379": "Amazfit Balance 3",
    "11206915": "Amazfit Bip Max",
}
