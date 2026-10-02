#!/usr/bin/env python3
"""HTTP boundary check for scripts/retain.py against a fake Frigate (stdlib only).

Everything retain.py decides happens across an HTTP boundary, so this starts a
fake Frigate on loopback and runs the real script as a subprocess with --once.
Assertions read only what left the process and what reached the fake upstream. It
adds no seam to production code: --api-base is an argument compose passes as a
literal anyway, and every other argument has a default.

The fake imitates Frigate 0.18 EventsQueryParams, where offset is ignored
outright (no such field) and both bounds are strict, so a walk that pages by
offset cannot pass and neither can one that sends the boundary value itself.
Rows are Frigate-shaped: the singular label, the boolean has_snapshot, a list of
zones, and a native "<stamp>-<6 lowercase alphanumerics>" id. Filtering=False is
the one departure, and it is what makes the worker's own label and snapshot
checks reachable. Its /api/config carries the full-frame camera_N_area on all four
cameras - person plus all ten animal classes on cameras 1-3, as the running
instance reports - and the person-only restricted polygon on camera_1 and
camera_4. The single-event GET, the delete that removes the row and the retain
body are here for scripts/check_console_api.py, which drives the gateway against
this same imitation rather than a second one.

Every pass that depends on what is armed points --state-file at a real temporary
directory, so a check never reads or writes the production /state volume.

    python3 scripts/check_retention.py
"""

import argparse
import contextlib
import http.server
import io
import json
import math
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import retain  # noqa: E402  (path set above, so the production module is the one under test)

PAGE_SIZE = "2"
BASE_TIME = int(time.time()) - 3600  # an hour back: comfortably inside the lookback
ANCIENT_TIME = int(time.time()) - 7 * 3600  # outside it, and never to be read
TIED = BASE_TIME + 470  # every tied event shares this start_time

# The zone this suite judges each camera's people in, and the ten COCO 91 animal
# classes cameras 1-3 track. Restated here rather than imported from retain.py, and
# that is the point: if the worker's map and this fake's ever drift apart, a check
# fails instead of the fake agreeing with whatever the code now expects.
ANIMAL_LABELS = (
    "bird", "cat", "dog", "horse", "sheep", "cow",
    "elephant", "bear", "zebra", "giraffe",
)
MAPPED_ZONE = {
    "camera_1": "camera_1_restricted",
    "camera_2": "camera_2_area",
    "camera_3": "camera_3_area",
    "camera_4": "camera_4_restricted",
}


def event(tag, camera, start, zones=None, label="person", snapshot=True, **extra):
    """A Frigate-shaped event: native id, one label, a bool has_snapshot, a zones list.

    The default zone is that camera's own mapped one, which is what a person event
    looks like once that camera is armed.
    """
    payload = {
        "id": f"{start}.123456-{tag}",
        "camera": camera,
        "camera_name": camera,
        "start_time": float(start),
        "end_time": float(start) + 2.5,
        "label": label,
        "zones": [MAPPED_ZONE.get(camera, f"{camera}_restricted")] if zones is None else list(zones),
        "has_snapshot": snapshot,
        "snapshot": f"{camera}/snapshot.jpg" if snapshot else None,
        "type": "object",
        "false_positive": False,
    }
    payload.update(extra)
    return payload


# A marker that must never appear in anything the gateway answers. The config is
# where Frigate keeps RTSP URLs with passwords, so it is the one body this check
# can prove does not travel: the fake carries it, and no reply may echo it.
CREDENTIAL_MARKER = "rtsp://cam-user:CREDENTIAL-MARKER@192.0.2.10:554/live"


def config_payload(missing=()):
    """A Frigate-shaped /api/config, with the credential marker in it.

    Every camera carries its own full-frame camera_N_area at loitering_time 0, which on
    cameras 1-3 lists person plus all ten animal classes, exactly as the running instance
    reports. Camera 1 and Camera 4 also carry their restricted polygon, person-only. The
    zone this worker maps for a camera therefore exists by default and all four cameras
    are ready; naming a camera in missing removes the one zone that camera is judged in,
    which is the "not configured yet" case the Arm control has to refuse.
    """
    missing = set(missing)
    cameras = {}
    for camera in ("camera_1", "camera_2", "camera_3", "camera_4"):
        labels = ["person"] if camera == "camera_4" else ["person"] + list(ANIMAL_LABELS)
        block_zones = {
            f"{camera}_area": {
                "coordinates": "0,0,1,0,1,1,0,1",
                "inertia": 3,
                "loitering_time": 0,
                "objects": labels,
            },
        }
        if camera in ("camera_1", "camera_4"):
            block_zones[f"{camera}_restricted"] = {
                "coordinates": "0,0,0.4,0,0.4,1,0,1",
                "inertia": 3,
                "loitering_time": 0,
                "objects": ["person"],
            }
        if camera in missing:
            block_zones.pop(MAPPED_ZONE[camera])
        cameras[camera] = {
            "ffmpeg": {"inputs": [{"path": CREDENTIAL_MARKER, "roles": ["detect"]}]},
            "objects": {"track": labels},
            "zones": block_zones,
            "review": {"alerts": {"enabled": False}},
            "snapshots": {"enabled": True, "retain": {"default": 0.25}},
            "record": {"enabled": False},
        }
    return {"cameras": cameras, "mqtt": {"enabled": False}}


def tied_group():
    """Seven events sharing one start_time: four pages at PAGE_SIZE 2."""
    return [event(f"tied{n:02d}", "camera_1", TIED) for n in range(1, 8)]


def tied_fixture():
    """Eleven events. The seven tied ones are the group a page boundary can split."""
    return [
        event("kilo01", "camera_1", BASE_TIME + 500),
        event("kilo02", "camera_4", BASE_TIME + 490),
        event("kilo03", "camera_4", BASE_TIME + 480),
        *tied_group(),
        event("old001", "camera_1", BASE_TIME + 400),
    ]


def entry_fixture(armed_at):
    """Ten rows around one arming moment on cameras 1-3, with camera_4 armed the whole
    time, so the boundary is the only variable.

    Rows before armed_at are inside the pass's six hour lookback, so each one is refused
    for being old rather than for being unread - but only on cameras 1-3. camera_4 has
    no arming moment and no start-time boundary at all, so the same shape on camera_4 is
    kept: an entry already under way is a candidate there. Every tag is exactly six
    lowercase alphanumerics, or the row would be counted as unaddressable instead of
    reaching the check it is here for.
    """
    def before(tag, camera, n, **extra):
        return event(tag, camera, armed_at - 30 - n, **extra)

    def after(tag, camera, n, **extra):
        return event(tag, camera, armed_at + 1 + n, **extra)

    return [
        after("newc01", "camera_1", 9, zones=["camera_1_restricted"]),   # 0  pins
        after("newc02", "camera_2", 8, zones=["camera_2_area"]),         # 1  pins
        after("newc03", "camera_3", 7, zones=["camera_3_area"]),         # 2  pins
        after("newc04", "camera_4", 6, zones=["camera_4_restricted"]),   # 3  pins
        before("oldc01", "camera_1", 5, zones=["camera_1_restricted"]),  # 4  before the arm
        before("oldc04", "camera_4", 4, zones=["camera_4_restricted"]),  # 5  pins anyway
        after("wrong1", "camera_4", 3, zones=["camera_1_restricted"]),   # 6  other zone
        after("areaz1", "camera_2", 2, zones=["camera_1_restricted"]),   # 7  other zone
        after("loit60", "camera_1", 1, zones=["camera_1_loitering"]),    # 8  the removed rule
        after("dog001", "camera_3", 0, label="dog", zones=["camera_3_area"]),  # 9 pins
    ]


def unfiltered_fixture():
    """One event per decision the worker has to make for itself, plus the tied group.

    This upstream applies no filter of its own, so every row reaches the worker
    and the native fields decide: one of each disqualifier, one already retained,
    and one already retained but behind the lookback, so never read at all. Every
    tag is exactly six lowercase alphanumerics, or the row would be counted as
    unaddressable instead of reaching the check it is here for.

    The animal rows are the point of the last four: a cat on camera_3 and a dog on
    camera_1 pin on sight, a cat on camera_4 is refused because that camera does
    not watch for animals, and a car is refused on both counts at once because it
    is not a label this suite tracks. No camera is armed in this fixture except the
    always-armed one, and the only camera_4 person row carries another camera's
    zone, so no person row here pins at all.
    """
    return [
        event("qual02", "camera_3", BASE_TIME + 500),                               # 0  no pin, off
        event("noclip", "camera_2", BASE_TIME + 490, snapshot=False),               # 1  no snapshot
        event("cat001", "camera_3", BASE_TIME + 480, label="cat"),                 # 2  animal, pins
        event("wrong2", "camera_4", BASE_TIME + 465, zones=["camera_1_restricted"]),  # 3 other zone
        event("nocam2", "camera_9", BASE_TIME + 460),                              # 4  no such camera
        event("kept02", "camera_1", BASE_TIME + 455, retain_indefinitely=True),     # 5  already pinned
        event("arch02", "camera_1", ANCIENT_TIME, retain_indefinitely=True),        # 6  behind the window
        *tied_group(),                                                            # 7-13  all off
        event("dog001", "camera_1", BASE_TIME + 445, label="dog"),                 # 14  animal, pins
        event("cat004", "camera_4", BASE_TIME + 440, label="cat"),                 # 15  wrong camera
        event("car001", "camera_2", BASE_TIME + 435, label="car"),                 # 16  untracked label
    ]


class FakeFrigate:
    """The upstream's observable behaviour, and nothing else.

    A read answers has_snapshot, labels, limit, sort, before and after, with no
    offset branch: any walk that sends one gets the newest page again. Both bounds
    are strict, and the newest first order is Frigate's sort=date_desc, not an
    order= direction of its own. A successful retain sets retain_indefinitely,
    exactly as Frigate does, so the next pass sees the previous pass's work. A
    successful delete removes the row, so a second delete of the same id answers
    404 the way the real one does; quiet_deletes and refused_deletes are the two
    ways a delete can come back 2xx-with-nothing-done or simply fail.
    """

    def __init__(self, events, refusals=None, quiet_acks=None, filtering=True, config="ok"):
        self.events = list(events)
        self.reads = []
        self.posts = []
        self.pinned = []
        self.deletes = []
        self.calls = []  # every (method, path), so a caller can prove it sent nothing
        self.post_bodies = []  # every retain body, so no un-pin can hide in one
        self.attempts = {}
        self.filtering = filtering
        # "ok" carries every camera's own zone, "no-zones" is a valid config with none of
        # them, "no-camera-2" is one missing area on one camera, and "error" is the route
        # failing outright.
        self.config = config
        self.config_reads = 0
        self.refusals = dict.fromkeys(refusals or ())
        self.quiet_acks = dict.fromkeys(quiet_acks or ())
        self.quiet_deletes = {}
        self.refused_deletes = {}

    def _one(self, query, key, default=None):
        return (query.get(key) or [default])[0]

    def _store(self, event_id):
        for candidate in self.events:
            if candidate["id"] == event_id:
                return candidate
        return None

    def _read(self, query):
        self.reads.append({key: value[0] for key, value in query.items()})
        rows = list(self.events)
        if self.filtering:
            wanted = {label.strip() for label in (self._one(query, "labels") or "").split(",")
                      if label.strip()}
            rows = [row for row in rows
                    if row.get("has_snapshot") is True
                    and (not wanted or row.get("label") in wanted)]
        rows.sort(key=lambda row: row["start_time"],
                  reverse=self._one(query, "sort", "date_desc") == "date_desc")
        before = self._one(query, "before")
        if before is not None:
            rows = [row for row in rows if row["start_time"] < float(before)]
        after = self._one(query, "after")
        if after is not None:
            rows = [row for row in rows if row["start_time"] > float(after)]
        return 200, json.dumps(rows[: int(self._one(query, "limit", 100))]).encode()

    def _delete(self, event_id):
        self.deletes.append(event_id)
        if event_id in self.refused_deletes:
            return self.refused_deletes[event_id], b'{"error": "delete failed"}'
        if event_id in self.quiet_deletes:
            # HTTP 200 that did not do the thing.
            return 200, b'{"success": false}'
        self.events = [row for row in self.events if row["id"] != event_id]
        return 200, b'{"success": true}'

    def handle(self, method, path, query, body=None):
        self.calls.append((method, path))
        if method == "GET" and path == "/api/config":
            # Read only, and the only route here the worker parses. It carries the
            # credential marker, so a reply that echoed any of it would be caught.
            self.config_reads += 1
            if self.config == "error":
                return 500, b'{"error": "unavailable"}'
            if self.config == "no-camera-2":
                missing = {"camera_2"}
            elif self.config == "no-zones":
                missing = {"camera_1", "camera_2", "camera_3", "camera_4"}
            else:
                missing = ()
            return 200, json.dumps(config_payload(missing)).encode()
        if method == "GET" and path == "/api/events":
            return self._read(query)
        if method == "GET" and path.startswith("/api/events/"):
            stored = self._store(path[len("/api/events/"):])
            if stored is None:
                return 404, b'{"error": "not found"}'
            return 200, json.dumps(stored).encode()
        if method == "POST" and path.startswith("/api/events/") and path.endswith("/retain"):
            event_id = path[len("/api/events/"):-len("/retain")]
            self.posts.append(event_id)
            self.post_bodies.append(body)
            attempts = self.attempts.get(event_id, 0) + 1
            self.attempts[event_id] = attempts
            # Both refusals are transient upstream conditions - a locked database
            # and a rolled-back transaction - so each one is refused on the first
            # attempt only and the next pass gets through.
            if event_id in self.refusals and attempts == 1:
                return self.refusals[event_id], b'{"error": "database is locked"}'
            if event_id in self.quiet_acks and attempts == 1:
                # HTTP 200 that did not do the thing.
                return 200, b'{"success": false}'
            stored = self._store(event_id)
            if stored is not None:
                stored["retain_indefinitely"] = True
            self.pinned.append(event_id)
            return 200, b'{"success": true}'
        if method == "DELETE" and path.startswith("/api/events/"):
            return self._delete(path[len("/api/events/"):])
        return 404, b'{"error": "not found"}'


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def _serve(self):
        parsed = urllib.parse.urlsplit(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length)) if length else None
        status, payload = self.server.fake.handle(
            self.command, parsed.path, urllib.parse.parse_qs(parsed.query), body
        )
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = do_DELETE = _serve

    def log_message(self, *args):
        pass


@contextlib.contextmanager
def serving(fake):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.fake = fake
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def run_retain(base, *extra, state_file=None):
    return subprocess.run(
        [sys.executable, str(HERE / "retain.py"), "--api-base", base, "--page-size", PAGE_SIZE,
         "--timeout", "5", *(["--state-file", str(state_file)] if state_file else []), *extra],
        capture_output=True, text=True, timeout=120,
    )


def armed_state(base, *cameras, at=None, folder=None):
    """The production EntryState, armed the way the gateway arms it: readiness read
    from the fake's own /api/config, then the named cameras armed and saved.

    Arming has no command-line option and deliberately no seam for one, so a pass that
    is armed is driven through the same object the gateway holds, writing one state file
    under the caller's own temporary folder. Nothing is restored into it: a second call
    reads the config again.
    """
    state = retain.EntryState(str(Path(folder) / "entry.json"))
    api = retain.Api(base, 5)
    now = time.time() if at is None else at
    ready = state.ready(api, now)
    assert all(ready.values()), f"the fake config left a camera not ready: {state.snapshot()}"
    assert state.apply({camera: True for camera in cameras}, now), \
        f"the arming could not be saved: {folder}"
    return state


def run_armed(base, state):
    """The real run_pass in this process, over HTTP to the fake, with that state."""
    return retain.run_pass(retain.Api(base, 5), int(PAGE_SIZE), 50, state, time.time())


def clean(result):
    """Nothing printed may carry a body, a URL with userinfo, or a traceback."""
    text = result.stdout + result.stderr
    for leak in ("Traceback", "rtsp://", "Authorization", "password"):
        assert leak not in text, f"{leak!r} reached the output:\n{text}"
    return text


def ids(fake, only=None):
    """The fixture's ids in sorted order, or the rows at the given fixture indices."""
    chosen = fake.events if only is None else [fake.events[number] for number in only]
    return sorted(row["id"] for row in chosen)


def cursors(fake):
    return [read.get("before") for read in fake.reads]


def limits(fake):
    return [int(read["limit"]) for read in fake.reads]


def floors(fake):
    return [read.get("after") for read in fake.reads]


def check_defaults():
    print("checking scripts/retain.py defaults")
    assert retain.API_BASE == "http://frigate:5000", f"default API base {retain.API_BASE!r}"
    assert retain.POLL_SECONDS == 30, f"default poll interval {retain.POLL_SECONDS!r}s"
    # Cameras 1-3 are armed by hand, camera_4 is always armed, and the mapped zone is
    # the restricted polygon on the two approaches and the existing full-frame area on
    # the two house views. No loitering zone anywhere: the 60 second full-frame rule is
    # gone, so there is no such name left to require.
    assert retain.ENTRY_CAMERAS == ("camera_1", "camera_2", "camera_3"), \
        f"manual entry cameras {retain.ENTRY_CAMERAS}"
    assert retain.ALWAYS_CAMERA == "camera_4", f"the always-armed camera is {retain.ALWAYS_CAMERA!r}"
    assert retain.ALWAYS_CAMERA not in retain.ENTRY_CAMERAS, \
        "the always-armed camera is also a manual one"
    assert retain.ENTRY_ZONE == MAPPED_ZONE, \
        f"the mapped zones are {retain.ENTRY_ZONE}, not the ones this suite judges in"
    assert retain.ENTRY_ZONE_PERSON_ONLY == ("camera_1", "camera_4"), \
        f"the person-only zones are {retain.ENTRY_ZONE_PERSON_ONLY}"
    assert retain.STATE_FILE == "/state/entry.json", f"the state file is {retain.STATE_FILE!r}"
    assert retain.MAX_STATE_BYTES == 4096, f"state ceiling {retain.MAX_STATE_BYTES!r}"
    assert retain.TARGET_LABEL == "person", f"target label {retain.TARGET_LABEL!r}"
    # The ten COCO 91 animal classes, in the labelmap's order, on cameras 1-3 only.
    assert retain.ANIMAL_LABELS == ANIMAL_LABELS, f"animal labels {retain.ANIMAL_LABELS}"
    assert retain.ANIMAL_CAMERAS == ("camera_1", "camera_2", "camera_3"), \
        f"animals must stay off camera_4: {retain.ANIMAL_CAMERAS}"
    assert retain.PINNED_LABELS == ("person",) + retain.ANIMAL_LABELS, \
        f"pinned labels {retain.PINNED_LABELS}"
    assert retain.LOOKBACK_SECONDS == 6 * 3600, \
        f"the lookback must stay the six hours Frigate keeps a snapshot {retain.LOOKBACK_SECONDS}s"
    # The listable/deletable set is still all four cameras: what an operator may
    # reach must not move when the retention policy does.
    assert retain.CAMERAS == ("camera_1", "camera_2", "camera_3", "camera_4"), \
        f"configured cameras {retain.CAMERAS}"
    print("ok  internal base http://frigate:5000, 30s cadence, four cameras listed and "
          "deletable, camera_4 always armed and cameras 1-3 armed by hand in /state/entry.json, "
          "the mapped zones are the two restricted polygons and the two existing house areas, "
          "one person label plus 10 COCO animal labels on cameras 1-3, six hour lookback")


def check_tied_walk():
    """Eleven events, seven of them sharing one start_time: every one is pinned."""
    fake = FakeFrigate(tied_fixture())
    with tempfile.TemporaryDirectory() as folder:
        with serving(fake) as base:
            # camera_1 armed before every row exists, and camera_4 needs no arming at
            # all, so without the one arming this walk would leave two rows unpinned
            # and prove nothing about paging.
            state = armed_state(base, "camera_1", at=BASE_TIME - 60, folder=folder)
            with contextlib.redirect_stdout(io.StringIO()) as out:
                assert run_armed(base, state) == 0
            assert "0 not addressable" in out.getvalue(), \
                f"a tied row was unreadable:\n{out.getvalue()}"
    missing = set(ids(fake)) - set(fake.pinned)
    assert not missing, f"the walk missed {sorted(missing)}, including the oldest tied row"
    assert len(fake.pinned) == 11, f"{len(fake.pinned)} pins for 11 events"
    print(f"ok  11 events over {len(fake.reads)} pages, every one pinned once "
          f"(limits {limits(fake)}, cursors {cursors(fake)})")
    return fake


def check_cursor_shape(fake):
    """The walk stepped by start_time and re-read the tied group instead of stepping over it."""
    assert all("offset" not in read for read in fake.reads), \
        f"an offset parameter was sent; Frigate 0.18 ignores it: {fake.reads}"
    assert cursors(fake)[0] is None, f"the first page must be uncursored: {fake.reads[0]}"
    expected_floor = retain.after_cutoff()
    for read in fake.reads:
        assert read["sort"] == "date_desc", f"a page was not start_time DESC: {read}"
        assert read["has_snapshot"] == "1" and read["labels"] == ",".join(retain.PINNED_LABELS), \
            f"query {read}"
        assert read.get("after") is not None, f"a page was read with no lookback floor: {read}"
        assert abs(float(read["after"]) - expected_floor) < 5, \
            f"the lookback floor moved between pages of one pass: {floors(fake)}"
    # The boundary is strict, so the cursor that keeps the tie is one float above it.
    tied_cursor = f"{math.nextafter(float(TIED), math.inf)}"
    page_sizes = limits(fake)
    assert page_sizes[0] == int(PAGE_SIZE) == page_sizes[1], f"pages did not start at {PAGE_SIZE}"
    assert cursors(fake).count(tied_cursor) >= 2, \
        f"the tied boundary was stepped over instead of re-read: {cursors(fake)}"
    assert any(size > int(PAGE_SIZE) for size, cursor in zip(page_sizes, cursors(fake))
               if cursor == tied_cursor), \
        f"the tied boundary did not widen the page: {fake.reads}"
    print(f"ok  no offset sent; every read start_time DESC (sort=date_desc) behind one six hour "
          f"floor; the 7-row tied group was re-read at the strict cursor with the page widened "
          f"{PAGE_SIZE} -> {max(page_sizes)}")


def check_qualification():
    """One event per decision on the native fields, and two pins that fail before they stick.

    Nothing is armed by hand here, so every person row in this fixture except the two
    camera_4 rows is a further negative: a person inside the area that camera watches pins
    nothing while it is off, and only the animals ever reach a POST.
    """
    fake = FakeFrigate(unfiltered_fixture(), filtering=False)
    qualifying = set(ids(fake, [2, 14]))
    refused, quiet = ids(fake, [2])[0], ids(fake, [14])[0]
    fake.refusals[refused] = 500
    fake.quiet_acks[quiet] = True
    with tempfile.TemporaryDirectory() as folder:
        state_file = Path(folder) / "entry.json"
        with serving(fake) as base:
            first = run_retain(base, "--once", state_file=state_file)
            text = clean(first)
            assert first.returncode == 1, f"a pass with two refused pins exited {first.returncode}:\n{text}"
            assert "2 pin(s) refused by Frigate" in text, f"the refusals were not reported:\n{text}"
            assert f"{refused} (HTTP 500)" in text, f"the HTTP status was not reported:\n{text}"
            assert f"{quiet} (HTTP 2xx with no success:true in the reply)" in text, \
                f"success:false was not reported as a refusal:\n{text}"
            assert "0 pinned, 0 already retained" in text, f"pass 1 pin summary:\n{text}"
            assert "camera_4 always armed, no manual camera armed" in text, \
                f"the armed state was not logged:\n{text}"
            assert ("1 without a snapshot, 1 not a person or a tracked animal, "
                    "1 an animal on a camera that does not watch for them, "
                    "9 while entry review was not armed, 0 before entry review was armed, "
                    "1 outside its own monitored area, "
                    "1 from no configured camera") in text, \
                f"pass 1 skip counts:\n{text}"
            # 16 of 17 rows: the retained row behind the six hour floor was never read.
            assert "16 unique event(s)" in text, f"the walk read rows it should not have:\n{text}"
            assert sorted(fake.pinned) == [], f"pinned {sorted(fake.pinned)} while both writes failed"
            assert len(fake.posts) == 2, f"{len(fake.posts)} pins attempted for 2 animal rows"
            assert len(fake.posts) == len(set(fake.posts)), f"an event was pinned twice: {fake.posts}"
            for never in ids(fake, [0, 1, 3, 4, 5, 6, 7, 9, 11, 12, 13, 15, 16]):
                assert never not in fake.posts, f"{never} was POSTed and should not have been"
            assert fake.deletes == [], f"the automated pass deleted {fake.deletes}"
            print("ok  a Home pass over an unfiltered upstream: nine person rows, including "
                  "one inside its own area and one already retained, never POSTed; so did a "
                  "snapshot-less row, a camera_4 row carrying another camera's zone, an unknown "
                  "camera and an untracked label; the row behind the six hour floor was never read")

            second = run_retain(base, "--once", state_file=state_file)
            text2 = clean(second)
            assert second.returncode == 0, f"pass 2 exited {second.returncode}:\n{text2}"
            assert second.stdout.count("pinned") == 1, f"pass 2 pinned more than its two retries:\n{text2}"
            assert "2 pinned" in text2 and "0 already retained" in text2, f"pass 2 summary:\n{text2}"
    assert len(fake.posts) == 4, f"{len(fake.posts)} pins in total, expected 2 then 2"
    assert set(map(json.dumps, fake.post_bodies)) == {'{"retain_indefinitely": true}'}, \
        f"a pin call carried something other than retain_indefinitely: {fake.post_bodies}"
    assert sorted(fake.pinned) == sorted(qualifying), \
        f"the pinned set {sorted(fake.pinned)} never reached every qualifying event"
    assert fake.deletes == [], f"a delete appeared after two automated passes: {fake.deletes}"
    print("ok  pass 2 pinned exactly the two rows that failed and exited 0, so a refused write "
          "cannot be reported as a completed one")
    print("ok  every pin carried retain_indefinitely:true and nothing else, so no automated "
          "pass can un-pin a record an earlier one kept")


def check_disarmed_is_the_default():
    """A --once pass with no saved arming is Home: it keeps camera_4 and nothing else,
    and it touches nothing on disk."""
    fake = FakeFrigate([
        event("just01", "camera_1", int(time.time()) - 60, zones=["camera_1_restricted"]),
        event("just04", "camera_4", int(time.time()) - 60, zones=["camera_4_restricted"]),
        event("just02", "camera_2", int(time.time()) - 60, zones=["camera_2_area"]),
    ])
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "entry.json"
        with serving(fake) as base:
            result = run_retain(base, "--once", state_file=path)
        text = clean(result)
        assert result.returncode == 0, f"exit {result.returncode}:\n{text}"
        assert "3 unique event(s)" in text, f"the candidate rows were never read:\n{text}"
        assert "0 not addressable" in text, f"a candidate row was unreadable:\n{text}"
        assert "camera_4 always armed, no manual camera armed" in text, \
            f"the always-armed camera was not reported:\n{text}"
        assert sorted(fake.posts) == sorted(ids(fake, [1])), \
            f"a Home pass kept {sorted(fake.posts)}, expected camera_4's person row alone"
        assert fake.deletes == [], f"a disarmed pass deleted {fake.deletes}"
        assert not path.exists(), f"a read-only pass wrote the state file: {path.read_text()}"
        fresh = retain.EntryState(str(path)).snapshot()
        assert fresh["camera_4"] == {"armed": True, "ready": False, "armed_since": None}, \
            f"a fresh installation did not come back with camera_4 always armed: {fresh}"
        for camera in ("camera_1", "camera_2", "camera_3"):
            assert not fresh[camera]["armed"] and fresh[camera]["armed_since"] is None, \
                f"a fresh installation was not Home on {camera}: {fresh}"
    print("ok  with no saved arming a --once pass is Home: camera_4's person row is kept with "
          "no arming, the house cameras' rows are not, the state file is not created, and a "
          "state built on the same path reads camera_4 always armed and nothing else armed")


def check_entry_rule():
    """Arming is the whole rule: only a person, only the zone that camera watches, and
    on cameras 1-3 only from the moment the arm happened. camera_4 has no moment."""
    armed_at = int(time.time()) - 5
    fake = FakeFrigate(entry_fixture(armed_at))
    new1, new2, new3, new4 = ids(fake, [0, 1, 2, 3])
    old1, old4 = ids(fake, [4])[0], ids(fake, [5])[0]
    wrong4, wrong2, loitered = ids(fake, [6, 7, 8])
    dog = ids(fake, [9])[0]
    with tempfile.TemporaryDirectory() as folder:
        with serving(fake) as base:
            state = armed_state(base, "camera_1", "camera_2", "camera_3", at=armed_at,
                                folder=folder)
            with contextlib.redirect_stdout(io.StringIO()) as out:
                assert run_armed(base, state) == 0
            text = out.getvalue()
            # Every negative below only means something if the walk actually read it.
            assert f"{len(fake.events)} unique event(s)" in text, \
                f"the walk did not read all ten rows:\n{text}"
            assert "0 not addressable" in text, f"a row was unreadable:\n{text}"
            assert sorted(fake.pinned) == sorted([new1, new2, new3, new4, old4, dog]), \
                f"pinned {sorted(fake.pinned)}, expected the three armed houses, both camera_4 " \
                "rows and the on-sight dog"
            for never in (old1, wrong4, wrong2, loitered):
                assert never not in fake.posts, f"{never} was POSTed and should not have been"
            assert fake.deletes == [], f"an armed pass deleted {fake.deletes}"
            print("ok  armed: a person inside the area that camera watches is pinned on "
                  "camera_2 and camera_3 exactly as it is on camera_1, while a person row on "
                  "an off camera pins nothing")
            print("ok  an entry that began before the arm is not pinned on cameras 1-3, so "
                  "arming cannot reach back into the six hours the pass reads, and the same "
                  "row on camera_4 is pinned because that camera has no arming moment")
            print("ok  another camera's zone and the removed 60 second camera_N_loitering zone "
                  "both pin nothing")
            print("ok  the area is behind the other gates: a dog carrying camera_3_area is "
                  "still pinned as an animal on sight, whatever the person's zone rule says")

            # Arming twice must not move the moment the first arming recorded.
            first_since = state.snapshot()["camera_1"]["armed_since"]
            assert state.apply({"camera_1": True}, time.time()), "a repeat arm did not save"
            assert state.snapshot()["camera_1"]["armed_since"] == first_since, \
                "re-arming moved the armed_since, so the boundary a pass uses moved with it"
            print("ok  arming an armed camera again is idempotent and keeps the original "
                  "armed_since, so a repeated click cannot widen what a pass may keep")

            # And a disarm stops new pins without touching what is already kept.
            assert state.apply({"camera_1": False}, time.time()), "a disarm did not save"
            fresh_rows = [event(f"lat{n:03d}", "camera_1", int(time.time()) - n,
                                zones=["camera_1_restricted"]) for n in range(1, 4)]
            fake.events.extend(fresh_rows)
            with contextlib.redirect_stdout(io.StringIO()) as out:
                assert run_armed(base, state) == 0
            text = out.getvalue()
            for row in fresh_rows:
                assert row["id"] not in fake.posts, f"{row['id']} pinned after a disarm"
            # The rows have to have been read to be proof of anything: a row the walk
            # could not address would pass the line above for the wrong reason.
            assert f"{len(fake.events)} unique event(s)" in text, \
                f"the three new entries were never read, so nothing above proves anything:\n{text}"
            assert "0 not addressable" in text, f"a new entry was unreadable:\n{text}"
            assert new1 in [row["id"] for row in fake.events
                            if row.get("retain_indefinitely") is True], \
                "a disarm cleared a flag that had already been set"
            print("ok  disarming stops new pins and never releases one already kept, so the old "
                  "pile from the removed 60 second rule is untouched")


def check_saved_arming_survives_a_new_process():
    """Two separate --once processes and one real file on disk: the second keeps what
    the first was told, and a file this cannot read is Home rather than Away."""
    moment = int(time.time()) - 120
    house = event("save02", "camera_2", moment + 11, zones=["camera_2_area"])
    area_4 = event("save04", "camera_4", moment + 12, zones=["camera_4_restricted"])
    fake = FakeFrigate([event("save01", "camera_1", moment + 10, zones=["camera_1_restricted"]),
                        house, area_4])
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "entry.json"
        with serving(fake) as base:
            assert run_retain(base, "--once", state_file=path).returncode == 0
            assert sorted(fake.pinned) == [area_4["id"]], \
                f"a Home pass kept {sorted(fake.pinned)}"
            assert not path.exists(), "a pass that writes nothing created the state file"

            # Armed through the production writer, exactly as the gateway arms it.
            armed_state(base, "camera_2", at=moment, folder=folder)
            saved = json.loads(path.read_text())
            assert set(saved["cameras"]) == {"camera_1", "camera_2", "camera_3"}, \
                f"the saved file names {sorted(saved['cameras'])}, and camera_4 is not in it"
            assert saved["cameras"]["camera_2"] == {"armed": True, "armed_since": float(moment)}, \
                f"the saved arming is {saved['cameras']['camera_2']}"
            reread = retain.EntryState(str(path)).snapshot()
            assert reread["camera_2"]["armed_since"] == float(moment) and \
                reread["camera_1"]["armed"] is False, \
                f"a fresh state did not read the file back: {reread}"
            assert run_retain(base, "--once", state_file=path).returncode == 0
            assert sorted(fake.pinned) == sorted([house["id"], area_4["id"]]), \
                f"a new process did not keep the saved arming: {sorted(fake.pinned)}"

            # Files this cannot read are Home, and camera_4 is armed whatever one of them
            # says. Each is written and read through the production constructor.
            late_house = event("late02", "camera_2", moment + 20, zones=["camera_2_area"])
            late_area_4 = event("late04", "camera_4", moment + 21, zones=["camera_4_restricted"])
            fake.events.extend([late_house, late_area_4])
            for label, text in (
                ("not json", "{not json"),
                ("a bare array", "[]"),
                ("a version of true", '{"version": true, "cameras": {}}'),
                ("an unknown version", '{"version": 2, "cameras": {}}'),
                ("a huge timestamp",
                 '{"version": 1, "cameras": {"camera_1": {"armed": true, "armed_since": 1e400},'
                 ' "camera_2": {"armed": false, "armed_since": null},'
                 ' "camera_3": {"armed": false, "armed_since": null}}}'),
                ("a string timestamp",
                 '{"version": 1, "cameras": {"camera_1": {"armed": true, "armed_since": "now"},'
                 ' "camera_2": {"armed": false, "armed_since": null},'
                 ' "camera_3": {"armed": false, "armed_since": null}}}'),
                ("camera_4 written in",
                 '{"version": 1, "cameras": {"camera_1": {"armed": false, "armed_since": null},'
                 ' "camera_2": {"armed": true, "armed_since": null},'
                 ' "camera_3": {"armed": false, "armed_since": null},'
                 ' "camera_4": {"armed": false, "armed_since": null}}}'),
                ("an oversized file", '{"pad": "' + "x" * (retain.MAX_STATE_BYTES + 64) + '"}'),
            ):
                path.write_text(text)
                broken = retain.EntryState(str(path))
                assert broken.mode() == "home", f"a file that is {label} was not Home"
                assert retain.STATE_UNREADABLE in broken.notice(), \
                    f"a file that is {label} was not reported to the dashboard"
                assert broken.snapshot()["camera_4"] == {"armed": True, "ready": False,
                                                         "armed_since": None}, \
                    f"a file that is {label} reached camera_4: {broken.snapshot()}"
                result = run_retain(base, "--once", state_file=path)
                assert result.returncode == 0, f"a pass over a file that is {label} failed:\n{clean(result)}"
            assert late_house["id"] not in fake.posts, \
                "a pass pinned a house camera the file that was there did not arm"
            assert late_area_4["id"] in fake.pinned, \
                "camera_4 stopped keeping people because the state file was unreadable"
    print("ok  two real processes and one real file: a saved arming is read back by the next "
          "process, and eight files this cannot read each come back Home with the dashboard told, "
          "while camera_4 keeps its person row through every one of them")


def check_state_file_failure_keeps_the_old_one():
    """A save that cannot land leaves the previous valid file whole, and nothing claims
    otherwise."""
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "entry.json"
        with contextlib.redirect_stdout(io.StringIO()) as out:
            state = retain.EntryState(str(path))
            assert state.apply({"camera_1": True, "camera_2": True}, 1790910000.0), \
                "the first arming did not save"
        first = path.read_text()
        assert json.loads(first)["cameras"]["camera_1"]["armed_since"] == 1790910000.0
        # A directory where the file should be: the write fails and there is nothing to
        # fall back to, which is the other end of the same boundary.
        blocked = Path(folder) / "blocked"
        blocked.mkdir()
        with contextlib.redirect_stdout(io.StringIO()):
            refused = retain.EntryState(str(blocked))
            assert not refused.apply({"camera_1": True}, 1790910999.0), \
                "a save into a directory reported success"
            assert not refused.apply({"camera_1": False}, 1790910999.0), \
                "a disarm whose save failed reported success"
        assert list(blocked.iterdir()) == [], \
            f"a failed save left something behind: {list(blocked.iterdir())}"
        assert refused.snapshot()["camera_1"]["armed"] is False, \
            "a save that failed changed the running state"
        # And a save that does land keeps the earlier moment rather than moving it.
        with contextlib.redirect_stdout(io.StringIO()):
            assert state.apply({"camera_1": False}, 1790912000.0)
        assert json.loads(path.read_text())["cameras"]["camera_1"] == {"armed": False,
                                                                        "armed_since": None}, \
            f"the file after a disarm is {path.read_text()}"
        assert json.loads(path.read_text())["cameras"]["camera_2"]["armed_since"] == 1790910000.0, \
            "disarming one camera moved another's arming moment"
        assert not path.with_name(path.name + ".new").exists(), "the scratch file was left behind"
    print("ok  a save into a directory is refused, changes no flag, writes nothing and leaves no "
          "scratch, while the earlier valid file keeps every moment it recorded")


def check_arming_needs_a_configured_zone():
    """A camera whose area the service does not report, and a config that cannot be read
    at all, both refuse to arm, pin nothing for people, and leave camera_4 armed."""
    for label, config in (("a valid config with no such area", "no-zones"),
                          ("a config route that fails", "error")):
        fake = FakeFrigate([event("just01", "camera_1", int(time.time()) - 60,
                                  zones=["camera_1_restricted"]),
                            event("just04", "camera_4", int(time.time()) - 60,
                                  zones=["camera_4_restricted"])], config=config)
        with tempfile.TemporaryDirectory() as folder:
            with serving(fake) as base:
                state = retain.EntryState(str(Path(folder) / "entry.json"))
                ready = state.ready(retain.Api(base, 5), time.time(), force=True)
                assert ready == {camera: False for camera in retain.CAMERAS}, \
                    f"{label} still reported ready: {ready}"
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    assert run_armed(base, state) == 0
                text = out.getvalue()
                assert "2 unique event(s)" in text and "0 not addressable" in text, \
                    f"the candidate rows were not read:\n{text}"
                assert fake.posts == [], f"an unready camera still pinned {fake.posts}"
                assert fake.config_reads >= 1, "readiness never read the config at all"
                assert state.snapshot()["camera_4"]["armed"] is True, \
                    "the always-armed camera followed the config"
        print(f"ok  {label}: every camera reads not ready, so nothing is pinned for people - "
              "including camera_4 - while camera_4's own armed flag is unaffected and the "
              "answer came from the camera service's own config")


def check_revalidating_keeps_the_setting():
    """A camera whose area stops being confirmed stops pinning and nothing else: the
    setting and the moment it was armed survive, and the pins resume by themselves."""
    armed_at = int(time.time()) - 5
    fake = FakeFrigate(entry_fixture(armed_at))
    manual = ids(fake, [0, 1, 2])
    old1, old4 = ids(fake, [4])[0], ids(fake, [5])[0]
    new4, dog = ids(fake, [3])[0], ids(fake, [9])[0]
    with tempfile.TemporaryDirectory() as folder:
        with serving(fake) as base:
            state = armed_state(base, "camera_1", "camera_2", "camera_3", at=armed_at,
                                folder=folder)
            assert state.guard("camera_1") == armed_at, "the arm did not take"
            fake.config = "no-zones"  # the area is gone from a config that still reads fine
            state.ready(retain.Api(base, 5), time.time(), force=True)
            assert state.guard("camera_1") is None, \
                "a camera that stopped being ready still handed out an arming ceiling"
            for camera in ("camera_1", "camera_2", "camera_3"):
                row = state.snapshot()[camera]
                assert row["armed"] and row["armed_since"] == armed_at and not row["ready"], \
                    f"the setting or its moment was lost while {camera} was not ready: {row}"
            with contextlib.redirect_stdout(io.StringIO()):
                assert run_armed(base, state) == 0
            assert fake.pinned == [dog], \
                f"pinned {sorted(fake.pinned)} after the area stopped being ready; only the " \
                "on-sight animal may pin"
            assert fake.deletes == [], f"a revalidation deleted {fake.deletes}"

            # The config comes back and the pins resume, against the moment the operator
            # armed rather than the moment it recovered.
            fake.config = "ok"
            state.ready(retain.Api(base, 5), time.time(), force=True)
            with contextlib.redirect_stdout(io.StringIO()):
                assert run_armed(base, state) == 0
            assert sorted(fake.pinned) == sorted(manual + [new4, old4, dog]), \
                f"pinned {sorted(fake.pinned)} after the area came back"
            assert old1 not in fake.posts, \
                "the arming moment moved, so a pass reached further back than it was armed for"
            print("ok  an area that stops being confirmed stops pins and keeps every setting and "
                  "its armed_since; the pins resume by themselves when it is reported again, "
                  "still measured from the moment the camera was armed, and nothing is deleted")


def check_animal_rule():
    """An animal on cameras 1-3 is pinned on sight; on camera_4 it is not pinned at all.

    The animal rows carry no arming behind them, so this proves the animal pin is
    its own rule and owes nothing to entry review.
    """
    events = [event(f"a{label[:3]}0{n}", camera, BASE_TIME + 500 - n, label=label, zones=[])
              for n, (label, camera) in enumerate((
                  ("dog", "camera_1"), ("cat", "camera_2"), ("cow", "camera_3"),
                  ("bird", "camera_4"), ("bear", "camera_4"), ("zebra", "camera_3"),
              ))]
    fake = FakeFrigate(events)
    pinned_on_sight = ids(fake, [0, 1, 2, 5])
    refused_camera = ids(fake, [3, 4])
    with tempfile.TemporaryDirectory() as folder:
        with serving(fake) as base:
            result = run_retain(base, "--once", state_file=Path(folder) / "entry.json")
            text = clean(result)
            assert result.returncode == 0, f"exit {result.returncode}:\n{text}"
            assert sorted(fake.pinned) == sorted(pinned_on_sight), \
                f"pinned {sorted(fake.pinned)}, expected {sorted(pinned_on_sight)}"
            for never in refused_camera:
                assert never not in fake.posts, f"{never} on camera_4 was POSTed"
            assert "2 an animal on a camera that does not watch for them" in text, \
                f"the camera-4 refusals were not counted:\n{text}"
            print("ok  every animal label on cameras 1-3 pins with no zone and no arming at all, "
                  "and the same labels on camera_4 are refused before any POST")
    # And the archive/delete gate agrees, or a pinned row could exist that the
    # operator can never find or remove.
    for row in events[:3]:
        assert retain.disqualification(row) is None, \
            f"a pinned animal row was not listable or deletable: {row['id']}"
    for row in events[3:5]:
        assert retain.disqualification(row) is not None, \
            f"a camera_4 animal row reached the archive or the delete path: {row['id']}"
    print("ok  the listing and deletion gate accepts the pinned animal rows and refuses "
          "the camera_4 ones, so nothing pinned is unreachable and nothing unpinned is listed")


def check_page_cap():
    """A walk that cannot reach the end must fail, not report a pass it did not run."""
    fake = FakeFrigate(tied_fixture() + [event(f"extra{n:02d}", "camera_1", BASE_TIME + 300 + n)
                                         for n in range(20)])
    with serving(fake) as base:
        result = run_retain(base, "--once", "--max-pages", "2")
        text = clean(result)
        assert result.returncode == 1, f"an unfinished walk exited {result.returncode}:\n{text}"
        assert "page cap stopped the walk short of the end of the list" in text, \
            f"the cap was not reported as a failure:\n{text}"
        assert "retrying on the next cycle" in text, f"the failure was not retryable:\n{text}"
        assert fake.posts == [], f"an unfinished pass pinned {fake.posts}"
        print("ok  a walk stopped by the page cap exits 1, pins nothing, and names the cap")


def check_cli_positivity():
    """A bound that cannot make progress is refused at the command line, not clamped."""
    fake = FakeFrigate(tied_fixture())
    refused_count = 0
    with serving(fake) as base:
        for option, values in (("--page-size", ("0", "-1")),
                               ("--max-pages", ("0", "-1")),
                               ("--interval", ("0", "-2.5")),
                               ("--timeout", ("0", "-2.5"))):
            for value in values:
                result = run_retain(base, "--once", option, value)
                text = clean(result)
                assert result.returncode == 2, \
                    f"{option} {value} exited {result.returncode} instead of refusing:\n{text}"
                refused_count += 1
        assert fake.reads == [] and fake.posts == [], \
            f"a refused bound still reached the upstream: {fake.reads} {fake.posts}"
    print(f"ok  {refused_count} non-positive bounds refused with exit 2, before any request")


def check_manual_delete():
    fake = FakeFrigate(tied_fixture())
    with serving(fake) as base:
        for bad in ("1790910000.123456-ABCDEF", "1790910000.123456-abc12", "1790910000.123456",
                    "../config/config.yml", "1790910000.123456-abc123/retain", ""):
            refused = run_retain(base, "--delete-reviewed", bad)
            assert refused.returncode == 2, f"--delete-reviewed {bad!r} exited {refused.returncode}"
            assert fake.deletes == [], f"--delete-reviewed {bad!r} reached the upstream"
        assert "refused" in clean(refused).lower(), "the refusal was not explained"
        good = ids(fake, [3])[0]
        deleted = run_retain(base, "--delete-reviewed", good)
        assert deleted.returncode == 0, f"--delete-reviewed {good} exited {deleted.returncode}"
        assert fake.deletes == [good], f"upstream saw {fake.deletes}"
        print("ok  6 non-native ids refused with exit 2 and no request; one native id deleted "
              "on request")


def check_unreachable(base):
    gone = run_retain(base, "--once")
    text = clean(gone)
    assert gone.returncode == 1, f"an unreachable Frigate exited {gone.returncode}:\n{text}"
    assert "retrying on the next cycle" in text, f"the failure was not retryable:\n{text}"
    print("ok  Frigate down: exit 1, one log line, no traceback, no request attempted")


def main():
    parser = argparse.ArgumentParser(description="retention worker boundary check")
    parser.parse_args()
    check_defaults()
    check_cursor_shape(check_tied_walk())
    check_disarmed_is_the_default()
    check_saved_arming_survives_a_new_process()
    check_state_file_failure_keeps_the_old_one()
    check_entry_rule()
    check_arming_needs_a_configured_zone()
    check_revalidating_keeps_the_setting()
    check_animal_rule()
    check_qualification()
    check_page_cap()
    check_cli_positivity()
    check_manual_delete()
    with serving(FakeFrigate(tied_fixture())) as base:
        pass  # the listener is closed again, so this base now refuses connections
    check_unreachable(base)
    print("\nPASS  every assertion held")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as err:
        print(f"\nFAIL  {err}")
        sys.exit(1)