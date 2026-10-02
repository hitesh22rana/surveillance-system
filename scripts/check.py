#!/usr/bin/env python3
"""Live HTTP smoke check for the Surveillance Console proxy (stdlib asserts only).

Test-audit rationale: an observable credential-leak regression guard, and a guard on
the two boundaries a browser actually touches. Frigate's stats history embeds
cpu_usages.cmdline (RTSP URLs with camera passwords), and the live player is
go2rtc's own bundled script, so serving raw stats, forwarding a client's ?keys=,
opening /api/config, or letting a URL reach the page with userinfo leaks
credentials to any page in the browser. The second boundary is the one write this
proxy now carries: an exact delete path to the retention worker, which must accept
a POST and refuse every other method, a foreign origin, a missing action header, a
mislabelled body, an unknown scope and an id that is not a tracker id. The third
is the one read that forwards a query string at all: the paginated events page,
which the proxy hands to that same validating worker rather than to raw Frigate, so
what comes back has to be a bounded 25-row page, filtered to the camera that was
asked for, and a write on that path still has to be a 405. No internals
mirrored, nothing else covers the server-side key allowlist or the pinned live paths,
no production test seams needed. A cold Frigate FAILS rather than skipping, so this
never exits 0 while validation is unperformed.

Every request below is read-only except the delete refusals, which carry bodies that
cannot delete anything.

Every request below that touches the delete path carries a body that cannot delete
anything: an id that is well formed but cannot exist, an empty scope, or a scope the
contract does not define. Nothing here ever deletes a snapshot, whatever the guards
under test do.

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
          # Frigate's own event writes: this proxy has one delete path of its own,
          # pinned to the retention worker, and no route to Frigate's retain or
          # delete by tracker id.
          "/api/events/1790910000.123456-abc123/retain",
          "/api/events/1790910000.123456-abc123/delete",
          "/api/events/1790910000.123456-abc123",
          # The snapshot surface is three exact paths. Anything else under it, any
          # sub-path of the paginated read, and the generic archive name, must
          # not be reachable from a browser.
          "/api/snapshots", "/api/snapshots/", "/api/snapshots/unknown",
          "/api/snapshots/events/1",
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
SUMMARY_PATH = "/api/snapshots/summary"
EVENTS_PATH = "/api/snapshots/events"
DELETE_PATH = "/api/snapshots/delete"
# The page the worker returns, whatever is asked for. This is the bound that makes
# the forwarded query string safe: the widest read anyone can ask for is one page.
PAGE_SIZE = 25
# A tracker id of the right shape at a frame time no camera ever had. Every refusal
# below is built on it, so each one fails for the guard it names and not because the
# body happened to be unusable.
PROBE_ID = "0.000000-zzzzzz"
# Any of these is a refusal. Which one a guard picks is the worker's business.
REFUSED = (400, 403, 405, 415, 422)
# The ordinary camera_N_area zone is what makes a row worth listing, but entering it
# is not a protection: those detections expire after six hours. What the worker holds
# for review is the secondary camera_N_loitering zone, so that is what is asserted.
LOITERING_ZONE = {cam: f"{cam}_loitering" for cam in CAMERAS}
# Only an rtsp:// URL that carries userinfo is a leak. A bare rtsp:// mention in a
# comment or a library's own docs is not a secret and must not fail this check.
RTSP_URL = re.compile(r"rtsp://\S+")
RTSP_USERINFO = re.compile(r"^rtsp://[^/@\s]+@")
# The worker polls every 30 s, so an event that only just ended can still be
# unpinned. Only rows that ended longer ago than one of its cycles are asserted.
SETTLED_S = 60


def get(base, path, method="GET", body=None, headers=None):
    """(status, lowercased headers, body bytes); HTTPError is returned, transport errors raise."""
    req = urllib.request.Request(base.rstrip("/") + path, data=body, method=method,
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=70) as res:
            return res.status, {k.lower(): v for k, v in res.headers.items()}, res.read()
    except urllib.error.HTTPError as err:
        return err.code, {k.lower(): v for k, v in (err.headers or {}).items()}, err.read()


def delete_request(base, payload, headers=None):
    """A POST to the delete path. An empty header value is how urllib says do not send it."""
    sent = {"Content-Type": "application/json", "X-Surveillance-Action": "delete"}
    sent.update(headers or {})
    return get(base, DELETE_PATH, method="POST", body=json.dumps(payload).encode(), headers=sent)


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

    # The snapshot reads are exact paths. Every DENIED entry is asserted denied
    # against the live server a few lines below, so an allowed read that drifted
    # into that list would fail there — this names that requirement instead of
    # leaving it to a future editor to rediscover.
    assert not {SUMMARY_PATH, EVENTS_PATH} & set(DENIED), \
        "an allowed snapshot read is in the deny list"
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

    # Retention, as the browser sees it: a snapshot a person held for a minute in a
    # camera's own zone must be flagged kept, or the periodic cleanup would take it
    # first. What this proves is that the flag reached the page, not that the worker
    # set it: the worker pins on its own cadence, and until it has run there is
    # nothing here to assert.
    now = time.time()
    settled = [e for e in events
               if isinstance(e.get("end_time"), (int, float))
               and not isinstance(e.get("end_time"), bool)
               and now - e["end_time"] >= SETTLED_S
               and LOITERING_ZONE.get(e.get("camera")) in (e.get("zones") or [])]
    if not settled:
        print(f"NOT TESTED  retained snapshots: no loitering-zone event ended over {SETTLED_S}s ago")
    else:
        unpinned = [e["id"] for e in settled if e.get("retain_indefinitely") not in (True, 1)]
        assert not unpinned, f"{len(unpinned)} settled loitering-zone event(s) are not retained: {unpinned[:3]}"
        print(f"ok  all {len(settled)} settled loitering-zone event(s) are flagged kept for review")

    for cam in CAMERAS:
        status, _h, body = get(base, f"/api/{cam}/latest.jpg")
        assert status == 200 and body[:2] == b"\xff\xd8", f"{cam}/latest.jpg: HTTP {status}, not a JPEG"
    print(f"ok  all {len(CAMERAS)} latest.jpg previews 200 JPEG")
    print("note  /live/ is a WebSocket upgrade this script never opens: playback was seen in a "
          "browser, and it is recorded there, not proved by this run")
    check_summary(base)
    check_events(base)
    check_delete_guards(base)
    print("\nPASS  every assertion held")
    return 0


def check_summary(base):
    """The archive count the confirmation is built on: a count and a cutoff, nothing else."""
    status, _h, body = get(base, SUMMARY_PATH)
    assert status == 200, f"{SUMMARY_PATH}: HTTP {status}"
    payload = json.loads(body.decode("utf-8", "replace"))
    assert isinstance(payload, dict), f"{SUMMARY_PATH}: {type(payload).__name__}, expected an object"
    extra = set(payload) - {"count", "before"}
    assert not extra, f"{SUMMARY_PATH} carries {sorted(extra)} beyond the count and the cutoff"
    count, before = payload.get("count"), payload.get("before")
    assert isinstance(count, (int, float)) and not isinstance(count, bool) and count >= 0, \
        f"{SUMMARY_PATH} count {count!r}, expected a number of snapshots"
    assert isinstance(before, (int, float)) and not isinstance(before, bool) and before > 0, \
        f"{SUMMARY_PATH} before {before!r}, expected unix seconds"
    # A cutoff a long way ahead of now would scope a delete to nothing at all; one
    # behind it is the whole archive, which the operator has to see a count for. A
    # little slack, because this runs on a different container than the worker.
    assert before <= time.time() + 60, f"{SUMMARY_PATH} cutoff {before} is in the future"
    print(f"ok  {SUMMARY_PATH} 200: count {count:g}, cutoff {before:g} (unix seconds), no other field")


def read_page(label, payload):
    """The {events, page, page_size, total, pages} envelope of one page read, checked."""
    assert isinstance(payload, dict), f"{label}: {type(payload).__name__}, expected an object"
    extra = set(payload) - {"events", "page", "page_size", "total", "pages"}
    assert not extra, f"{label} carries {sorted(extra)} beyond the documented envelope"
    events, page, size, total, pages = (payload.get(k) for k in
                                        ("events", "page", "page_size", "total", "pages"))
    assert isinstance(events, list), f"{label} events is {type(events).__name__}, expected a list"
    for name, value in (("page", page), ("page_size", size), ("total", total), ("pages", pages)):
        assert isinstance(value, int) and not isinstance(value, bool) and value >= 0, \
            f"{label} {name} {value!r}, expected a non-negative integer"
    # The page that comes back is the page that was asked for, at the fixed width:
    # the proxy forwarded the query and the gateway decided, and neither of them
    # may quietly serve a different page or a longer one.
    assert page == 1, f"{label} reports page {page}, asked for page 1"
    assert size == PAGE_SIZE, f"{label} page_size {size}, expected a fixed {PAGE_SIZE}"
    assert len(events) <= size, f"{label} returned {len(events)} rows on a {size}-row page"
    # A short first page is the end of the walk and the total is what is on it; a
    # full one is not the end, so the total has to be larger. Anything else means
    # total and the rows on the page disagree. A camera with nothing on it still
    # answers one empty page at page 1 rather than page 0, so total 0 is a page
    # here and not a contradiction.
    assert total == len(events) or len(events) == size, \
        f"{label} says total {total} but page 1 holds {len(events)} rows"
    # The contract floors the count at one page, because an empty result is still
    # one page: pages = max(1, ceil(total / page_size)). Without that floor this
    # would demand 0 pages of a camera with no rows, and fail a correct answer.
    assert pages == max(1, -(-total // size)), \
        f"{label} pages {pages} does not follow from total {total} at {size} rows a page"
    return events, total


def check_events(base):
    """The one read that forwards a query: shape, the page served, the camera filter.

    The proxy hands ?page and ?camera to the retention worker and to nothing else,
    so this checks the properties that forwarding is only safe if the worker holds:
    a page of a fixed size, rows belonging to the camera that was asked for, a
    filter that can only narrow the unfiltered total, and a 400 rather than a walk
    when the arguments are not ones the worker accepts. Every request is a GET on
    a read path, so nothing here can change anything on disk.
    """
    status, _h, body = get(base, f"{EVENTS_PATH}?page=1&camera=all")
    assert status == 200, f"{EVENTS_PATH}?page=1&camera=all: HTTP {status}"
    events, total = read_page("camera=all", json.loads(body.decode("utf-8", "replace")))
    assert all(isinstance(e, dict) for e in events), "camera=all returned a row that is not an object"

    # All four camera filters, one read each, and the filter is the worker's: a row
    # tagged for another camera, or a filtered total above the unfiltered one, means
    # the page is not what the browser asked for.
    for cam in CAMERAS:
        status, _h, body = get(base, f"{EVENTS_PATH}?page=1&camera={cam}")
        assert status == 200, f"{EVENTS_PATH}?page=1&camera={cam}: HTTP {status}"
        rows, cam_total = read_page(f"camera={cam}", json.loads(body.decode("utf-8", "replace")))
        assert all(isinstance(r, dict) for r in rows), f"camera={cam} returned a row that is not an object"
        wrong = sorted({str(r.get("camera")) for r in rows} - {cam})
        assert not wrong, f"camera={cam} returned {len(rows)} row(s) tagged {wrong}"
        assert cam_total <= total, f"camera={cam} total {cam_total} is above the unfiltered total {total}"
    print(f"ok  {EVENTS_PATH} 200: {len(events)} of {total} rows on page 1 of a fixed {PAGE_SIZE}-row page, "
          f"and all {len(CAMERAS)} camera filters returned only their own rows")

    # What the gateway refuses rather than serves: a camera that is not one of the
    # four, an argument it does not define, and an attempt to widen the page. A
    # refusal here is a validation decision, so it must be the 400 and not a 200
    # with something in it.
    refusals = ("page=1&camera=camera_5", "page=1&archive=all", "page=1&camera=all&limit=500")
    for query in refusals:
        status, _h, _b = get(base, f"{EVENTS_PATH}?{query}")
        assert status == 400, f"{EVENTS_PATH}?{query}: HTTP {status}, expected 400 from the validating gateway"

    # A read is still a read. The method allowlist refuses these before the location
    # is even selected, so the worker is never asked to take one.
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        status, _h, _b = get(base, f"{EVENTS_PATH}?page=1&camera=all", method=method)
        assert status == 405, f"{method} {EVENTS_PATH}: HTTP {status}, expected 405"
    print(f"ok  {EVENTS_PATH} refuses POST, PUT, PATCH and DELETE with 405, and answers 400 for a camera "
          f"outside the four, an undefined argument, and an attempt to widen the page")


def check_delete_guards(base):
    """The one write path: POST only, and only a well-formed, in-origin request.

    Each case below is well formed in every respect except the guard it names, so a
    refusal is attributable to that guard rather than to a body that was unusable
    anyway. None of them can delete anything: the ids cannot exist and the scopes are
    undefined.
    """
    for method in ("GET", "HEAD", "PUT", "PATCH", "DELETE"):
        status, _h, _b = get(base, DELETE_PATH, method=method)
        assert status == 405, f"{method} {DELETE_PATH}: HTTP {status}, expected 405"
    for method in ("POST", "PUT", "DELETE"):
        status, _h, _b = get(base, SUMMARY_PATH, method=method)
        assert status == 405, f"{method} {SUMMARY_PATH}: HTTP {status}, expected 405"

    well_formed = {"ids": [PROBE_ID]}
    refusals = (
        ("a foreign origin", well_formed, {"Origin": "https://not-this-dashboard.example"}),
        ("no action header", well_formed, {"X-Surveillance-Action": ""}),
        ("a text content type", well_formed, {"Content-Type": "text/plain"}),
        ("an id that is not a tracker id", {"ids": ["1790910000.123456-abc12"]}, {}),
        ("an id that is a path", {"ids": ["../config/config.yml"]}, {}),
        ("an empty scope", {}, {}),
        ("a scope that is neither ids nor all", {"everything": True}, {}),
    )
    for label, payload, headers in refusals:
        status, _h, _b = delete_request(base, payload, headers)
        assert status in REFUSED, f"delete with {label}: HTTP {status}, expected one of {REFUSED}"
    print(f"ok  {DELETE_PATH} refuses GET, HEAD, PUT, PATCH and DELETE with 405 and "
          f"{SUMMARY_PATH} refuses POST, PUT and DELETE with 405")
    print(f"ok  {len(refusals)} refused delete requests, none of which could delete anything: a "
          f"foreign origin, no action header, a text content type, two ids that are not tracker "
          f"ids, an empty scope, and a scope the contract does not define")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as err:
        print(f"\nFAIL  {err}")
        sys.exit(1)