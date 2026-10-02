#!/usr/bin/env python3
"""Keep entry snapshots for review, and the animals seen on cameras 1-3.

Three rules, and what each one is for.

A person is retained only while a person has switched entry review on for that
camera by hand, and only for an event that carries that camera's own restricted
zone and started at or after the moment it was armed. Ordinary family movement
through the house is not evidence of anything, so nothing is retained by
default: arming is a person's decision, it is per camera, and it lives in this
process's memory only, so every restart comes back disarmed and nothing stays
armed by accident. Disarming stops new pins and never unpins what is already
kept.

The 60-second full-frame person rule that preceded this is gone, on every
camera. Events retained under it stay exactly as they are: this worker only ever
sets retain_indefinitely, it has no un-pin path and no sweep, and there is no
migration.

An animal on camera_1, camera_2 or camera_3 is pinned on sight, because presence
is the whole of the evidence, and that rule is unchanged. camera_4 tracks person
only and is held to that here regardless of what Frigate is configured to track.

With --serve the same process also answers the dashboard's endpoints: the read-only
count and the paginated archive behind the browser, the entry arming status and
its one write, and the one controlled manual deletion. Nothing there deletes
unless a request arrives on an exact path with the dashboard's own Host, an
Origin equal to it, a JSON content type and the matching action header, and says
what it means to delete. No path or query of the request is ever forwarded
upstream: each endpoint builds its own upstream call from literals, validated ids
and validated filters.

A camera is ready to arm only when Frigate's own /api/config reports that camera's
expected restricted zone as person-only at loitering_time 0. That read is
refreshed on a timer and after any change, is parsed only into booleans, and is
never forwarded to the browser: the config carries RTSP URLs with credentials,
so nothing from it but two true-or-false answers is ever returned, and a failed
read leaves every camera not ready.

Stdlib only, internal API only. Log lines carry ids, camera names, booleans,
counts and status codes only.

    python3 scripts/retain.py --once
    python3 scripts/retain.py --delete-reviewed 1790910000.123456-abc123
    python3 scripts/retain.py --serve
"""

import argparse
import contextlib
import http.server
import json
import math
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

CAMERAS = ("camera_1", "camera_2", "camera_3", "camera_4")
TARGET_LABEL = "person"
# The two cameras a person may arm entry review on, and the only two that have a
# restricted zone at all. Camera_1 and camera_4 watch the area_4 approach;
# camera_2 and camera_3 are internal views, where ordinary family movement
# must never accumulate into a kept record, so a person event on either is never
# pinned here however many zones it carries. Not a judgement about either room.
ENTRY_CAMERAS = ("camera_1", "camera_4")
# One name per entry camera, and deliberately distinct from camera_N_area and from
# the removed camera_N_loitering: an event carries this zone only once inertia is
# met at loitering_time 0, which is what makes crossing into the area the whole
# signal rather than a dwell. Whether the polygon is drawn at all is the owner's
# open decision, and until it is, no event carries the name and nothing pins.
RESTRICTED_ZONE = {camera: f"{camera}_restricted" for camera in ENTRY_CAMERAS}
# The ten COCO 91 animal classes, in the labelmap's own order. The model reports a
# species and nothing about harm, so this list is "an animal this suite watches",
# never "a dangerous animal". There is no rat, mouse, rodent, snake or scorpion
# class in COCO 91, so none of those can appear here at all.
ANIMAL_LABELS = (
    "bird", "cat", "dog", "horse", "sheep", "cow",
    "elephant", "bear", "zebra", "giraffe",
)
# Cameras 1-3 track the animals; camera_4 is person-only in the Frigate config and
# is kept that way here as a second, independent gate. Frigate's track list is the
# first gate, this is the one that does not depend on Frigate having been
# reconfigured: an animal event arriving on camera_4 is refused rather than pinned.
ANIMAL_CAMERAS = ("camera_1", "camera_2", "camera_3")
# Everything this suite keeps, listed and deleted alike. This is one set on
# purpose: an animal snapshot the worker pins is an animal snapshot the operator
# must be able to find and delete, and a row the worker cannot see would be a row
# nobody can ever remove.
PINNED_LABELS = (TARGET_LABEL,) + ANIMAL_LABELS
REASONS = (  # the order a skipped event is counted in, one line per run_pass
    "without a snapshot",
    "not a person or a tracked animal",
    "an animal on a camera that does not watch for them",
    "while entry review was disarmed",
    "before entry review was armed",
    "outside its own restricted zone",
    "on a camera with no restricted zone",
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
# Frigate's config read is cached and revalidated at most this often. The poll
# asks every 30 s and the status read can come every few seconds, so without a
# floor each one would hit /api/config, which is the heaviest route in the stack.
# Arming forces a fresh read instead of waiting out the window.
CONFIG_REVALIDATE_SECONDS = 60.0

# Frigate's own tracker id is "<frame_time>-<rand_id>", e.g. 1790910000.123456-abc123.
# Anything not exactly this form never reaches a request path. fullmatch, not
# match: a trailing newline must not survive the check.
EVENT_ID = re.compile(r"[0-9]+(?:\.[0-9]+)?-[a-z0-9]{6}")

EXIT_OK = 0
EXIT_NO_PASS = 1  # the pass did not complete; the next cycle tries again
EXIT_BAD_INPUT = 2  # refused before any request was made


class EntryState:
    """Arming for the two entry cameras, in this process's memory and nowhere else.

    No file, no database and no upstream write: a restart therefore forgets
    everything and comes back disarmed, which is the safe direction to forget in.
    The config read behind `ready` is cached, so a status read never turns into
    one request per row, and it is forced after any change so a ready verdict
    can never lag the geometry it was read from.
    """

    def __init__(self):
        self._cameras = {camera: {"armed": False, "armed_since": None} for camera in ENTRY_CAMERAS}
        self._ready = {camera: False for camera in ENTRY_CAMERAS}
        self._checked_at = None

    def snapshot(self):
        """The whole state as plain booleans and one number, safe to hand out."""
        return {camera: dict(self._cameras[camera], ready=self._ready[camera]) for camera in ENTRY_CAMERAS}

    def _cache_fresh(self, now):
        return self._checked_at is not None and now - self._checked_at < CONFIG_REVALIDATE_SECONDS

    def ready(self, api, now, force=False):
        """Whether each entry camera may be armed, from Frigate's own config.

        Fail closed. A transport error, a non-2xx, a body that is not JSON or a
        shape this file cannot read is not a config with no zones: it leaves every
        camera not ready and every armed flag cleared, so the next POST refuses
        rather than trusting the last good answer. Nothing from the config but
        these booleans is kept, and the body itself is never stored or returned.
        """
        if not force and self._cache_fresh(now):
            return self._ready
        try:
            ready = restricted_zones_ready(self._read_config(api))
        except (urllib.error.URLError, OSError, ValueError):
            ready = {camera: False for camera in ENTRY_CAMERAS}
            log("entry: Frigate's config could not be read, so every entry camera is "
                "not ready and disarmed")
        if ready != self._ready:
            log("entry: ready " + ", ".join(f"{camera}={str(ready[camera]).lower()}"
                                            for camera in ENTRY_CAMERAS))
        self._ready = ready
        self._checked_at = now
        # After every result, not only a fresh one: a camera that has just stopped
        # being ready must stop pinning at once. This is not an un-pin — an event
        # already pinned keeps its flag, because this worker has no path that
        # clears one. It only stops further pins.
        for camera in ENTRY_CAMERAS:
            if not ready[camera]:
                self._cameras[camera] = {"armed": False, "armed_since": None}
        return ready

    @staticmethod
    def _read_config(api):
        status, body = api.call("/api/config")
        if not 200 <= status < 300:
            raise ValueError("config was not 2xx")
        return json.loads(body.decode("utf-8", "replace"))

    def guard(self, camera):
        """The armed_since a candidate event has to clear, or None.

        None means "nothing is being kept for people on this camera": a camera
        with no restricted zone, and one that is disarmed or not ready. All three
        land here, in one place, so no caller reaches a pin without passing this.
        """
        if camera not in self._cameras or not self._ready.get(camera):
            return None
        state = self._cameras[camera]
        if not state["armed"]:
            return None
        return state["armed_since"]

    def arm(self, camera, now):
        """Turn one camera on. Readiness is checked by the caller, so this only
        records the moment; there is no path here that writes upstream.

        Arming a camera that is already armed keeps the moment the first arming
        recorded rather than moving it. A repeated click, or a page that re-sends
        the same request, must not slide the boundary forwards and let a pass reach
        further back into history than the person who armed it intended.
        """
        if self._cameras[camera]["armed"]:
            return
        self._cameras[camera] = {"armed": True, "armed_since": now}

    def disarm(self, camera, now):
        """Stop new pins. Never unpins, never clears a kept record, and works on a
        camera that has stopped being ready: turning it off is always allowed."""
        self._cameras[camera] = {"armed": False, "armed_since": None}


def restricted_zones_ready(config):
    """{camera: bool}: does Frigate's own config carry this camera's expected
    restricted zone, person-only at loitering_time 0?

    Read only far enough to answer that, and answer nothing else. The config
    carries RTSP URLs with credentials, so it is parsed here and never stored,
    never logged and never returned: what leaves this function is two booleans.
    A shape this does not recognise is False, never a guess.
    """
    cameras = config.get("cameras") if isinstance(config, dict) else None
    if not isinstance(cameras, dict):
        return {camera: False for camera in ENTRY_CAMERAS}
    ready = {}
    for camera in ENTRY_CAMERAS:
        block = cameras.get(camera)
        zones = block.get("zones") if isinstance(block, dict) else None
        zone = zones.get(RESTRICTED_ZONE[camera]) if isinstance(zones, dict) else None
        if not isinstance(zone, dict):
            ready[camera] = False
            continue
        labels = zone.get("objects")
        # Exactly person: an animal in this zone would be indistinguishable from a
        # person crossing the line, which is the one thing this zone cannot be.
        ready[camera] = zone.get("loitering_time") == 0 and labels == [TARGET_LABEL]
    return ready


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
        "labels": ",".join(PINNED_LABELS),
        "limit": limit,
        # Frigate's events endpoint sorts with its own sort= parameter rather than a
        # bare direction, and date_desc is newest first.
        "sort": "date_desc",
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
    rest of it forever. A boundary of None reads the newest rows and walks down.
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
    """Why this event is not one of this suite's snapshot-bearing person-or-animal
    events, or None if it is one. Camera, snapshot and label only.

    This is the check that gates listing and deletion, and deliberately no zone,
    no arming state and no time: it answers "may an operator reach this row",
    which must not change when the retention policy does. A person on any of the
    four cameras stays listable and deletable whether or not anybody armed
    anything, so an old kept record can still be removed.
    """
    camera = event.get("camera")
    if not isinstance(camera, str) or camera not in CAMERAS:
        return "from no configured camera"
    # The upstream already filters on label and snapshot; re-checking is what stops
    # a change in its filter semantics from silently widening what this retains.
    if event.get("has_snapshot") is not True:
        return "without a snapshot"
    # The query filter is plural (labels=) but one event carries one label.
    label = event.get("label")
    if label not in PINNED_LABELS:
        return "not a person or a tracked animal"
    if label in ANIMAL_LABELS and camera not in ANIMAL_CAMERAS:
        return "an animal on a camera that does not watch for them"
    return None


def pin_disqualification(event, entry, now):
    """Why this event must not be pinned, or None if it qualifies.

    A person is pinned only when all four of these hold: that camera's entry
    review is armed right now, the event carries that camera's own restricted
    zone, the event started at or after the armed_since of the arming that let it
    through, and the event is inside the pass's lookback. A 60-second full-frame
    presence pins nothing any more, on any camera.

    The armed_since comparison is exact on purpose. An event that began before
    somebody switched a camera on is not one they switched on to keep, so arming
    can never reach back into the six hours of archive the pass reads. That
    leaves one known gap, stated rather than hidden: a track that entered the
    restricted area before the arm and is still running will not be pinned at all,
    because Frigate reports one start_time for the whole track. Catching those
    needs the current-zone-entry signal on MQTT, which is not built here.

    An animal is pinned the moment it is seen, whichever zones it carries.
    Presence is the whole of the evidence: an animal in the area zone has already
    been snapshotted with loitering_time 0, and making it then serve 60 more
    seconds in a zone it is never in would be 60 seconds it was loose and
    unrecorded. An animal is never an entry and never needs arming.
    """
    reason = disqualification(event)
    if reason is not None:
        return reason
    if event.get("label") in ANIMAL_LABELS:
        return None
    camera = event["camera"]
    armed_since = entry.guard(camera)
    if armed_since is None:
        return ("on a camera with no restricted zone" if camera not in RESTRICTED_ZONE
                else "while entry review was disarmed")
    if RESTRICTED_ZONE[camera] not in zones(event):
        return "outside its own restricted zone"
    stamp = start_time(event)
    if stamp is None or stamp < armed_since:
        return "before entry review was armed"
    return None


def run_pass(api, page_size, max_pages, entry, now=None):
    """One poll -> read -> pin pass. 0 only if the pass ran to the end and every pin stuck."""
    if now is None:
        now = time.time()
    # Readiness comes from the cached config read, so a 30 s pass does not turn
    # into a /api/config request. A forced check would be a stall on every pass,
    # which is not what revalidating on a timer is for.
    entry.ready(api, now)
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
            reason = pin_disqualification(event, entry, now)
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
    armed = ", ".join(f"{camera} armed" for camera in ENTRY_CAMERAS
                      if entry.snapshot()[camera]["armed"]) or "no entry camera armed"
    log(f"scan: {walk['pages']} page(s) of at most {page_size} rows, {len(events)} unique event(s), "
        f"{walk['malformed']} not addressable by a native id")
    log(f"pin: {len(pinned)} pinned, {already} already retained, {armed}; {counts}")
    if failed:
        log(
            f"{len(failed)} pin(s) refused by Frigate, left unpinned and retried next cycle: "
            + ", ".join(f"{event_id} ({detail})" for event_id, detail in failed)
        )
        return EXIT_NO_PASS
    return EXIT_OK


def poll_forever(api, args, entry, lock=None):
    log(f"polling {redacted(api.base)} every {args.interval:g}s for {len(ANIMAL_LABELS)} animal "
        f"labels on {', '.join(ANIMAL_CAMERAS)}, and for people only on the entry cameras "
        f"{', '.join(ENTRY_CAMERAS)} while they are armed by hand")
    try:
        while True:
            started = time.monotonic()
            # The same lock the gateway takes for an arm, a disarm or a deletion, so
            # a pass and any of those writes never interleave: one walk, then one
            # write, in turn. A disarm therefore waits for the pass in flight.
            with (lock if lock is not None else contextlib.nullcontext()):
                run_pass(api, args.page_size, args.max_pages, entry)
            # Measured from the start of the pass, so a slow pass does not push the
            # cadence out and bursts cannot stack up back-to-back passes.
            time.sleep(max(0.0, args.interval - (time.monotonic() - started)))
    except KeyboardInterrupt:
        log("stopped")
        return EXIT_OK


def delete_reviewed(api, event_id):
    """Delete one event by name, for an operator at a terminal: the call that works
    with the gateway stopped. The id is matched before anything is sent, so a typo
    is refused rather than guessed, and nothing else in this file reaches this path.
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


# ---------------------------------------------------------------------------
# The gateway
# ---------------------------------------------------------------------------
#
# Two write paths in the project, and every rule is a refusal by default: exact
# paths, one Host the dashboard is published on, an Origin equal to that Host on
# each write, a JSON content type, the action header that write requires, and a
# body that has already been fully validated before a single upstream request is
# made. Nothing about the request line reaches Frigate: each upstream call below
# is built from literals, ids that passed the native id pattern, and query
# parameters that passed their own filter.
#
# One of the two writes deletes an event, irreversibly. The other flips an
# in-memory arming flag for one of two cameras, writes nothing upstream and
# touches no configuration: Frigate's /api/config is read here only to answer
# whether the expected restricted zone exists, and only booleans leave it.

GATEWAY_LISTEN = "0.0.0.0:8090"  # the internal network only; compose publishes no port for it
ALLOWED_HOSTS = ("localhost:8088", "127.0.0.1:8088")  # where the dashboard is published
SUMMARY_PATH = "/api/snapshots/summary"
EVENTS_PATH = "/api/snapshots/events"
DELETE_PATH = "/api/snapshots/delete"
ENTRY_PATH = "/api/security/entry"
ACTION_HEADER = "X-Surveillance-Action"
ACTION_VALUE = "delete"
ENTRY_ACTION_VALUE = "arm-entry"
# Shown in every status answer, so "armed" is never read as a permanent state: it
# is held in this process's memory and nothing survives a restart.
ENTRY_NOTICE = ("Arming is held in this process's memory only. Every restart comes back "
                "disarmed, and disarming never releases a snapshot already kept. An entry "
                "already under way when you arm can be missed.")
MAX_DELETE_IDS = 1000
MAX_BODY_BYTES = 64 * 1024
EVENTS_PAGE_SIZE = 25  # rows per archive page, fixed so a page number always means this count
CAMERA_FILTERS = ("all",) + CAMERAS  # "all" is the default; the rest are the configured cameras
MAX_PAGE_DIGITS = 9  # past any archive, so a longer run of digits is a page past the last


class Refused(Exception):
    """A request refused before anything was sent upstream. status is a literal here,
    never anything a client sent."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def entry_status(entry, api, now, force=False):
    """The whole entry answer: the cameras, two booleans and one number each, and
    the restart notice. Nothing from Frigate's config is in here.

    force re-reads the config before answering, which the POST needs and the GET
    does not: a request that has just armed a camera must not be refused by a
    cached verdict taken before the operator drew the zone.
    """
    entry.ready(api, now, force)
    return {
        "cameras": list(ENTRY_CAMERAS),
        "notice": ENTRY_NOTICE,
        "state": entry.snapshot(),
    }


def entry_plan(body):
    """(camera, armed) or Refused, before any lock is taken or config is read.

    Exactly two keys, exactly the two entry cameras, and a real boolean: a string
    "true", a 1, a missing field or a third key is refused rather than guessed
    at, because this is the one body that turns retention on.
    """
    if not isinstance(body, dict):
        raise Refused(400, 'send {"camera":"camera_1"|"camera_4","armed":true|false}')
    if set(body) != {"camera", "armed"}:
        raise Refused(400, "send camera and armed, and nothing else")
    camera = body["camera"]
    armed = body["armed"]
    if not isinstance(camera, str) or camera not in ENTRY_CAMERAS:
        raise Refused(400, f"camera must be {ENTRY_CAMERAS[0]} or {ENTRY_CAMERAS[1]}; "
                           "camera_2 and camera_3 have no restricted area")
    if armed is not True and armed is not False:
        raise Refused(400, "armed must be the boolean true or false")
    return camera, armed


def select_archive(api, cutoff, page_size, max_pages):
    """(ids, walk): every eligible event strictly older than cutoff, or PassFailed.

    after=0 and no upper bound, because the count an operator is shown has to be
    the number a deletion would actually reach. The cutoff is applied here rather
    than in the query, so the newest events are excluded by this code and not by
    an upstream bound that could drift between the count and the deletion.
    PassFailed means the walk never reached the end of the list, and the caller
    must then delete nothing at all.
    """
    events, walk = walk_events(api, 0.0, page_size, max_pages)
    chosen = []
    for event_id, event in events.items():
        if disqualification(event) is not None:
            continue
        stamp = start_time(event)
        if stamp is not None and stamp < cutoff:
            chosen.append(event_id)
    return sorted(chosen), walk


def page_number(value):
    """The page asked for, or Refused.

    ASCII digits only, at least one of them, and at least 1: fullmatch rather than
    str.isdigit, because isdigit accepts other scripts' digits and int() then reads
    what looks like ten as two.

    Shape first, length second, and the order is the point: a long run of anything
    that is not a positive whole number is a malformed query, so it is refused
    whatever its length, rather than answered as a page past the end. Only a value
    already known to be a positive whole number can be too long, and then it is a
    page past the last, which the caller clamps, so it is never converted.
    """
    if not re.fullmatch(r"[0-9]+", value) or not value.strip("0"):
        raise Refused(400, "page must be a whole number of at least 1")
    if len(value) > MAX_PAGE_DIGITS:
        return 10 ** MAX_PAGE_DIGITS
    return int(value)


def events_filters(query):
    """(page, camera) from the request's own query, or Refused.

    Exactly two optional parameters, each at most once, and nothing else. A repeat, an
    unknown name, a blank, a page that is not a positive whole number and a camera that
    is not "all" or one of the four configured cameras are all refused here, before a
    single upstream request is made, and none of the client's own text is echoed back.
    """
    for key, values in query.items():
        if key not in ("page", "camera"):
            raise Refused(400, "send only page and camera, each at most once")
        if len(values) != 1:
            raise Refused(400, f"{key} was sent more than once")
    page = page_number(query.get("page", ["1"])[0])
    camera = query.get("camera", ["all"])[0]
    if camera not in CAMERA_FILTERS:
        raise Refused(400, f"camera must be all or one of {', '.join(CAMERAS)}")
    return page, camera


def select_events(api, page, camera, page_size, max_pages):
    """{events, page, page_size, total, pages}: one page of the archive, or PassFailed.

    The rows are this suite's own saved person snapshots on a configured camera, which is
    the same set select_archive counts, so this can never offer an id the summary would
    not or hide one it would. Newest first, and start_time DESC then id DESC, so two rows
    sharing a start_time still have one order. The camera filter is applied here, before
    the page is cut, so total and pages always describe the filtered set.

    A page past the last is clamped to the last rather than refused, because a dashboard
    that was one page behind after rows were deleted asks for a page that is gone and
    still needs an answer. An empty archive is one empty page.
    """
    # ponytail: O(n) archive scan bounded by MAX_PAGES; use an indexed query or cursor if it outgrows the cap.
    events, _walk = walk_events(api, 0.0, page_size, max_pages)
    rows = [event for event in events.values() if disqualification(event) is None]
    if camera != "all":
        rows = [event for event in rows if event.get("camera") == camera]
    # Every row here arrived through native_id and start_time, so both keys are present.
    rows.sort(key=lambda event: (start_time(event), event["id"]), reverse=True)
    total = len(rows)
    pages = max(1, -(-total // EVENTS_PAGE_SIZE))
    current = min(page, pages)
    offset = (current - 1) * EVENTS_PAGE_SIZE
    return {
        "events": rows[offset:offset + EVENTS_PAGE_SIZE],
        "page": current,
        "page_size": EVENTS_PAGE_SIZE,
        "total": total,
        "pages": pages,
    }


def open_event(event, now):
    """True while Frigate has not ended this event: no end_time yet, or one in the
    future. A still-open detection can write a new snapshot afterwards, which is
    reported rather than prevented."""
    value = event.get("end_time") if isinstance(event, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return True
    return not math.isfinite(value) or value > now


def delete_one(api, event_id, now):
    """(outcome, detail, is_open) for one selected id: deleted, missing or failed.

    The lookup is the whole point: camera, snapshot and label are checked on
    Frigate's own event before any DELETE is sent, so an id that belongs to
    another camera, or to something that is not a saved person snapshot, is never
    destroyed on the strength of its name. 404 is counted, not failed, so a repeat
    of a deletion that already happened is idempotent.
    """
    path = "/api/events/" + urllib.parse.quote(event_id, safe="")
    try:
        status, body = api.call(path)
        if status == 404:
            return "missing", "no such event upstream", False
        if not 200 <= status < 300:
            return "failed", f"lookup HTTP {status}", False
        try:
            event = json.loads(body.decode("utf-8", "replace"))
        except ValueError:
            return "failed", "lookup body is not JSON", False
        reason = disqualification(event) if isinstance(event, dict) else "lookup body is not an event"
        if reason is not None:
            return "failed", reason, False
        is_open = open_event(event, now)
        status, body = api.call(path, method="DELETE")
    except (urllib.error.URLError, OSError) as err:
        # Type only. A URLError string carries the URL it was raised for.
        return "failed", f"{type(err).__name__} reaching Frigate", False
    if 200 <= status < 300 and succeeded(body):
        return "deleted", f"HTTP {status}", is_open
    # 2xx with success:false is not a deletion, and is reported as one.
    detail = "HTTP 2xx with no success:true in the reply" if 200 <= status < 300 else f"HTTP {status}"
    return "failed", detail, is_open


def delete_selected(api, event_ids, now):
    """The reply body: what was deleted, what was already gone, what was not, and
    how many of the selected events were still open. Every selected id lands in
    exactly one of deleted, missing and failed, so the three add up to what was
    asked for."""
    deleted = missing = active = 0
    failed = []
    for event_id in event_ids:
        outcome, detail, is_open = delete_one(api, event_id, now)
        active += 1 if is_open else 0
        if outcome == "deleted":
            deleted += 1
        elif outcome == "missing":
            missing += 1
        else:
            failed.append(event_id)
            log(f"delete refused: {event_id} ({detail})")
    return {"deleted": deleted, "missing": missing, "failed": failed, "active": active}


def delete_plan(body, now):
    """("ids", sorted unique ids) or ("all", cutoff), or Refused.

    Every check here runs before one byte is sent upstream, so a malformed or
    ambiguous body cannot delete anything however plausible it looks. Exactly one
    mode per request, ids checked against the native pattern, and a cutoff that is
    finite, not negative and not in the future.
    """
    if not isinstance(body, dict):
        raise Refused(400, "the request body must be a JSON object")
    if "ids" in body:
        if set(body) != {"ids"}:
            raise Refused(
                400,
                f'send either {{"ids": [...]}} or {{"all": true, "before": <unix seconds>}}, '
                "and nothing else",
            )
        requested = body["ids"]
        if not isinstance(requested, list):
            raise Refused(400, "ids must be a list of event ids")
        if not requested:
            raise Refused(400, "ids was empty, so there is nothing to delete")
        if len(requested) > MAX_DELETE_IDS:
            raise Refused(400, f"ids held more than {MAX_DELETE_IDS} values")
        for value in requested:
            # fullmatch, not match: a trailing newline or a path segment must not
            # survive into a request line.
            if not isinstance(value, str) or not EVENT_ID.fullmatch(value):
                raise Refused(
                    400,
                    "every id must be a Frigate event id, <frame_time>-<6 lowercase alphanumerics>",
                )
        return "ids", sorted(set(requested))  # a duplicate is one event, deleted once
    if set(body) != {"all", "before"}:
        raise Refused(
            400,
            f'send either {{"ids": [...]}} or {{"all": true, "before": <unix seconds>}}, '
            "and nothing else",
        )
    if body["all"] is not True:
        raise Refused(400, "all must be true")
    cutoff = body["before"]
    if isinstance(cutoff, bool) or not isinstance(cutoff, (int, float)):
        raise Refused(400, "before must be a unix timestamp in seconds")
    if not math.isfinite(cutoff):
        raise Refused(400, "before must be a finite timestamp")
    if cutoff > now:
        raise Refused(400, "before is in the future, so no event is older than it")
    if cutoff < 0:
        raise Refused(400, "before is negative")
    return "all", float(cutoff)


class Gateway(http.server.BaseHTTPRequestHandler):
    """Four endpoints — summary, events, delete and entry — and a refusal for
    everything else.

    The Host is checked on each, because the browser only ever reaches this
    through the dashboard, and a request naming any other origin is not the
    dashboard's. No CORS header is ever sent, so no other page can read a reply
    even if it manages to send one.
    """

    protocol_version = "HTTP/1.0"
    server_version = "surveillance-retention"
    sys_version = ""  # the base class would otherwise send the interpreter version
    status = 0

    def do_GET(self):
        self._route()

    do_POST = do_HEAD = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_TRACE = do_GET

    def log_message(self, *args):
        pass  # one summary line per request is written by _route, not by the base class

    def send_error(self, code, message=None, explain=None):
        """Every refusal is JSON, including the ones the base class raises for a bad
        request line, an oversized header or a method it does not implement."""
        self.close_connection = True
        self._json(code, {"error": "refused"})

    def _json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.status = status
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        # No Access-Control-Allow-Origin, ever: this replies to the dashboard, and
        # to nothing else.
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _route(self):
        method = self.command
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        try:
            if (self.headers.get("Host") or "").strip() not in ALLOWED_HOSTS:
                raise Refused(403, "this answers the dashboard's own origin only")
            if path == SUMMARY_PATH:
                if method not in ("GET", "HEAD"):
                    raise Refused(405, "this endpoint answers GET only")
                return self._json(200, self._summary())
            if path == EVENTS_PATH:
                if method not in ("GET", "HEAD"):
                    raise Refused(405, "this endpoint answers GET only")
                return self._json(200, self._events(parsed.query))
            if path == DELETE_PATH:
                if method != "POST":
                    raise Refused(405, "this endpoint answers POST only")
                return self._json(200, self._delete())
            if path == ENTRY_PATH:
                # One path, two verbs: the status is a read and the arming is the
                # only write on it. Neither accepts a query string, so nothing a
                # client appends can select a camera or widen the body.
                if parsed.query:
                    raise Refused(400, "this endpoint takes no query string")
                if method in ("GET", "HEAD"):
                    return self._json(200, self._entry())
                if method == "POST":
                    return self._json(200, self._entry_write())
                raise Refused(405, "this endpoint answers GET and POST only")
            raise Refused(404, "no such endpoint")
        except Refused as err:
            self._json(err.status, {"error": str(err)})
        except PassFailed as err:
            # The walk did not reach the end of the list, so nothing was deleted.
            self._json(503, {"error": str(err)})
        except (urllib.error.URLError, OSError) as err:
            # Type only: a URLError string carries the URL it was raised for.
            log(f"gateway: {type(err).__name__} reaching Frigate; no reply was invented")
            self._json(502, {"error": "Frigate could not be reached"})
        finally:
            # The route is one of four literals or "unknown"; a client's own path is
            # never echoed into a log line.
            known = path if path in (SUMMARY_PATH, EVENTS_PATH, DELETE_PATH, ENTRY_PATH) else "unknown"
            log(f"gateway: {method} {known} -> {self.status}")

    def _summary(self):
        """{count, before}: how many of this suite's person snapshots are older than
        the whole second named, however old they are.

        before is a whole second taken before the walk, and count is exactly what
        a deletion naming that second would reach, so the operator is never shown
        a number that a later deletion cannot deliver. Events newer than that
        second are deliberately not counted: they are the last few seconds of
        live detection, not archive.
        """
        cutoff = math.floor(time.time())
        with self.server.lock:
            ids, walk = select_archive(self.server.api, cutoff, self.server.page_size,
                                       self.server.max_pages)
        log(f"summary: {len(ids)} archived snapshot(s) older than {cutoff}, "
            f"{walk['pages']} page(s) read")
        return {"count": len(ids), "before": cutoff}

    def _events(self, raw_query):
        """One page of the archive, read through the same lock the poll loop takes, so a
        page and a pass never interleave their walks over the same upstream.

        The query is validated first, so a malformed one costs nothing upstream and the
        reply says what was wrong rather than a truncated archive.
        """
        page, camera = events_filters(urllib.parse.parse_qs(raw_query, keep_blank_values=True))
        with self.server.lock:
            payload = select_events(self.server.api, page, camera, self.server.page_size,
                                    self.server.max_pages)
        log(f"events: page {payload['page']} of {payload['pages']}, {payload['total']} "
            f"archived snapshot(s){'' if camera == 'all' else f' on {camera}'}, "
            f"{len(payload['events'])} on this page")
        return payload

    def _delete(self):
        self._require_same_origin()
        body = self._read_body()
        now = time.time()
        mode, value = delete_plan(body, now)
        # One lock for the walk and every delete in it: the poll pass waits its
        # turn, and a deletion never sees a half-finished pass.
        with self.server.lock:
            if mode == "ids":
                selected = list(value)
                named = f"{len(selected)} selected id(s)"
            else:
                selected, _walk = select_archive(self.server.api, value, self.server.page_size,
                                                 self.server.max_pages)
                named = f"{len(selected)} archived id(s) older than {value:g}"
            reply = delete_selected(self.server.api, selected, now)
        log(f"delete: {named}; {reply['deleted']} deleted, {reply['missing']} already gone, "
            f"{len(reply['failed'])} refused, {reply['active']} still open")
        return reply

    def _entry(self):
        """The arming status, read under the lock so it cannot be answered from
        half a state change. The config read is cached inside it, so a status poll
        is not one /api/config request."""
        with self.server.lock:
            payload = entry_status(self.server.entry, self.server.api, time.time())
        state = payload["state"]
        log("entry: " + ", ".join(
            f"{camera} ready={str(state[camera]['ready']).lower()} "
            f"armed={str(state[camera]['armed']).lower()}" for camera in payload["cameras"]))
        return payload

    def _entry_write(self):
        """Arm or disarm one camera, in memory. No upstream write, no config edit,
        and nothing that can delete a record: this flag only decides what a later
        poll pass is allowed to keep.
        """
        self._require_same_origin(ENTRY_ACTION_VALUE)
        camera, armed = entry_plan(self._read_body())
        now = time.time()
        # The same lock the poll pass takes, so an arm or a disarm can never land
        # halfway through one. It waits for a pass in flight rather than racing it,
        # and the answer it gives is the state the pass will see next.
        with self.server.lock:
            # Re-read either way, so the reply reports the state a poll would see.
            # Only arming is refused without it: turning a camera off has to work
            # even when Frigate's config cannot be read, or a camera that stopped
            # being ready could never be turned off.
            ready = self.server.entry.ready(self.server.api, now, force=True)
            if armed:
                if not ready.get(camera):
                    raise Refused(409, "that camera's restricted area is not configured in "
                                       "Frigate yet, so nothing would be kept by arming it")
                self.server.entry.arm(camera, now)
            else:
                self.server.entry.disarm(camera, now)
            state = self.server.entry.snapshot()[camera]
            payload = {
                "cameras": list(ENTRY_CAMERAS),
                "notice": ENTRY_NOTICE,
                "state": self.server.entry.snapshot(),
            }
        log(f"entry: {camera} {'armed' if armed else 'disarmed'} by operator request "
            f"(armed_since={state['armed_since']})")
        return payload

    def _require_same_origin(self, action=ACTION_VALUE):
        """The three things a cross-site page cannot produce on its own: the exact
        Origin, a JSON content type, and a header that forces a preflight this
        server never answers.

        action is the one header value this write is allowed to carry, and it is
        named by the caller rather than defaulted into the check: a delete can
        never be sent as an arming, and an arming can never be sent as a delete.
        """
        host = (self.headers.get("Host") or "").strip()
        if (self.headers.get("Origin") or "") != f"http://{host}":
            raise Refused(403, "the Origin must be this dashboard's own")
        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if content_type != "application/json":
            raise Refused(415, "the request body must be application/json")
        if (self.headers.get(ACTION_HEADER) or "").strip().lower() != action:
            raise Refused(403, f"{ACTION_HEADER} must be {action}")

    def _read_body(self):
        """The body, read once and parsed, or Refused. Bounded before it is read,
        so an oversized body is never buffered."""
        raw = self.headers.get("Content-Length")
        if raw is None:
            raise Refused(411, "the request must carry a Content-Length")
        try:
            length = int(raw)
        except ValueError:
            raise Refused(400, "the Content-Length is not a number")
        if length < 0:
            raise Refused(400, "the Content-Length is negative")
        if length > MAX_BODY_BYTES:
            raise Refused(413, f"the request body is over the {MAX_BODY_BYTES} byte limit")
        body = self.rfile.read(length) if length else b""
        if len(body) != length:
            raise Refused(400, "the request body was shorter than its Content-Length")
        try:
            return json.loads(body.decode("utf-8"))
        except ValueError:
            raise Refused(400, "the request body is not JSON")


class GatewayServer(http.server.ThreadingHTTPServer):
    """The gateway's own server, holding the upstream handle and the one lock that
    the poll loop shares."""

    daemon_threads = True
    allow_reuse_address = True


def serve(api, args, entry):
    """The poll loop and the gateway in one process, sharing one lock and one arming
    state. The loop runs here and the server on a daemon thread, so Ctrl-C stops
    both. The state is created disarmed and nothing in this process writes it
    anywhere, so a restart is always a restart back to off."""
    host, port = args.listen
    server = GatewayServer((host, port), Gateway)
    server.api = api
    server.lock = threading.Lock()
    server.entry = entry
    server.page_size = args.page_size
    server.max_pages = args.max_pages
    thread = threading.Thread(target=server.serve_forever, name="gateway", daemon=True)
    thread.start()
    log(f"serving the gateway on {host}:{port}, for the dashboard on "
        f"{' or '.join(ALLOWED_HOSTS)} only; entry review disarmed on "
        f"{', '.join(ENTRY_CAMERAS)}")
    try:
        return poll_forever(api, args, entry, server.lock)
    finally:
        server.shutdown()
        server.server_close()


def listen_address(value):
    """argparse type for HOST:PORT. A missing or out-of-range port is refused here
    rather than by the socket layer, where the message would name nothing useful."""
    host, separator, port = value.rpartition(":")
    if not separator or not host:
        raise argparse.ArgumentTypeError(f"{value!r} is not HOST:PORT")
    try:
        number = int(port)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} has no numeric port")
    if not 1 <= number <= 65535:
        raise argparse.ArgumentTypeError(f"the port {number} is outside 1-65535")
    return host, number


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
        description="Keep entry snapshots for armed cameras 1 and 4, and the animals "
                    "seen on cameras 1-3, in Frigate"
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
    parser.add_argument("--listen", type=listen_address, default=listen_address(GATEWAY_LISTEN),
                        metavar="HOST:PORT",
                        help=f"where --serve listens (default {GATEWAY_LISTEN})")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="run one pass and exit")
    mode.add_argument(
        "--delete-reviewed", metavar="ID", help="delete one event by native id, then exit"
    )
    mode.add_argument("--serve", action="store_true",
                      help="run the poll loop and the gateway together")
    args = parser.parse_args(argv)
    api = Api(args.api_base, args.timeout)
    # Fresh, and therefore disarmed, on every run. There is deliberately no
    # option that seeds it armed: arming is a person's decision made on the
    # dashboard, and a --once or a bare poll loop therefore pins people only when
    # some armed process is holding the state for them.
    entry = EntryState()
    if args.delete_reviewed is not None:
        return delete_reviewed(api, args.delete_reviewed)
    if args.once:
        return run_pass(api, args.page_size, args.max_pages, entry)
    if args.serve:
        return serve(api, args, entry)
    return poll_forever(api, args, entry)


if __name__ == "__main__":
    sys.exit(main())