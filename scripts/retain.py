#!/usr/bin/env python3
"""Pin the person events that entered a camera's own zone, so they survive pruning.

Frigate ages ordinary snapshots out on a timer (snapshots.retain.default), and a
zone somebody walks into every few minutes fills that window faster than it is
read. This worker sets Frigate's own retain_indefinitely on those events instead.
It deletes nothing.

Stdlib only, internal API only, and stateless: Frigate's own flag answers "is
this already pinned", so a restart neither re-pins nor forgets. Log lines carry
ids, camera names, counts and status codes only.

    python3 scripts/retain.py --once
    python3 scripts/retain.py --delete-reviewed 1790910000.123456-abc123
"""

import argparse
import json
import math
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

CAMERAS = ("camera_1", "camera_2", "camera_3", "camera_4")
REQUIRED_ZONE = {camera: f"{camera}_area" for camera in CAMERAS}
TARGET_LABEL = "person"
REASONS = (  # the order a skipped event is counted in, one line per run_pass
    "without a snapshot",
    f"not a {TARGET_LABEL}",
    "outside their own zone",
    "from no configured camera",
)

API_BASE = "http://frigate:5000"  # the published ports stay loopback; this one is internal only
POLL_SECONDS = 30.0
PAGE_SIZE = 100  # rows per page
MAX_PAGES = 50  # one pass refuses to read more than this many pages
MAX_LIMIT = 1000  # ceiling for the page size the tie rule expands to
TIMEOUT = 10.0
# The window one pass reads. Frigate is the thing that keeps snapshots: an
# ordinary one ages out after six hours (snapshots.retain.default: 0.25 days), so
# a pass running every 30s pins a row long before that, and re-reading anything
# older would only cost. The margin is clock skew against Frigate.
LOOKBACK_SECONDS = 6 * 60 * 60
LOOKBACK_MARGIN_SECONDS = 5 * 60

# Frigate's own tracker id is "<frame_time>-<rand_id>", e.g. 1790910000.123456-abc123.
# Anything not exactly this form never reaches a request path. fullmatch, not
# match: a trailing newline must not survive the check.
EVENT_ID = re.compile(r"[0-9]+(?:\.[0-9]+)?-[a-z0-9]{6}")

EXIT_OK = 0
EXIT_NO_PASS = 1  # the pass did not complete; the next cycle tries again
EXIT_BAD_INPUT = 2  # refused before any request was made


class PassFailed(Exception):
    """The pass cannot run, or could not run to the end of the list."""


def log(message):
    """One line to stdout. Nothing reaching here may hold a body, URL or secret."""
    print(f"retain: {message}", flush=True)


def redacted(url):
    """A URL safe to print: any userinfo is dropped rather than logged."""
    parts = urllib.parse.urlsplit(url)
    if parts.hostname is None or (parts.username is None and parts.password is None):
        return url
    netloc = f"REDACTED@{parts.hostname}"
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, parts.query, ""))


class Api:
    """Just enough Frigate HTTP to read events and flip one flag.

    Every call returns (status, body bytes); a transport failure propagates, so
    "Frigate said no" stays distinguishable from "Frigate was not there".
    """

    def __init__(self, base, timeout=TIMEOUT):
        self.base = base.rstrip("/")
        self.timeout = timeout

    def call(self, path, method="GET", params=None, body=None):
        url = self.base + path
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Accept", "application/json")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as err:
            error_body = err.read()
            err.close()
            return err.code, error_body


def native_id(event):
    """(id, reason): the event's id, and only in Frigate's native form."""
    value = event.get("id") if isinstance(event, dict) else None
    if not isinstance(value, str):
        return None, "id missing or not a string"
    if not EVENT_ID.fullmatch(value):
        return None, "id is not <timestamp>-<6 lowercase alphanumerics>"
    return value, None


def start_time(event):
    value = event.get("start_time") if isinstance(event, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def zones(event):
    value = event.get("zones") if isinstance(event, dict) else None
    return value if isinstance(value, list) else []


def after_cutoff():
    """The strict start_time floor for one pass: the lookback, plus skew slack."""
    return time.time() - LOOKBACK_SECONDS - LOOKBACK_MARGIN_SECONDS


def fetch_page(api, after, boundary, limit):
    """One page of candidate events, newest first: start_time < before and > after.

    Frigate 0.18 EventsQueryParams has no offset field, so the walk pages on
    before alone; both bounds are strict.
    """
    params = {
        "has_snapshot": "1",
        "labels": TARGET_LABEL,
        "limit": limit,
        "order": "desc",
        "after": after,
    }
    if boundary is not None:
        # nextafter is the smallest float above the boundary: nothing is
        # representable in between, so every row tied with it comes back.
        params["before"] = math.nextafter(boundary, math.inf)
    status, body = api.call("/api/events", params=params)
    if not 200 <= status < 300:
        raise PassFailed(f"GET /api/events returned HTTP {status}")
    try:
        page = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        raise PassFailed("GET /api/events returned a body that is not JSON")
    if not isinstance(page, list):
        raise PassFailed(f"GET /api/events returned {type(page).__name__}, expected a list")
    return page


def walk_events(api, after, page_size, max_pages):
    """Every candidate event keyed by id, or PassFailed. Never a partial pass as a whole one.

    A full page whose oldest row is not older than the boundary means unseen rows
    are tied with it, so the page widens and the same boundary is re-read instead
    of moved: that re-read is the whole tied group, and moving on would skip the
    rest of it forever.
    """
    events = {}
    malformed = 0
    boundary = None
    limit = page_size
    pages = 0
    while True:
        if pages >= max_pages:
            raise PassFailed(f"the {max_pages} page cap stopped the walk short of the end of the list")
        page = fetch_page(api, after, boundary, limit)
        pages += 1
        if not page:
            break
        oldest = None
        for event in page:
            event_id, _reason = native_id(event)
            stamp = start_time(event)
            if event_id is None or stamp is None:
                # Counted, never echoed: the value came from the upstream, and
                # without a start_time the walk cannot place the row on its cursor.
                malformed += 1
                continue
            events.setdefault(event_id, event)
            oldest = stamp if oldest is None else min(oldest, stamp)
        if len(page) < limit:
            break  # short page: the upstream had nothing more inside the window
        if oldest is None:
            raise PassFailed("GET /api/events returned a full page with no usable id and start_time")
        if boundary is not None and oldest >= boundary:
            if limit >= MAX_LIMIT:
                raise PassFailed(f"more than {MAX_LIMIT} events share the start_time {boundary}")
            limit = min(limit * 4, MAX_LIMIT)
            continue
        boundary = oldest
        limit = page_size
    return events, {"pages": pages, "malformed": malformed}


def succeeded(body):
    """Frigate answers 2xx with {"success": true}. The body is the verdict."""
    try:
        reply = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return False
    return isinstance(reply, dict) and reply.get("success") is True


def pin(api, event_id):
    """(ok, detail): ask Frigate to keep this event forever. 2xx alone is not success."""
    path = "/api/events/" + urllib.parse.quote(event_id, safe="") + "/retain"
    status, body = api.call(path, method="POST", body={"retain_indefinitely": True})
    if not 200 <= status < 300:
        return False, f"HTTP {status}"
    if not succeeded(body):
        return False, "HTTP 2xx with no success:true in the reply"
    return True, f"HTTP {status}"


def disqualification(event):
    """Why this event must not be pinned, or None if it qualifies."""
    camera = event.get("camera")
    if not isinstance(camera, str) or camera not in REQUIRED_ZONE:
        return "from no configured camera"
    # The upstream already filters on label and snapshot; re-checking is what stops
    # a change in its filter semantics from silently widening what this retains.
    if event.get("has_snapshot") is not True:
        return "without a snapshot"
    # The query filter is plural (labels=) but one event carries one label.
    if event.get("label") != TARGET_LABEL:
        return f"not a {TARGET_LABEL}"
    # A zone polygon is per camera, so camera_2 naming camera_1_area is not
    # somebody entering camera_2.
    if REQUIRED_ZONE[camera] not in zones(event):
        return "outside their own zone"
    return None


def run_pass(api, page_size, max_pages):
    """One poll -> read -> pin pass. 0 only if the pass ran to the end and every pin stuck."""
    pinned = []
    failed = []
    already = 0
    skipped = {}
    # Captured once, so every page of one pass reads the same window and a pass
    # running for minutes cannot fold its later pages past rows its first one read.
    after = after_cutoff()
    try:
        events, walk = walk_events(api, after, page_size, max_pages)
        # Sorted by id, and the id embeds its start_time, so the same upstream
        # produces the same pass in the same order every cycle.
        for event in sorted(events.values(), key=lambda e: e["id"]):
            reason = disqualification(event)
            if reason is not None:
                skipped[reason] = skipped.get(reason, 0) + 1
                continue
            event_id, _reason = native_id(event)
            if event_id is None:
                continue  # counted by the walk
            if event.get("retain_indefinitely") is True:
                already += 1
                continue
            ok, detail = pin(api, event_id)
            (pinned if ok else failed).append((event_id, detail))
    except PassFailed as err:
        log(f"pass skipped: {err}; retrying on the next cycle")
        return EXIT_NO_PASS
    except (urllib.error.URLError, OSError) as err:
        # Type only. A URLError string carries the URL it was raised for.
        log(f"pass skipped: {type(err).__name__} reaching Frigate; retrying on the next cycle")
        return EXIT_NO_PASS
    counts = ", ".join(f"{skipped.get(reason, 0)} {reason}" for reason in REASONS)
    log(f"scan: {walk['pages']} page(s) of at most {page_size} rows, {len(events)} unique event(s), "
        f"{walk['malformed']} not addressable by a native id")
    log(f"pin: {len(pinned)} pinned, {already} already retained, {counts}")
    if failed:
        log(
            f"{len(failed)} pin(s) refused by Frigate, left unpinned and retried next cycle: "
            + ", ".join(f"{event_id} ({detail})" for event_id, detail in failed)
        )
        return EXIT_NO_PASS
    return EXIT_OK


def poll_forever(api, args):
    log(f"polling {redacted(api.base)} every {args.interval:g}s for person events with snapshots")
    try:
        while True:
            started = time.monotonic()
            run_pass(api, args.page_size, args.max_pages)
            # Measured from the start of the pass, so a slow pass does not push the
            # cadence out and bursts cannot stack up back-to-back passes.
            time.sleep(max(0.0, args.interval - (time.monotonic() - started)))
    except KeyboardInterrupt:
        log("stopped")
        return EXIT_OK


def delete_reviewed(api, event_id):
    """Delete one event: the only destructive call here, reachable only by naming one
    native id by hand. The id is matched before anything is sent, so a typo is
    refused rather than guessed.
    """
    if not EVENT_ID.fullmatch(event_id):
        print(
            f"refused: {event_id!r} is not a Frigate event id "
            "(<frame_time>-<6 lowercase alphanumerics>). Nothing was requested.",
            file=sys.stderr,
        )
        return EXIT_BAD_INPUT
    path = "/api/events/" + urllib.parse.quote(event_id, safe="")
    try:
        status, body = api.call(path, method="DELETE")
    except (urllib.error.URLError, OSError) as err:
        print(f"delete failed: {type(err).__name__} reaching Frigate", file=sys.stderr)
        return EXIT_NO_PASS
    if 200 <= status < 300 and succeeded(body):
        log(f"deleted {event_id} on operator request (HTTP {status})")
        return EXIT_OK
    print(f"delete failed: {event_id} (HTTP {status})", file=sys.stderr)
    return EXIT_NO_PASS


def positive_int(value):
    """argparse type for a bound that must be at least 1. A zero page size or cap would
    stall the walk before it read anything, so it is refused here, not discovered by
    a pass."""
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError(f"{value!r} is not at least 1")
    return number


def positive_float(value):
    """argparse type for a duration that must be above 0, for the same reason."""
    number = float(value)
    if not number > 0:
        raise argparse.ArgumentTypeError(f"{value!r} is not above 0")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Pin person events that entered a camera's own zone in Frigate"
    )
    parser.add_argument("--api-base", default=API_BASE, help="internal Frigate API base URL")
    parser.add_argument("--interval", type=positive_float, default=POLL_SECONDS,
                        help="seconds between passes")
    parser.add_argument("--page-size", type=positive_int, default=PAGE_SIZE,
                        help="rows per events page")
    parser.add_argument("--max-pages", type=positive_int, default=MAX_PAGES,
                        help="page cap for one pass")
    parser.add_argument("--timeout", type=positive_float, default=TIMEOUT,
                        help="per-request timeout, seconds")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="run one pass and exit")
    mode.add_argument(
        "--delete-reviewed", metavar="ID", help="delete one event by native id, then exit"
    )
    args = parser.parse_args(argv)
    api = Api(args.api_base, args.timeout)
    if args.delete_reviewed is not None:
        return delete_reviewed(api, args.delete_reviewed)
    if args.once:
        return run_pass(api, args.page_size, args.max_pages)
    return poll_forever(api, args)


if __name__ == "__main__":
    sys.exit(main())