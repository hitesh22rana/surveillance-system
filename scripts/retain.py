#!/usr/bin/env python3
"""Keep person snapshots for review, and the animals seen on cameras 1-3.

Two rules for people, one of which is fixed, and what each one is for.

camera_4 watches the area_4 and is always armed. A person inside
camera_4_restricted is a candidate for a person to look at. There is no control for
it, no request that turns it off, and neither preset touches it: it is armed from
startup, it is not in the state file, and no writable file can override it. It is
still gated on Frigate reporting the zone, so a zone that stops being configured
shows it unavailable and pins nothing, and it recovers by itself once it is back.

camera_1, camera_2 and camera_3 are armed by hand. A person is kept on camera_1 only
inside its own configured camera_1_restricted, and on camera_2 and camera_3 only in
that camera's full-frame monitored area while that camera is armed. Ordinary family
movement through the house is not evidence of anything, so nothing is kept there by
default: Home keeps none of the three, Away arms all three at one moment, and
switching one by hand makes the mode Custom.

Those settings are saved, so a restart comes back as they were and a fresh install
comes back Home. One small JSON file in the one writable volume holds three flags and
three timestamps and nothing else: no credential, no configuration, no upstream body.
It is written atomically, a write that does not land changes nothing here and is
reported as a failure, and a file that cannot be read is never guessed away from — it
is Home, with the controls shown as unavailable.

Arming reaches forwards on those three and never backwards: an event that began
before the arm is not one somebody switched a camera on to keep, so an entry already
under way can be missed. camera_4 has no arming moment at all, so its only boundary is
the pass's own six hour window and a track in progress is kept like any other.
Disarming stops new pins, never unpins what is already kept, and needs no zone.

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

A camera may be armed only when Frigate's own /api/config reports the zone that
camera needs — person-only on the two restricted polygons, and carrying person on the
full-frame areas camera_2 and camera_3 already track their animals in — at
loitering_time 0. That read is refreshed on a timer and after any change, is parsed
only into booleans, and is never forwarded to the browser: the config carries RTSP
URLs with credentials, so nothing from it but four true-or-false answers is ever
returned, and a failed read leaves every camera not ready without touching the
operator's settings.

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
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

CAMERAS = ("camera_1", "camera_2", "camera_3", "camera_4")
TARGET_LABEL = "person"
# The three cameras a person may arm by hand. camera_1 and camera_4 watch an
# approach and have a restricted polygon of their own; camera_2 and camera_3 are the
# internal views, whose monitored area is the whole frame, so arming one of them
# keeps any person in view until it is switched off again.
ENTRY_CAMERAS = ("camera_1", "camera_2", "camera_3")
# The one camera that is always armed, with no control and no state of its own.
ALWAYS_CAMERA = "camera_4"
# The zone a person event has to carry for that camera to be a candidate: the
# restricted polygon on camera_1 and camera_4, where an event carries the zone only
# once inertia is met at loitering_time 0 and crossing into the area is the whole
# signal; and the existing full-frame area zone on camera_2 and camera_3, where
# presence in view is the whole of it.
ENTRY_ZONE = {
    "camera_1": "camera_1_restricted",
    "camera_2": "camera_2_area",
    "camera_3": "camera_3_area",
    "camera_4": "camera_4_restricted",
}
# The two restricted polygons, which have to be person-only. camera_2 and camera_3 are
# not: their mapped zone is the full-frame area those two cameras already carry the ten
# animal classes in, so there person is one of the labels the zone accepts, and the
# animal rule below is what keeps an animal.
ENTRY_ZONE_PERSON_ONLY = ("camera_1", "camera_4")
# camera_4's guard is the start of time: it has no arming moment, so nothing it sees
# is refused for having started early.
ALWAYS_SINCE = 0.0
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
    "while entry review was not armed",
    "before entry review was armed",
    "outside its own monitored area",
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

# The operator's manual arming, saved so a restart comes back as they left it. One
# file in the one writable volume: the container itself is still read-only, and
# nothing else in this process writes anywhere. MAX_STATE_BYTES is a deliberate
# ceiling on what that file can ever be, so nothing else can be written into it.
STATE_FILE = "/state/entry.json"
MAX_STATE_BYTES = 4096
STATE_VERSION = 1
# Reported in every status answer when the file could not be read, and shown by the
# dashboard as unavailable. Home is the direction to fall back in: it keeps no person
# snapshot, rather than one nobody chose.
STATE_UNREADABLE = ("Saved arming settings could not be read. Cameras 1-3 are disarmed "
                    "(Home); Camera 4 stays always armed.")

# Frigate's own tracker id is "<frame_time>-<rand_id>", e.g. 1790910000.123456-abc123.
# Anything not exactly this form never reaches a request path. fullmatch, not
# match: a trailing newline must not survive the check.
EVENT_ID = re.compile(r"[0-9]+(?:\.[0-9]+)?-[a-z0-9]{6}")

EXIT_OK = 0
EXIT_NO_PASS = 1  # the pass did not complete; the next cycle tries again
EXIT_BAD_INPUT = 2  # refused before any request was made


def default_desired():
    """Every camera's setting before anything is saved: camera_4 always armed, the
    three manual ones off, which is Home."""
    cameras = {ALWAYS_CAMERA: {"armed": True, "armed_since": None}}
    for camera in ENTRY_CAMERAS:
        cameras[camera] = {"armed": False, "armed_since": None}
    return cameras


def positive_stamp(value):
    """A finite timestamp above zero, or None.

    The conversion is the guarded part, not the comparison: float() on an enormous
    integer raises OverflowError rather than answering infinity, and a saved file is
    untrusted input, so that has to come out as "not a file this reads" rather than as
    a traceback at startup. A boolean is refused too, because True == 1.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        stamp = float(value)
    except (OverflowError, ValueError):
        return None
    return stamp if math.isfinite(stamp) and stamp > 0 else None


def read_desired(path):
    """(desired, error): the saved manual settings, validated field by field.

    A missing file is a fresh installation, not an error. Anything else this cannot
    read — not JSON, not this shape, a version that is not this version, a camera that
    is neither armed nor off, a timestamp that is not a number, a fourth camera that is
    not one of the three manual ones — is Home with everything manual off and
    STATE_UNREADABLE beside it. The direction to fall back in is the one that keeps
    nothing.

    camera_4 is not in the schema, and this only ever reads the three manual cameras, so
    nothing written into that file can switch the always-armed camera off however it is
    spelled. The read is bounded before it is parsed, so a large file is not a large
    file this reads either.
    """
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_STATE_BYTES + 1)
    except FileNotFoundError:
        return default_desired(), None
    except OSError:
        return default_desired(), STATE_UNREADABLE
    if len(raw) > MAX_STATE_BYTES:
        return default_desired(), STATE_UNREADABLE
    try:
        document = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        return default_desired(), STATE_UNREADABLE
    version = document.get("version") if isinstance(document, dict) else None
    if type(version) is not int or version != STATE_VERSION:
        return default_desired(), STATE_UNREADABLE
    saved = document.get("cameras")
    if not isinstance(saved, dict) or set(saved) != set(ENTRY_CAMERAS):
        return default_desired(), STATE_UNREADABLE
    desired = default_desired()
    for camera in ENTRY_CAMERAS:
        row = saved[camera]
        armed = row.get("armed") if isinstance(row, dict) else None
        since = row.get("armed_since") if isinstance(row, dict) else None
        if armed is not True and armed is not False:
            return default_desired(), STATE_UNREADABLE
        if armed:
            stamp = positive_stamp(since)
            if stamp is None:
                return default_desired(), STATE_UNREADABLE
        elif since is not None:
            return default_desired(), STATE_UNREADABLE
        desired[camera] = {"armed": armed, "armed_since": positive_stamp(since) if armed else None}
    return desired, None


def write_desired(path, desired):
    """The one write in this process: atomic, small, and camera_4 nowhere in it.

    Written and flushed beside the old file, then renamed over it, so a failure at any
    point leaves the previous valid file whole rather than a half-written one. The
    ceiling is checked before anything is opened, so a document that could not fit
    never reaches the filesystem. OSError is the only failure this raises.
    """
    document = json.dumps(
        {"version": STATE_VERSION,
         "cameras": {camera: desired[camera] for camera in ENTRY_CAMERAS}},
        sort_keys=True,
    )
    if len(document) > MAX_STATE_BYTES:
        raise OSError("the saved arming would be over its own ceiling")
    temporary = f"{path}.new"
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            handle.write(document)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError:
        # The old file was never touched, so only the scratch is left to remove.
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


class EntryState:
    """Who is armed: camera_4 always, and cameras 1-3 as the operator left them.

    Two facts are kept apart on purpose. `armed` is the operator's setting, and it is
    saved, so a restart comes back as it was. `ready` is whether Frigate's own config
    currently reports the zone that camera needs, and it is not saved and does not
    touch the setting: a camera that stops being ready stops pinning and nothing else,
    then starts again by itself once the config says so, with the moment it was armed
    intact. The config read behind `ready` is cached, so a status read never turns
    into one request per row, and it is forced after any write so a ready verdict can
    never lag the geometry it was read from.
    """

    def __init__(self, path=STATE_FILE):
        self._path = path
        self._desired, self._error = read_desired(path)
        self._ready = {camera: False for camera in CAMERAS}
        self._checked_at = None

    def snapshot(self):
        """The whole state as plain booleans and one number each, safe to hand out."""
        return {camera: dict(self._desired[camera], ready=self._ready[camera]) for camera in CAMERAS}

    def mode(self):
        """home, away or custom, from the three manual settings and nothing else."""
        armed = [self._desired[camera]["armed"] for camera in ENTRY_CAMERAS]
        if not any(armed):
            return "home"
        return "away" if all(armed) else "custom"

    def notice(self):
        """The one paragraph the dashboard shows: what is always armed, what the two
        presets do, and what the saved setting is allowed to be. Appended to that, only
        when it is true, is the one thing that went wrong reading the file — never the
        path and never the exception. Desired against effective is on the rows."""
        text = ("Camera 4's area_4 is always armed. Home disarms Cameras 1-3; Away arms "
                "them. Settings survive restarts. Tracks already underway when 1-3 are "
                "armed may be missed.")
        if self._error is not None:
            text += f" {self._error}."
        return text

    def _cache_fresh(self, now):
        return self._checked_at is not None and now - self._checked_at < CONFIG_REVALIDATE_SECONDS

    def ready(self, api, now, force=False):
        """Whether each camera's zone is configured, and may therefore be pinned.

        Fail closed. A transport error, a non-2xx, a body that is not JSON or a shape
        this file cannot read is not a config with no zones: it leaves every camera not
        ready, so the next pass pins no person and the next POST refuses rather than
        trusting the last good answer. Nothing from the config but these booleans is
        kept, and the body itself is never stored or returned. The operator's settings
        are not touched here, so this is not an un-pin: it stops further pins and they
        resume by themselves.
        """
        if not force and self._cache_fresh(now):
            return self._ready
        try:
            ready = entry_zones_ready(self._read_config(api))
        except (urllib.error.URLError, OSError, ValueError):
            ready = {camera: False for camera in CAMERAS}
            log("entry: the camera service's config could not be read, so no person snapshot "
                "is being pinned and the settings are untouched")
        if ready != self._ready:
            log("entry: ready " + ", ".join(f"{camera}={str(ready[camera]).lower()}"
                                            for camera in CAMERAS))
        self._ready = ready
        self._checked_at = now
        return ready

    @staticmethod
    def _read_config(api):
        status, body = api.call("/api/config")
        if not 200 <= status < 300:
            raise ValueError("config was not 2xx")
        return json.loads(body.decode("utf-8", "replace"))

    def guard(self, camera):
        """The moment a candidate event has to reach to be kept, or None.

        None means "nothing is being kept for people on this camera": one that is off,
        and one whose zone the camera service does not currently report. Both land here,
        in one place, so no caller reaches a pin without passing this. camera_4 is armed
        with no moment recorded, so its guard is the start of time and a track already
        under way is a candidate like any other.
        """
        if not self._ready.get(camera):
            return None
        state = self._desired.get(camera)
        if not state or not state["armed"]:
            return None
        return ALWAYS_SINCE if camera == ALWAYS_CAMERA else state["armed_since"]

    def apply(self, changes, now):
        """True once the named cameras' settings are saved, False if they were not.

        The file is written first and this process's flags move only after it is, so a
        write that fails leaves the running state exactly as it was and the caller
        reports a failure instead of an arming. One write per request, so the away
        preset arms all three at one moment or none of them.

        Arming a camera that is already armed keeps the moment the first arming
        recorded rather than moving it: a repeated click, or a page that re-sends the
        same request, must not slide the boundary and let a pass reach further back
        into history than the person who armed it intended.
        """
        proposed = {camera: dict(self._desired[camera]) for camera in CAMERAS}
        for camera, armed in changes.items():
            if camera == ALWAYS_CAMERA:
                raise Refused(400, f"{ALWAYS_CAMERA} is always armed and cannot be changed")
            if armed and not proposed[camera]["armed"]:
                proposed[camera] = {"armed": True, "armed_since": now}
            elif not armed:
                proposed[camera] = {"armed": False, "armed_since": None}
        try:
            write_desired(self._path, proposed)
        except OSError as err:
            # Type only: an OSError message carries the path it failed on.
            log(f"entry: the arming could not be saved ({type(err).__name__}), so nothing changed")
            return False
        self._desired = proposed
        self._error = None
        return True


def entry_zones_ready(config):
    """{camera: bool}: does Frigate's own config carry the zone that camera needs,
    person-only at loitering_time 0?

    Read only far enough to answer that, and answer nothing else. The config
    carries RTSP URLs with credentials, so it is parsed here and never stored,
    never logged and never returned: what leaves this function is four booleans.
    A shape this does not recognise is False, never a guess.
    """
    cameras = config.get("cameras") if isinstance(config, dict) else None
    if not isinstance(cameras, dict):
        return {camera: False for camera in CAMERAS}
    ready = {}
    for camera in CAMERAS:
        block = cameras.get(camera)
        zones = block.get("zones") if isinstance(block, dict) else None
        zone = zones.get(ENTRY_ZONE[camera]) if isinstance(zones, dict) else None
        labels = zone.get("objects") if isinstance(zone, dict) else None
        if not isinstance(labels, list) or TARGET_LABEL not in labels:
            ready[camera] = False
            continue
        # Exactly person on the two restricted polygons: an animal inside one of those
        # lines is not the same evidence, and is kept by the animal rule instead. The
        # full-frame areas on camera_2 and camera_3 carry the animal classes those two
        # cameras track, so there person is one label among several.
        person_only = labels == [TARGET_LABEL]
        ready[camera] = zone.get("loitering_time") == 0 and (
            person_only or camera not in ENTRY_ZONE_PERSON_ONLY)
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

    A person is pinned only when all of these hold: that camera's entry review is
    armed right now, the event carries the zone that camera watches, and — for the
    three cameras a person arms by hand — the event started at or after the
    armed_since of the arming that let it through. A 60-second full-frame presence
    pins nothing any more, on any camera.

    The armed_since comparison is exact, and it is why cameras 1-3 can reach back
    no further than the moment they were armed: an event that began before somebody
    switched a camera on is not one they switched on to keep. That leaves one known
    gap, stated rather than hidden: a track that entered the area before the arm and
    is still running will not be pinned on those three, because Frigate reports one
    start_time for the whole track. camera_4 has no arming moment, so it has no such
    boundary and a track in progress is kept there like any other. Catching those on
    1-3 needs the current-zone-entry signal on MQTT, which is not built here.

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
        return "while entry review was not armed"
    if ENTRY_ZONE[camera] not in zones(event):
        return "outside its own monitored area"
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
    state = entry.snapshot()
    held = ", ".join(camera for camera in ENTRY_CAMERAS if state[camera]["armed"])
    log(f"scan: {walk['pages']} page(s) of at most {page_size} rows, {len(events)} unique event(s), "
        f"{walk['malformed']} not addressable by a native id")
    log(f"pin: {len(pinned)} pinned, {already} already retained, {ALWAYS_CAMERA} always armed, "
        f"{held or 'no manual camera armed'}; {counts}")
    if failed:
        log(
            f"{len(failed)} pin(s) refused by Frigate, left unpinned and retried next cycle: "
            + ", ".join(f"{event_id} ({detail})" for event_id, detail in failed)
        )
        return EXIT_NO_PASS
    return EXIT_OK


def poll_forever(api, args, entry, lock=None):
    log(f"polling {redacted(api.base)} every {args.interval:g}s for {len(ANIMAL_LABELS)} animal "
        f"labels on {', '.join(ANIMAL_CAMERAS)}, and for people on all four cameras: "
        f"{ALWAYS_CAMERA} always, {', '.join(ENTRY_CAMERAS)} while they are armed by hand")
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
# One of the two writes deletes an event, irreversibly. The other saves a change to
# the operator's arming for one of three cameras, or sets the Home and Away presets for
# all three at once. It writes nothing upstream and touches no configuration:
# Frigate's /api/config is read here only to answer whether the zone that camera needs
# exists, and only booleans leave it. camera_4 is armed from startup and is not in that
# state file at all, so nothing on this route, and nothing written to disk, can turn it
# off.

GATEWAY_LISTEN = "0.0.0.0:8090"  # the internal network only; compose publishes no port for it
ALLOWED_HOSTS = ("localhost:8088", "127.0.0.1:8088")  # where the dashboard is published
SUMMARY_PATH = "/api/snapshots/summary"
EVENTS_PATH = "/api/snapshots/events"
DELETE_PATH = "/api/snapshots/delete"
ENTRY_PATH = "/api/security/entry"
ACTION_HEADER = "X-Surveillance-Action"
ACTION_VALUE = "delete"
ENTRY_ACTION_VALUE = "arm-entry"
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
    """The whole entry answer: the four cameras, the mode they add up to, the camera
    that is always armed, and one boolean, one number and one notice each. Nothing
    from the camera service's config is in here, and neither is the state file's path
    or anything that went wrong reading it beyond that one notice.

    force re-reads the config before answering, which the POST needs and the GET
    does not: a request that has just armed a camera must not be refused by a
    cached verdict taken before the operator drew the zone.
    """
    entry.ready(api, now, force)
    return {
        "cameras": list(CAMERAS),
        "mode": entry.mode(),
        "always_armed": ALWAYS_CAMERA,
        "notice": entry.notice(),
        "state": entry.snapshot(),
    }


def entry_plan(body):
    """(changes, named) or Refused, before any lock is taken or config is read.

    Exactly two shapes and nothing else: {"mode": "home"|"away"} for the two presets,
    or {"camera": one of cameras 1-3, "armed": true|false} for one camera. A third key, a
    missing key, a string "true", a 1, an unknown camera, or camera_4 in either form is
    refused rather than guessed at, because this is the one body that turns retention
    on. camera_4 has no shape here at all: it is always armed, and refusing to write to
    it is the only way that can be said on the wire.
    """
    if not isinstance(body, dict):
        raise Refused(400, 'send {"mode":"home"|"away"}, or '
                           '{"camera":"camera_1"|"camera_2"|"camera_3","armed":true|false}')
    if set(body) == {"mode"}:
        mode = body["mode"]
        if mode in ("home", "away"):
            return {camera: mode == "away" for camera in ENTRY_CAMERAS}, mode
        raise Refused(400, "mode must be the string home or away")
    if set(body) != {"camera", "armed"}:
        raise Refused(400, "send mode, or camera and armed, and nothing else")
    camera = body["camera"]
    armed = body["armed"]
    if not isinstance(camera, str) or camera not in ENTRY_CAMERAS:
        raise Refused(400, f"camera must be one of {', '.join(ENTRY_CAMERAS)}; "
                           f"{ALWAYS_CAMERA} is always armed and cannot be armed or disarmed here")
    if armed is not True and armed is not False:
        raise Refused(400, "armed must be the boolean true or false")
    return {camera: armed}, camera


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
        log(f"entry: {payload['mode']}, " + ", ".join(
            f"{camera} armed={str(state[camera]['armed']).lower()} "
            f"ready={str(state[camera]['ready']).lower()}" for camera in payload["cameras"]))
        return payload

    def _entry_write(self):
        """Arm or disarm one camera, or set the Home and Away presets for all three.

        No upstream write, no config edit, and nothing that can delete a record: this
        only saves what a later poll pass is allowed to keep. The change is durable
        before this process believes it, so the answer is always the state a restart
        would come back to.
        """
        self._require_same_origin(ENTRY_ACTION_VALUE)
        changes, named = entry_plan(self._read_body())
        now = time.time()
        # The same lock the poll pass takes, so a change can never land halfway
        # through one. It waits for a pass in flight rather than racing it, and the
        # answer it gives is the state the pass will see next.
        with self.server.lock:
            # Re-read either way, so the reply reports the state a poll would see.
            # Turning something off never needs it, or a camera that stopped being
            # ready could never be turned off; arming does, or it would arm a camera
            # nothing would be kept on.
            ready = self.server.entry.ready(self.server.api, now, force=True)
            unconfigured = sorted(camera for camera, armed in changes.items()
                                  if armed and not ready.get(camera))
            if unconfigured:
                raise Refused(409, "the area for " + ", ".join(unconfigured) + " is not "
                                   "configured in the camera service yet, so nothing would "
                                   "be kept by arming it")
            if not self.server.entry.apply(changes, now):
                # The saved file is the record, so a write that did not land is not an
                # arming: the flags in this process are left where they were and the
                # answer says so rather than reporting a change a restart would lose.
                raise Refused(503, "the arming could not be saved, so nothing changed")
            payload = entry_status(self.server.entry, self.server.api, now)
        state = payload["state"]
        log(f"entry: {named} by operator request, now {payload['mode']} (" + ", ".join(
            f"{camera} armed_since={state[camera]['armed_since']}" for camera in ENTRY_CAMERAS
        ) + f"), {ALWAYS_CAMERA} always armed")
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
    both. The state was read from the one file this process writes, so a restart comes
    back as the operator left it and a fresh install comes back Home."""
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
        f"{' or '.join(ALLOWED_HOSTS)} only; {ALWAYS_CAMERA} always armed, entry review on "
        f"{', '.join(ENTRY_CAMERAS)} at {entry.mode()}")
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
        description="Keep the person snapshots cameras 1-3 are armed for, camera_4's always, "
                    "and the animals seen on cameras 1-3, in Frigate"
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
    parser.add_argument("--state-file", default=STATE_FILE, metavar="PATH",
                        help=f"where the manual arming for cameras 1-3 is saved between "
                             f"restarts, default {STATE_FILE}")
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
    # Read from the one file this process writes, never seeded from anywhere else:
    # arming is a person's decision made on the dashboard, and a fresh installation
    # with no file is Home. camera_4 is armed here, in this constructor, whatever
    # that file holds.
    entry = EntryState(args.state_file)
    if args.delete_reviewed is not None:
        return delete_reviewed(api, args.delete_reviewed)
    if args.once:
        return run_pass(api, args.page_size, args.max_pages, entry)
    if args.serve:
        return serve(api, args, entry)
    return poll_forever(api, args, entry)


if __name__ == "__main__":
    sys.exit(main())