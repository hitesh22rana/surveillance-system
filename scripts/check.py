#!/usr/bin/env python3
"""Live HTTP smoke check for the Zone Console proxy (stdlib asserts only).

Test-audit rationale: an observable credential-leak regression guard, and a guard on
the two boundaries a browser actually touches. Frigate's stats history embeds
cpu_usages.cmdline (RTSP URLs with camera passwords), and the live player is
go2rtc's own bundled script, so serving raw stats, forwarding a client's ?keys=,
opening /api/config, or letting a URL reach the page with userinfo leaks
credentials to any page in the browser. No internals mirrored, nothing else
covers the server-side key allowlist or the pinned live paths, no production test
seams needed. A cold Frigate FAILS rather than skipping, so this never exits 0
while validation is unperformed.

This is a transport check. It does not open a WebSocket and cannot prove that any
frame was decoded: live playback is recorded in docs/validation.md from a
browser session, not from this script.

    python3 scripts/check.py [--base http://127.0.0.1:8088]
"""

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request

CAMERAS = ("camera_1", "camera_2", "camera_3", "camera_4")
DENIED = ("/api/config/raw", "/api/stats/history", "/api/logs", "/api/restart", "/nginx.conf",
          "/api/camera_5/latest.jpg",
          # go2rtc's own surface: the proxy pins one script and four live paths, so
          # its config, its stream list, its control WebSocket and any other camera
          # must all be unreachable from here.
          "/api/streams", "/api/ws", "/api/go2rtc/streams",
          "/live/camera_5", "/live/camera_1/extra")
WRITES = ("/api/stats", "/live/camera_1")
FORBIDDEN = ("cmdline", "cpu_usages", "rtsp://")
ALLOWED = {"cameras": {"camera_fps", "process_fps", "detection_enabled"},
           "detectors": {"inference_speed"}}
# service is an allowed top-level group (see the sample check), but it carries a flat
# scalar, not a per-name object map, so its fields get their own allowlist and check.
SCALARS = {"last_updated"}
EVENT_ID = re.compile(r"^[0-9]+(\.[0-9]+)?-[a-z0-9]{6}$")  # native id, not the old 16-hex one
REQUIRED_ZONE = {cam: f"{cam}_area" for cam in CAMERAS}
# Only an rtsp:// URL that carries userinfo is a leak. A bare rtsp:// mention in a
# comment or a library's own docs is not a secret and must not fail this check.
RTSP_URL = re.compile(r"rtsp://\S+")
RTSP_USERINFO = re.compile(r"^rtsp://[^/@\s]+@")
# The worker polls every 30 s, so an event that only just ended can still be
# unpinned. Only rows that ended longer ago than one of its cycles are asserted.
SETTLED_S = 60


def get(base, path, method="GET"):
    """(status, lowercased headers, body bytes); HTTPError is returned, transport errors raise."""
    req = urllib.request.Request(base.rstrip("/") + path, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            return res.status, {k.lower(): v for k, v in res.headers.items()}, res.read()
    except urllib.error.HTTPError as err:
        return err.code, {k.lower(): v for k, v in (err.headers or {}).items()}, err.read()


def main():
    ap = argparse.ArgumentParser(description="live proxy smoke check")
    ap.add_argument("--base", default="http://127.0.0.1:8088", help="dashboard base URL")
    base = ap.parse_args().base
    print(f"checking {base}")
    status, headers, index = get(base, "/")
    assert status == 200, f"root: HTTP {status}"
    csp = headers.get("content-security-policy", "")
    frame = re.search(r"frame-ancestors ([^;]+)", csp)
    assert frame and frame.group(1) in ("'self'", "'none'"), f"frame-ancestors not self-scoped in {csp!r}"
    # The live player's MediaSource is attached to a blob: URL, so media-src has to
    # allow blob: and nothing else. A missing blob: is a live view that can never
    # start; an extra source here is a widened surface.
    media = re.search(r"media-src ([^;]+)", csp)
    assert media and set(media.group(1).split()) == {"'self'", "blob:"}, \
        f"media-src is {media.group(1) if media else None!r}, expected exactly 'self' and blob:"
    assert headers.get("referrer-policy", "").lower() == "no-referrer" and headers.get("x-content-type-options", "").lower() == "nosniff", f"referrer-policy {headers.get('referrer-policy')!r} / nosniff {headers.get('x-content-type-options')!r}"
    print(f"ok  root 200; frame-ancestors {frame.group(1)}, media-src 'self' blob:, referrer no-referrer, nosniff")

    for path in DENIED:
        status, _h, _b = get(base, path)
        assert status in (403, 404, 405), f"{path}: HTTP {status}, expected a denial"
    for path in WRITES:
        for method in ("POST", "DELETE"):
            status, _h, _b = get(base, path, method=method)
            assert status in (403, 405), f"{method} {path}: HTTP {status}, expected 403 or 405"
    print(f"ok  {len(DENIED)} forbidden paths denied; POST and DELETE refused on {', '.join(WRITES)}")

    # The player itself: the copy go2rtc ships, served as a module, is what the page
    # imports. Anything served here is code the browser runs, so it is also checked
    # for credentials. No rtsp:// URL may arrive with userinfo; a bare rtsp://
    # mention is not a secret.
    assets = {"index.html": index.decode("utf-8", "replace")}
    status, headers, body = get(base, "/app.js")
    assert status == 200, f"/app.js: HTTP {status}"
    assets["app.js"] = body.decode("utf-8", "replace")
    status, headers, body = get(base, "/video-rtc.js")
    assert status == 200, f"/video-rtc.js: HTTP {status}"
    ctype = headers.get("content-type", "").lower()
    assert "javascript" in ctype, f"/video-rtc.js content-type {ctype!r}"
    player = body.decode("utf-8", "replace")
    assets["video-rtc.js"] = player
    assert "VideoRTC" in player and re.search(r"\bexport\b", player), \
        "/video-rtc.js is not the bundled VideoRTC module the page imports"
    for name, text in assets.items():
        leaked = [url for url in RTSP_URL.findall(text) if RTSP_USERINFO.match(url)]
        assert not leaked, f"{name} serves rtsp:// with credentials: {leaked[:2]}"
    print(f"ok  /video-rtc.js 200 {ctype.split(';')[0]} and is the VideoRTC module; "
          f"{len(assets)} served assets carry no rtsp:// credential")

    latest = None
    for label, path in (("plain", "/api/stats"), ("override", "/api/stats?keys=cpu_usages.cmdline")):
        status, _h, body = get(base, path)
        assert status == 200, f"{label} /api/stats: HTTP {status} (Frigate must be up)"
        text = body.decode("utf-8", "replace")
        assert not (leaked := [n for n in FORBIDDEN if n in text.lower()]), f"{label} /api/stats leaked {leaked}"
        assert isinstance(history := json.loads(text), list) and history, f"{label} /api/stats: history must be a non-empty list"
        for sample in history:
            extra = set(sample) - (set(ALLOWED) | {"service"})
            assert not extra, f"{label} sample carries groups {sorted(extra)}"
            for group, fields in ALLOWED.items():
                for name, entry in (sample.get(group) or {}).items():
                    extra = set(entry) - fields if isinstance(entry, dict) else {"<not an object>"}
                    assert not extra, f"{label} {group}.{name} carries {sorted(extra)}"
            service = sample.get("service") or {}
            extra = set(service) - SCALARS if isinstance(service, dict) else {"<not an object>"}
            assert not extra, f"{label} service carries {sorted(extra)}"
            value = service.get("last_updated")
            assert isinstance(value, (int, float)) and not isinstance(value, bool), f"{label} service.last_updated {value!r}, expected a number"
        latest = history[-1]
        print(f"ok  {label} /api/stats 200, {len(history)} samples, allowlist held, no cmdline/cpu_usages/rtsp")

    cameras = latest.get("cameras") or {}
    assert set(CAMERAS) <= set(cameras), f"latest sample cameras {sorted(cameras)}"
    for cam in CAMERAS:
        entry = cameras[cam]
        assert entry.get("detection_enabled") == 1 and entry.get("camera_fps", 0) > 0 and entry.get("process_fps", 0) > 0, f"{cam} is not a working detection camera: {entry}"
    updated = (latest.get("service") or {}).get("last_updated")
    assert isinstance(updated, (int, float)) and 0 <= time.time() - updated <= 20, f"service.last_updated {updated!r} is stale"
    print(f"ok  latest sample: 4 cameras detecting, service.last_updated {time.time() - updated:.1f}s old")

    status, _h, body = get(base, "/api/events?limit=25")
    assert status == 200, f"/api/events: HTTP {status}"
    assert isinstance(events := json.loads(body.decode("utf-8", "replace")), list), f"/api/events: {type(events).__name__}, expected a list"
    bad = [e.get("id") for e in events if not isinstance(e.get("id"), str) or not EVENT_ID.match(e["id"])]
    assert not bad, f"non-native event ids {bad[:3]}"
    print(f"ok  /api/events 200, list of {len(events)}, ids are native timestamp-rand")

    shots = [e for e in events if e.get("has_snapshot")]
    if not shots:
        print("NOT TESTED  event snapshot: no has_snapshot event returned, no event evidence either way")
    else:
        eid = shots[0]["id"]
        status, _h, body = get(base, f"/api/events/{eid}/snapshot.jpg")
        assert status == 200 and body[:2] == b"\xff\xd8", f"snapshot {eid}: HTTP {status}, not a JPEG"
        print(f"ok  event {eid} snapshot 200 JPEG")

    # Retention, as the browser sees it: a snapshot a person triggered in a camera's
    # own zone must be flagged kept, or the periodic cleanup would take it first.
    # What this proves is that the flag reached the page, not that the worker set
    # it: the worker pins on its own cadence, and until it has run there is nothing
    # here to assert.
    now = time.time()
    settled = [e for e in events
               if isinstance(e.get("end_time"), (int, float))
               and not isinstance(e.get("end_time"), bool)
               and now - e["end_time"] >= SETTLED_S
               and REQUIRED_ZONE.get(e.get("camera")) in (e.get("zones") or [])]
    if not settled:
        print(f"NOT TESTED  retained snapshots: no required-zone event ended over {SETTLED_S}s ago")
    else:
        unpinned = [e["id"] for e in settled if e.get("retain_indefinitely") not in (True, 1)]
        assert not unpinned, f"{len(unpinned)} settled required-zone event(s) are not retained: {unpinned[:3]}"
        print(f"ok  all {len(settled)} settled required-zone event(s) are flagged kept for review")

    for cam in CAMERAS:
        status, _h, body = get(base, f"/api/{cam}/latest.jpg")
        assert status == 200 and body[:2] == b"\xff\xd8", f"{cam}/latest.jpg: HTTP {status}, not a JPEG"
    print(f"ok  all {len(CAMERAS)} latest.jpg previews 200 JPEG")
    print("note  /live/ is a WebSocket upgrade this script never opens: playback was seen in a "
          "browser, and it is recorded there, not proved by this run")
    print("\nPASS  every assertion held")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as err:
        print(f"\nFAIL  {err}")
        sys.exit(1)