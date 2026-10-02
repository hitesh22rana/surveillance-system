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
checks reachable. The single-event GET, the delete that removes the row and the
retain body are here for scripts/check_console_api.py, which drives the gateway
against this same imitation rather than a second one.

    python3 scripts/check_retention.py
"""

import argparse
import contextlib
import http.server
import json
import math
import subprocess
import sys
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


def event(tag, camera, start, zones=None, label="person", snapshot=True, **extra):
    """A Frigate-shaped event: native id, one label, a bool has_snapshot, a zones list."""
    payload = {
        "id": f"{start}.123456-{tag}",
        "camera": camera,
        "camera_name": camera,
        "start_time": float(start),
        "end_time": float(start) + 2.5,
        "label": label,
        "zones": [f"{camera}_loitering"] if zones is None else list(zones),
        "has_snapshot": snapshot,
        "snapshot": f"{camera}/snapshot.jpg" if snapshot else None,
        "type": "object",
        "false_positive": False,
    }
    payload.update(extra)
    return payload


def tied_group():
    """Seven events sharing one start_time: four pages at PAGE_SIZE 2."""
    return [event(f"tied{n:02d}", "camera_2", TIED) for n in range(1, 8)]


def tied_fixture():
    """Eleven events. The seven tied ones are the group a page boundary can split."""
    return [
        event("kilo01", "camera_1", BASE_TIME + 500),
        event("kilo02", "camera_4", BASE_TIME + 490),
        event("kilo03", "camera_3", BASE_TIME + 480),
        *tied_group(),
        event("old001", "camera_1", BASE_TIME + 400),
    ]


def zone_fixture():
    """Three person snapshots, each qualifying on camera, snapshot and label, so the
    zone is the only thing left to decide: one that merely entered the ordinary
    area zone, one that loitered in another camera's loitering zone, and one that
    loitered in its own."""
    return [
        event("areav1", "camera_1", BASE_TIME + 500, zones=["camera_1_area"]),
        event("loit01", "camera_2", BASE_TIME + 490, zones=["camera_1_loitering"]),
        event("loit02", "camera_3", BASE_TIME + 480, zones=["camera_3_area", "camera_3_loitering"]),
    ]


def unfiltered_fixture():
    """One event per decision the worker has to make for itself, plus the tied group.

    This upstream applies no filter of its own, so every row reaches the worker
    and the native fields decide: one of each disqualifier, one already retained,
    and one already retained but behind the lookback, so never read at all. Every
    tag is exactly six lowercase alphanumerics, or the row would be counted as
    unaddressable instead of reaching the check it is here for.

    The animal rows are the point of the last four: a cat on camera_3 and a dog on
    camera_1 pin with no zone at all, a cat on camera_4 is refused because that
    camera does not watch for animals, and a car is refused on both counts at once
    because it is not a label this suite tracks.
    """
    return [
        event("qual02", "camera_1", BASE_TIME + 500),                            # 0  pins
        event("noclip", "camera_2", BASE_TIME + 490, snapshot=False),               # 1  no snapshot
        event("cat001", "camera_3", BASE_TIME + 480, label="cat"),                 # 2  animal, pins
        event("wrong2", "camera_4", BASE_TIME + 465, zones=["camera_1_loitering"]),  # 3  other zone
        event("nocam2", "camera_9", BASE_TIME + 460),                              # 4  no such camera
        event("kept02", "camera_1", BASE_TIME + 455, retain_indefinitely=True),     # 5  already pinned
        event("arch02", "camera_1", ANCIENT_TIME, retain_indefinitely=True),        # 6  behind the window
        *tied_group(),                                                            # 7-13  all pin
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

    def __init__(self, events, refusals=None, quiet_acks=None, filtering=True):
        self.events = list(events)
        self.reads = []
        self.posts = []
        self.pinned = []
        self.deletes = []
        self.calls = []  # every (method, path), so a caller can prove it sent nothing
        self.post_bodies = []  # every retain body, so no un-pin can hide in one
        self.attempts = {}
        self.filtering = filtering
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
            # Both refusals are transient upstream conditions — a locked database
            # and a rolled-back transaction — so each one is refused on the first
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


def run_retain(base, *extra):
    return subprocess.run(
        [sys.executable, str(HERE / "retain.py"), "--api-base", base, "--page-size", PAGE_SIZE,
         "--timeout", "5", *extra],
        capture_output=True, text=True, timeout=120,
    )


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
    assert set(retain.REQUIRED_ZONE.values()) == {f"camera_{n}_loitering" for n in range(1, 5)}, \
        f"required zones {sorted(retain.REQUIRED_ZONE.values())}"
    assert retain.TARGET_LABEL == "person", f"target label {retain.TARGET_LABEL!r}"
    # The ten COCO 91 animal classes, in the labelmap's order, on cameras 1-3 only.
    assert retain.ANIMAL_LABELS == (
        "bird", "cat", "dog", "horse", "sheep", "cow",
        "elephant", "bear", "zebra", "giraffe",
    ), f"animal labels {retain.ANIMAL_LABELS}"
    assert retain.ANIMAL_CAMERAS == ("camera_1", "camera_2", "camera_3"), \
        f"animals must stay off camera_4: {retain.ANIMAL_CAMERAS}"
    assert retain.PINNED_LABELS == ("person",) + retain.ANIMAL_LABELS, \
        f"pinned labels {retain.PINNED_LABELS}"
    # camera_4 must not be reachable through any animal label, or the exclusion is
    # only a convention.
    assert not set(retain.ANIMAL_CAMERAS) & {"camera_4"}, \
        f"camera_4 watches for animals: {retain.ANIMAL_CAMERAS}"
    assert retain.LOOKBACK_SECONDS == 6 * 3600, \
        f"the lookback must stay the six hours Frigate keeps a snapshot {retain.LOOKBACK_SECONDS}s"
    print("ok  internal base http://frigate:5000, 30s cadence, four camera_N_loitering zones, "
          "one person label plus 10 COCO animal labels on cameras 1-3, six hour lookback")


def check_tied_walk():
    """Eleven events, seven of them sharing one start_time: every one is pinned."""
    fake = FakeFrigate(tied_fixture())
    with serving(fake) as base:
        result = run_retain(base, "--once")
        text = clean(result)
        assert result.returncode == 0, f"exit {result.returncode}:\n{text}"
        assert "11 unique event(s)" in text, f"the walk did not read all eleven rows:\n{text}"
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
    """One event per decision on the native fields, and two pins that fail before they stick."""
    fake = FakeFrigate(unfiltered_fixture(), filtering=False)
    qualifying = set(ids(fake, [0, 2, 14, *range(7, 14)]))
    refused, quiet = ids(fake, [8])[0], ids(fake, [10])[0]
    fake.refusals[refused] = 500
    fake.quiet_acks[quiet] = True
    with serving(fake) as base:
        first = run_retain(base, "--once")
        text = clean(first)
        assert first.returncode == 1, f"a pass with two refused pins exited {first.returncode}:\n{text}"
        assert "2 pin(s) refused by Frigate" in text, f"the refusals were not reported:\n{text}"
        assert f"{refused} (HTTP 500)" in text, f"the HTTP status was not reported:\n{text}"
        assert f"{quiet} (HTTP 2xx with no success:true in the reply)" in text, \
            f"success:false was not reported as a refusal:\n{text}"
        assert "8 pinned, 1 already retained" in text, f"pass 1 pin summary:\n{text}"
        assert ("1 without a snapshot, 1 not a person or a tracked animal, "
                "1 an animal on a camera that does not watch for them, "
                "1 outside its own loitering zone, 1 from no configured camera") in text, \
            f"pass 1 skip counts:\n{text}"
        # 16 of 17 rows: the retained row behind the six hour floor was never read.
        assert "16 unique event(s)" in text, f"the walk read rows it should not have:\n{text}"
        assert sorted(fake.pinned) == sorted(qualifying - {refused, quiet}), \
            f"pinned {sorted(fake.pinned)}"
        assert len(fake.posts) == 10, f"{len(fake.posts)} pins attempted for 10 qualifying rows"
        assert len(fake.posts) == len(set(fake.posts)), f"an event was pinned twice: {fake.posts}"
        for never in ids(fake, [1, 3, 4, 5, 6, 15, 16]):
            assert never not in fake.posts, f"{never} was POSTed and should not have been"
        assert fake.deletes == [], f"the automated pass deleted {fake.deletes}"
        print("ok  an unfiltered upstream: an untracked label, a snapshot-less row, a cat on "
              "camera_4, a row outside its own zone, an unknown camera and an already-retained "
              "row never POSTed, and the row behind the six hour floor was never read")

        second = run_retain(base, "--once")
        text2 = clean(second)
        assert second.returncode == 0, f"pass 2 exited {second.returncode}:\n{text2}"
        assert second.stdout.count("pinned") == 1, f"pass 2 pinned more than its two retries:\n{text2}"
        assert "2 pinned" in text2 and "9 already retained" in text2, f"pass 2 summary:\n{text2}"
        assert len(fake.posts) == 12, f"{len(fake.posts)} pins in total, expected 10 then 2"
        assert set(map(json.dumps, fake.post_bodies)) == {'{"retain_indefinitely": true}'}, \
            f"a pin call carried something other than retain_indefinitely: {fake.post_bodies}"
        assert sorted(fake.pinned) == sorted(qualifying), \
            f"the pinned set {sorted(fake.pinned)} never reached every qualifying event"
        assert fake.deletes == [], f"a delete appeared after two automated passes: {fake.deletes}"
        print("ok  pass 2 pinned exactly the two rows that failed and exited 0, so a refused write "
              "cannot be reported as a completed one")
        print("ok  every pin carried retain_indefinitely:true and nothing else, so no automated "
              "pass can un-pin a record an earlier one kept")


def check_zone_rule():
    """Being on camera is not evidence. Only the loitering zone pins."""
    fake = FakeFrigate(zone_fixture())
    ordinary, foreign_zone, loitered = ids(fake, [0])[0], ids(fake, [1])[0], ids(fake, [2])[0]
    with serving(fake) as base:
        result = run_retain(base, "--once")
        text = clean(result)
        assert result.returncode == 0, f"exit {result.returncode}:\n{text}"
        assert fake.pinned == [loitered], \
            f"pinned {fake.pinned}, not only the event that loitered in its own zone"
        assert ordinary not in fake.posts, \
            "a person who merely entered the ordinary area zone was pinned"
        assert foreign_zone not in fake.posts, \
            "an event loitering in another camera's zone was pinned"
        assert "2 outside its own loitering zone" in text, f"the two skips were not counted:\n{text}"
        print("ok  a person who entered only camera_N_area is not pinned; the same kind of "
              "event in camera_N_loitering is, and another camera's loitering zone is not")
        second = run_retain(base, "--once")
        assert second.returncode == 0, f"exit {second.returncode}:\n{clean(second)}"
        assert fake.pinned == [loitered], "the pinned event was pinned a second time"
        print("ok  a second pass pins nothing again, so a loitering event is pinned once")


def check_animal_rule():
    """An animal on cameras 1-3 is pinned on sight; on camera_4 it is not pinned at all.

    The animal rows carry no zone, so this proves the pin is not the loitering rule
    wearing an animal's label: without the on-sight branch they would be skipped.
    """
    events = [event(f"a{label[:3]}0{n}", camera, BASE_TIME + 500 - n, label=label)
              for n, (label, camera) in enumerate((
                  ("dog", "camera_1"), ("cat", "camera_2"), ("cow", "camera_3"),
                  ("bird", "camera_4"), ("bear", "camera_4"), ("zebra", "camera_3"),
              ))]
    fake = FakeFrigate(events)
    pinned_on_sight = ids(fake, [0, 1, 2, 5])
    refused_camera = ids(fake, [3, 4])
    assert not any(retain.pin_disqualification(row) is None for row in events[3:5]), \
        "an animal on camera_4 qualified"
    with serving(fake) as base:
        result = run_retain(base, "--once")
        text = clean(result)
        assert result.returncode == 0, f"exit {result.returncode}:\n{text}"
        assert sorted(fake.pinned) == sorted(pinned_on_sight), \
            f"pinned {sorted(fake.pinned)}, expected {sorted(pinned_on_sight)}"
        for never in refused_camera:
            assert never not in fake.posts, f"{never} on camera_4 was POSTed"
        assert "2 an animal on a camera that does not watch for them" in text, \
            f"the camera-4 refusals were not counted:\n{text}"
        print("ok  every animal label on cameras 1-3 pins with no zone at all, and the same "
              "labels on camera_4 are refused before any POST")
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
    check_zone_rule()
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