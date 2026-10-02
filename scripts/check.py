#!/usr/bin/env python3
"""Live HTTP smoke check for the Zone Console proxy (stdlib asserts only).

Test-audit rationale: an observable credential-leak regression guard. Frigate's
stats history embeds cpu_usages.cmdline (RTSP URLs with camera passwords), so
serving raw stats, forwarding a client's ?keys=, or opening /api/config leaks
them to any page in the browser. No internals mirrored, nothing else covers the
server-side key allowlist, no production test seams needed. A cold Frigate FAILS
rather than skipping, so this never exits 0 while validation is unperformed.

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
DENIED = ("/api/config/raw", "/api/stats/history", "/api/logs", "/api/restart", "/nginx.conf", "/api/camera_5/latest.jpg")
FORBIDDEN = ("cmdline", "cpu_usages", "rtsp://")
ALLOWED = {"cameras": {"camera_fps", "process_fps", "detection_enabled"},
           "detectors": {"inference_speed"}}
# service is an allowed top-level group (see the sample check), but it carries a flat
# scalar, not a per-name object map, so its fields get their own allowlist and check.
SCALARS = {"last_updated"}
EVENT_ID = re.compile(r"^[0-9]+(\.[0-9]+)?-[a-z0-9]{6}$")  # native id, not the old 16-hex one


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
    status, headers, _b = get(base, "/")
    assert status == 200, f"root: HTTP {status}"
    frame = re.search(r"frame-ancestors ([^;]+)", headers.get("content-security-policy", ""))
    assert frame and frame.group(1) in ("'self'", "'none'"), f"frame-ancestors not self-scoped in {headers.get('content-security-policy')!r}"
    assert headers.get("referrer-policy", "").lower() == "no-referrer" and headers.get("x-content-type-options", "").lower() == "nosniff", f"referrer-policy {headers.get('referrer-policy')!r} / nosniff {headers.get('x-content-type-options')!r}"
    print(f"ok  root 200; frame-ancestors {frame.group(1)}, referrer no-referrer, nosniff")

    for path in DENIED:
        status, _h, _b = get(base, path)
        assert status in (403, 404, 405), f"{path}: HTTP {status}, expected a denial"
    for method in ("POST", "DELETE"):
        status, _h, _b = get(base, "/api/stats", method=method)
        assert status in (403, 405), f"{method} /api/stats: HTTP {status}, expected 403 or 405"
    print(f"ok  {len(DENIED)} forbidden paths denied; POST and DELETE /api/stats refused with 403/405")

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

    for cam in CAMERAS:
        status, _h, body = get(base, f"/api/{cam}/latest.jpg")
        assert status == 200 and body[:2] == b"\xff\xd8", f"{cam}/latest.jpg: HTTP {status}, not a JPEG"
    print(f"ok  all {len(CAMERAS)} latest.jpg previews 200 JPEG\n\nPASS  every assertion held")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as err:
        print(f"\nFAIL  {err}")
        sys.exit(1)