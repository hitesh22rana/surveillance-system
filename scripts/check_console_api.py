#!/usr/bin/env python3
"""HTTP contract check for the retention worker's deletion gateway (stdlib only).

retain.py --serve is the only write path in this project, so this drives the real
gateway over HTTP exactly as the dashboard's browser drives it: Host, Origin,
Content-Type and X-Surveillance-Action written by hand, against a fake Frigate on
loopback. It adds no seam to production code - --api-base and --listen are the
operational settings compose and a test pass on the command line, and everything
else the gateway does has a default.

Two habits make the assertions mean something. Every refusal case sends a body
that would otherwise succeed, and then asserts the upstream was never called at
all, so a missing guard fails as a data-loss bug rather than as a status code.
Every walk and every page cap is inherited from the fake in check_retention.py,
which imitates Frigate 0.18 EventsQueryParams: no offset field, strict before and
after, the singular label on the row.

Nothing here touches real footage. The fake is the only upstream.

    python3 scripts/check_console_api.py
"""

import contextlib
import http.client
import json
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import retain  # noqa: E402  (path set above, so the production module is the one under test)
from check_retention import (  # noqa: E402  (path set above, so the fake is the one under test)
    CREDENTIAL_MARKER,
    FakeFrigate,
    event as retain_event,
    event,
    serving,
)

HOST = "127.0.0.1:8088"  # where compose publishes the dashboard
BASE_TIME = int(time.time()) - 6 * 3600  # six hours back: already past the six-hour window
ARCHIVE = 58  # eligible person snapshots, deeper than the newest 50 a dashboard shows
PAGE_SIZE = 7  # forces the walk past one page for a tiny fixture
KEPT = 27  # pre-flagged rows in the review fixture: more than one page of 25, so kept paging is real


def console_fixture():
    """A deep archive of this suite's person snapshots, plus one row of every kind
    the deletion path has to refuse. Every tag is six lowercase alphanumerics, so
    every row is addressable by a native id and none is skipped as unaddressable.

    The animal rows carry the new rule on both sides: a cat on camera_3 is a saved
    snapshot like any other and must delete, while a cat on camera_4 is refused,
    because camera_4 watches for people only.
    """
    rows = [event(f"a{n:05d}", f"camera_{n % 4 + 1}", BASE_TIME + n) for n in range(ARCHIVE)]
    rows += [
        event("act001", "camera_1", BASE_TIME + ARCHIVE, end_time=None),  # never ended
        event("areav1", "camera_2", BASE_TIME + ARCHIVE + 1, zones=["camera_2_area"]),
        event("outsid", "camera_9", BASE_TIME + ARCHIVE + 2, zones=["camera_9_loitering"]),
        event("cat001", "camera_3", BASE_TIME + ARCHIVE + 3, label="cat"),   # eligible
        event("dog004", "camera_4", BASE_TIME + ARCHIVE + 4, label="dog"),   # refused
        event("noclip", "camera_4", BASE_TIME + ARCHIVE + 5, snapshot=False),
    ]
    return rows


def review_fixture():
    """The console archive plus the rows only the kept flag can tell apart.

    Every row above is unpinned, so on their own they are one undifferentiated pile
    and nothing could prove a kept-only read is really narrowed. What the filter has
    to get right is the difference between a row the camera service flagged and a row
    that merely looks like it should be, so this adds both, and adds them at the new
    end so the flagged set is more than one page and a filter applied after the page
    was cut could not have produced it:

      - KEPT already-flagged rows on cameras 1 to 3;
      - "legacy", a flagged person row carrying no watched zone at all, which reads
        "Previously kept" on the page and is still a kept record;
      - "kcat01", a flagged animal on camera_3, which the flag carries exactly as it
        carries a person row and which no class or zone test would find on its own;
      - "loose1", a person inside its own watched zone that was never flagged, so it
        is not kept however much it resembles the rule that keeps one.

    The pre-flagged rows sit on cameras 1 to 3, which are Home in every run here, so
    nothing here depends on what the poll decides. camera_4 is left to the poll: it is
    armed from startup, so its rows are flagged while this check runs, and the kept
    view has to follow that flag just as it follows the ones written here.
    """
    rows = console_fixture()
    rows += [event(f"kept{n:02d}", f"camera_{n % 3 + 1}", BASE_TIME + 1000 + n,
                   retain_indefinitely=True) for n in range(KEPT)]
    rows += [
        event("legacy", "camera_2", BASE_TIME + 1200, zones=[], retain_indefinitely=True),
        event("kcat01", "camera_3", BASE_TIME + 1201, label="cat", retain_indefinitely=True),
        event("loose1", "camera_1", BASE_TIME + 1202),
    ]
    return rows


def newer_row(cutoff):
    """An event that started after the cutoff the summary named. int(), because the
    native id embeds the timestamp and two dots is not a Frigate id."""
    return event("newer1", "camera_1", int(cutoff) + 60)


def ineligible(fake, tag):
    for row in fake.events:
        if row["id"].endswith(f"-{tag}"):
            return row["id"]
    raise AssertionError(f"no fixture row tagged {tag}")


# Restated here and deliberately NOT imported from retain.py, which is the whole
# point of these two helpers: if the worker's policy and this check ever drift
# apart, the check fails instead of agreeing with whatever the code now says.
#
# A row is eligible when it is a configured camera's own snapshot-bearing event
# whose label this suite keeps: a person on any camera, or one of the ten COCO 91
# animal classes on camera_1, camera_2 or camera_3. camera_4 watches for people
# only, so an animal label there is not eligible however it reached the upstream.
ANIMAL_LABELS = (
    "bird", "cat", "dog", "horse", "sheep", "cow",
    "elephant", "bear", "zebra", "giraffe",
)
ANIMAL_CAMERAS = ("camera_1", "camera_2", "camera_3")


def eligible_row(row):
    """True for a row this suite's archive, count and delete paths must all accept."""
    return (row["camera"] in retain.CAMERAS
            and row.get("has_snapshot") is True
            and (row.get("label") == "person"
                 or (row.get("label") in ANIMAL_LABELS and row["camera"] in ANIMAL_CAMERAS)))


def eligible_ids(fake):
    """The rows the policy counts, restated here and deliberately not imported: a
    configured camera's own person-or-animal event that carries a snapshot, however old."""
    return sorted(row["id"] for row in fake.events if eligible_row(row))


def kept_row(row):
    """A row the kept-only read must carry: eligible on its own terms, and flagged kept
    by the camera service. The flag is the entire contract of that filter, so it is
    restated here the way eligible_row is rather than imported, and it is the flag
    alone - no label, no zone, no arming state."""
    return eligible_row(row) and row.get("retain_indefinitely") is True


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@contextlib.contextmanager
def gateway(base, interval=60, page_size=PAGE_SIZE, max_pages=50, state_file=None):
    """The real worker in --serve, on loopback, with every option an operational one.

    state_file is the production --state-file, pointed at the caller's own temporary
    directory so a check never touches the /state volume and never inherits whatever
    another run left in it.
    """
    port = free_port()
    process = subprocess.Popen(
        [sys.executable, str(HERE / "retain.py"), "--serve", "--api-base", base,
         "--listen", f"127.0.0.1:{port}", "--interval", str(interval),
         "--page-size", str(page_size), "--max-pages", str(max_pages), "--timeout", "5",
         *([ "--state-file", str(state_file)] if state_file else [])],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    output = []
    pump = threading.Thread(target=lambda: output.extend(iter(process.stdout.readline, "")),
                            daemon=True)
    pump.start()
    deadline = time.time() + 20
    try:
        while True:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    break
            except OSError:
                if time.time() > deadline:
                    raise AssertionError(f"nothing listened on {port}:\n{''.join(output)}")
                time.sleep(0.05)
        # The first poll pass owns the upstream until it runs to its end: it reads pages
        # and POSTs pins of its own throughout. Every case below asserts that the upstream
        # saw no further call, so it cannot capture a baseline until the pass that is
        # still writing has logged its own end - waiting on its first read would take the
        # baseline in the middle of a walk that goes on to post pins.
        while not pass_finished(output):
            if time.time() > deadline:
                raise AssertionError(f"the first poll pass never finished:\n{''.join(output)}")
            time.sleep(0.05)
        yield port, output
    finally:
        process.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=10)
        if process.poll() is None:
            process.kill()
        pump.join(timeout=5)


def pass_finished(output):
    """True once a poll pass has run to its end, whichever way it ended.

    A pass that completes logs its scan and pin counts; one that could not logs that it
    was skipped. Both lines come after that pass's last write, so either one means the
    poll is back in its sleep and the upstream is quiet.
    """
    text = "".join(output)
    return "retain: pin:" in text or "pass skipped:" in text


def call(port, method, path, body=None, raw=None, host=HOST, origin=HOST,
         content_type="application/json", action="delete", extra=None):
    """One request with every header written by hand. http.client is the only way to
    send a Host that is not this machine's own address, and the Host is half of what
    this gateway checks."""
    payload = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    headers = {}
    if host is not None:
        headers["Host"] = host
    if origin is not None:
        headers["Origin"] = origin if origin.startswith("http") else f"http://{origin}"
    if payload is not None and content_type is not None:
        headers["Content-Type"] = content_type
    if action is not None:
        headers["X-Surveillance-Action"] = action
    headers.update(extra or {})
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    try:
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        return response.status, {key.lower(): value for key, value in response.getheaders()}, \
            response.read()
    finally:
        connection.close()


def reply(port, path=retain.SUMMARY_PATH):
    status, _headers, body = call(port, "GET", path, origin=None)
    assert status == 200, f"summary answered HTTP {status}: {body!r}"
    return json.loads(body)


def leaks(output):
    """Nothing the gateway printed may carry a body, a URL with userinfo, or a
    traceback."""
    text = "".join(output)
    for leak in ("Traceback", "rtsp://", "Authorization", "password", "frigate:5000"):
        assert leak not in text, f"{leak!r} reached the gateway's output:\n{text}"
    return text


def check_defaults():
    print("checking the gateway's own bounds")
    assert retain.GATEWAY_LISTEN == "0.0.0.0:8090", f"default listen {retain.GATEWAY_LISTEN!r}"
    assert retain.ALLOWED_HOSTS == ("localhost:8088", "127.0.0.1:8088"), \
        f"allowed hosts {retain.ALLOWED_HOSTS!r}"
    assert retain.MAX_DELETE_IDS == 1000, f"id ceiling {retain.MAX_DELETE_IDS!r}"
    assert retain.MAX_BODY_BYTES == 64 * 1024, f"body ceiling {retain.MAX_BODY_BYTES!r}"
    assert (retain.SUMMARY_PATH, retain.EVENTS_PATH, retain.DELETE_PATH, retain.ENTRY_PATH) == \
        ("/api/snapshots/summary", "/api/snapshots/events", "/api/snapshots/delete",
         "/api/security/entry"), \
        "the four endpoint paths moved"
    assert retain.ENTRY_CAMERAS == ("camera_1", "camera_2", "camera_3"), \
        f"the cameras a person may arm {retain.ENTRY_CAMERAS!r}"
    assert retain.ALWAYS_CAMERA == "camera_4", \
        f"the always-armed camera is {retain.ALWAYS_CAMERA!r}"
    assert retain.STATE_FILE == "/state/entry.json", \
        f"the saved arming is at {retain.STATE_FILE!r}"
    assert retain.EVENTS_PAGE_SIZE == 25, f"the archive page size is {retain.EVENTS_PAGE_SIZE!r}"
    assert retain.CAMERA_FILTERS == ("all", "camera_1", "camera_2", "camera_3", "camera_4"), \
        f"the camera filters {retain.CAMERA_FILTERS!r}"
    assert retain.REVIEW_FILTERS == ("all", "kept"), \
        f"the review filters {retain.REVIEW_FILTERS!r}"
    print("ok  0.0.0.0:8090 by default, four exact paths, both dashboard Hosts, "
          "at most 1000 ids and 64 KiB per request, 25 rows per archive page, four "
          "camera filters plus all, an all/kept review scope, entry review armed by "
          "hand on cameras 1-3 with camera_4 always armed and saved in /state/entry.json")


def check_guards():
    """Every shape that is not the dashboard's own request is refused, and reaches
    nothing upstream. Each case below sends a body that would otherwise delete a row,
    so a missing guard shows up as a deleted fixture row."""
    fake = FakeFrigate(console_fixture())
    body = {"ids": [ineligible(fake, "act001")]}
    with serving(fake) as base:
        with gateway(base) as (port, output):
            positive = call(port, "GET", retain.SUMMARY_PATH, origin=None)
            assert positive[0] == 200, f"a plain summary read answered {positive[0]}"
            before = len(fake.calls)
            cases = [
                ("host is another port", dict(method="POST", path=retain.DELETE_PATH, body=body,
                                              host="127.0.0.1:8089"), 403),
                ("host is a name", dict(method="POST", path=retain.DELETE_PATH, body=body,
                                       host="surveillance.example"), 403),
                ("host absent", dict(method="POST", path=retain.DELETE_PATH, body=body,
                                     host=None), 403),
                ("origin is another site", dict(method="POST", path=retain.DELETE_PATH, body=body,
                                                origin="http://evil.example"), 403),
                ("origin absent", dict(method="POST", path=retain.DELETE_PATH, body=body,
                                       origin=None), 403),
                ("origin over https", dict(method="POST", path=retain.DELETE_PATH, body=body,
                                           origin=f"https://{HOST}"), 403),
                ("content type is text", dict(method="POST", path=retain.DELETE_PATH, body=body,
                                             content_type="text/plain"), 415),
                ("content type is a form", dict(method="POST", path=retain.DELETE_PATH, body=body,
                                                content_type="application/x-www-form-urlencoded"), 415),
                ("content type absent", dict(method="POST", path=retain.DELETE_PATH, body=body,
                                             content_type=None), 415),
                ("action header absent", dict(method="POST", path=retain.DELETE_PATH, body=body,
                                              action=None), 403),
                ("action header is something else", dict(method="POST", path=retain.DELETE_PATH,
                                                         body=body, action="delete-everything"), 403),
                ("preflight", dict(method="OPTIONS", path=retain.DELETE_PATH, origin=None), 405),
                ("read the delete path", dict(method="GET", path=retain.DELETE_PATH,
                                             origin=None), 405),
                ("post the summary path", dict(method="POST", path=retain.SUMMARY_PATH, body=body), 405),
                ("delete the summary path", dict(method="DELETE", path=retain.SUMMARY_PATH,
                                                 origin=None), 405),
                ("put the delete path", dict(method="PUT", path=retain.DELETE_PATH, body=body), 405),
                ("a config path", dict(method="GET", path="/api/config", origin=None), 404),
                ("a traversal past the delete path",
                 dict(method="POST", path="/api/snapshots/delete/../../events", body=body), 404),
                ("a suffix on the summary path",
                 dict(method="GET", path="/api/snapshots/summary/1", origin=None), 404),
                ("post the events path", dict(method="POST", path=retain.EVENTS_PATH,
                                              body=body), 405),
                ("delete the events path", dict(method="DELETE", path=retain.EVENTS_PATH,
                                                 origin=None), 405),
                ("a preflight on the events path",
                 dict(method="OPTIONS", path=retain.EVENTS_PATH,
                      origin=None), 405),
                ("a foreign host on the events path",
                 dict(method="GET", path=retain.EVENTS_PATH, origin=None,
                      host="127.0.0.1:8089"), 403),
                ("a suffix on the events path",
                 dict(method="GET", path="/api/snapshots/events/1", origin=None), 404),
            ]
            for name, kwargs, expected in cases:
                status, headers, body_bytes = call(port, **kwargs)
                assert status == expected, f"{name} answered HTTP {status}, expected {expected}"
                assert len(fake.calls) == before, \
                    f"{name} reached Frigate: {fake.calls[before:]}"
                assert "access-control-allow-origin" not in headers, \
                    f"{name} answered a CORS header: {sorted(headers)}"
                assert headers.get("content-type") == "application/json", \
                    f"{name} answered {headers.get('content-type')!r}, not JSON"
                assert b"traceback" not in body_bytes.lower(), f"{name} leaked a traceback"
            assert fake.deletes == [], f"a refused request deleted {fake.deletes}"
            text = leaks(output)
            assert "-> 403" in text and "-> 415" in text and "-> 404" in text, \
                f"the gateway did not log its refusals:\n{text}"
            print(f"ok  {len(cases)} refused shapes (wrong Host, no Origin, foreign Origin, "
                  f"https Origin, wrong or absent Content-Type, absent or wrong action header, "
                  f"OPTIONS, a write or a foreign Host offered to either read path, "
                  f"/api/config, a traversal, a suffix): "
                  f"every one left the upstream untouched")


def check_bodies():
    """A body is fully validated before a single request: the right status, and the
    upstream never hears about it."""
    fake = FakeFrigate(console_fixture())
    good = ineligible(fake, "act001")
    with serving(fake) as base:
        with gateway(base) as (port, output):
            before = len(fake.calls)
            padded = json.dumps({"ids": [good], "reason": "x" * (retain.MAX_BODY_BYTES + 64)})
            cases = [
                ("ids is not a list", {"ids": "nope"}, 400),
                ("ids is empty", {"ids": []}, 400),
                ("an id is uppercase", {"ids": ["1790910000.123456-ABCDEF"]}, 400),
                ("an id is a traversal", {"ids": ["../config/config.yml"]}, 400),
                ("an id carries a path", {"ids": ["1790910000.123456-abc123/retain"]}, 400),
                ("an id is not a string", {"ids": [1790910000.123456]}, 400),
                ("an id has a trailing newline", {"ids": ["1790910000.123456-abc123\n"]}, 400),
                ("too many ids", {"ids": [f"{BASE_TIME}.123456-m{n:05d}"
                                          for n in range(retain.MAX_DELETE_IDS + 1)]}, 400),
                ("both modes at once", {"ids": [good], "all": True, "before": BASE_TIME}, 400),
                ("neither mode", {}, 400),
                ("all without before", {"all": True}, 400),
                ("before without all", {"before": BASE_TIME}, 400),
                ("all is false", {"all": False, "before": BASE_TIME}, 400),
                ("all is a string", {"all": "true", "before": BASE_TIME}, 400),
                ("before is a string", {"all": True, "before": str(BASE_TIME)}, 400),
                ("before is a boolean", {"all": True, "before": True}, 400),
                ("before is negative", {"all": True, "before": -5}, 400),
                ("before is in the future", {"all": True, "before": time.time() + 3600}, 400),
                ("an unexpected key", {"ids": [good], "reason": "cleanup"}, 400),
            ]
            for name, body, expected in cases:
                status, _headers, _body = call(port, "POST", retain.DELETE_PATH, body=body)
                assert status == expected, f"{name} answered HTTP {status}, expected {expected}"
                assert len(fake.calls) == before, \
                    f"{name} reached Frigate: {fake.calls[before:]}"
            raws = [
                ("the body is an array", b"[1, 2, 3]", 400),
                ("the body is a string", b'"nope"', 400),
                ("the body is not JSON", b"{", 400),
                ("before is infinite", b'{"all": true, "before": 1e400}', 400),
                ("before is NaN", b'{"all": true, "before": NaN}', 400),
                ("the body is over the limit", padded.encode(), 413),
            ]
            for name, raw, expected in raws:
                status, _headers, _body = call(port, "POST", retain.DELETE_PATH, raw=raw)
                assert status == expected, f"{name} answered HTTP {status}, expected {expected}"
                assert len(fake.calls) == before, \
                    f"{name} reached Frigate: {fake.calls[before:]}"
            assert fake.deletes == [], f"a refused body deleted {fake.deletes}"
            leaks(output)
            print(f"ok  {len(cases) + len(raws)} malformed or ambiguous bodies refused with "
                  f"HTTP 400/413, every one before a request was sent upstream")


def check_summary():
    """The count reaches past the newest 50, and says nothing it should not."""
    fake = FakeFrigate(console_fixture())
    with serving(fake) as base:
        with gateway(base) as (port, output):
            first = len(fake.reads)
            status, headers, body = call(port, "GET", retain.SUMMARY_PATH, origin=None)
            assert status == 200, f"summary answered HTTP {status}: {body!r}"
            reads = fake.reads[first:]  # the poll's own reads are a different query
            payload = json.loads(body)
            assert set(payload) == {"count", "before"}, f"summary shape {sorted(payload)}"
            assert isinstance(payload["count"], int) and not isinstance(payload["count"], bool), \
                f"count {payload['count']!r} is not a number"
            assert isinstance(payload["before"], int) and payload["before"] <= int(time.time()), \
                f"before {payload['before']!r} is not a past whole second"
            expected = eligible_ids(fake)
            assert payload["count"] == len(expected), \
                f"count {payload['count']} for {len(expected)} eligible snapshots"
            assert ARCHIVE > 50 and payload["count"] > 50, \
                "the archive is not deeper than the newest 50, so this proves nothing"
            assert len(reads) > 1, f"the walk read {len(reads)} page(s), not a deep one"
            assert all("offset" not in read for read in reads), \
                f"an offset parameter was sent; Frigate 0.18 ignores it: {reads}"
            assert all(read["sort"] == "date_desc"
                       and read["labels"] == "person," + ",".join(ANIMAL_LABELS)
                       and read["has_snapshot"] == "1" for read in reads), \
                f"a page left the pinned query shape: {reads}"
            assert all(read.get("after") == "0.0" for read in reads), \
                f"the archive walk did not read from the beginning: {reads}"
            assert headers.get("cache-control") == "no-store", f"cache-control {headers!r}"
            assert "access-control-allow-origin" not in headers, f"CORS header in {sorted(headers)}"
            for leak in ("cmdline", "rtsp://", "/media/frigate", "snapshot.jpg"):
                assert leak not in body.decode(), f"{leak!r} reached the summary body"
            assert fake.deletes == [], f"reading the summary deleted {fake.deletes}"
            leaks(output)
            print(f"ok  summary: count {payload['count']} over {len(reads)} pages, all of them "
                  f"older than the newest 50, before {payload['before']}, two keys and nothing else")


def eligible_rows(fake):
    """The eligible rows in the order the archive view must return them: newest
    start_time first, and id descending among rows sharing one. Restated here and
    deliberately not imported, the same way eligible_ids is: the order is part of the
    contract, so reusing the production sort would only prove it sorts like itself."""
    rows = [row for row in fake.events if eligible_row(row)]
    return sorted(rows, key=lambda row: (row["start_time"], row["id"]), reverse=True)


def event_page(port, query=""):
    """One GET of the archive view, with no Origin: what the dashboard's own fetch does."""
    status, headers, body = call(port, "GET", f"{retain.EVENTS_PATH}{query}", origin=None)
    assert status == 200, f"{retain.EVENTS_PATH}{query} answered HTTP {status}: {body!r}"
    assert headers.get("cache-control") == "no-store", f"cache-control {headers!r}"
    assert "access-control-allow-origin" not in headers, f"CORS header in {sorted(headers)}"
    return json.loads(body)


def check_events_pages():
    """The archive view's pages: disjoint, in order, and reaching the oldest row."""
    fake = FakeFrigate(console_fixture())
    with serving(fake) as base:
        with gateway(base) as (port, output):
            expected = [row["id"] for row in eligible_rows(fake)]
            assert len(expected) > 2 * retain.EVENTS_PAGE_SIZE, \
                f"the archive holds {len(expected)} rows, not the three pages this proves"
            assert len(expected) == len(set(expected)), "two fixture rows share an id"
            first = event_page(port)
            assert set(first) == {"events", "page", "page_size", "total", "pages"}, \
                f"events shape {sorted(first)}"
            assert first["page"] == 1 and first["page_size"] == retain.EVENTS_PAGE_SIZE, \
                f"page {first['page']} of {first['page_size']} rows"
            assert first["total"] == len(expected), \
                f"total {first['total']} for {len(expected)} eligible snapshots"
            assert first["pages"] == 3, f"{first['total']} rows do not make three pages"
            assert len(first["events"]) == retain.EVENTS_PAGE_SIZE, \
                f"page 1 carried {len(first['events'])} rows"
            second = event_page(port, "?page=2")
            assert second["page"] == 2 and second["total"] == first["total"], \
                f"page 2 says {second['page']} of {second['total']}"
            first_ids = [row["id"] for row in first["events"]]
            second_ids = [row["id"] for row in second["events"]]
            assert set(first_ids).isdisjoint(second_ids), "two pages of the archive share an event"
            assert first_ids + second_ids == expected[:2 * retain.EVENTS_PAGE_SIZE], \
                "the first two pages are not the newest rows, in order"
            # The oldest rows are on the last page, which is what proves the walk reached
            # the far end: a walk that stopped early would answer 200 with a short page.
            last = event_page(port, "?page=3")
            assert last["page"] == 3, f"the last page answered page {last['page']}"
            last_ids = [row["id"] for row in last["events"]]
            assert len(last_ids) == len(expected) - 2 * retain.EVENTS_PAGE_SIZE, \
                f"the last page carried {len(last_ids)} rows, not the remainder"
            assert last_ids == expected[2 * retain.EVENTS_PAGE_SIZE:], \
                "the last page is not the oldest rows, so the oldest snapshot is on no page"
            assert expected[0] not in last_ids, "the newest snapshot is on the last page"
            # A dashboard that was left one page behind after rows were deleted asks for a
            # page that is gone, and has to get the last one rather than an error.
            beyond = event_page(port, "?page=99999")
            assert beyond["page"] == last["page"], \
                f"a page past the end answered page {beyond['page']}"
            assert [row["id"] for row in beyond["events"]] == [row["id"] for row in last["events"]], \
                "the clamped page is not the last page"
            for name, payload in (("page 1", first), ("page 3", last)):
                for row in payload["events"]:
                    for field in ("id", "camera", "start_time", "end_time", "label",
                                  "has_snapshot", "snapshot", "zones"):
                        assert field in row, f"{name} row {row.get('id')!r} has no {field}"
                    assert row["camera"] in retain.CAMERAS, \
                        f"{name} carried a row from {row['camera']}"
                    assert eligible_row(row), \
                        f"{name} carried {row['id']}, which is not a saved person-or-animal snapshot"
            for leak in ("cmdline", "rtsp://", "/media/frigate", "frigate:5000"):
                assert leak not in json.dumps(first), f"{leak!r} reached the events body"
            assert fake.deletes == [], f"reading the archive deleted {fake.deletes}"
            leaks(output)
            print(f"ok  archive view: {len(expected)} eligible snapshots in {first['pages']} "
                  f"pages of {retain.EVENTS_PAGE_SIZE}; pages 1 and 2 are disjoint and are the "
                  f"newest rows, page 3 reaches the oldest, and page 99999 clamps onto it")


def check_events_camera_filter():
    """A camera filter narrows the rows, the total and the page count together."""
    fake = FakeFrigate(console_fixture())
    with serving(fake) as base:
        with gateway(base) as (port, output):
            for camera in retain.CAMERAS:
                expected = [row["id"] for row in eligible_rows(fake) if row["camera"] == camera]
                assert expected, f"the fixture has no eligible row on {camera}"
                filtered = event_page(port, f"?camera={camera}")
                assert filtered["total"] == len(expected), \
                    f"{camera} reported {filtered['total']} of {len(expected)}"
                assert filtered["pages"] == max(1, -(-len(expected) // retain.EVENTS_PAGE_SIZE)), \
                    f"{camera} reported {filtered['pages']} pages for {len(expected)} rows"
                assert [row["id"] for row in filtered["events"]] == \
                    expected[:retain.EVENTS_PAGE_SIZE], \
                    f"{camera} page 1 is not its own newest rows"
            everything = event_page(port, "?camera=all")
            assert everything["total"] == len(eligible_rows(fake)), \
                f"camera=all reported {everything['total']}"
            first_page = event_page(port)
            assert everything["events"] == first_page["events"], "camera=all is not the default"
            # A camera with nothing archived is still one page of zero, not a page zero
            # and no events, so the dashboard's "page 1 of 1" never needs a special case.
            empty = FakeFrigate([])
            with serving(empty) as bare:
                with gateway(bare) as (other, _quiet):
                    blank = event_page(other)
                    assert blank == {"events": [], "page": 1, "page_size": retain.EVENTS_PAGE_SIZE,
                                     "total": 0, "pages": 1}, f"an empty archive answered {blank}"
            leaks(output)
            print(f"ok  camera filter: all four cameras report their own total, pages and rows, "
                  f"camera=all is the unfiltered default, and an empty archive is one empty page")


def check_events_query_refusals():
    """A malformed query is refused with the right status and reaches nothing upstream."""
    fake = FakeFrigate(console_fixture())
    with serving(fake) as base:
        with gateway(base) as (port, output):
            before = len(fake.calls)
            cases = [
                ("page is zero", "?page=0", 400),
                ("page is negative", "?page=-1", 400),
                ("page is a fraction", "?page=1.5", 400),
                ("page is not a number", "?page=abc", 400),
                ("page is signed", "?page=%2B2", 400),
                ("page is padded", "?page=%202", 400),
                ("page is an expression", "?page=2-1", 400),
                ("page is another script's digit", "?page=%D9%A2", 400),
                ("page is too long and not all digits", "?page=123456789x", 400),
                ("page is too long and all zeros", "?page=0000000000", 400),
                ("page is blank", "?page=", 400),
                ("page has no value", "?page", 400),
                ("page is repeated", "?page=1&page=2", 400),
                ("camera is unknown", "?camera=camera_9", 400),
                ("camera is a bare number", "?camera=1", 400),
                ("camera is padded", "?camera=camera_1%20", 400),
                ("camera is upper case", "?camera=CAMERA_1", 400),
                ("camera is blank", "?camera=", 400),
                ("camera is repeated", "?camera=all&camera=camera_1", 400),
                ("review is unknown", "?review=flagged", 400),
                ("review is a numeric flag", "?review=1", 400),
                ("review is a boolean", "?review=true", 400),
                ("review is a sublabel", "?review=previously_kept", 400),
                ("review is upper case", "?review=Kept", 400),
                ("review is padded", "?review=kept%20", 400),
                ("review is blank", "?review=", 400),
                ("review has no value", "?review", 400),
                ("review is repeated", "?review=all&review=kept", 400),
                ("review beside a bad camera", "?review=kept&camera=camera_9", 400),
                ("an unknown parameter beside review", "?review=kept&limit=25", 400),
                ("an unknown parameter", "?limit=25", 400),
                ("an unknown parameter beside a real one", "?page=1&offset=25", 400),
                ("a path parameter", "?path=/config", 400),
                ("a traversal", "?camera=../config", 400),
                ("an injection attempt", "?page=1;DROP", 400),
            ]
            for name, query, expected in cases:
                status, headers, body = call(port, "GET", f"{retain.EVENTS_PATH}{query}",
                                             origin=None)
                assert status == expected, \
                    f"{name} answered HTTP {status}, expected {expected}: {body!r}"
                assert len(fake.calls) == before, \
                    f"{name} reached Frigate: {fake.calls[before:]}"
                assert json.loads(body).keys() == {"error"}, f"{name} answered {body!r}"
                assert "access-control-allow-origin" not in headers, \
                    f"{name} answered a CORS header: {sorted(headers)}"
            # A page past the end is not a malformed query: it is answered by the last page.
            far = event_page(port, "?page=99999999999999999999999999")
            assert far["page"] == 3, f"a very long page number answered page {far['page']}"
            assert fake.deletes == [], f"a refused query deleted {fake.deletes}"
            leaks(output)
            print(f"ok  {len(cases)} malformed archive queries refused with HTTP 400, every one "
                  f"before a request was sent upstream; a page past the end is answered instead")


def check_events_review_filter():
    """review=kept narrows to the flagged rows, before the page is cut, and combines
    with the camera filter. The archive count behind the archive-wide delete stays the
    whole archive whatever this read was filtered to.

    The contract here is the flag and nothing else, so the fixture carries a legacy
    flagged row and a flagged animal that no class or zone test would find, and one
    unflagged person inside its own watched area that must stay out. Everything else
    is about ordering: total, pages and the rows have to describe one set, so a
    filter applied after the page was cut cannot pass.
    """
    fake = FakeFrigate(review_fixture())
    with serving(fake) as base:
        with gateway(base) as (port, output):
            every = [row["id"] for row in eligible_rows(fake)]
            kept = [row["id"] for row in eligible_rows(fake) if kept_row(row)]
            # No fixed count here: camera_4 is always armed, so the poll flags its rows
            # while this runs and the kept view has to follow that too. What has to hold
            # is that the flagged set is neither everything nor a single page.
            assert len(kept) < len(every), "the kept view is not narrower than the whole archive"
            assert len(kept) > retain.EVENTS_PAGE_SIZE, \
                "the flagged set fits on one page, so the paging below proves nothing"
            for tag in ("kept00", "legacy", "kcat01"):
                assert ineligible(fake, tag) in kept, \
                    f"{tag} is flagged kept and was left out of the kept view"
            assert ineligible(fake, "loose1") not in kept, \
                "an unflagged person inside its own watched area read as kept"
            assert ineligible(fake, "loose1") in every, \
                "the unflagged row is in no view at all"

            # Nothing named is everything, and naming all is the same answer.
            default = event_page(port)
            assert default["total"] == len(every), \
                f"the default reported {default['total']} of {len(every)}"
            assert event_page(port, "?review=all") == default, \
                "review=all is not the same answer as omitting it"

            only_kept = event_page(port, "?review=kept")
            assert only_kept["total"] == len(kept), \
                f"review=kept reported {only_kept['total']} of {len(kept)} flagged rows"
            assert only_kept["pages"] == max(1, -(-len(kept) // retain.EVENTS_PAGE_SIZE)), \
                f"review=kept reported {only_kept['pages']} pages for {len(kept)} rows"
            assert [row["id"] for row in only_kept["events"]] == \
                kept[:retain.EVENTS_PAGE_SIZE], \
                "review=kept page 1 is not the newest flagged rows, in order"

            # Page 2, which exists only because the kept rows outnumber a page.
            second = event_page(port, "?review=kept&page=2")
            assert second["page"] == 2 and second["total"] == only_kept["total"], \
                f"page 2 of the kept view says {second['page']} of {second['total']}"
            assert [row["id"] for row in second["events"]] == \
                kept[retain.EVENTS_PAGE_SIZE:2 * retain.EVENTS_PAGE_SIZE], \
                "page 2 of the kept view is not the remainder, in order"
            beyond = event_page(port, "?review=kept&page=99999")
            assert beyond["page"] == second["page"] and \
                [row["id"] for row in beyond["events"]] == \
                [row["id"] for row in second["events"]], \
                "a page past the end of the kept view did not clamp onto its last page"

            # Whatever the two pages between them carried, every row was flagged. A set
            # built from a class list instead would have lost legacy and kcat01 above and
            # picked up loose1, so this is the one assertion that names the flag itself.
            for row in only_kept["events"] + second["events"]:
                assert row.get("retain_indefinitely") is True, \
                    f"{row['id']} is on a kept page without the flag"

            # The two filters combine rather than one replacing the other.
            per_camera = []
            for camera in retain.CAMERAS:
                expected = [row["id"] for row in eligible_rows(fake)
                            if row["camera"] == camera and kept_row(row)]
                assert expected, f"the fixture has no flagged row on {camera}"
                both = event_page(port, f"?camera={camera}&review=kept")
                per_camera.append(both["total"])
                assert both["total"] == len(expected), \
                    f"{camera} flagged reported {both['total']} of {len(expected)}"
                assert both["pages"] == max(1, -(-len(expected) // retain.EVENTS_PAGE_SIZE)), \
                    f"{camera} flagged reported {both['pages']} pages for {len(expected)} rows"
                assert [row["id"] for row in both["events"]] == \
                    expected[:retain.EVENTS_PAGE_SIZE], \
                    f"{camera} flagged page 1 is not its own newest flagged rows"
            assert sum(per_camera) == only_kept["total"], \
                f"the four camera totals {per_camera} do not add up to {only_kept['total']}"
            assert only_kept["total"] < default["total"], \
                "combining the two filters widened the archive rather than narrowing it"

            # And the count behind the archive-wide delete is the whole archive, not this
            # page: it is read from the worker's own scope and never from a filtered read.
            summary = reply(port)
            assert summary["count"] == len(every), \
                f"the archive counted {summary['count']} of {len(every)} while the read was filtered"
            assert summary["count"] > only_kept["total"], \
                "the fixture cannot tell a whole-archive count from a flagged-only one"
            assert fake.deletes == [], f"reading the archive deleted {fake.deletes}"
            text = leaks(output)
            assert "kept for review only" in text, \
                f"the narrowed scope was not logged:\n{text}"
            print(f"ok  review filter: {only_kept['total']} flagged of {default['total']} eligible "
                  f"in {only_kept['pages']} pages, page 2 carrying the remainder and a page past "
                  f"the end clamping onto it, a legacy flagged row and a flagged animal included, "
                  f"an unflagged person inside its own area excluded, the four camera totals "
                  f"adding up, and the archive still counted at {summary['count']}")


def check_delete_query_refused():
    """The delete path takes no query string at all, so no argument can narrow a delete
    the confirmation did not describe.

    Each case sends a body that would otherwise delete a row, so a missing guard shows
    up as a destroyed fixture row rather than as a status code. The very same request
    without the argument still deletes, which is what says the refusal is about the
    query and not about the body.
    """
    fake = FakeFrigate(console_fixture())
    with serving(fake) as base:
        with gateway(base) as (port, output):
            before = len(fake.calls)
            target = ineligible(fake, "a00000")
            body = {"ids": [target]}
            for query in ("?review=kept", "?camera=camera_1", "?page=2&review=kept",
                          "?review=all", "?all=true", "?before=1", "?ids=a00000"):
                status, headers, body_bytes = call(port, "POST", retain.DELETE_PATH + query,
                                                   body=body)
                assert status == 400, \
                    f"a delete carrying {query!r} answered HTTP {status}: {body_bytes!r}"
                assert json.loads(body_bytes).keys() == {"error"}, \
                    f"a delete carrying {query!r} answered {body_bytes!r}"
                assert len(fake.calls) == before, \
                    f"a delete carrying {query!r} reached Frigate: {fake.calls[before:]}"
                assert fake.deletes == [], f"a delete carrying {query!r} deleted {fake.deletes}"
                assert target in [row["id"] for row in fake.events], \
                    f"a delete carrying {query!r} destroyed {target}"
            status, _headers, body_bytes = call(port, "POST", retain.DELETE_PATH, body=body)
            assert status == 200, f"the unqueried delete answered HTTP {status}: {body_bytes!r}"
            assert json.loads(body_bytes)["deleted"] == 1, \
                f"the unqueried delete did not delete: {body_bytes!r}"
            assert fake.deletes == [target], f"upstream saw {fake.deletes}"
            leaks(output)
    print(f"ok  7 query strings refused with HTTP 400 on the delete path, every one before a "
          f"request was sent and with {target} still upstream; the same body without the query "
          f"deletes it, so the refusal is about the argument and not the scope")


def check_selected_delete():
    """Named ids: exactly those and no others, duplicates folded, foreign rows and
    rows that are not a saved person snapshot refused."""
    fake = FakeFrigate(console_fixture())
    with serving(fake) as base:
        with gateway(base) as (port, output):
            ordinary = ineligible(fake, "areav1")  # in the area zone only: still a snapshot
            opened = ineligible(fake, "act001")  # no end_time: a detection still running
            loitered = ineligible(fake, "a00000")
            foreign = ineligible(fake, "outsid")
            cat = ineligible(fake, "dog004")   # a dog on camera_4: not a camera that watches
            no_clip = ineligible(fake, "noclip")
            unknown = f"{BASE_TIME}.123456-zzzzzz"  # native in form, absent upstream
            asked = [loitered, ordinary, opened, foreign, cat, no_clip, unknown, loitered]
            status, headers, body = call(port, "POST", retain.DELETE_PATH, body={"ids": asked})
            assert status == 200, f"the delete answered HTTP {status}: {body!r}"
            payload = json.loads(body)
            assert set(payload) == {"deleted", "missing", "failed", "active"}, \
                f"delete reply shape {sorted(payload)}"
            assert payload["deleted"] == 3, f"deleted {payload['deleted']}, expected 3: {payload}"
            assert payload["missing"] == 1, f"missing {payload['missing']}, expected the unknown id"
            assert sorted(payload["failed"]) == sorted([foreign, cat, no_clip]), \
                f"failed {payload['failed']}"
            assert payload["active"] == 1, f"active {payload['active']}, expected the open one"
            assert payload["deleted"] + payload["missing"] + len(payload["failed"]) == len(set(asked)), \
                f"the three counts do not add up to what was asked for: {payload}"
            assert sorted(fake.deletes) == sorted([loitered, ordinary, opened]), \
                f"the upstream saw {sorted(fake.deletes)}"
            assert len(fake.deletes) == len(set(fake.deletes)), "an id was deleted twice"
            for row in (foreign, cat, no_clip):
                assert row in [r["id"] for r in fake.events], f"{row} was destroyed upstream"
            assert "access-control-allow-origin" not in headers, f"CORS header in {sorted(headers)}"
            repeat = json.loads(call(port, "POST", retain.DELETE_PATH,
                                     body={"ids": [loitered, ordinary, opened]})[2])
            assert repeat == {"deleted": 0, "missing": 3, "failed": [], "active": 0}, \
                f"a repeated delete was not idempotent: {repeat}"
            assert len(fake.deletes) == 3, f"a repeated delete reached Frigate again: {fake.deletes}"
            text = leaks(output)
            assert f"{foreign} (from no configured camera)" in text, \
                f"the foreign camera refusal was not reported:\n{text}"
            assert f"{cat} (an animal on a camera that does not watch for them)" in text, \
                f"the camera_4 animal refusal was not reported:\n{text}"
            print("ok  8 asked ids became 7: 3 deleted, 1 already gone, 3 refused (another "
                  "camera's row, a dog on camera_4, a row with no snapshot), 1 still open; a "
                  "repeat of the same three reports them all missing and sends nothing")

            # The other half of the animal rule: a cat on camera_3 is a saved snapshot
            # like any other. If the worker pinned it, the operator still has to be able
            # to remove it, so it must delete rather than sit in the archive forever.
            stray = ineligible(fake, "cat001")
            animal = json.loads(call(port, "POST", retain.DELETE_PATH,
                                     body={"ids": [stray]})[2])
            assert animal == {"deleted": 1, "missing": 0, "failed": [], "active": 0}, \
                f"a cat on camera_3 could not be deleted: {animal}"
            assert stray not in [row["id"] for row in fake.events], \
                "a cat on camera_3 was refused by the delete path"
            print("ok  a cat on camera_3 deletes like any other saved snapshot, so a row the "
                  "worker pins on sight is never a row nobody can remove")


def check_partial_failures():
    """A native 2xx carrying success:false, and a native failure, are both reported as
    not deleted rather than counted."""
    fake = FakeFrigate(console_fixture())
    quiet = ineligible(fake, "a00001")
    refused = ineligible(fake, "a00002")
    good = ineligible(fake, "a00003")
    fake.quiet_deletes[quiet] = True
    fake.refused_deletes[refused] = 500
    with serving(fake) as base:
        with gateway(base) as (port, output):
            status, _headers, body = call(port, "POST", retain.DELETE_PATH,
                                          body={"ids": [good, quiet, refused]})
            assert status == 200, f"the delete answered HTTP {status}: {body!r}"
            payload = json.loads(body)
            assert payload["deleted"] == 1, f"a partial failure was counted as a deletion: {payload}"
            assert payload["missing"] == 0 and payload["active"] == 0, f"payload {payload}"
            assert sorted(payload["failed"]) == sorted([quiet, refused]), \
                f"the two refusals were not both reported: {payload}"
            remaining = [row["id"] for row in fake.events]
            assert good not in remaining, "the deleted row is still there"
            assert quiet in remaining and refused in remaining, \
                f"a row that was not deleted is gone from the upstream: {sorted(remaining)}"
            text = leaks(output)
            assert f"{quiet} (HTTP 2xx with no success:true in the reply)" in text, \
                f"a quiet 2xx was not reported as a refusal:\n{text}"
            assert f"{refused} (HTTP 500)" in text, f"the 500 was not reported:\n{text}"
            print("ok  one real delete, one HTTP 200 with success:false and one HTTP 500, all "
                  "three reported: deleted 1, failed 2, and both rows still upstream")


def check_archive_delete():
    """The bounded archive deletion: everything eligible and older than the summary's
    own cutoff, nothing newer, and nothing at all if the walk cannot finish."""
    fake = FakeFrigate(console_fixture())
    with serving(fake) as base:
        with gateway(base) as (port, output):
            summary = reply(port)
            expected = eligible_ids(fake)
            assert summary["count"] == len(expected) > 50, f"summary {summary} for {len(expected)}"
            fake.events.append(newer_row(summary["before"]))
            newer = fake.events[-1]["id"]
            assert newer not in expected, "the newer row was counted as archive"
            reads_before = len(fake.reads)
            status, _headers, body = call(port, "POST", retain.DELETE_PATH,
                                          body={"all": True, "before": summary["before"]})
            assert status == 200, f"the archive delete answered HTTP {status}: {body!r}"
            payload = json.loads(body)
            assert payload["deleted"] == len(expected), \
                f"deleted {payload['deleted']} of {len(expected)}: {payload}"
            assert payload["missing"] == 0 and payload["failed"] == [], f"payload {payload}"
            assert payload["active"] == 1, f"the open detection was not reported: {payload}"
            assert sorted(fake.deletes) == expected, \
                f"the upstream saw {len(fake.deletes)} rows, not the {len(expected)} selected"
            assert newer not in fake.deletes, "a snapshot newer than the cutoff was deleted"
            assert newer in [row["id"] for row in fake.events], "the newer row was destroyed"
            assert len(fake.reads) - reads_before > 1, "the archive walk did not page"
            assert all("offset" not in read for read in fake.reads), \
                f"an offset parameter was sent: {fake.reads}"
            repeat = json.loads(call(port, "POST", retain.DELETE_PATH,
                                     body={"all": True, "before": summary["before"]})[2])
            assert repeat == {"deleted": 0, "missing": 0, "failed": [], "active": 0}, \
                f"the second archive delete was not idempotent: {repeat}"
            assert len(fake.deletes) == len(expected), \
                f"the second archive delete sent more deletes: {len(fake.deletes)}"
            assert newer in [row["id"] for row in fake.events], "the newer row was destroyed"
            leaks(output)
            print(f"ok  the summary's own cutoff deleted {len(expected)} snapshots across "
                  f"{len(fake.reads) - reads_before} pages, left the one newer row alone, "
                  f"reported 1 still open, and a repeat selected nothing and sent nothing")


def check_page_cap_fails_closed():
    """A walk that cannot reach the end of the list must delete nothing."""
    fake = FakeFrigate(console_fixture())
    with serving(fake) as base:
        with gateway(base, max_pages=2) as (port, output):
            status, _headers, body = call(port, "GET", retain.SUMMARY_PATH, origin=None)
            assert status == 503, f"a capped summary answered HTTP {status}: {body!r}"
            # An archive page is a count over the whole walk, so a capped one must fail
            # closed too rather than answer a short page that reads like a whole one.
            status, _headers, body = call(port, "GET", retain.EVENTS_PATH, origin=None)
            assert status == 503, f"a capped archive page answered HTTP {status}: {body!r}"
            assert "page cap" in body.decode(), f"the cap was not named: {body!r}"
            status, _headers, body = call(port, "POST", retain.DELETE_PATH,
                                          body={"all": True, "before": int(time.time())})
            assert status == 503, f"a capped archive delete answered HTTP {status}: {body!r}"
            assert "page cap" in body.decode(), f"the cap was not named: {body!r}"
            assert fake.deletes == [], f"a capped walk deleted {fake.deletes}"
            # Only the list reads are allowed here. /api/config is the readiness
            # read, which is a GET and never touches an event by id; looking one
            # up is what a delete would do before sending it.
            assert not any(method == "GET" and path not in ("/api/events", "/api/config")
                           for method, path in fake.calls), \
                f"a capped delete looked an event up anyway: {fake.calls}"
            assert not any(method == "GET" and path.startswith("/api/events/")
                           for method, path in fake.calls), \
                f"a capped delete looked one event up by id: {fake.calls}"
            print("ok  with the page cap two pages short of the end of the list, all three "
                  "endpoints answer 503, name the cap, and not one event was looked up or deleted")


def check_poll_never_deletes():
    """The poll keeps running, keeps pinning, and never deletes."""
    fake = FakeFrigate(console_fixture())
    with serving(fake) as base:
        with gateway(base, interval=1) as (port, output):
            deadline = time.time() + 20
            # Two logged scans, not a page count: one pass walks the fixture several
            # times over, so only the worker's own line says how many passes ran.
            while sum("scan:" in line for line in output) < 2 and time.time() < deadline:
                time.sleep(0.05)
            assert fake.posts, f"the poll never pinned anything:\n{''.join(output)}"
            text = leaks(output)
            assert text.count("scan:") >= 2, f"fewer than two passes ran:\n{text}"
            assert fake.deletes == [], f"the poll deleted {fake.deletes}"
            summary = reply(port)  # still serving, and still only reading
            assert summary["count"] == len(eligible_ids(fake)), \
                f"the poll changed the archive: {summary}"
            assert fake.deletes == [], f"reading the summary deleted {fake.deletes}"
            print("ok  two poll passes pinned real rows and issued no DELETE at all; the "
                  "summary still answered without changing the archive")


def entry_state(port):
    """The arming status, over the real path, with no action header - it is a read.

    Every field is checked, because a reply this page cannot read is a status it does not
    have: shown as unavailable rather than filled in with a guess. The mode has to agree
    with the three flags it is derived from, camera_4 has to be armed with no moment, and
    the three manual cameras must agree with themselves - armed with a moment, or off with
    none.
    """
    status, _headers, body = call(port, "GET", retain.ENTRY_PATH, origin=None, action=None)
    assert status == 200, f"the entry status answered HTTP {status}: {body!r}"
    payload = json.loads(body)
    assert set(payload) == {"cameras", "mode", "always_armed", "notice", "state"}, \
        f"entry status keys {sorted(payload)}"
    assert payload["cameras"] == list(retain.CAMERAS), f"entry cameras {payload['cameras']}"
    assert payload["always_armed"] == retain.ALWAYS_CAMERA, \
        f"the always-armed camera is reported as {payload['always_armed']!r}"
    assert set(payload["state"]) == set(retain.CAMERAS), f"entry state {sorted(payload['state'])}"
    for camera, row in payload["state"].items():
        assert isinstance(row["ready"], bool) and isinstance(row["armed"], bool), \
            f"{camera} state {row}"
        if camera == retain.ALWAYS_CAMERA:
            assert row["armed"] is True and row["armed_since"] is None, \
                f"the always-armed camera is not always armed: {row}"
        elif row["armed"]:
            assert isinstance(row["armed_since"], (int, float)) and row["armed_since"] > 0, \
                f"{camera} is armed with no moment: {row}"
        else:
            assert row["armed_since"] is None, f"{camera} is off but kept a moment: {row}"
    manual = {camera: payload["state"][camera]["armed"] for camera in retain.ENTRY_CAMERAS}
    expected = "home" if not any(manual.values()) else ("away" if all(manual.values()) else "custom")
    assert payload["mode"] == expected, \
        f"the reply says {payload['mode']!r} for {manual}"
    return payload


def arm(port, camera, armed, **overrides):
    """One arming POST for one camera. The action header is the one the arming expects
    unless a case deliberately sends a different one."""
    kwargs = {"body": {"camera": camera, "armed": armed},
              "action": retain.ENTRY_ACTION_VALUE}
    kwargs.update(overrides)
    return call(port, "POST", retain.ENTRY_PATH, **kwargs)


def preset(port, mode, **overrides):
    """One preset POST, Home or Away, for all three cameras at once."""
    kwargs = {"body": {"mode": mode}, "action": retain.ENTRY_ACTION_VALUE}
    kwargs.update(overrides)
    return call(port, "POST", retain.ENTRY_PATH, **kwargs)


def check_entry_reads_home_and_camera_4_is_always():
    """A fresh process is Home, with camera_4 armed and no arming moment of its own."""
    fake = FakeFrigate(console_fixture())
    with tempfile.TemporaryDirectory() as folder:
        with serving(fake) as base:
            with gateway(base, state_file=Path(folder) / "entry.json") as (port, output):
                first = entry_state(port)
                assert first["mode"] == "home", f"a fresh process is {first['mode']!r}"
                for camera in retain.ENTRY_CAMERAS:
                    row = first["state"][camera]
                    assert not row["armed"] and row["armed_since"] is None, \
                        f"a fresh process armed {camera}: {row}"
                assert first["state"][retain.ALWAYS_CAMERA] == {"armed": True, "ready": True,
                                                                "armed_since": None}, \
                    f"camera_4 on a fresh process: {first['state'][retain.ALWAYS_CAMERA]}"
                assert "always armed" in first["notice"], f"the notice is missing: {first['notice']}"
                assert "restart" in first["notice"].lower(), \
                    f"the notice does not say the setting survives: {first['notice']}"
                assert "fighting" not in first["notice"].lower(), \
                    "the notice claims a rule that does not exist"
                assert fake.deletes == [], f"reading the status deleted {fake.deletes}"
                assert not (Path(folder) / "entry.json").exists(), \
                    "reading the status wrote the state file"
                text = leaks(output)
                assert "entry: home," in text, f"the status was not logged:\n{text}"
    print("ok  a fresh process answers Home on cameras 1-3 with no arming moment, camera_4 armed "
          "with none of its own, the notice saying what is kept and what cannot be turned off, "
          "and nothing written to disk")


def check_entry_write_refusals():
    """Every shape that is not the dashboard's own arming is refused, and camera_4 cannot
    be written at all."""
    fake = FakeFrigate(console_fixture())
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "entry.json"
        with serving(fake) as base:
            with gateway(base, state_file=path) as (port, output):
                # Refusals, each proven to leave the state exactly as it was.
                refusals = (
                    ("another camera's Host", dict(host="192.0.2.9:8088")),
                    ("no Origin", dict(origin=None)),
                    ("a foreign Origin", dict(origin="evil.invalid")),
                    ("an https Origin", dict(origin="https://127.0.0.1:8088")),
                    ("a form content type", dict(content_type="text/plain")),
                    ("no content type", dict(content_type=None)),
                    ("the delete action header", dict(action="delete")),
                    ("no action header", dict(action=None)),
                )
                for name, kwargs in refusals:
                    status, _headers, body = arm(port, "camera_1", True, **kwargs)
                    assert status in (400, 403, 415), \
                        f"arming with {name} answered HTTP {status} instead of refusing: {body!r}"
                    assert entry_state(port)["mode"] == "home", \
                        f"arming with {name} changed the state anyway"
                # The path is exact and takes no query, so the same guards answer it.
                for name, suffix in (("a query string", "?camera=camera_1"),
                                     ("a path suffix", "/now")):
                    status, _headers, body = call(port, "POST", retain.ENTRY_PATH + suffix,
                                                  body={"camera": "camera_1", "armed": True},
                                                  action=retain.ENTRY_ACTION_VALUE)
                    assert status in (400, 404), \
                        f"arming with {name} answered HTTP {status} instead of refusing: {body!r}"
                assert entry_state(port)["mode"] == "home", \
                    "arming with a query string or a path suffix changed the state anyway"
                print(f"ok  {len(refusals) + 2} refusal shapes on the arming POST - foreign Host, "
                      "missing or foreign Origin, https Origin, wrong or absent content type, the "
                      "delete action header, no action header, a query string and a path suffix - "
                      "each refused with the state unchanged")

                # Bodies, before any of them can arm anything.
                bodies = (
                    ("camera_4 armed", dict(body={"camera": "camera_4", "armed": True})),
                    ("camera_4 disarmed", dict(body={"camera": "camera_4", "armed": False})),
                    ("an unknown camera", dict(body={"camera": "camera_9", "armed": True})),
                    ("a missing camera", dict(body={"armed": True})),
                    ("a missing armed", dict(body={"camera": "camera_1"})),
                    ("a third key", dict(body={"camera": "camera_1", "armed": True, "until": 99})),
                    ("a string true", dict(body={"camera": "camera_1", "armed": "true"})),
                    ("a numeric 1", dict(body={"camera": "camera_1", "armed": 1})),
                    ("an unknown mode", dict(body={"mode": "night"})),
                    ("a numeric mode", dict(body={"mode": 1})),
                    ("both shapes at once", dict(body={"mode": "home", "camera": "camera_1",
                                                       "armed": True})),
                    ("a mode and a camera", dict(body={"mode": "away", "camera": "camera_1"})),
                    ("neither shape", dict(body={})),
                    ("a list", dict(body=["camera_1", True])),
                    ("a string", dict(body="camera_1")),
                    ("null", dict(raw=b"null")),
                    ("an oversized body", dict(body={"camera": "camera_1", "armed": True,
                                                     "pad": "x" * (retain.MAX_BODY_BYTES + 1)})),
                )
                for name, kwargs in bodies:
                    status, _headers, body = call(port, "POST", retain.ENTRY_PATH,
                                                  action=retain.ENTRY_ACTION_VALUE, **kwargs)
                    assert status in (400, 413), \
                        f"a body naming {name} answered HTTP {status}: {body!r}"
                    state = entry_state(port)
                    assert state["mode"] == "home", f"a body naming {name} armed something"
                    assert state["state"][retain.ALWAYS_CAMERA]["armed"] is True, \
                        f"a body naming {name} reached camera_4"
                assert not path.exists(), f"a refused body wrote {path.read_text()}"
                print(f"ok  {len(bodies)} bodies refused with HTTP 400 or 413 - camera_4 armed and "
                      "disarmed, an unknown camera, a missing key, a third key, a string or numeric "
                      "armed, an unknown or numeric mode, both shapes at once, neither shape, four "
                      "wrong JSON types and an oversized body - each before anything could be armed")

                # The one preset that works with nothing configured, and the one camera that
                # can be armed, so what follows is measured against a state that moved.
                status, _headers, body = preset(port, "away")
                assert status == 200, f"Away answered HTTP {status}: {body!r}"
                payload = entry_state(port)
                assert payload["mode"] == "away", f"Away answered {payload['mode']!r}"
                assert all(payload["state"][camera]["armed"] for camera in retain.ENTRY_CAMERAS), \
                    f"Away left a camera off: {payload['state']}"
                print("ok  the Away preset arms all three cameras in one request, and the reply "
                      "reports away")


def check_entry_presets_and_toggles():
    """One moment for the whole preset, individual toggles that override it, and Home."""
    armed_at = int(time.time()) - 30
    fake = FakeFrigate(console_fixture())
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "entry.json"
        with serving(fake) as base:
            with gateway(base, state_file=path) as (port, output):
                status, _headers, body = preset(port, "away")
                assert status == 200, f"Away answered HTTP {status}: {body!r}"
                away = entry_state(port)["state"]
                moments = {away[camera]["armed_since"] for camera in retain.ENTRY_CAMERAS}
                assert len(moments) == 1 and moments.pop() >= armed_at, \
                    f"Away did not arm all three at one moment: {away}"
                # Repeating it is idempotent, and it must not move the moment.
                first_moment = next(iter(away.values()))["armed_since"]
                assert preset(port, "away")[0] == 200
                again = entry_state(port)["state"]
                assert all(again[camera]["armed_since"] == first_moment
                           for camera in retain.ENTRY_CAMERAS), \
                    f"a repeated Away moved the arming moment: {again}"
                # One camera off makes the mode Custom, and leaves the other two alone.
                status, _headers, body = arm(port, "camera_3", False)
                assert status == 200, f"disarming camera_3 answered HTTP {status}: {body!r}"
                custom = entry_state(port)
                assert custom["mode"] == "custom", f"a mixed answer called {custom['mode']!r}"
                assert custom["state"]["camera_1"] == away["camera_1"], \
                    f"disarming one camera changed another: {custom['state']}"
                assert custom["state"]["camera_3"] == {"armed": False, "ready": True,
                                                       "armed_since": None}, \
                    f"camera_3 after a disarm: {custom['state']['camera_3']}"
                # And one camera on from Home is a single arming, with its own moment.
                assert preset(port, "home")[0] == 200
                home = entry_state(port)
                assert home["mode"] == "home", f"Home answered {home['mode']!r}"
                assert all(home["state"][camera]["armed_since"] is None
                           for camera in retain.ENTRY_CAMERAS), \
                    f"Home left a moment behind: {home['state']}"
                assert arm(port, "camera_2", True)[0] == 200
                one = entry_state(port)
                assert one["mode"] == "custom" and one["state"]["camera_2"]["armed_since"], \
                    f"one arming did not read as custom: {one}"
                assert home["state"][retain.ALWAYS_CAMERA]["armed_since"] is None, \
                    "camera_4 grew an arming moment"
                assert fake.deletes == [], f"an arming write deleted {fake.deletes}"
                saved = json.loads(path.read_text())
                assert set(saved["cameras"]) == set(retain.ENTRY_CAMERAS), \
                    f"the saved file names {sorted(saved['cameras'])}"
                assert saved["cameras"]["camera_2"]["armed"] is True and \
                    saved["cameras"]["camera_1"]["armed"] is False, \
                    f"the saved file is {saved['cameras']}"
    print("ok  Away arms all three at one moment and a repeat keeps that moment; a single camera "
          "off or on makes the mode Custom and leaves the others alone; Home clears all three; "
          "camera_4 never grows an arming moment; the file on disk holds the three flags")


def check_entry_state_file():
    """The saved arming across a real restart, a file that cannot be read, and a save that
    cannot land."""
    fake = FakeFrigate(console_fixture())
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "entry.json"
        with serving(fake) as base:
            with gateway(base, state_file=path) as (port, _first):
                assert preset(port, "away")[0] == 200
                before = entry_state(port)["state"]
            # A second process, same file: this is what a restart of the service is.
            with gateway(base, state_file=path) as (port, _second):
                after = entry_state(port)["state"]
                assert after == before, f"a restart came back {after}, not {before}"
                assert entry_state(port)["mode"] == "away", "a restart did not come back Away"
                print("ok  Away, saved in a real file, is still Away with the same arming moments "
                      "after the worker process is replaced")

            # A save that cannot land, with that valid Away file behind it: the previous
            # valid file must come out whole and the process must still report Away. The
            # scratch the write needs is turned into a directory, so the failure is the real
            # filesystem refusing the write rather than anything simulated.
            with gateway(base, state_file=path) as (port, output):
                saved_bytes = path.read_bytes()
                blocked = path.with_name(path.name + ".new")
                blocked.mkdir()
                status, _headers, body = preset(port, "home")
                assert status == 503, f"a save that could not land answered {status}: {body!r}"
                assert b"/" not in body, f"the refusal carried a path: {body!r}"
                assert path.read_bytes() == saved_bytes, "a failed save changed the saved file"
                stuck = entry_state(port)
                assert stuck["mode"] == "away" and stuck["state"] == before, \
                    f"a failed save changed what the process reports: {stuck['state']}"
                assert preset(port, "home")[0] == 503, "a second failed save reported success"
                assert path.read_bytes() == saved_bytes, "a failed save changed the saved file"
                text = leaks(output)
                assert "could not be saved" in text, f"the failure was not logged:\n{text}"
                # Clear the obstacle and the very same request lands.
                blocked.rmdir()
                assert preset(port, "home")[0] == 200, "Home did not save once the write could"
                assert entry_state(port)["mode"] == "home", "Home did not take after saving"
                assert json.loads(path.read_text())["cameras"]["camera_1"]["armed"] is False, \
                    "the saved file does not say Home"
                assert not blocked.exists(), "the scratch was left behind after a good save"
            print("ok  with the write blocked by a real filesystem refusal, Home is answered 503, "
                  "the valid Away file comes out byte for byte unchanged, the process still "
                  "reports Away with the same moments, and the same request saves as soon as the "
                  "obstacle is gone")

            # A file this cannot read is Home, and camera_4 is armed through it.
            broken = '{"version": 1, "cameras": {"camera_1": {"armed": tr'
            path.write_text(broken)
            with gateway(base, state_file=path) as (port, _third):
                payload = entry_state(port)
                assert payload["mode"] == "home", \
                    f"a file that cannot be read came back {payload['mode']!r}"
                assert retain.STATE_UNREADABLE in payload["notice"], \
                    f"the dashboard was not told: {payload['notice']!r}"
                assert payload["state"][retain.ALWAYS_CAMERA] == {"armed": True, "ready": True,
                                                                  "armed_since": None}, \
                    f"camera_4 followed an unreadable file: {payload['state']}"
                assert path.read_text() == broken, "reading the status rewrote the file"

            # camera_4 written into the file, in every shape that could mean anything, and
            # nothing it says is honoured.
            for label, text in (
                ("an explicit off", '{"version": 1, "cameras": {"camera_1": {"armed": false, '
                                    '"armed_since": null}, "camera_2": {"armed": true, '
                                    '"armed_since": 1790910000.0}, "camera_3": {"armed": false, '
                                    '"armed_since": null}, "camera_4": {"armed": false, '
                                    '"armed_since": null}}}'),
                ("an explicit off with no moment", '{"version": 1, "cameras": {"camera_1": '
                                                   '{"armed": false, "armed_since": null}, '
                                                   '"camera_2": {"armed": false, '
                                                   '"armed_since": null}, "camera_3": '
                                                   '{"armed": false, "armed_since": null}, '
                                                   '"camera_4": {"armed": 0, '
                                                   '"armed_since": 1790910000.0}}}'),
            ):
                path.write_text(text)
                with gateway(base, state_file=path) as (port, _fourth):
                    payload = entry_state(port)
                    assert payload["state"][retain.ALWAYS_CAMERA] == {"armed": True, "ready": True,
                                                                      "armed_since": None}, \
                        f"a file with camera_4 {label} turned it off: {payload['state']}"
                    assert payload["mode"] == "home", \
                        f"a file naming a fourth camera was honoured: {payload['mode']!r}"
            print("ok  a file naming camera_4 in any shape is not honoured: camera_4 is armed with "
                  "no moment of its own through both, and a file with a fourth camera in it is "
                  "not read at all")
    assert fake.deletes == [], f"the arming checks deleted {fake.deletes}"


def check_entry_ready_gating():
    """An area the camera service does not report refuses arming and stops the pins, keeps
    every setting, and recovers by itself; Home never needs it."""
    fake = FakeFrigate(console_fixture())
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "entry.json"
        with serving(fake) as base:
            with gateway(base, state_file=path) as (port, _output):
                assert preset(port, "away")[0] == 200
                armed = entry_state(port)["state"]
                # The route itself is failing. A read is answered from the cached verdict,
                # so the arming POST is what forces the fresh read that finds it.
                fake.config = "error"
                for camera in retain.ENTRY_CAMERAS:
                    status, _headers, body = arm(port, camera, True)
                    assert status == 409, \
                        f"arming {camera} with no readable config answered {status}: {body!r}"
                assert preset(port, "away")[0] == 409, "Away was accepted with no readable config"
                blind = entry_state(port)
                assert all(not row["ready"] for row in blind["state"].values()), \
                    f"a config that cannot be read still reported ready: {blind['state']}"
                assert blind["mode"] == "away", \
                    f"a config that cannot be read changed the mode: {blind['mode']!r}"
                for camera in retain.CAMERAS:
                    assert blind["state"][camera] == {"armed": armed[camera]["armed"], "ready": False,
                                                     "armed_since": armed[camera]["armed_since"]}, \
                        f"a failed config read changed more than the readiness on {camera}: " \
                        f"{blind['state'][camera]}"
                assert arm(port, "camera_1", False)[0] == 200, \
                    "disarming must work even when nothing can be pinned"
                # Home is always available: it keeps nothing, so it needs no area.
                assert preset(port, "home")[0] == 200, "Home was refused with no readable config"
                assert entry_state(port)["mode"] == "home", "Home did not take"

                # A config that reads fine but reports no area for camera_2 gates only that one.
                fake.config = "no-camera-2"
                assert arm(port, "camera_2", True)[0] == 409, \
                    "arming camera_2 with no area configured was accepted"
                assert arm(port, "camera_1", True)[0] == 200, \
                    "arming camera_1 was refused for camera_2's missing area"
                assert preset(port, "away")[0] == 409, "Away was accepted with one area missing"
                assert entry_state(port)["mode"] == "custom", \
                    "Away failed without arming every camera"
                assert entry_state(port)["state"]["camera_2"]["armed"] is False, \
                    "a refused arming still armed camera_2"
                # And the config comes back by itself.
                fake.config = "ok"
                assert preset(port, "away")[0] == 200, "Away was refused after the areas returned"
                recovered = entry_state(port)
                assert all(row["ready"] for row in recovered["state"].values()), \
                    f"the areas did not recover: {recovered['state']}"
                assert recovered["mode"] == "away", "Away did not take after recovery"
                print("ok  with nothing readable every arming and Away is refused with 409, the "
                      "settings and the mode survive untouched, disarming and Home still work, and "
                      "one missing area gates only its own camera - then Away works by itself once "
                      "the areas are reported again")


def check_entry_status_hides_the_config():
    """The config carries RTSP credentials and the state file a path, so the status must
    carry none of it."""
    fake = FakeFrigate(console_fixture())
    with tempfile.TemporaryDirectory() as folder:
        with serving(fake) as base:
            with gateway(base, state_file=Path(folder) / "entry.json") as (port, output):
                payload = entry_state(port)
                blob = json.dumps(payload)
                for leak in ("CREDENTIAL-MARKER", "rtsp://", "camera_9_area", "ffmpeg", "detect",
                             "192.0.2.10", "password", "track", "entry.json", "/state", "Traceback"):
                    assert leak not in blob, f"{leak!r} reached the entry status: {blob}"
                assert payload["notice"] and isinstance(payload["notice"], str), "no notice"
                assert json.dumps(payload) == blob, "the status shape is not stable"
                # A refusal carries no config and no path either.
                status, _headers, body = arm(port, "camera_4", False)
                assert status == 400, f"a refused arming answered HTTP {status}: {body!r}"
                for leak in ("CREDENTIAL-MARKER", "rtsp://", "entry.json"):
                    assert leak not in body.decode(), f"{leak!r} reached a refusal: {body!r}"
                leaks(output)
    print("ok  the entry status and its refusals carry four booleans and one number per camera, "
          "the mode and the always-armed camera, and nothing from the camera service's config or "
          "the state file's path")


def check_entry_and_deletion_are_independent():
    """Arming changes what a pass keeps and nothing about what an operator may reach.

    The archive scope is the one an operator has to be able to use, so it cannot
    move when the retention policy does.
    """
    fake = FakeFrigate(console_fixture())
    with tempfile.TemporaryDirectory() as folder:
        with serving(fake) as base:
            with gateway(base, state_file=Path(folder) / "entry.json") as (port, output):
                eligible = len(eligible_ids(fake))
                summary = reply(port)
                assert summary["count"] == eligible, \
                    f"the archive counted {summary['count']} of {eligible} while Home"
                status, _headers, body = arm(port, "camera_1", True)
                assert status == 200, f"arming answered HTTP {status}: {body!r}"
                assert reply(port)["count"] == eligible, "arming changed what the archive reaches"
                target = [event_id for event_id, tag in
                          ((row["id"], row["id"].rsplit("-", 1)[-1]) for row in fake.events)
                          if tag.startswith("a000")]
                status, _headers, body = call(port, "POST", retain.DELETE_PATH,
                                              body={"ids": sorted(target)},
                                              action=retain.ACTION_VALUE)
                assert status == 200, f"a named delete answered HTTP {status}: {body!r}"
                result = json.loads(body)
                assert result["deleted"] == len(target), f"deleted {result} for {len(target)} ids"
                assert result["failed"] == [], f"an armed camera refused a deletable row: {result}"
                assert fake.deletes == sorted(target), f"upstream saw {fake.deletes}"
                print(f"ok  the archive scope is identical at Home and armed ({eligible} rows), and "
                      f"{len(target)} of them still delete by name afterwards, so what an operator "
                      f"may reach does not move with the retention policy")
                leaks(output)


def check_entry_confirmation_markup():
    """The confirmation in the served markup: one native dialog in the classes the
    deletion already uses, Cancel as a submit carrying the default focus, and no control
    for the always-armed camera.

    What the dialog *decides* and *sends* is owned by scripts/check_entry_ui.mjs, which
    runs the page's own functions; only the markup belongs here.
    """
    root = HERE.parent / "dashboard"
    html = (root / "index.html").read_text()
    app = (root / "app.js").read_text()
    assert html.count('class="confirm"\n      id="entry-confirm"') == 1, \
        "the entry confirmation is not one native <dialog> in the confirm classes"
    assert html.count('<form method="dialog" class="confirm__form">') == 2, \
        "the entry confirmation is not built on a method=dialog form like the delete one"
    assert html.count('type="submit" id="entry-cancel" autofocus>Cancel</button>') == 1, \
        "Cancel is not a submit carrying autofocus, so it is not the default focus"
    assert html.count('id="entry-confirm-button"') == 1, "there is not exactly one Confirm"
    for element in ("entry-confirm", "entry-confirm-title", "entry-confirm-scope",
                    "entry-cancel", "entry-confirm-button", "entry-confirm-label"):
        assert f'$("{element}")' in app, f"the page never reads {element}"
    assert 'data-entry-action="camera_4"' not in html, "camera_4 grew a control"
    assert 'data-entry-preset="camera_4"' not in html, "camera_4 grew a preset"
    print("ok  one native <dialog> in the existing confirm classes, Cancel focused first, one "
          "Confirm, every id the page reads present, and no control for the always-armed camera")


def main():
    print("checking the retention gateway against a fake Frigate")
    check_defaults()
    for check in (check_guards, check_bodies, check_summary, check_events_pages,
                  check_events_camera_filter, check_events_query_refusals,
                  check_events_review_filter, check_delete_query_refused,
                  check_selected_delete, check_partial_failures, check_archive_delete,
                  check_page_cap_fails_closed, check_entry_reads_home_and_camera_4_is_always,
                  check_entry_write_refusals, check_entry_presets_and_toggles,
                  check_entry_state_file, check_entry_ready_gating,
                  check_entry_confirmation_markup,
                  check_entry_status_hides_the_config,
                  check_entry_and_deletion_are_independent, check_poll_never_deletes):
        check()
    print("\nPASS  every assertion held")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as err:
        print(f"\nFAIL  {err}")
        sys.exit(1)