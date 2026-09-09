#!/usr/bin/env python3
"""
PAGASA Radar Mosaic -> GeoTIFF downloader
==========================================

Fetches radar mosaic frames from PAGASA's public Radar page and exports
them as georeferenced GeoTIFFs covering the Philippines (openable directly
in QGIS, ArcGIS, or any GIS tool without manual georeferencing).

How it works
------------
PAGASA's radar page (https://pagasa.dost.gov.ph/radar) loads its frames by
POSTing to an internal API:

    POST https://pagasa.dost.gov.ph/api/HybridTimeline?t=<cache-buster>

which returns JSON like:

    {
      "rainfall_estimate": [ {"time": "...", "url": "https://api.meteopilipinas.gov.ph/.../mosaic-hybrid/xxx.png"}, ... ],
      "reflectivity": [ ... ]
    }

Each `url` is a plain PNG (a colorized radar mosaic image, NOT already
georeferenced). This script downloads a chosen frame and wraps it in a
GeoTIFF using the same lat/lon bounding box PAGASA's own map viewer uses
to place the image on the map (see `RADAR_EXTENT` below, taken from
PAGASA's radar/map.js).

IMPORTANT CAVEAT
-----------------
This does NOT recover the original scientific radar data (dBZ or mm/hr
values) -- PAGASA only publishes a rendered, already-colorized PNG image,
not the underlying raster grid. The resulting GeoTIFF is a *georeferenced
picture* of the radar mosaic (RGB/RGBA pixels), suitable for viewing,
overlaying, and rough visual analysis in GIS software -- not for
extracting precise rainfall/reflectivity values pixel-by-pixel (that would
require decoding PAGASA's colour ramp against its legend image, which is
possible but out of scope here; ask if you want that added).

Usage
-----
    python radar_to_tiff.py                        # latest rainfall_estimate frame
    python radar_to_tiff.py --product reflectivity  # latest reflectivity frame
    python radar_to_tiff.py --which all             # every available frame
    python radar_to_tiff.py --which index --index 0 # a specific frame (0 = oldest)
    python radar_to_tiff.py --outdir ./radar_tiffs
    python radar_to_tiff.py --lightning             # live lightning strikes -> CSV instead
    python radar_to_tiff.py --watch                 # keep polling, save every NEW radar frame
    python radar_to_tiff.py --watch --interval 300   # ...every 5 min (matches PAGASA's own cadence)
    python radar_to_tiff.py --retrieve selection.json  # download frames picked & saved via the GUI
    python radar_to_tiff.py --lightning --lightning-source panahon  # richer live feed, see below
    python radar_to_tiff.py --lightning --lightning-source panahon --watch  # stream it continuously
    python radar_to_tiff.py --lightning --watch --window 60          # rolling 60-min CSV, oldest trimmed
    python radar_to_tiff.py --lightning --watch --split 60           # new CSV auto-saved every 60 min

Lightning data
--------------
PAGASA runs a live lightning-strike feed at
POST https://pagasa.dost.gov.ph/api/Lightning (used by the homepage map, not
the radar page). --lightning fetches whatever strikes are currently being
reported and writes them straight to a CSV (timestamp, latitude, longitude,
type, amplitude, url, plus any extra fields PAGASA includes). It's often
empty -- that just means no lightning is being detected right now, same as
an empty CycloneTrack means no active cyclone.

PAGASA also exposes the SAME underlying lightning network through a second,
separate website, panahon.gov.ph ("PAGASA Nationwide Hydromet Observation
Network") -- pass --lightning-source panahon to use it instead. It's a
different delivery mechanism (a live Socket.IO/websocket push feed at
wss://ws.panahon.gov.ph, event "lx.data" -- confirmed directly against the
live site) rather than a polled REST call, and each strike record is richer:
precise sub-second timestamp, cloud-to-ground vs. cloud-to-cloud
classification, peak current (kA), estimated height (m for in-cloud
strikes), and the number of sensors that detected it (a rough confidence
signal). One-shot --lightning mode listens for --duration seconds (default
20) and writes whatever it caught; --watch mode keeps the connection open
and streams strikes to the CSV as they arrive, no polling interval needed.
Requires the extra `python-socketio[client]` + `websocket-client` packages
(see Requirements below) -- pagasa-source lightning and all radar features
work fine without them.

(A third source, meteologix.com, was evaluated and dropped -- its data was
reachable, but sweeping its per-province pages got this script's IP
rate-limited/blocked by the site's CDN, Akamai, even at a deliberately slow,
sequential request pace. Not worth the fragility given pagasa/panahon above
already cover the same underlying lightning network reliably.)

(A commercial source, Xweather's /lightning/flash/closest API, was also
built and tried, but was removed again: its Lightning endpoint carries a
10x cost multiplier on top of its normal per-request pricing, so a
--watch run polling every region on any reasonable interval burns through
its free monthly quota in well under a day. Not worth it when pagasa/panahon
above are the same underlying PAGASA network and cost nothing.)

`--watch` mode (any lightning source) has three mutually exclusive output
modes, picked with at most one flag:
  - (neither flag)   Continuous: one ever-growing CSV, every strike appended
                      forever, nothing ever dropped or rotated.
  - --window MINUTES Rolling window: a single CSV that's rewritten in place
                      to always hold roughly the trailing MINUTES minutes;
                      strikes older than that are trimmed away.
  - --split MINUTES  Split: writes to one CSV for MINUTES minutes, then
                      finalizes it and starts a brand-new CSV for the next
                      MINUTES-minute segment, indefinitely -- nothing is
                      ever dropped, you just get a fresh, auto-saved file
                      every MINUTES minutes instead of one file that keeps
                      growing or trimming itself.
--window and --split cannot be used together.

Requirements
------------
    pip install requests pillow numpy rasterio
    pip install "python-socketio[client]" websocket-client  # only for --lightning-source panahon
    pip install playwright  # only for --lightning-source panahon (fetching its connection ticket --
                             # panahon.gov.ph's ticket endpoint rejects scripted HTTP clients no
                             # matter how closely they spoof a real browser's headers/TLS fingerprint,
                             # so this drives an actual headless Chromium instead -- see
                             # _require_playwright() and _fetch_panahon_ws_ticket() for the full story)
    playwright install chromium  # one-time browser download playwright itself needs
"""

import argparse
import csv
import io
import json
import sys
import threading
import time
from pathlib import Path

import numpy as np
import requests
from PIL import Image

try:
    import rasterio
    from rasterio.transform import from_bounds
except ImportError:
    print(
        "This script needs 'rasterio' to write GeoTIFFs.\n"
        "Install it with:  pip install rasterio\n",
        file=sys.stderr,
    )
    raise

TIMELINE_URL = "https://pagasa.dost.gov.ph/api/HybridTimeline"

# PAGASA's live lightning-strike feed -- separate endpoint from the radar
# timeline above. It's what the homepage map (app/home/map-ol.v2.js) calls
# to draw the toggleable "Lightning" layer. Each record is a single strike
# with type/amplitude/timestamp/lat/lon/icon-url.
LIGHTNING_URL = "https://pagasa.dost.gov.ph/api/Lightning"

# panahon.gov.ph -- a separate PAGASA website ("PAGASA Nationwide Hydromet
# Observation Network") that exposes the same underlying lightning network
# through a live Socket.IO push feed instead of a polled REST call.
# Confirmed directly against the live site: connecting a Socket.IO client
# to this URL and listening for the "lx.data" event delivers one JSON
# message per strike, e.g.
#   {"type": 0, "time": "2026-08-29T09:53:14.525933899Z",
#    "latitude": 14.713833, "longitude": 121.101239,
#    "peakCurrent": -12496, "numSensors": 12, "icHeight": 0}
# type: 1 = "Cloud to Cloud" (in-cloud), anything else = "Cloud to Ground".
#
# AUTH: as of this writing, the server DOES require a short-lived
# connection ticket (it didn't when this was first confirmed -- see
# _fetch_panahon_ws_ticket() below for how this was reverse-engineered
# from the live site's own bundled JS after it started rejecting
# ticket-less connections with a "bad_ticket" connect_error). No cookie
# or session is needed to obtain one, just a plain GET to
# PANAHON_WS_TICKET_URL -- see that function's docstring for the full
# mechanism.
PANAHON_LIGHTNING_WS_URL = "https://ws.panahon.gov.ph"
PANAHON_WS_TICKET_URL = "https://panahon.gov.ph/api/v1/ws-ticket"
PANAHON_LIGHTNING_EVENT = "lx.data"

# ws.panahon.gov.ph's websocket upgrade can reject a client that opens a
# *fresh* connection directly as a websocket (HTTP 400, no explanation) --
# confirmed directly, and cross-origin/headers made no difference (even a
# real browser tab on a completely different PAGASA site got the same
# rejection connecting this way; a browser only succeeds when it first
# hits the plain HTTPS polling endpoint, which returns 200 every time
# regardless of headers, and *then* upgrades an already-established
# session to websocket). So this connects with transports=["polling",
# "websocket"] (polling first) rather than websocket-first, matching
# python-engineio's own default order -- if the follow-up upgrade to
# websocket also gets rejected, the library falls back to plain polling
# silently rather than failing the whole connection (confirmed by reading
# its source: an upgrade failure only logs a warning, a fresh direct
# websocket connection failure is what raises). These headers are sent
# anyway since they match what PAGASA's other endpoints need (see
# BROWSER_HEADERS below) -- harmless either way, just not what fixed this.
PANAHON_WS_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Origin": "https://panahon.gov.ph",
    "Referer": "https://panahon.gov.ph/",
}

# Bounding box PAGASA's own map.js uses to place the radar image
# (radarBoundaries.leftBottom / rightTop), in EPSG:4326 (lon, lat).
RADAR_EXTENT = {
    "west": 115.969111093,
    "south": 3.80912641587,
    "east": 129.511990464,
    "north": 22.322581275,
}

# PAGASA's actual rainfall_estimate legend ramp, sampled directly from
# https://pagasa.dost.gov.ph/themes/hiraia/assets/images/rainfall_estimate_legend.png
# (13 discrete color bins, ordered low -> high rainfall intensity).
PAGASA_LEGEND_BINS = [
    (129, 248, 226),  # light cyan
    (90, 157, 248),   # light blue
    (11, 5, 249),     # deep blue
    (143, 255, 59),   # bright green
    (108, 199, 34),   # medium green
    (87, 146, 28),    # dark green
    (254, 255, 55),   # yellow
    (223, 183, 23),   # gold
    (232, 152, 29),   # orange
    (219, 8, 0),      # red
    (158, 5, 0),      # dark red
    (225, 0, 255),    # magenta
    (174, 87, 217),   # purple
]

# Classic NWS/NEXRAD reflectivity color table, same bin count/order
# (low -> high) so it swaps in 1:1 for PAGASA's ramp above.
NWS_CLASSIC_BINS = [
    (110, 110, 110),  # dBZ 0 -- gray, radar clutter floor (only the lowest step)
    (144, 220, 144),  # pale green -- transition out of gray starts immediately
    (0, 220, 0),      # green
    (0, 190, 0),      # medium-bright green
    (0, 160, 0),      # darker green
    (109, 195, 0),    # yellow-green
    (255, 228, 0),    # dBZ 30 -- yellow
    (255, 153, 0),    # orange
    (255, 41, 0),     # red-orange
    (213, 1, 0),      # dBZ 45 -- red
    (163, 0, 10),     # dark red / maroon
    (234, 0, 223),    # magenta
    (159, 0, 255),    # dBZ 65+ -- purple
]

# Variants of the same ramp above, differing only in the lowest bin(s) --
# the radar-clutter floor -- every other step is identical across all of
# them. A bin can be `None` instead of an (r, g, b) tuple -- see
# recolor_image() below -- meaning "make this transparent" rather than any
# particular color.
#
# - classic_nws_dark_gray: a darker neutral gray for that lowest step.
# - classic_nws_light_gray: a lighter neutral gray for that lowest step.
# - classic_nws_no_gray: no gray anywhere -- the lowest step is made fully
#   transparent (erased) instead of colored at all, so there's no gray
#   band and nothing stands in for it either.
# - classic_nws_two_gray: TWO gray steps instead of one -- dark gray at
#   the very bottom, lighter gray above it -- before the ramp switches to
#   color at the (previously second) green step.
NWS_DARK_GRAY_BINS = [(60, 60, 60)] + NWS_CLASSIC_BINS[1:]
NWS_LIGHT_GRAY_BINS = [(195, 195, 195)] + NWS_CLASSIC_BINS[1:]
NWS_NO_GRAY_BINS = [None] + NWS_CLASSIC_BINS[1:]
NWS_TWO_GRAY_BINS = [(110, 110, 110), (170, 170, 170)] + NWS_CLASSIC_BINS[2:]

PALETTES = {
    "classic_nws": NWS_CLASSIC_BINS,
    "classic_nws_dark_gray": NWS_DARK_GRAY_BINS,
    "classic_nws_light_gray": NWS_LIGHT_GRAY_BINS,
    "classic_nws_no_gray": NWS_NO_GRAY_BINS,
    "classic_nws_two_gray": NWS_TWO_GRAY_BINS,
}


def recolor_image(img: Image.Image, palette_name: str) -> Image.Image:
    """Remap PAGASA's rainfall color ramp onto a different palette.

    Each pixel is matched to its nearest color in PAGASA_LEGEND_BINS (by
    RGB distance) and swapped for the color at the same rank in the target
    palette -- so relative rainfall intensity is preserved even though we
    never see the underlying numeric values, only the rendered colors.
    The original alpha channel (transparency / no-data areas) is kept as-is
    for every bin EXCEPT one whose palette entry is `None` -- those pixels
    are forced fully transparent (alpha 0) instead of given any color, so
    that band is erased from the image rather than recolored.
    """
    new_bins = PALETTES[palette_name]
    arr = np.array(img)  # H, W, 4 (RGBA)
    rgb = arr[:, :, :3].astype(np.int32)
    alpha = arr[:, :, 3]

    old_bins_arr = np.array(PAGASA_LEGEND_BINS, dtype=np.int32)  # (13, 3)
    # `None` bins get a black placeholder color -- it's never actually
    # seen, since those pixels' alpha is forced to 0 below.
    new_bins_arr = np.array(
        [b if b is not None else (0, 0, 0) for b in new_bins], dtype=np.uint8
    )  # (13, 3)
    transparent_bins = np.array([b is None for b in new_bins])  # (13,)

    h, w, _ = rgb.shape
    flat = rgb.reshape(-1, 3)  # (N, 3)

    # squared distance from every pixel to every legend bin color -> (N, 13)
    diffs = flat[:, None, :] - old_bins_arr[None, :, :]
    dists = np.sum(diffs * diffs, axis=2)
    nearest = np.argmin(dists, axis=1)  # (N,)

    recolored = new_bins_arr[nearest].reshape(h, w, 3)

    new_alpha = alpha
    if transparent_bins.any():
        is_transparent = transparent_bins[nearest].reshape(h, w)
        new_alpha = np.where(is_transparent, 0, alpha).astype(np.uint8)

    out = np.dstack([recolored, new_alpha])
    return Image.fromarray(out.astype(np.uint8), mode="RGBA")

# A realistic browser User-Agent + the headers jQuery's $.post() sends
# automatically (X-Requested-With, Accept) -- PAGASA's backend appears to
# use these to distinguish a real AJAX call from a bare script/bot request.
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

AJAX_HEADERS = {
    **BROWSER_HEADERS,
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://pagasa.dost.gov.ph/radar",
    "Origin": "https://pagasa.dost.gov.ph",
}

# Same idea as AJAX_HEADERS, but with Referer pointing at the homepage --
# that's the page that actually calls /api/Lightning, not /radar.
LIGHTNING_HEADERS = {
    **BROWSER_HEADERS,
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://pagasa.dost.gov.ph/",
    "Origin": "https://pagasa.dost.gov.ph",
}

# Reused across requests so cookies set by the initial page load (session,
# WAF/anti-bot, CSRF, etc.) are sent back on the API call, same as a real
# browser would.
SESSION = requests.Session()

# Set to True via --insecure. Only ever disables verification for the
# meteopilipinas.gov.ph image host below -- see the SSL error message for
# why this exists and the safer fix to try first.
INSECURE = False

def fetch_timeline() -> dict:
    """POST to PAGASA's HybridTimeline API and return the parsed JSON."""
    # Prime the session by loading the actual page first, exactly like a
    # browser would before firing its AJAX call -- picks up any cookies
    # the server/WAF sets on first visit.
    SESSION.get("https://pagasa.dost.gov.ph/radar", headers=BROWSER_HEADERS, timeout=20)

    cache_buster = int(time.time() * 1000)
    resp = SESSION.post(
        TIMELINE_URL,
        params={"t": cache_buster},
        headers=AJAX_HEADERS,
        timeout=20,
    )
    resp.raise_for_status()

    try:
        return resp.json()
    except ValueError:
        preview = resp.text[:500].replace("\n", " ")
        raise RuntimeError(
            "PAGASA's API didn't return JSON (status "
            f"{resp.status_code}, content-type={resp.headers.get('Content-Type')}).\n"
            f"First 500 chars of response:\n{preview}\n\n"
            "This usually means a bot/WAF check or geo-block is intercepting "
            "the request. Try again in a moment, or open "
            "https://pagasa.dost.gov.ph/radar in a normal browser to confirm "
            "the site itself is up."
        ) from None


def fetch_lightning() -> list:
    """POST to PAGASA's Lightning API and return the parsed list of strikes."""
    # Prime the session against the homepage -- that's the page that fires
    # this call, same reasoning as priming against /radar for the timeline.
    SESSION.get("https://pagasa.dost.gov.ph/", headers=BROWSER_HEADERS, timeout=20)

    cache_buster = int(time.time() * 1000)
    resp = SESSION.post(
        LIGHTNING_URL,
        params={"t": cache_buster},
        headers=LIGHTNING_HEADERS,
        timeout=20,
    )
    resp.raise_for_status()

    try:
        data = resp.json()
    except ValueError:
        preview = resp.text[:500].replace("\n", " ")
        raise RuntimeError(
            "PAGASA's Lightning API didn't return JSON (status "
            f"{resp.status_code}, content-type={resp.headers.get('Content-Type')}).\n"
            f"First 500 chars of response:\n{preview}\n\n"
            "Same kind of bot/WAF check as the radar timeline call can cause "
            "this -- try again in a moment, or open https://pagasa.dost.gov.ph "
            "in a normal browser to confirm the site itself is up."
        ) from None

    # Be tolerant of the exact envelope shape -- a bare list, or an object
    # wrapping the list under some key -- so a minor API change doesn't
    # silently break this.
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("data", "Lightning", "lightning", "result", "results"):
            if isinstance(data.get(key), list):
                return data[key]
    return []


def lightning_to_csv(strikes: list, out_path: Path) -> Path:
    """Write lightning strike records to a CSV file."""
    out_path.parent.mkdir(parents=True, exist_ok=True)

    fieldnames = ["timestamp", "latitude", "longitude", "type", "amplitude", "url"]
    # Pick up any fields PAGASA sends that we didn't anticipate, so nothing
    # silently gets dropped if the schema grows.
    for strike in strikes:
        for key in strike.keys():
            if key not in fieldnames:
                fieldnames.append(key)

    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for strike in strikes:
            writer.writerow(strike)

    return out_path


def _require_socketio():
    """Lazy-import python-socketio so it's only a hard requirement for the
    panahon.gov.ph lightning feature -- everything else in this script
    works fine without it installed."""
    try:
        import socketio
    except ImportError:
        raise RuntimeError(
            "--lightning-source panahon needs the 'python-socketio' and "
            "'websocket-client' packages (not required for anything else "
            "in this script). Install with:\n\n"
            '    pip install "python-socketio[client]" websocket-client\n'
        ) from None
    return socketio


def _require_playwright():
    """Lazy-import Playwright so it's only a hard requirement for
    --lightning-source panahon specifically (fetching its connection
    ticket) -- everything else in this script, including
    --lightning-source pagasa, works fine without it installed.

    WHY A REAL BROWSER, NOT JUST ANOTHER HTTP CLIENT: this replaced a
    long chain of attempts to fetch panahon.gov.ph's connection ticket
    with a spoofing HTTP client -- first plain `requests` (404, blamed on
    missing headers -- wrong), then `requests` with real
    Origin/Referer/Sec-Fetch-* headers and a primed session (still 404,
    identically -- wrong again), then curl_cffi impersonating a real
    Chrome TLS handshake (still 404 -- also wrong), then curl_cffi with
    the actual required `?token=` CSRF parameter + matching session
    cookie (reverse-engineered directly from the site's own JS, and
    confirmed CORRECT against a local mock server built to enforce that
    exact contract -- and STILL got the identical 404 against the real
    site), then curl_cffi with its Origin header dropped and its
    User-Agent left consistent with its own TLS fingerprint instead of a
    separately hardcoded one (still 404). Every one of those requests was
    provably carrying the right data; a real Chrome browser, given the
    identical URL and token, succeeded every single time it was tried,
    live, throughout all of this. That combination -- correct content,
    consistently rejected regardless of how carefully the request's shape
    was spoofed -- points at something checking properties of the client
    that a general-purpose HTTP library fundamentally can't replicate
    (accumulated TLS session state, HTTP/2 frame-level behavior, or
    something else below the headers). Rather than keep guessing at
    fingerprint details one at a time, this drives an ACTUAL headless
    Chromium via Playwright for this one request -- sidestepping the
    whole problem by not needing to spoof anything.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError(
            "--lightning-source panahon needs the 'playwright' package "
            "(plus its Chromium browser download) to fetch its connection "
            "ticket -- not required for anything else in this script, "
            "including --lightning-source pagasa. Install with:\n\n"
            "    pip install playwright\n"
            "    playwright install chromium\n"
        ) from None
    return sync_playwright


PANAHON_LIGHTNING_FIELDS = [
    "timestamp", "latitude", "longitude", "type", "amplitude_ka",
    "height_m", "num_sensors",
]


def _normalize_panahon_strike(raw: dict) -> dict:
    """Turn a raw panahon.gov.ph 'lx.data' event into the flatter record
    shape used by PANAHON_LIGHTNING_FIELDS."""
    return {
        "timestamp": raw.get("time"),
        "latitude": raw.get("latitude"),
        "longitude": raw.get("longitude"),
        "type": "Cloud to Cloud" if raw.get("type") == 1 else "Cloud to Ground",
        "amplitude_ka": raw.get("peakCurrent"),
        "height_m": raw.get("icHeight"),
        "num_sensors": raw.get("numSensors"),
    }


# The exact fetch the site's own JS (js/beta.js) makes for its connection
# ticket, run INSIDE the page via page.evaluate() rather than reimplemented
# from outside it -- so it's not just "a browser loaded the page", it's
# literally the site's own client-side code making this specific request,
# with the browser handling cookies/CSRF/whatever-else automatically the
# same way it would for any real visitor. Confirmed directly against the
# live site's bundled JS that this is exactly what it does on its own
# (read the csrf-token meta tag, GET this URL with it, same-origin
# credentials) before ever calling _panahon_connect_with_retries().
_PANAHON_TICKET_JS = """
    async () => {
        const meta = document.querySelector('meta[name="csrf-token"]');
        if (!meta) return {error: "no csrf-token meta tag on the page"};
        const token = meta.getAttribute("content");
        const url = "/api/v1/ws-ticket?token=" + encodeURIComponent(token);
        let res;
        try {
            res = await fetch(url, {credentials: "same-origin", cache: "no-store"});
        } catch (e) {
            return {error: "fetch() itself threw: " + e};
        }
        if (!res.ok) return {error: "ticket request returned HTTP " + res.status};
        let data;
        try {
            data = await res.json();
        } catch (e) {
            return {error: "ticket response wasn't JSON: " + e};
        }
        if (!data || typeof data.ticket !== "string") return {error: "no usable 'ticket' field in response"};
        return {ticket: data.ticket};
    }
"""


# Consecutive-failure counter for _fetch_panahon_ws_ticket(), used only to
# print a loud, easy-to-spot warning if ticket fetches keep failing for a
# while (e.g. panahon.gov.ph itself having an outage or being slow) --
# python-socketio's own reconnection logic (see watch_lightning_panahon()'s
# build_client()) already keeps retrying regardless, unlimited, 30-60s
# apart, so this doesn't change behavior at all. It only adds visibility:
# without it, a long stretch of these failures just looks like the same
# line repeating in an otherwise-normal-looking log, easy to miss unless
# someone happens to be reading it live -- this makes it show up as a
# GitHub Actions annotation instead (the same kind of banner used for a
# genuine crash), so a long outage doesn't go unnoticed.
_panahon_ticket_consecutive_failures = 0
_PANAHON_TICKET_FAILURE_WARNING_EVERY = 5


def _note_panahon_ticket_failure():
    global _panahon_ticket_consecutive_failures
    _panahon_ticket_consecutive_failures += 1
    if _panahon_ticket_consecutive_failures % _PANAHON_TICKET_FAILURE_WARNING_EVERY == 0:
        print(
            f"::warning::panahon.gov.ph connection ticket has failed "
            f"{_panahon_ticket_consecutive_failures} times in a row. This is "
            "almost always panahon.gov.ph itself being slow or briefly down, "
            "not a bug here -- it keeps retrying automatically (unlimited "
            "attempts, 30-60s apart) and should recover on its own once the "
            "site responds normally again. No action needed unless this "
            "keeps climbing for an extended stretch (say, 30+ minutes)."
        )


def _note_panahon_ticket_success():
    global _panahon_ticket_consecutive_failures
    _panahon_ticket_consecutive_failures = 0


def _fetch_panahon_ws_ticket() -> str | None:
    """Fetch one short-lived (~120s) connection ticket from panahon.gov.ph,
    required by its Socket.IO server -- confirmed directly by reading the
    live site's own bundled JS after ticket-less connects started being
    rejected with a server-sent "bad_ticket" connect_error.

    Launches a fresh headless Chromium (see _require_playwright() for why
    a real browser is used here instead of a spoofing HTTP client),
    navigates it to https://panahon.gov.ph/, then runs _PANAHON_TICKET_JS
    inside that page to fetch the ticket exactly as the site's own code
    would. A fresh browser instance is used for every call rather than
    kept running across calls -- simpler to reason about, and a ticket
    needs to be fetched fresh on every connection/reconnection attempt
    anyway (see _panahon_auth() below) so there's little to gain by
    keeping one warm; the cost is a few extra seconds per attempt for
    Chromium to launch and the page to load, which is a fine trade against
    every previous approach failing outright.

    Returns None on failure rather than raising (fetching a ticket is
    itself best-effort, matching the site's own code, which falls back to
    connecting with empty auth rather than blocking when a ticket can't be
    obtained) -- but prints exactly why first. That matters: a
    silently-swallowed failure here and a server-rejected ticket both
    surface identically downstream (as a "bad_ticket" connect_error), and
    those are two completely different problems to chase -- this is the
    only place that can actually tell them apart.
    """
    sync_playwright = _require_playwright()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                # ignore_https_errors mirrors this script's --insecure /
                # "Skip SSL verification" flag (INSECURE), same as every
                # other request this file makes.
                context = browser.new_context(ignore_https_errors=INSECURE)
                page = context.new_page()
                page.goto("https://panahon.gov.ph/", wait_until="domcontentloaded", timeout=40000)
                result = page.evaluate(_PANAHON_TICKET_JS)
            finally:
                browser.close()
    except Exception as e:
        print(f"  [panahon ticket] headless browser couldn't fetch a ticket: {e}")
        _note_panahon_ticket_failure()
        return None
    error = result.get("error") if isinstance(result, dict) else "unexpected result shape"
    if error:
        print(f"  [panahon ticket] {error}")
        _note_panahon_ticket_failure()
        return None
    ticket = result.get("ticket")
    if not isinstance(ticket, str):
        print(f"  [panahon ticket] response had no usable 'ticket' field: {result!r}")
        _note_panahon_ticket_failure()
        return None
    _note_panahon_ticket_success()
    return ticket


def _panahon_auth():
    """The Socket.IO `auth` value for a panahon.gov.ph connection --
    passed to sio.connect() as this function itself (NOT called here):
    python-socketio invokes a callable `auth` fresh on every connection
    AND every reconnection attempt, which is exactly what's needed since
    each ticket from _fetch_panahon_ws_ticket() is short-lived. Mirrors
    the site's own fallback exactly: {} (no ticket) rather than raising,
    if a ticket couldn't be obtained -- connecting with no ticket at all
    is itself a real (if unlikely to succeed) thing the live site does,
    and the resulting connect_error is diagnosed same as ever (with
    _fetch_panahon_ws_ticket() above having already printed why, if the
    fetch itself is what failed)."""
    ticket = _fetch_panahon_ws_ticket()
    return {"ticket": ticket} if ticket else {}


def _panahon_connect_error_message(url: str, e: Exception, connect_error_reason: list) -> str:
    """Build a diagnostic message for a failed panahon.gov.ph Socket.IO
    connection attempt, distinguishing two genuinely different failure
    points that socketio.Client.connect() can raise from -- lumping them
    into one generic "your network blocks this" message (as earlier
    versions of this function did) actively misleads for the second case:

    1. The low-level Engine.IO transport itself never connected at all --
       DNS failure, connection refused/reset, TLS failure, or the initial
       HTTPS polling request timing out outright. THIS is the case a
       network-level block (a firewall/proxy dropping outbound traffic to
       ws.panahon.gov.ph specifically) would actually produce.

    2. That transport DID connect -- the plain HTTPS polling request
       succeeded -- but the Socket.IO-level handshake for the "/"
       namespace was then rejected or simply never acknowledged within
       the connection timeout. python-socketio raises this specific case
       with the message "One or more namespaces failed to connect: /".
       Getting HERE means the network is fine; blaming it (as the old
       message did) sends you looking in the wrong place. If the server
       sent an explicit rejection, `connect_error_reason` -- filled in by
       a `@sio.event def connect_error(data)` handler registered on the
       client, since python-socketio only surfaces that reason through
       the event, not through the raised exception itself -- carries it,
       and it's usually far more actionable than the generic message
       alone. In particular, a `bad_ticket` reason means the server
       rejected (or never received) the connection ticket that
       _panahon_auth()/_fetch_panahon_ws_ticket() now fetch and send
       automatically -- see the dedicated branch below for that case.
    """
    reason = connect_error_reason[0] if connect_error_reason else None
    is_namespace_failure = "namespaces failed to connect" in str(e)
    reason_text = str(reason) if reason is not None else ""
    is_ticket_failure = "ticket" in reason_text.lower()

    lines = [f"Couldn't connect to panahon.gov.ph's lightning feed at {url}: {e}", ""]

    if is_ticket_failure:
        lines.append(
            "The server rejected the connection ticket this script fetches "
            f"automatically before connecting (from {PANAHON_WS_TICKET_URL}) "
            f"-- reported reason: {reason!r}."
        )
        lines.append(
            "\nCheck the line(s) just above this error tagged '[panahon "
            "ticket]', if any: that's _fetch_panahon_ws_ticket() reporting "
            "on the ticket REQUEST itself (network/SSL failure, or a "
            "malformed response) -- their absence means the request "
            "succeeded and returned what looked like a normal ticket, and "
            "it was the SERVER that rejected it at connect time, which is "
            "the more suspicious case: if this happens on every single "
            "attempt rather than intermittently, a timing race is unlikely "
            "to be the explanation (see below), and it's worth suspecting "
            "the ticket itself being invalid from the start rather than "
            "simply expired.\n\n"
            "A ticket is only valid for a short window (~120 seconds when "
            "this was last checked) and is meant to be used almost "
            "immediately, so occasional failures are most often just a "
            "timing hiccup -- the ticket expired between being fetched and "
            "being used, e.g. because the connection attempt itself took a "
            "while, or a reconnect reused one instead of fetching fresh (it "
            "shouldn't -- if you see this repeatedly, that logic may be "
            "worth double-checking). Simply trying again usually works for "
            "this case.\n\n"
            "If it keeps failing every time, panahon.gov.ph's server-side "
            "ticket contract may have changed again since this was last "
            "confirmed against the live site (last confirmed requirement: "
            "the ticket request needs a `?token=` query parameter carrying "
            "the value of the homepage's `<meta name=\"csrf-token\">` tag, "
            "plus the session cookies from that same homepage load -- see "
            "_fetch_panahon_ws_ticket()'s docstring) -- in that case, "
            "opening https://panahon.gov.ph in a normal browser, enabling "
            "its 'Realtime Lightning' layer, and checking the browser's "
            "Network tab for a fresh call to /api/v1/ws-ticket (and how "
            "its result is used) is the most reliable way to re-diagnose "
            "this.\n\n"
            "--lightning-source pagasa uses a completely different (plain "
            "HTTPS polling, no Socket.IO, no ticket) endpoint and doesn't "
            "depend on any of this."
        )
    elif is_namespace_failure:
        lines.append(
            "This is a Socket.IO-level handshake failure, not a lower-level "
            "connection failure -- the plain HTTPS request to the server "
            "already succeeded (a network block would show up as a "
            "different, lower-level error instead), but the server either "
            "rejected the \"/\" namespace or never acknowledged it within "
            "the connection timeout."
        )
        if reason is not None:
            lines.append(f"\nServer-reported reason: {reason!r}")
        lines.append(
            "\nMost likely causes, roughly in order: a transient server-side "
            "hiccup (just try again -- this is often the whole story); the "
            "connection timeout (15s) being too short if the server happens "
            "to be slow right now; or the installed python-socketio / "
            "python-engineio / websocket-client versions being incompatible "
            "with what the server currently expects. Check versions with:\n\n"
            "    pip show python-socketio python-engineio websocket-client\n\n"
            "and try upgrading them if they look old:\n\n"
            "    pip install -U \"python-socketio[client]\" python-engineio websocket-client\n\n"
            "If it keeps failing, --lightning-source pagasa uses a completely "
            "different (plain HTTPS polling, no Socket.IO) endpoint and "
            "doesn't depend on any of this."
        )
    else:
        lines.append(
            "This connects via plain HTTPS polling first (like PAGASA's "
            "other endpoints), then tries to upgrade to a websocket -- so "
            "getting here at all means even that initial plain HTTPS "
            "request failed, not just the websocket upgrade. Most likely "
            "cause: your network blocks outbound connections to "
            "ws.panahon.gov.ph specifically -- this can fail even though "
            "the rest of this script (and --lightning-source pagasa) works "
            "fine. Try opening https://panahon.gov.ph in a normal browser "
            "to confirm the site itself is reachable from this network."
        )

    return "\n".join(lines)


def _panahon_connect_with_retries(build_client, url: str, stop_event=None, retries: int = 5, retry_delay: int = 15):
    """Call build_client() to get a fresh, fully-configured-but-not-yet-
    connected socketio.Client, then attempt sio.connect() on it -- retrying
    on failure, with a real delay between attempts, rather than failing
    hard on the very first one.

    Originally added on the theory that panahon.gov.ph's backend was
    intermittently flaky -- that theory turned out to be WRONG (the real,
    deterministic cause was a missing CSRF token/cookie pairing on the
    ticket request, now fixed directly in _fetch_panahon_ws_ticket(); see
    its docstring for the full story). Kept anyway as a cheap safety net
    for genuine transient hiccups (an ordinary dropped connection, a slow
    DNS lookup, one bad round-trip) that can still happen to any live
    service and are worth one quiet retry rather than an immediate hard
    failure -- just no longer billed as working around a known flaky
    backend, since that specific claim wasn't true.

    A FRESH client is built for every attempt (build_client() is called
    once per attempt) rather than reusing one across retries -- simpler to
    reason about than trusting a socketio.Client's internal state after a
    failed connect() call, at the cost of re-registering event handlers
    each time (cheap, and build_client's closures over the caller's own
    state -- e.g. watch_lightning_panahon()'s `seen`/`lock`/`out_path` --
    work identically regardless of which Client instance they end up
    attached to). build_client() must return (sio, connect_error_reason)
    -- the latter a list that a `connect_error` handler registered on
    that sio appends to, exactly as elsewhere in this file, so the FINAL
    failure (if retries are exhausted) can still be diagnosed with
    whatever reason the server actually gave on that last attempt.

    Returns the connected client on success. Raises RuntimeError (via
    _panahon_connect_error_message(), so the final failure is diagnosed
    exactly as it always was) once retries are exhausted, or immediately
    if stop_event becomes set between attempts (checked so a long retry
    delay doesn't ignore the GUI's Stop button).
    """
    last_exc = None
    last_reason = []
    for attempt in range(1, retries + 2):
        sio, connect_error_reason = build_client()
        try:
            sio.connect(
                url, headers=PANAHON_WS_HEADERS, auth=_panahon_auth,
                transports=["polling", "websocket"], wait_timeout=15,
            )
            return sio
        except Exception as e:
            last_exc = e
            last_reason = connect_error_reason
            if attempt > retries or (stop_event is not None and stop_event.is_set()):
                break
            print(
                f"  [connect attempt {attempt}/{retries + 1} failed, "
                f"retrying in {retry_delay}s: {e}]"
            )
            for _ in range(retry_delay):
                if stop_event is not None and stop_event.is_set():
                    break
                time.sleep(1)
    raise RuntimeError(_panahon_connect_error_message(url, last_exc, last_reason)) from last_exc


def fetch_lightning_panahon(duration: int = 20, url: str = PANAHON_LIGHTNING_WS_URL) -> list:
    """Connect to panahon.gov.ph's live lightning websocket, collect
    whatever strikes arrive over `duration` seconds, then disconnect.

    Unlike fetch_lightning() (a REST snapshot of PAGASA's own short rolling
    window), this is a live push feed with no history to ask for -- a
    one-shot call can only ever catch strikes that happen to occur while
    it's connected. Use --watch (watch_lightning_panahon) to keep listening
    indefinitely instead of picking a fixed duration.

    The initial connection is retried a few times (see
    _panahon_connect_with_retries()) as a safety net against ordinary
    transient network hiccups -- a fewer/shorter retry budget than
    watch_lightning_panahon() uses, since this is meant to be a quick
    one-off snapshot rather than a long-running session.
    """
    socketio = _require_socketio()
    _require_playwright()  # fails fast, clearly, if missing -- needed by _panahon_auth() below
    strikes = []

    def build_client():
        # ssl_verify=not INSECURE: ws.panahon.gov.ph has been observed
        # failing Python's TLS verification with "unable to get local
        # issuer certificate" (a missing-intermediate-certificate server
        # misconfiguration -- confirmed live, same class of issue as
        # api.meteopilipinas.gov.ph's already-documented SSL error in
        # download_png() above) even though a real browser connects fine
        # (browsers auto-fetch a missing intermediate; Python's ssl module
        # doesn't). Without this, --insecure had no effect on the panahon
        # websocket connection itself -- only on download_png()'s image
        # fetches -- even though the GUI's "Skip SSL verification" checkbox
        # implies it covers everything.
        sio = socketio.Client(reconnection=False, ssl_verify=not INSECURE)

        @sio.on(PANAHON_LIGHTNING_EVENT)
        def _on_strike(raw):
            strikes.append(_normalize_panahon_strike(raw))

        # Filled in by python-socketio if the server itself rejects the
        # "/" namespace handshake with an explicit reason -- see
        # _panahon_connect_error_message() for why this matters.
        connect_error_reason = []

        @sio.event
        def connect_error(data=None):
            connect_error_reason.append(data)

        return sio, connect_error_reason

    sio = _panahon_connect_with_retries(build_client, url, retries=3, retry_delay=10)

    time.sleep(max(0, duration))
    sio.disconnect()
    return strikes


def panahon_lightning_to_csv(strikes: list, out_path: Path) -> Path:
    """Write panahon.gov.ph lightning strike records to a CSV file."""
    out_path.parent.mkdir(parents=True, exist_ok=True)

    fieldnames = list(PANAHON_LIGHTNING_FIELDS)
    for strike in strikes:
        for key in strike.keys():
            if key not in fieldnames:
                fieldnames.append(key)

    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for strike in strikes:
            writer.writerow(strike)

    return out_path


def watch_lightning_panahon(
    outdir: Path,
    window_minutes: int = None,
    stop_event=None,
    url: str = PANAHON_LIGHTNING_WS_URL,
    split_minutes: int = None,
) -> Path:
    """Stay connected to panahon.gov.ph's live lightning websocket and log
    every strike as it arrives -- no polling interval needed, since this is
    a push feed rather than something you ask for repeatedly.

    Same three mutually exclusive modes as watch_lightning() (pass at most
    one of window_minutes / split_minutes), de-duplicated on
    (timestamp, latitude, longitude):

    - Continuous (default, both args None): append-only running log.
    - window_minutes=N: rolling-window trim -- the CSV is rewritten on
      every new strike to hold only strikes first seen within the last N
      minutes.
    - split_minutes=N: segment rotation -- writes to one CSV for N minutes,
      then finalizes it and starts a fresh one for the next N-minute
      segment. Checked both on every incoming strike and once a second in
      the idle heartbeat below, so a quiet segment still rotates on time
      instead of silently stretching until the next strike happens to
      arrive.

    The initial connect is retried automatically (5 attempts, 15s apart)
    before giving up -- mainly as a safety net for ordinary transient
    network hiccups (see _panahon_connect_with_retries()'s docstring; an
    earlier theory that panahon.gov.ph's backend itself was flaky turned
    out to be a misdiagnosis of what was actually a deterministic missing-
    CSRF-token bug, now fixed in _fetch_panahon_ws_ticket()). Once
    connected, the ongoing watch relies on python-socketio's own
    reconnection logic.

    Stops on Ctrl+C (or when stop_event is set, for callers like the GUI).
    """
    if window_minutes and split_minutes:
        raise ValueError("window_minutes and split_minutes are mutually exclusive -- pick one.")

    socketio = _require_socketio()
    _require_playwright()  # fails fast, clearly, if missing -- needed by _panahon_auth() below
    outdir.mkdir(parents=True, exist_ok=True)

    def make_path(now: float) -> Path:
        stamp = time.strftime("%b_%d_%Y_%I%M%p", time.localtime(now))
        if split_minutes:
            return outdir / f"panahon_lightning_split{split_minutes}min_{stamp}.csv"
        elif window_minutes:
            return outdir / f"panahon_lightning_last{window_minutes}min_{stamp}.csv"
        else:
            return outdir / f"panahon_lightning_log_{stamp}.csv"

    # value: (strike_dict, epoch_seconds_first_observed). IMPORTANT: unlike
    # a polled watch loop, python-socketio's threading async_mode (the
    # default) dispatches EACH incoming message on its OWN new background
    # thread -- confirmed directly (a burst of strikes produced a fresh
    # "Thread-NNN (_handle_eio_message)" per strike, all running at once,
    # not one thread handling them one at a time). So every read/modify of
    # `seen`/`out_path`/`segment_start` below has to be treated as
    # genuinely concurrent and guarded by `lock` -- without it, two strikes
    # arriving close together can race on the same dict/file (this is also
    # what caused the dict-vs-dict sort crash this replaced: two strikes
    # landing in the same time.time() tick, on Windows in particular where
    # the clock's resolution is much coarser than Linux's, made
    # `sorted((t, s) for ...)` fall back to comparing the strike dicts
    # themselves once their `t` values tied -- fixed below with an explicit
    # sort key instead of sorting the tuples directly). In split mode this
    # is reset to {} at the start of every new segment.
    seen: dict = {}
    lock = threading.Lock()
    segment_start = time.time()
    out_path = make_path(segment_start)

    def rewrite_window_file(now: float) -> int:
        cutoff = now - window_minutes * 60
        rows = sorted(
            ((t, s) for s, t in seen.values() if t >= cutoff), key=lambda pair: pair[0]
        )
        with out_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=PANAHON_LIGHTNING_FIELDS, extrasaction="ignore")
            writer.writeheader()
            for t, s in rows:
                writer.writerow(s)
        return len(rows)

    def append_row(s: dict):
        write_header = not out_path.exists()
        with out_path.open("a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=PANAHON_LIGHTNING_FIELDS, extrasaction="ignore")
            if write_header:
                writer.writeheader()
            writer.writerow(s)

    def maybe_rotate_segment_locked(now: float):
        # Caller must already hold `lock`. No-op unless split mode and the
        # current segment has run its full duration.
        nonlocal segment_start, out_path, seen
        if not split_minutes or now - segment_start < split_minutes * 60:
            return
        print(
            f"  [{time.strftime('%H:%M:%S')}] {split_minutes}-min segment complete "
            f"({len(seen)} strike(s)) -- saved {out_path}"
        )
        segment_start = now
        out_path = make_path(now)
        seen = {}
        print(f"  [{time.strftime('%H:%M:%S')}] starting new segment -- {out_path}\n")

    def build_client():
        # reconnection_delay/_max deliberately much slower than
        # python-socketio's own defaults: each reconnect attempt fetches a
        # fresh ticket via a full headless-browser launch (see
        # _fetch_panahon_ws_ticket()), so retrying every 5s during a long
        # panahon.gov.ph bad patch means dozens of browser launches for
        # nothing -- wasteful on this machine and needlessly heavy on
        # their server for no better odds of success. 30-60s is still
        # plenty responsive (recovers within a minute of the server coming
        # back) without hammering either side during an extended outage.
        # ssl_verify=not INSECURE -- see the matching comment in
        # fetch_lightning_panahon()'s build_client() above: ws.panahon.gov.ph
        # can fail Python's TLS verification ("unable to get local issuer
        # certificate") even though a real browser connects to it fine.
        sio = socketio.Client(
            reconnection=True, reconnection_attempts=0,
            reconnection_delay=30, reconnection_delay_max=60,
            ssl_verify=not INSECURE,
        )

        # Filled in by python-socketio if the server rejects the initial "/"
        # namespace handshake with an explicit reason -- see
        # _panahon_connect_error_message() for why this matters.
        connect_error_reason = []

        @sio.event
        def connect_error(data=None):
            connect_error_reason.append(data)

        @sio.on(PANAHON_LIGHTNING_EVENT)
        def _on_strike(raw):
            s = _normalize_panahon_strike(raw)
            key = (s["timestamp"], s["latitude"], s["longitude"])
            with lock:
                now = time.time()
                maybe_rotate_segment_locked(now)
                if key in seen:
                    return
                seen[key] = (s, now)
                if window_minutes:
                    cutoff = now - window_minutes * 60
                    for k in [k for k, (_, t) in seen.items() if t < cutoff]:
                        del seen[k]
                    total = rewrite_window_file(now)
                else:
                    append_row(s)
                    total = len(seen)
            if window_minutes:
                print(
                    f"  [{time.strftime('%H:%M:%S')}] +1 strike ({s['type']}) -- "
                    f"{total} strike(s) currently within the last {window_minutes} min"
                )
            else:
                label = f"segment total: {total}" if split_minutes else f"total logged: {total}"
                print(f"  [{time.strftime('%H:%M:%S')}] +1 strike ({s['type']}) ({label})")

        return sio, connect_error_reason

    print(f"Connecting to panahon.gov.ph's live lightning feed ({url}) ...")
    sio = _panahon_connect_with_retries(build_client, url, stop_event=stop_event, retries=5, retry_delay=15)

    if split_minutes:
        print(
            f"Connected. Splitting into a new CSV every {split_minutes} minutes -- "
            f"first segment: {out_path}\n"
        )
    elif window_minutes:
        print(
            f"Connected. Rolling {window_minutes}-minute window mode: {out_path} "
            f"always reflects roughly the last {window_minutes} min.\n"
        )
    else:
        print(f"Connected. Logging every strike to {out_path}\n")
    print("Press Ctrl+C to stop.\n")

    try:
        while True:
            if stop_event is not None and stop_event.is_set():
                break
            time.sleep(1)
            if split_minutes:
                with lock:
                    maybe_rotate_segment_locked(time.time())
    except KeyboardInterrupt:
        pass

    sio.disconnect()
    if split_minutes:
        print(
            f"\nStopped. Final (unfinished) segment had {len(seen)} strike(s) "
            f"-- see {out_path} (and any earlier completed segments in {outdir})"
        )
    else:
        print(f"\nStopped. {len(seen)} strike(s) currently tracked -- see {out_path}")
    return out_path


def watch_radar(product: str, outdir: Path, interval: int, recolor: str = None, stop_event=None) -> Path:
    """Poll PAGASA's radar timeline repeatedly and save every NEW frame as
    its own GeoTIFF, building up an archive that outlasts PAGASA's own
    ~75-minute rolling window (6 frames, 15 min apart -- confirmed live,
    and there's no date/range parameter on this endpoint to ask for more
    history directly, same situation as the lightning feed).

    A frame is considered "new" the first time its `time` string is seen
    (PAGASA emits a fresh one every 15 minutes and drops the oldest, so
    de-duplicating on that string is enough -- it never repeats for a
    genuinely different image). Every new frame is written out immediately
    via process_frame(), same as a one-off fetch, so nothing is held only
    in memory.

    Unlike watch_lightning() there's no --window mode here: every frame
    this catches is worth keeping (they're already sparse, one every 15
    min), so this always accumulates rather than trims.

    Stops on Ctrl+C (or when stop_event is set, for callers like the GUI).
    """
    outdir.mkdir(parents=True, exist_ok=True)
    seen: set = set()

    print(f"Watching '{product}' radar frames every {interval}s.")
    print(
        "PAGASA only ever exposes ~75 minutes of frames at a time (6 frames, "
        "15 min apart) -- this polls repeatedly and keeps every new frame it "
        f"catches in {outdir}, building a longer archive than PAGASA itself "
        "keeps. Nothing before this starts running can be recovered.\n"
    )
    print("Press Ctrl+C to stop.\n")

    def poll_once():
        timeline = fetch_timeline()
        frames = timeline.get(product, [])
        new_frames = [f for f in frames if f["time"] not in seen]
        for f in new_frames:
            seen.add(f["time"])
            process_frame(product, f, outdir, recolor)
        if new_frames:
            print(
                f"  [{time.strftime('%H:%M:%S')}] +{len(new_frames)} new frame(s) "
                f"(total caught: {len(seen)})"
            )
        else:
            print(f"  [{time.strftime('%H:%M:%S')}] no new frames (total caught: {len(seen)})")

    try:
        while True:
            if stop_event is not None and stop_event.is_set():
                break
            try:
                poll_once()
            except Exception as e:
                print(f"  [poll error, will retry] {e}")
            for _ in range(interval):
                if stop_event is not None and stop_event.is_set():
                    break
                time.sleep(1)
    except KeyboardInterrupt:
        pass

    print(f"\nStopped. {len(seen)} frame(s) caught this run -- see {outdir}")
    return outdir


LIGHTNING_LOG_FIELDS = ["timestamp", "latitude", "longitude", "type", "amplitude", "url"]
LIGHTNING_WINDOW_FIELDS = ["first_seen"] + LIGHTNING_LOG_FIELDS


def watch_lightning(
    outdir: Path, interval: int, window_minutes: int = None, stop_event=None, split_minutes: int = None
) -> Path:
    """Poll PAGASA's live lightning feed repeatedly.

    PAGASA's /api/Lightning only ever returns a short rolling window of very
    recent strikes -- confirmed directly against their own live site and
    their own frontend code (a plain `$.post('/api/Lightning')`, no
    date/duration parameter exists to ask for more history). A one-shot
    fetch will miss anything that already aged out, and there is no way to
    retroactively recover strikes from before this starts running.

    Three mutually exclusive modes (pass at most one of window_minutes /
    split_minutes), all de-duplicated on (timestamp, latitude, longitude):

    - Continuous (default, both args None): one append-only running log --
      every new strike ever seen this session stays in the CSV forever.
    - window_minutes=N: rolling-window trim -- the SAME CSV is rewritten
      every poll to contain only strikes first observed within the last N
      minutes; older ones are dropped, so it always reflects "roughly the
      last N minutes" as one file, but never keeps anything older than
      that. NOTE: the window is measured from when *this script* first saw
      each strike, not PAGASA's own `timestamp` field (kept in the CSV
      as-is, not used for the age math) -- so it's only fully accurate
      once this has been running continuously for at least N minutes.
    - split_minutes=N: segment rotation -- writes to one CSV for N minutes,
      then finalizes it (no more writes) and starts a brand new CSV for
      the next N-minute segment, indefinitely. Unlike window_minutes,
      nothing is ever dropped -- every strike ends up in exactly one
      segment file, you just get a fresh file every N minutes instead of
      one that keeps growing or one that keeps trimming itself.

    Stops on Ctrl+C (or when stop_event is set, for callers like the GUI
    that need a non-blocking way to stop it).
    """
    if window_minutes and split_minutes:
        raise ValueError("window_minutes and split_minutes are mutually exclusive -- pick one.")

    outdir.mkdir(parents=True, exist_ok=True)

    def make_path(now: float) -> Path:
        stamp = time.strftime("%b_%d_%Y_%I%M%p", time.localtime(now))
        if split_minutes:
            return outdir / f"pagasa_lightning_split{split_minutes}min_{stamp}.csv"
        elif window_minutes:
            return outdir / f"pagasa_lightning_last{window_minutes}min_{stamp}.csv"
        else:
            return outdir / f"pagasa_lightning_log_{stamp}.csv"

    segment_start = time.time()
    out_path = make_path(segment_start)

    # value: (strike_dict, epoch_seconds_first_observed). In split mode this
    # is reset to {} at the start of every new segment, so dedup is scoped
    # to "within this segment" rather than across the whole run.
    seen: dict = {}

    print(f"Watching for lightning strikes every {interval}s.")
    if split_minutes:
        print(
            f"Splitting into a new CSV every {split_minutes} minutes -- each "
            "file covers its own segment and is finalized (no more writes) "
            f"the moment the next one starts. First segment: {out_path}\n"
        )
    elif window_minutes:
        print(
            f"Rolling {window_minutes}-minute window mode: {out_path} always "
            f"reflects roughly the last {window_minutes} min, oldest entries "
            "drop off automatically. This can only include strikes seen "
            "since this started running -- nothing from before that.\n"
        )
    else:
        print(f"Logging every new strike seen to {out_path}\n")
    print("Press Ctrl+C to stop.\n")

    def rewrite_window_file(now: float) -> int:
        cutoff = now - window_minutes * 60
        # key= avoids comparing the dicts themselves as a tiebreaker --
        # every strike found in the same poll shares the same `now`, so
        # ties here are the normal case, not an edge case (sorting the
        # raw (t, s) tuples directly would then fall through to `s < s`,
        # which dicts don't support, and crash).
        rows = sorted(
            ((t, s) for s, t in seen.values() if t >= cutoff), key=lambda pair: pair[0]
        )
        with out_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=LIGHTNING_WINDOW_FIELDS, extrasaction="ignore")
            writer.writeheader()
            for t, s in rows:
                row = dict(s)
                row["first_seen"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))
                writer.writerow(row)
        return len(rows)

    def poll_once():
        nonlocal out_path, segment_start, seen
        now = time.time()

        if split_minutes and now - segment_start >= split_minutes * 60:
            print(
                f"  [{time.strftime('%H:%M:%S')}] {split_minutes}-min segment complete "
                f"({len(seen)} strike(s)) -- saved {out_path}"
            )
            segment_start = now
            out_path = make_path(now)
            seen = {}
            print(f"  [{time.strftime('%H:%M:%S')}] starting new segment -- {out_path}\n")

        strikes = fetch_lightning()
        new = []
        for s in strikes:
            key = (s.get("timestamp"), s.get("latitude"), s.get("longitude"))
            if key not in seen:
                seen[key] = (s, now)
                new.append(s)

        total_label = f"segment total: {len(seen)}" if split_minutes else f"total logged: {len(seen)}"

        if window_minutes:
            cutoff = now - window_minutes * 60
            for key in [k for k, (_, t) in seen.items() if t < cutoff]:
                del seen[key]
            active = rewrite_window_file(now)
            print(
                f"  [{time.strftime('%H:%M:%S')}] +{len(new)} new -- "
                f"{active} strike(s) currently within the last {window_minutes} min"
            )
        elif new:
            write_header = not out_path.exists()
            with out_path.open("a", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=LIGHTNING_LOG_FIELDS, extrasaction="ignore")
                if write_header:
                    writer.writeheader()
                for s in new:
                    writer.writerow(s)
            print(f"  [{time.strftime('%H:%M:%S')}] +{len(new)} new strike(s) ({total_label})")
        else:
            print(f"  [{time.strftime('%H:%M:%S')}] no new strikes ({total_label})")

    try:
        while True:
            if stop_event is not None and stop_event.is_set():
                break
            try:
                poll_once()
            except Exception as e:
                print(f"  [poll error, will retry] {e}")
            for _ in range(interval):
                if stop_event is not None and stop_event.is_set():
                    break
                time.sleep(1)
    except KeyboardInterrupt:
        pass

    if split_minutes:
        print(
            f"\nStopped. Final (unfinished) segment had {len(seen)} strike(s) "
            f"-- see {out_path} (and any earlier completed segments in {outdir})"
        )
    else:
        print(f"\nStopped. {len(seen)} strike(s) currently tracked -- see {out_path}")
    return out_path


def download_png(url: str) -> Image.Image:
    try:
        resp = SESSION.get(url, headers=AJAX_HEADERS, timeout=30, verify=not INSECURE)
    except requests.exceptions.SSLError as e:
        raise RuntimeError(
            f"SSL certificate verification failed for {url}\n\n"
            "This is almost always the image host (api.meteopilipinas.gov.ph) "
            "not sending its intermediate certificate during the handshake -- "
            "a server misconfiguration. Browsers/Windows tolerate this because "
            "they auto-fetch the missing intermediate cert; Python's OpenSSL "
            "stack does not by default. Two fixes, easiest first:\n\n"
            "  1) pip install pip-system-certs\n"
            "     Then just rerun this script unchanged -- this makes Python "
            "trust what Windows already trusts (including auto-fetching "
            "missing intermediates), which is the correct long-term fix.\n\n"
            "  2) Rerun with --insecure\n"
            "     Skips certificate verification for the image download only. "
            "Only reasonable because this is a public, non-sensitive radar "
            "image with no login/credentials involved -- don't use --insecure "
            "for anything that handles sensitive data.\n\n"
            f"Original error: {e}"
        ) from None
    resp.raise_for_status()
    return Image.open(io.BytesIO(resp.content)).convert("RGBA")


def unique_path(path: Path) -> Path:
    """Return `path` unchanged if nothing exists there yet, otherwise the
    same name with " (2)", " (3)", ... inserted before the extension --
    the first one that doesn't collide with an existing file.

    Used for every output file instead of ever overwriting one in place:
    on Windows, GDAL/rasterio (the GeoTIFF writer) deletes an existing
    file before rewriting it, and that delete can fail with "Permission
    denied" if anything else has the file open -- File Explorer's preview
    pane, a OneDrive sync in progress, an image viewer, another copy of
    this script/GUI running at the same time. Picking a fresh name up
    front avoids that failure mode entirely, and as a side benefit keeps
    every past fetch of the same frame instead of silently replacing it.
    """
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    n = 2
    while True:
        candidate = path.with_name(f"{stem} ({n}){suffix}")
        if not candidate.exists():
            return candidate
        n += 1


def png_to_geotiff(img: Image.Image, out_path: Path) -> None:
    """Write a PIL image as a georeferenced GeoTIFF over the PHL radar extent."""
    arr = np.array(img)  # shape (H, W, 4) -> R,G,B,A
    height, width = arr.shape[0], arr.shape[1]
    bands = np.moveaxis(arr, -1, 0)  # (4, H, W) for rasterio (band, row, col)

    transform = from_bounds(
        RADAR_EXTENT["west"],
        RADAR_EXTENT["south"],
        RADAR_EXTENT["east"],
        RADAR_EXTENT["north"],
        width,
        height,
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with rasterio.open(
            out_path,
            "w",
            driver="GTiff",
            height=height,
            width=width,
            count=4,
            dtype=bands.dtype,
            crs="EPSG:4326",
            transform=transform,
            photometric="RGB",
            alpha="yes",
            compress="deflate",
        ) as dst:
            dst.write(bands)
    except Exception as e:
        raise RuntimeError(
            f"Couldn't write {out_path}\n\n"
            "This is usually Windows refusing to let go of the file -- "
            "something else likely has it open (File Explorer's preview "
            "pane, a photo/image viewer, QGIS, a OneDrive sync in "
            "progress, or another copy of this script/GUI running at the "
            "same time). Close whatever might be viewing that file and "
            "try again, or point the output folder at a different "
            "location.\n\n"
            f"Original error: {e}"
        ) from None


def safe_stamp(time_str: str) -> str:
    return (
        time_str.replace(",", "")
        .replace(":", "")
        .replace(" ", "_")
    )


def process_frame(product: str, frame: dict, outdir: Path, recolor: str = None) -> Path:
    print(f"  fetching {product} @ {frame['time']} ...")
    img = download_png(frame["url"])
    name_suffix = ""
    if recolor:
        print(f"  recoloring with '{recolor}' palette...")
        img = recolor_image(img, recolor)
        name_suffix = f"_{recolor}"
    out_name = f"pagasa_{product}{name_suffix}_{safe_stamp(frame['time'])}.tif"
    out_path = unique_path(outdir / out_name)
    png_to_geotiff(img, out_path)
    print(f"  -> wrote {out_path}")
    return out_path


def save_selection(product: str, frames: list, outdir: Path) -> Path:
    """Save a chosen subset of currently-listed frames (just their `time`
    and `url` -- no image bytes) to a small local JSON manifest, so they
    can be downloaded later with retrieve_selection() without having to
    re-list PAGASA's timeline or remember which ones you wanted. Always
    creates a brand-new file (via unique_path -- never overwrites); use
    append_selection() instead to grow an existing one.

    Each frame starts with "retrieved": False -- retrieve_selection() sets
    it True (and records "output_path") once that frame is actually
    downloaded, so re-running Retrieve later only goes after what's new.

    IMPORTANT: none of this extends PAGASA's own ~75-minute rolling
    window. Saving (or appending to) a selection only lets you defer WHEN
    you download the frames you've picked -- it can't retrieve one that's
    since aged out of PAGASA's feed, because the `url` itself stops
    resolving once that happens (there's no separate "hold this frame"
    request on PAGASA's end). Retrieve sooner rather than later, and see
    append_selection() + watch_radar() for two different ways to build a
    longer-running local archive despite that limit.
    """
    outdir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%b_%d_%Y_%I%M%p")
    out_path = unique_path(outdir / f"pagasa_selection_{stamp}.json")
    manifest = {
        "product": product,
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "frames": [{"time": f["time"], "url": f["url"], "retrieved": False} for f in frames],
    }
    with out_path.open("w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
    return out_path


def load_selection(path: Path) -> dict:
    """Load a manifest written by save_selection() / append_selection()."""
    with Path(path).open("r", encoding="utf-8") as fh:
        return json.load(fh)


def append_selection(path: Path, product: str, frames: list) -> tuple:
    """Merge newly-picked frames into an EXISTING selection manifest
    instead of starting a new file -- so repeated Refresh-list -> select ->
    Save rounds can build one running catalog of every frame you've ever
    picked, rather than a fresh file each time. De-duplicated on `time`
    (a frame already in the manifest is left untouched, including its
    "retrieved" status -- appending never re-marks something as pending
    that was already downloaded).

    Returns (path, added_count, total_count). Raises ValueError if
    `product` doesn't match what the manifest was started with (mixing
    products in one manifest isn't supported -- start a fresh one with
    save_selection() instead).

    Same caveat as save_selection(): this only grows the local pick-list,
    it doesn't and can't resurrect a frame's PAGASA URL after that frame
    has aged out of PAGASA's ~75-minute window -- Retrieve still needs to
    run before that happens for a newly-appended entry to succeed.
    """
    path = Path(path)
    manifest = load_selection(path)
    if manifest.get("product") != product:
        raise ValueError(
            f"Manifest {path.name} was started for product "
            f"'{manifest.get('product')}', not '{product}' -- start a new "
            "selection for a different product instead of appending."
        )
    existing_times = {f["time"] for f in manifest["frames"]}
    added = 0
    for f in frames:
        if f["time"] not in existing_times:
            manifest["frames"].append({"time": f["time"], "url": f["url"], "retrieved": False})
            existing_times.add(f["time"])
            added += 1
    manifest["saved_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with path.open("w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
    return path, added, len(manifest["frames"])


def remove_from_selection(path: Path, times: list) -> tuple:
    """Remove specific frames (matched by their `time` string) from an
    existing selection manifest, rewriting it in place.

    This only edits the manifest/pick-list -- it never deletes a GeoTIFF
    already downloaded for a removed frame. If you also want that file
    gone, delete it yourself (or via the GUI, which opens the file's
    folder for you rather than deleting anything on your behalf).

    Returns (path, removed_count, remaining_count).
    """
    path = Path(path)
    manifest = load_selection(path)
    before = len(manifest["frames"])
    times_to_remove = set(times)
    manifest["frames"] = [f for f in manifest["frames"] if f["time"] not in times_to_remove]
    removed = before - len(manifest["frames"])
    manifest["saved_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with path.open("w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
    return path, removed, len(manifest["frames"])


def mark_not_retrieved(path: Path, time_str: str) -> Path:
    """Reset one frame's "retrieved" status back to False (and drop its
    recorded output_path), e.g. after its downloaded GeoTIFF was deleted
    outside this tool -- so retrieve_selection() knows to fetch it again
    instead of silently skipping it as already-done. Does nothing (no
    error) if `time_str` isn't found in the manifest.
    """
    path = Path(path)
    manifest = load_selection(path)
    for f in manifest["frames"]:
        if f["time"] == time_str:
            f["retrieved"] = False
            f.pop("output_path", None)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
    return path


def import_selection(path: Path, source_path: Path) -> tuple:
    """Merge another manifest file's (`source_path`) frames into an
    existing one (`path`), rewriting `path` in place. Unlike
    append_selection() (which takes a plain list of {time, url} frames,
    e.g. fresh picks from the Browse list), this carries over each
    imported frame's own "retrieved"/"output_path" status from the source
    manifest rather than resetting it -- so a frame already downloaded in
    the source file is recognized as already downloaded here too, without
    needing to Retrieve it again.

    A frame whose `time` is already present in `path` is left completely
    untouched -- the existing entry always wins; importing never
    overwrites or downgrades a frame you already have tracked.

    Raises ValueError if the two manifests were started for different
    products (mixing products in one manifest isn't supported).

    Returns (path, added_count, total_count).
    """
    path = Path(path)
    dest = load_selection(path)
    source = load_selection(Path(source_path))
    if dest.get("product") != source.get("product"):
        raise ValueError(
            f"Can't import: {Path(source_path).name} is for product "
            f"'{source.get('product')}', but {path.name} is for "
            f"'{dest.get('product')}'."
        )
    existing_times = {f["time"] for f in dest["frames"]}
    added = 0
    for f in source.get("frames", []):
        if f["time"] not in existing_times:
            entry = {"time": f["time"], "url": f["url"], "retrieved": bool(f.get("retrieved", False))}
            if f.get("output_path"):
                entry["output_path"] = f["output_path"]
            dest["frames"].append(entry)
            existing_times.add(f["time"])
            added += 1
    dest["saved_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with path.open("w", encoding="utf-8") as fh:
        json.dump(dest, fh, indent=2)
    return path, added, len(dest["frames"])


def retrieve_selection(
    manifest: dict,
    outdir: Path,
    recolor: str = None,
    on_frame_error=None,
    only_new: bool = True,
    manifest_path: Path = None,
) -> list:
    """Download + convert frames listed in a saved selection manifest.

    With only_new=True (the default), a frame already marked
    "retrieved": true is skipped -- so calling this again later on a
    manifest that's had more frames appended to it only goes after what's
    actually new, instead of re-downloading everything from scratch every
    time. Pass only_new=False to retry every frame regardless (e.g. if
    you deleted the output files and want them regenerated).

    Each successful frame is marked "retrieved": true on the manifest dict
    (with its output path recorded); pass manifest_path to also persist
    that back to disk after every frame, so progress survives being
    interrupted partway through and is remembered the next time this
    manifest is loaded (by the GUI or a later --retrieve run), not just
    within this one call.

    Returns the list of output paths written THIS call. A frame whose URL
    has aged out of PAGASA's rolling window (or otherwise fails) is
    skipped -- reported via on_frame_error(frame, exception) if given,
    otherwise printed -- rather than aborting the whole batch, since one
    stale pick shouldn't cost you the rest that are still good.
    """
    product = manifest["product"]
    written = []
    for frame in manifest["frames"]:
        if only_new and frame.get("retrieved"):
            continue
        try:
            out_path = process_frame(product, frame, outdir, recolor)
            written.append(out_path)
            frame["retrieved"] = True
            frame["output_path"] = str(out_path)
        except Exception as e:
            if on_frame_error:
                on_frame_error(frame, e)
            else:
                print(f"  [skipped] {frame.get('time')}: {e}")
        finally:
            if manifest_path is not None:
                with Path(manifest_path).open("w", encoding="utf-8") as fh:
                    json.dump(manifest, fh, indent=2)
    return written


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--product",
        choices=["rainfall_estimate", "reflectivity"],
        default="rainfall_estimate",
        help="Radar product to fetch (default: rainfall_estimate).",
    )
    parser.add_argument(
        "--which",
        choices=["latest", "all", "index"],
        default="latest",
        help="Which frame(s) to convert (default: latest).",
    )
    parser.add_argument(
        "--index",
        type=int,
        default=-1,
        help="Frame index to use when --which=index (0 = oldest available).",
    )
    parser.add_argument(
        "--outdir",
        type=Path,
        default=Path("./radar_tiffs"),
        help="Output directory for GeoTIFF files (default: ./radar_tiffs).",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help=(
            "Skip SSL certificate verification when downloading the radar "
            "image (only use if you hit a SSLCertVerificationError -- see "
            "that error message for the safer fix to try first)."
        ),
    )
    parser.add_argument(
        "--recolor",
        choices=list(PALETTES.keys()),
        default=None,
        help=(
            "Recolor the radar image onto a different palette instead of "
            "PAGASA's default ramp -- all five are the same green/yellow/"
            "orange/red/magenta NWS-style ramp, matched to PAGASA's bins "
            "1:1, differing only in the lowest bin(s) (the clutter-floor): "
            "'classic_nws' = medium gray, 'classic_nws_dark_gray' = darker "
            "gray, 'classic_nws_light_gray' = lighter gray, "
            "'classic_nws_no_gray' = no gray at all (that band is made "
            "fully transparent instead of colored), 'classic_nws_two_gray' "
            "= two gray steps (dark then light) before the ramp switches "
            "to color."
        ),
    )
    parser.add_argument(
        "--lightning",
        action="store_true",
        help=(
            "Fetch PAGASA's live lightning-strike feed instead of the radar "
            "mosaic, and save it as a CSV file (--product/--which/--index/"
            "--recolor are ignored in this mode)."
        ),
    )
    parser.add_argument(
        "--lightning-source",
        choices=["pagasa", "panahon"],
        default="pagasa",
        help=(
            "Which site to pull --lightning data from (default: pagasa). "
            "'pagasa' = pagasa.dost.gov.ph's polled REST snapshot (basic "
            "fields). 'panahon' = panahon.gov.ph's live Socket.IO push feed "
            "for the same underlying lightning network -- richer per-strike "
            "fields (cloud-to-ground/cloud-to-cloud, peak current, height, "
            "sensor count) but needs the extra python-socketio package (see "
            "module docstring) and, in one-shot mode, only catches whatever "
            "happens during --duration seconds rather than a snapshot of "
            "'right now'."
        ),
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=20,
        metavar="SECONDS",
        help=(
            "Used with --lightning --lightning-source panahon (one-shot, "
            "not --watch): how many seconds to stay connected and collect "
            "strikes before disconnecting and writing the CSV (default: 20). "
            "Ignored for --lightning-source pagasa and for --watch mode, "
            "where panahon just streams until stopped instead."
        ),
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help=(
            "Instead of one fetch, keep polling every --interval seconds. "
            "With --lightning: append newly-seen strikes to a running CSV "
            "log (PAGASA's feed only holds a short window of recent "
            "strikes). Without --lightning: save every NEW radar frame as "
            "its own GeoTIFF as it appears, building an archive longer than "
            "the ~75 minutes PAGASA itself keeps available (--which/--index "
            "are ignored in this mode -- every new frame is caught, not "
            "just one). Stop either mode with Ctrl+C."
        ),
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=60,
        help=(
            "Seconds between polls in --watch mode (default: 60). For radar "
            "watch specifically, PAGASA only publishes a new frame every 15 "
            "minutes, so an interval like 300 (5 min) catches every frame "
            "just as reliably with far fewer requests -- the default of 60 "
            "still works, just polls more often than it needs to."
        ),
    )
    parser.add_argument(
        "--window",
        type=int,
        default=None,
        metavar="MINUTES",
        help=(
            "Used with --lightning --watch: instead of an ever-growing log, "
            "keep rewriting the SAME CSV every poll/strike to hold only "
            "strikes seen in roughly the last MINUTES minutes (oldest "
            "entries drop off automatically -- nothing older is ever kept). "
            "Mutually exclusive with --split. Can only reflect activity "
            "seen since this started running -- there's no way to fetch "
            "strikes from before that."
        ),
    )
    parser.add_argument(
        "--split",
        type=int,
        default=None,
        metavar="MINUTES",
        help=(
            "Used with --lightning --watch: instead of one ever-growing log "
            "(or a rolling-trim window), write to one CSV for MINUTES "
            "minutes, then finalize it and start a brand new CSV for the "
            "next MINUTES-minute segment, indefinitely -- nothing is ever "
            "dropped, you just get a fresh file every MINUTES minutes "
            "instead of one file that keeps growing or trimming itself. "
            "Mutually exclusive with --window."
        ),
    )
    parser.add_argument(
        "--retrieve",
        type=Path,
        default=None,
        metavar="MANIFEST.json",
        help=(
            "Download + convert frames listed in a selection manifest "
            "previously saved/appended from the GUI's Select & Save / "
            "Retrieve section (or your own JSON in the same {product, "
            "frames: [{time, url}]} shape). By default only frames not "
            "already marked \"retrieved\" in the manifest are fetched -- "
            "pass --retrieve-all to redo every frame regardless. The "
            "manifest file itself is updated in place as frames succeed, "
            "so re-running this later (e.g. after the GUI appends more "
            "picks to the same file) only goes after what's new. Ignores "
            "--product/--which/--index. Combine with --recolor to recolor "
            "them on the way in. Frames whose URL has aged out of "
            "PAGASA's ~75-minute window are skipped with a message rather "
            "than stopping the whole batch."
        ),
    )
    parser.add_argument(
        "--retrieve-all",
        action="store_true",
        help="Used with --retrieve: also re-download frames already marked retrieved in the manifest.",
    )
    parser.add_argument(
        "--import-into",
        type=Path,
        default=None,
        metavar="SOURCE.json",
        help=(
            "Merge another manifest's frames into the one named by "
            "--retrieve, rewriting it in place, then exit without "
            "retrieving anything (combine with a separate --retrieve run "
            "afterwards). Frames already present (by timestamp) in the "
            "--retrieve manifest are left untouched; new ones carry over "
            "their retrieved/not-retrieved status from SOURCE.json rather "
            "than being reset to pending."
        ),
    )
    args = parser.parse_args()

    if args.insecure:
        global INSECURE
        INSECURE = True
        import urllib3

        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        print("WARNING: running with --insecure, SSL verification is OFF for the image download.\n")

    if args.import_into:
        if not args.retrieve:
            print("--import-into requires --retrieve <manifest to import into>.", file=sys.stderr)
            sys.exit(1)
        path, added, total = import_selection(args.retrieve, args.import_into)
        print(f"Imported {added} new frame(s) from {args.import_into} -> {path} ({total} total).")
        return

    if args.retrieve:
        print(f"Loading selection manifest {args.retrieve} ...")
        manifest = load_selection(args.retrieve)
        n = len(manifest.get("frames", []))
        already = sum(1 for f in manifest.get("frames", []) if f.get("retrieved"))
        to_try = n if args.retrieve_all else n - already
        print(
            f"{n} frame(s) in manifest for product '{manifest.get('product')}' "
            f"({already} already retrieved previously) -- attempting {to_try} now."
        )
        args.outdir.mkdir(parents=True, exist_ok=True)
        written = retrieve_selection(
            manifest,
            args.outdir,
            recolor=args.recolor,
            only_new=not args.retrieve_all,
            manifest_path=args.retrieve,
        )
        print(f"\nDone. {len(written)} frame(s) retrieved this run ({already + len(written)}/{n} total).")
        if len(written) < to_try:
            print(
                "(The rest most likely aged out of PAGASA's rolling window "
                "before this ran -- see the [skipped] lines above.)"
            )
        return

    if args.lightning and args.watch:
        if args.window and args.split:
            print("--window and --split are mutually exclusive -- pick one.", file=sys.stderr)
            sys.exit(1)
        if args.lightning_source == "panahon":
            watch_lightning_panahon(args.outdir, window_minutes=args.window, split_minutes=args.split)
        else:
            watch_lightning(
                args.outdir, args.interval, window_minutes=args.window, split_minutes=args.split
            )
        return

    if args.watch:
        watch_radar(args.product, args.outdir, args.interval, recolor=args.recolor)
        return

    if args.lightning and args.lightning_source == "panahon":
        print(f"Connecting to panahon.gov.ph's live lightning feed for {args.duration}s...")
        strikes = fetch_lightning_panahon(duration=args.duration)
        print(f"Caught {len(strikes)} strike(s) during that window.")

        args.outdir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%b_%d_%Y_%I%M%p")
        out_path = unique_path(args.outdir / f"panahon_lightning_{stamp}.csv")
        panahon_lightning_to_csv(strikes, out_path)
        print(f"-> wrote {out_path}")
        if not strikes:
            print(
                "(No strikes caught -- that just means none happened to "
                "occur during this window; try again, use --duration to "
                "listen longer, or use --watch to stream continuously. The "
                "CSV was still written with headers only.)"
            )
        print("Done.")
        return

    if args.lightning:
        print("Fetching current lightning data from PAGASA...")
        strikes = fetch_lightning()
        print(f"Found {len(strikes)} strike(s) reported right now.")

        args.outdir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%b_%d_%Y_%I%M%p")
        out_path = unique_path(args.outdir / f"pagasa_lightning_{stamp}.csv")
        lightning_to_csv(strikes, out_path)
        print(f"-> wrote {out_path}")
        if not strikes:
            print(
                "(No strikes right now -- this just means no lightning is "
                "currently being detected; the CSV was still written with "
                "headers only, so you can confirm the fetch ran.)"
            )
        print("Done.")
        return

    print("Fetching current radar timeline from PAGASA...")
    timeline = fetch_timeline()
    frames = timeline.get(args.product, [])

    if not frames:
        print(f"No frames available right now for product '{args.product}'.", file=sys.stderr)
        sys.exit(1)

    print(f"Found {len(frames)} frame(s) for '{args.product}':")
    for i, f in enumerate(frames):
        print(f"  [{i}] {f['time']}")

    args.outdir.mkdir(parents=True, exist_ok=True)

    if args.which == "latest":
        process_frame(args.product, frames[-1], args.outdir, args.recolor)
    elif args.which == "all":
        for f in frames:
            process_frame(args.product, f, args.outdir, args.recolor)
    elif args.which == "index":
        if not (0 <= args.index < len(frames)):
            print(f"--index must be between 0 and {len(frames) - 1}", file=sys.stderr)
            sys.exit(1)
        process_frame(args.product, frames[args.index], args.outdir, args.recolor)

    print("Done.")


if __name__ == "__main__":
    main()
