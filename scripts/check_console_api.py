#!/usr/bin/env python3
"""HTTP contract check for the retention worker's deletion gateway (stdlib only).

retain.py --serve is the only write path in this project, so this drives the real
gateway over HTTP exactly as the dashboard's browser drives it: Host, Origin,
Content-Type and X-Surveillance-Action written by hand, against a fake Frigate on
loopback. It adds no seam to production code — --api-base and --listen are the
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
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import retain  # noqa: E402  (path set above, so the production module is the one under test)
from check_retention import FakeFrigate, event, serving  # noqa: E402

HOST = "127.0.0.1:8088"  # where compose publishes the dashboard
BASE_TIME = int(time.time()) - 6 * 3600  # six hours back: already past the six-hour window
ARCHIVE = 58  # eligible person snapshots, deeper than the newest 50 a dashboard shows
PAGE_SIZE = 7  # forces the walk past one page for a tiny fixture


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


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@contextlib.contextmanager
def gateway(base, interval=60, page_size=PAGE_SIZE, max_pages=50):
    """The real worker in --serve, on loopback, with every option an operational one."""
    port = free_port()
    process = subprocess.Popen(
        [sys.executable, str(HERE / "retain.py"), "--serve", "--api-base", base,
         "--listen", f"127.0.0.1:{port}", "--interval", str(interval),
         "--page-size", str(page_size), "--max-pages", str(max_pages), "--timeout", "5"],
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
        # still writing has logged its own end — waiting on its first read would take the
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
    assert (retain.SUMMARY_PATH, retain.EVENTS_PATH, retain.DELETE_PATH) == \
        ("/api/snapshots/summary", "/api/snapshots/events", "/api/snapshots/delete"), \
        "the three endpoint paths moved"
    assert retain.EVENTS_PAGE_SIZE == 25, f"the archive page size is {retain.EVENTS_PAGE_SIZE!r}"
    assert retain.CAMERA_FILTERS == ("all", "camera_1", "camera_2", "camera_3", "camera_4"), \
        f"the camera filters {retain.CAMERA_FILTERS!r}"
    print("ok  0.0.0.0:8090 by default, three exact paths, both dashboard Hosts, "
          "at most 1000 ids and 64 KiB per request, 25 rows per archive page and four "
          "camera filters plus all")


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
            assert not any(method == "GET" and path != "/api/events" for method, path in fake.calls), \
                f"a capped delete looked an event up anyway: {fake.calls}"
            leaks(output)
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


def main():
    print("checking the retention deletion gateway against a fake Frigate")
    check_defaults()
    for check in (check_guards, check_bodies, check_summary, check_events_pages,
                  check_events_camera_filter, check_events_query_refusals,
                  check_selected_delete, check_partial_failures, check_archive_delete,
                  check_page_cap_fails_closed, check_poll_never_deletes):
        check()
    print("\nPASS  every assertion held")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as err:
        print(f"\nFAIL  {err}")
        sys.exit(1)