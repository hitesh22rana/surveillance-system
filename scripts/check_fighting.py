#!/usr/bin/env python3
"""HTTP boundary check for scripts/fighting.py against a fake Frigate (stdlib only).

Every decision this worker makes about an event happens across an HTTP boundary, so
this starts a fake Frigate on loopback and points the real Worker at it. Assertions
read only what left the worker and what reached the fake: the requests the fake
received, their exact bodies, and the one status answer it serves. It adds no seam
to production code, and it needs no machine learning package: the model path is
imported lazily inside fighting.py, so the temporal rule, the buffer rules, the
pending file, the matching gates and the status read are all reachable with the
standard library alone.

The fake imitates Frigate 0.18 closely enough to be worth something: the events
endpoint answers has_snapshot, labels, limit, sort, before and after with both
bounds strict and sort=date_desc newest first, a sub-label writes the label onto
the row, and a retain sets retain_indefinitely, so the next sweep sees what the
last one did. It applies no filter of its own, which is deliberate: that is what
makes the worker's own snapshot, label, camera, id and time gates reachable at all.
Each of those gates is checked again inside fighting.py, so a row that only Frigate
would have filtered away is a row the worker must still refuse.

Nothing here is evidence about whether this model detects a fight. It is evidence
about requests, refusals, files and one status read.

    python3 scripts/check_fighting.py
"""

import argparse
import contextlib
import http.client
import http.server
import io
import json
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fighting  # noqa: E402  (path set above, so the production module is the one under test)
import retain  # noqa: E402

BASE = int(time.time()) - 900  # a quarter of an hour back: inside the six hour window
END = BASE + 400  # the candidate interval's end; rows are placed around it
# A marker that must never appear in anything the worker answers or logs. The
# credential is the kind of thing the config carries, so it is the right thing to
# hunt for in a status read.
CREDENTIAL_MARKER = "rtsp://cam-user:CREDENTIAL-MARKER@192.0.2.10:554/live"
# The exact keys the status answer may carry. Anything else, and anything holding a
# path, a URL or raw error text, is refused.
STATUS_KEYS = {"experimental", "label", "model", "model_revision", "model_state",
               "threshold", "window_frames", "capture_fps", "votes_required",
               "cooldown_seconds", "pending", "notice", "cameras"}
CAMERA_KEYS = {"state", "last_capture", "last_scored", "score", "last_candidate",
               "last_matched"}


def row(tag, camera, start, end=None, label="person", snapshot=True, **extra):
    """A Frigate-shaped row: native id, one label, a bool has_snapshot, a zones list.

    end=None is a track Frigate has not ended, which is the running case.
    """
    payload = {
        "id": f"{start}.123456-{tag}",
        "camera": camera,
        "camera_name": camera,
        "start_time": float(start),
        "end_time": None if end is None else float(end),
        "label": label,
        "zones": [f"{camera}_area"],
        "has_snapshot": snapshot,
        "snapshot": f"{camera}/snapshot.jpg" if snapshot else None,
        "type": "object",
        "false_positive": False,
    }
    payload.update(extra)
    return payload


def candidate(camera="camera_2", start=BASE, end=END, score=0.93, at=None):
    """One candidate interval, in the shape the pending file holds."""
    return {"camera": camera, "start": float(start), "end": float(end), "score": score,
            "at": float(at if at is not None else time.time())}


class FakeFrigate:
    """The upstream's observable behaviour, and nothing else.

    refusals and quiet map an event id to the first answer its sub-label should get
    back, and retain_refusals and retain_quiet the same for its keep: an HTTP status
    that is not 2xx, or an HTTP 200 that did not do the thing. Each is refused on the
    first attempt only, so the next sweep gets through and a retry is observable
    rather than assumed.
    """

    def __init__(self, rows=(), refusals=None, quiet=None, retain_refusals=None,
                 retain_quiet=None, stats="ok", latest=b"\xff\xd8still"):
        self.rows = list(rows)
        self.calls = []
        self.sub_labels = []
        self.retains = []
        self.deletes = []
        self.answers = []  # (write, event id, HTTP status), so a refusal can be asserted
        self.tag_attempts = {}
        self.retain_attempts = {}
        self.retain_bodies = {}
        self.stats = stats
        self.latest = latest
        # A refusal keeps the status it was given, because the point of a refusal here
        # is the status Frigate actually returns.
        self.refusals = dict(refusals or {})
        self.quiet = dict.fromkeys(quiet or ())
        self.retain_refusals = dict(retain_refusals or {})
        self.retain_quiet = dict.fromkeys(retain_quiet or ())

    # -- reads ---------------------------------------------------------------

    def _one(self, query, key, default=None):
        return (query.get(key) or [default])[0]

    def _read(self, query):
        # A row whose start_time is not a number cannot be placed on a cursor, so it
        # is sorted last and never compared with a bound. The worker's own walk drops
        # such a row for the same reason.
        rows = [item for item in self.rows]
        rows.sort(key=lambda item: item["start_time"]
                  if isinstance(item.get("start_time"), (int, float))
                  and not isinstance(item.get("start_time"), bool) else 0.0,
                  reverse=self._one(query, "sort", "date_desc") == "date_desc")
        before, after = self._one(query, "before"), self._one(query, "after")
        placed = [item for item in rows if isinstance(item.get("start_time"), (int, float))
                  and not isinstance(item.get("start_time"), bool)]
        if before is not None:
            placed = [item for item in placed if item["start_time"] < float(before)]
        if after is not None:
            placed = [item for item in placed if item["start_time"] > float(after)]
        limit = int(self._one(query, "limit", 100))
        return 200, json.dumps(placed[:limit]).encode()

    def _stats(self):
        if self.stats == "error":
            return 503, b'{"error": "unavailable"}'
        if self.stats == "not-json":
            return 200, b"not json at all"
        if self.stats == "zero":
            body = {"cameras": {"camera_2": {"camera_fps": 5.0, "process_fps": 0.0},
                                "camera_3": {"camera_fps": 5.0, "process_fps": 3.0}}}
            return 200, json.dumps(body).encode()
        if self.stats == "empty":
            return 200, b'{"cameras": {}}'
        if self.stats == "missing":
            return 200, b'{"cameras": {"camera_1": {"camera_fps": 5.0, "process_fps": 3.0}}}'
        body = {"cameras": {"camera_2": {"camera_fps": 5.0, "process_fps": 3.0},
                            "camera_3": {"camera_fps": 4.5, "process_fps": 2.9},
                            "camera_1": {"camera_fps": 0.0, "process_fps": 0.0}},
                "detectors": {"frigate": {"ffmpeg": 1.0, "infps": 5.0}},
                "service": {"uptime": 10, "version": "0.18.0"},
                "config_path": "/config/config.yml",
                "credential": CREDENTIAL_MARKER}
        return 200, json.dumps(body).encode()

    # -- writes --------------------------------------------------------------

    @staticmethod
    def _did_it(payload):
        """True when the reply is Frigate's own "it happened": 2xx with success:true.

        Decided here, from the JSON alone, rather than with the production helper:
        a fake that agreed with the code it is imitating would prove nothing about
        either. A 200 that says success:false is Frigate not doing the thing.
        """
        try:
            document = json.loads(payload.decode("utf-8", "replace"))
        except ValueError:
            return False
        return isinstance(document, dict) and document.get("success") is True

    def _attempt(self, write, event_id, attempts, refusals, quiet):
        attempts[event_id] = attempts.get(event_id, 0) + 1
        number = attempts[event_id]
        if event_id in refusals and number == 1:
            return refusals[event_id], b'{"error": "database is locked"}'
        if event_id in quiet and number == 1:
            return 200, b'{"success": false}'  # HTTP 200 that did not do the thing
        return 200, b'{"success": true}'

    def _sub_label(self, event_id, body):
        self.sub_labels.append((event_id, body))
        status, payload = self._attempt("sub_label", event_id, self.tag_attempts,
                                       self.refusals, self.quiet)
        self.answers.append(("sub_label", event_id, status))
        if 200 <= status < 300 and self._did_it(payload):
            self._store(event_id, "sub_label", body.get("subLabel"))
        return status, payload

    def _retain(self, event_id, body):
        self.retains.append((event_id, body))
        self.retain_bodies.setdefault(event_id, []).append(body)
        status, payload = self._attempt("retain", event_id, self.retain_attempts,
                                       self.retain_refusals, self.retain_quiet)
        self.answers.append(("retain", event_id, status))
        if 200 <= status < 300 and self._did_it(payload) \
                and isinstance(body, dict) and body.get("retain_indefinitely") is True:
            self._store(event_id, "retain_indefinitely", True)
        return status, payload

    def _store(self, event_id, key, value):
        for item in self.rows:
            if item["id"] == event_id:
                item[key] = value
                return item
        return None

    def handle(self, method, path, query, body):
        self.calls.append((method, path))
        if method == "GET" and path == "/api/stats":
            return self._stats()
        if method == "GET" and path == "/api/events":
            return self._read(query)
        if method == "GET" and path.startswith("/api/") and path.endswith("/latest.jpg"):
            if self.latest is None:
                return 404, b'{"error": "not found"}'
            return 200, self.latest
        if path.startswith("/api/events/"):
            event_id, _, action = path[len("/api/events/"):].rpartition("/")
            if method == "POST" and action == "sub_label":
                return self._sub_label(event_id, body)
            if method == "POST" and action == "retain":
                return self._retain(event_id, body)
            if method == "DELETE":
                self.deletes.append(event_id)
                return 200, b'{"success": true}'
            if method == "GET" and not action:
                stored = self._store(event_id, "id", event_id)
                if stored is None:
                    return 404, b'{"error": "not found"}'
                return 200, json.dumps(stored).encode()
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


def make_worker(base, folder, **kwargs):
    """A real Worker over HTTP to the fake, with a real pending file of its own.

    Every pass that touches the filesystem points the state file at a temporary
    directory, so this never reads or writes the production volume.
    """
    options = {"threshold": fighting.DEFAULT_THRESHOLD, "page_size": 2, "max_pages": 50,
               "model_dir": str(Path(folder) / "models")}
    options.update(kwargs)
    return fighting.Worker(retain.Api(base, 5), fighting.Pending(str(Path(folder) / "pending.json")),
                           options["threshold"], options["page_size"], options["max_pages"],
                           options["model_dir"])


def sweep(worker):
    """One matching pass with the output captured, as the loop would run it."""
    with contextlib.redirect_stdout(io.StringIO()) as out:
        worker.match_pending(time.time())
    return out.getvalue()


def ids(fake, which="sub_labels"):
    return sorted(event_id for event_id, _body in getattr(fake, which))


def clean_log(text):
    """Nothing printed may carry a body, a credential, a URL or a traceback."""
    for leak in ("Traceback", "rtsp://", "Authorization", CREDENTIAL_MARKER, "/api/"):
        assert leak not in text, f"{leak!r} reached the log:\n{text}"
    return text


# ---------------------------------------------------------------------------
# The contract with the model itself
# ---------------------------------------------------------------------------

class StubConfig:
    def __init__(self, **fields):
        self.__dict__.update(fields)


class StubModel:
    def __init__(self, **fields):
        self.config = StubConfig(**fields)


def check_model_contract():
    """The shape the pinned checkpoint actually has, and the ones that must refuse.

    The real checkpoint's own config carries the default placeholders LABEL_0 and
    LABEL_1 rather than words, and root loaded that exact revision with a clean
    loading report. So the placeholder pair has to be accepted, a spelled-out pair
    has to be accepted, a swapped pair must refuse rather than be read the other way
    round, and the frame count, the class count and the integer indices must all be
    exact.
    """
    for labels in ({0: "LABEL_0", 1: "LABEL_1"}, {0: "nonviolent", 1: "violent"},
                   {0: "NON-VIOLENT", 1: "Violent"}):
        fighting.check_contract(StubModel(num_frames=16, num_labels=2, id2label=labels))
    refused = 0
    for fields in (
        {"num_frames": 8, "num_labels": 2, "id2label": {0: "LABEL_0", 1: "LABEL_1"}},
        {"num_frames": 16, "num_labels": 3, "id2label": {0: "LABEL_0", 1: "LABEL_1"}},
        {"num_frames": 16, "num_labels": 2, "id2label": {0: "LABEL_1", 1: "LABEL_0"}},
        {"num_frames": 16, "num_labels": 2, "id2label": {0: "violent", 1: "nonviolent"}},
        {"num_frames": 16, "num_labels": 2, "id2label": {0: "LABEL_0", 1: "cat"}},
        {"num_frames": 16, "num_labels": 2, "id2label": {0: "LABEL_0"}},
        {"num_frames": 16, "num_labels": 2, "id2label": {0: "LABEL_0", 1: "LABEL_1", 2: "x"}},
        {"num_frames": 16, "num_labels": 2, "id2label": {"0": "LABEL_0", "1": "LABEL_1"}},
        {"num_frames": "16", "num_labels": 2, "id2label": {0: "LABEL_0", 1: "LABEL_1"}},
        {"num_frames": True, "num_labels": 2, "id2label": {0: "LABEL_0", 1: "LABEL_1"}},
    ):
        try:
            fighting.check_contract(StubModel(**fields))
        except fighting.ModelRefused:
            refused += 1
            continue
        raise AssertionError(f"a model of {fields} was not refused")
    assert fighting.VIOLENT_INDEX == 1, f"the violent class is index {fighting.VIOLENT_INDEX}"
    print(f"ok  the pinned placeholder pair and the spelled-out pair are both read as index 0 "
          f"nonviolent and index 1 violent, and {refused} wrong shapes refuse")


def check_cli():
    """A threshold outside (0, 1] is refused by argparse, before any request exists."""
    refused = 0
    with tempfile.TemporaryDirectory() as folder:
        state = str(Path(folder) / "pending.json")
        for value in ("0", "1.5", "-0.2", "nan", "inf", "not-a-number"):
            result = subprocess.run(
                [sys.executable, str(HERE / "fighting.py"), "--api-base", "http://127.0.0.1:1",
                 "--state-file", state, "--threshold", value],
                capture_output=True, text=True, timeout=60,
            )
            assert result.returncode == 2, f"--threshold {value} exited {result.returncode}"
            refused += 1
    for value in ("0.9", "1", "0.0001"):
        assert fighting.threshold_value(value) == float(value)
    print(f"ok  {refused} thresholds outside the range refused with exit 2, and 0.9, 1 and 0.0001 "
          "are taken as given")


# ---------------------------------------------------------------------------
# The temporal rule, the buffer and the freshness of an answer
# ---------------------------------------------------------------------------

def frames(count, seed=b"\xff\xd8frame"):
    """Distinct placeholder frame bytes. Not images: nothing here decodes them."""
    return [seed + str(index).encode() for index in range(count)]


class Feed:
    """One camera filled at 3 FPS on a synthetic clock, so `now` is always just after
    the newest frame. Nothing here decodes anything: the rules under test are about
    time, not pixels."""

    def __init__(self, window, clock=1000.0):
        self.window = window
        self.clock = clock
        self.taken = 0

    def fill(self, count=None):
        for index in range(count or fighting.WINDOW_FRAMES):
            self.clock += 1 / 3
            self.window.offer(f"frame{self.taken}-{index}".encode(), self.clock)
        self.taken += count or fighting.WINDOW_FRAMES
        return self.clock

    def take(self):
        taken = self.window.take(self.clock + 0.2)
        assert taken is not None, f"a full buffer did not yield a window at {self.clock}"
        return taken

    def scored(self, score):
        start, end, _frames, generation = self.last
        return self.window.scored(score, start, end, generation, self.clock + 0.4)


def check_temporal_rule():
    """Two continuous above-threshold windows confirm; anything between them does not.

    The rule is about time, so it is driven with timestamps rather than with a model:
    a low score in the middle, a gap longer than a second, a queue jump that leaves
    the two windows more than a second apart, and the cooldown after a confirmation.
    """
    window = fighting.CameraWindow("camera_2", 0.9)
    feed = Feed(window)
    feed.fill()
    feed.last = feed.take()
    assert len(feed.last[2]) == fighting.WINDOW_FRAMES, f"a window held {len(feed.last[2])} frames"
    assert feed.scored(0.95) is None, "one window confirmed on its own"
    feed.fill()
    feed.last = feed.take()
    assert feed.scored(0.10) is None
    assert window.streak.count == 0, "a low score left a vote standing"

    # Two continuous windows: confirmed, spanning the pair, at the higher score.
    window = fighting.CameraWindow("camera_2", 0.9)
    feed = Feed(window)
    feed.fill()
    feed.last = feed.take()
    first_start, first_end = feed.last[0], feed.last[1]
    assert feed.scored(0.91) is None, "one window of the pair confirmed on its own"
    feed.fill()
    feed.last = feed.take()
    assert feed.last[0] - first_end <= fighting.GAP_SECONDS, \
        "the check built a pair that is not continuous"
    interval = feed.scored(0.99)
    assert interval is not None, "two continuous windows above the threshold did not confirm"
    assert interval["start"] == first_start and interval["end"] == feed.last[1], \
        f"the interval is {interval}"
    assert interval["score"] == 0.99, f"the interval score is {interval['score']}"

    # The cooldown drops the pair that would otherwise confirm, and then expires.
    feed.fill()
    feed.last = feed.take()
    assert feed.scored(0.99) is None, "a third window confirmed"
    feed.fill()
    feed.last = feed.take()
    assert feed.scored(0.99) is None, "a pair inside the cooldown confirmed"
    feed.clock += fighting.COOLDOWN_SECONDS
    feed.fill()
    feed.last = feed.take()
    assert feed.scored(0.99) is None
    feed.fill()
    feed.last = feed.take()
    assert feed.scored(0.99) is not None, "a pair after the cooldown did not confirm"

    # Two windows that never touched cannot confirm, however high they score.
    window = fighting.CameraWindow("camera_3", 0.9)
    feed = Feed(window)
    feed.fill()
    feed.last = feed.take()
    assert feed.scored(0.99) is None
    feed.clock += 30.0
    feed.fill()
    feed.last = feed.take()
    assert feed.scored(0.99) is None, "two windows 30 s apart confirmed"
    assert window.streak.count == 1, "the second of the pair did not become a single vote"
    print("ok  two continuous above-threshold windows confirm an interval spanning the pair, and "
          "a low score, a gap, a queue jump and the cooldown each refuse")


def check_capture_rules():
    """Sixteen identical frames, a gap, a dropped buffer and a stale window are all refused."""
    assert fighting.all_identical([b"same"] * fighting.WINDOW_FRAMES), "identical frames missed"
    assert not fighting.all_identical(frames(fighting.WINDOW_FRAMES)), \
        "sixteen distinct frames were called identical"

    window = fighting.CameraWindow("camera_2", 0.9)
    for index in range(fighting.WINDOW_FRAMES - 1):
        window.offer(f"f{index}", 1000.0 + index / 3)
    assert window.take(1006.0) is None, "a short buffer produced a window"
    # A gap over a second empties the window, so the 16th frame starts it again.
    window.offer("late", 1000.0 + (fighting.WINDOW_FRAMES - 1) / 3 + 1.5)
    assert window.take(1006.0) is None, "a gap of a second and a half did not empty the window"
    assert window.gaps == 1, f"the gap was counted {window.gaps} times"

    # A buffer that overflows loses its oldest frame, so the votes go with it and the
    # window that comes out is a whole one rather than one with a hole in it.
    window = fighting.CameraWindow("camera_2", 0.9)
    for index in range(fighting.BUFFER_FRAMES):
        window.offer(f"f{index}", 1000.0 + index / 3)
    last_frame = 1000.0 + (fighting.BUFFER_FRAMES - 1) / 3
    taken = window.take(last_frame + 0.2)
    assert window.scored(0.99, taken[0], taken[1], taken[3], last_frame + 0.4) is None
    assert window.streak.count == 1, "the first window of a pair did not vote"
    assert window.score == 0.99, "a scored window did not leave its score behind"
    # Sixteen more frames fill the buffer again; the one after those drops the oldest.
    for step in range(1, fighting.WINDOW_FRAMES + 2):
        window.offer(f"f{step}", last_frame + step / 3)
    assert window.streak.count == 0, "a dropped frame left the vote standing"
    assert window.score is None, "a dropped frame left the score from the run before it"
    after = window.take(last_frame + (fighting.WINDOW_FRAMES + 2) / 3 + 0.2)
    assert after is not None, "the buffer could not be read after an overflow"
    assert after[0] > taken[0], "the overflowed buffer handed back a window with a hole in it"
    assert len(window._frames) == fighting.BUFFER_FRAMES - fighting.WINDOW_FRAMES, \
        f"{len(window._frames)} frames were left after one window"

    # A window that waited longer than the staleness bound is refused outright.
    window = fighting.CameraWindow("camera_2", 0.9)
    for index in range(fighting.WINDOW_FRAMES):
        window.offer(f"g{index}", 1000.0 + index / 3)
    assert window.take(1005.0 + fighting.STALE_SECONDS + 1) is None, \
        "a window that waited over the staleness bound was scored"

    # An answer that comes back for a generation which has moved on is discarded, and
    # so is one whose window has aged out while the model was thinking.
    window = fighting.CameraWindow("camera_2", 0.9)
    for index in range(fighting.WINDOW_FRAMES):
        window.offer(f"h{index}", 1000.0 + index / 3)
    taken = window.take(1005.2)
    window.offer("after", 1005.4)  # the same run: this frame is the one after the window
    assert window.scored(0.95, taken[0], taken[1], taken[3], 1005.6) is None, \
        "one window confirmed on its own"
    window.fail(1005.8)
    assert window.scored(0.99, taken[0], taken[1], taken[3], 1006.0) is None, \
        "an answer survived a capture failure"
    assert window.score is None, f"a discarded answer left the score at {window.score}"
    assert window.scored(0.99, taken[0], taken[1], taken[3], taken[1] + 30.0) is None, \
        "an answer whose window had aged out was kept"
    print("ok  identical frames, a one second gap, an overflowing buffer and a window that waited "
          "too long are all refused, and an answer for a run that has moved on is discarded")


def check_window_states():
    """Warming is bounded, a refused model hides the score, and stale capture is unavailable."""
    now = time.time()
    window = fighting.CameraWindow("camera_2", 0.9)
    window.started(now)
    window.offer("frame", now)
    assert window.snapshot(now, True)["state"] == fighting.WARMING, "a fresh camera was not warming"
    assert window.snapshot(now + fighting.STALE_SECONDS + 1, True)["state"] == fighting.UNAVAILABLE, \
        "warming outlived its own bound"
    window = fighting.CameraWindow("camera_2", 0.9)
    window.started(now)
    window.offer("frame", now)
    assert window.snapshot(now, False)["state"] == fighting.UNAVAILABLE, \
        "a camera was reported under a model that is not usable"
    # A score is reported only under a usable model, and only while it is recent.
    window = fighting.CameraWindow("camera_2", 0.9)
    window.started(now)
    for index in range(fighting.WINDOW_FRAMES):
        window.offer(f"k{index}", now + index / 3)
    taken = window.take(now + 5.2)
    window.offer("after", now + 5.4)
    window.scored(0.95, taken[0], taken[1], taken[3], now + 5.6)
    live = window.snapshot(now + 5.7, True)
    assert live["state"] == fighting.ANALYZING, f"a scored camera reported {live['state']}"
    assert live["score"] == 0.95, f"the score is {live['score']}"
    assert window.snapshot(now + 5.7, False)["score"] is None, \
        "a score was reported under a refused model"
    # Frames arriving again do not make an old score current.
    clock = now + 5.9
    for index in range(40):
        clock += 1 / 3
        window.offer(f"n{clock}".encode(), clock)
    stale = window.snapshot(clock + 0.2, True)
    assert stale["state"] == fighting.UNAVAILABLE, \
        f"a score older than the bound was reported as {stale['state']}"
    assert stale["score"] is None, "a stale score was reported as current"
    assert stale["last_capture"] is not None, "the current frame was not reported either"
    # Repeated inference failures end in unavailable, not in warming forever.
    window = fighting.CameraWindow("camera_2", 0.9)
    window.started(now)
    clock = now
    for _attempt in range(4):
        for index in range(fighting.WINDOW_FRAMES):
            clock += 1 / 3
            window.offer(f"n{clock}".encode(), clock)
        window.reject(window.take(clock + 0.2)[3])
    assert window.score is None, "a refused window left a score behind"
    refused = window.snapshot(clock + 0.4, True)
    assert refused["state"] == fighting.UNAVAILABLE, \
        f"a camera that never scored is still {refused['state']}"
    assert refused["last_capture"] is not None, "and its frames were not current"
    print("ok  warming is bounded by its own ten seconds, a model that is not usable reports "
          "unavailable with no score, and a camera that never scored stops saying warming")


# ---------------------------------------------------------------------------
# The pending file
# ---------------------------------------------------------------------------

def check_pending_file():
    """A saved interval survives a restart, a file this cannot read is refused whole, and a
    save that cannot land leaves the previous bytes exactly as they were."""
    with tempfile.TemporaryDirectory() as folder:
        path = str(Path(folder) / "pending.json")
        pending = fighting.Pending(path)
        assert pending.candidates == [], "a fresh file came back with intervals"
        assert pending.add(candidate(camera="camera_2")), "the first interval did not save"
        assert pending.add(candidate(camera="camera_3", start=BASE + 100, end=BASE + 300)), \
            "the second interval did not save"
        saved = json.loads(Path(path).read_text())
        assert set(saved) == {"version", "candidates"}, f"the file holds {sorted(saved)}"
        assert saved["version"] == fighting.STATE_VERSION
        assert [row["camera"] for row in saved["candidates"]] == ["camera_2", "camera_3"], \
            f"the file holds {[row['camera'] for row in saved['candidates']]}"

        # A restart comes back to both, and they are the same rows.
        restarted = fighting.Pending(path)
        assert restarted.error is None, f"a saved file was reported as {restarted.error}"
        assert [row["camera"] for row in restarted.candidates] == ["camera_2", "camera_3"]
        assert all(set(row) == fighting.CANDIDATE_KEYS for row in restarted.candidates), \
            "a restored interval does not hold exactly the known keys"

        # The ceiling drops the oldest rather than growing without end.
        crowd = BASE - 4000  # far enough back that every one of these is in the past
        crowded = fighting.Pending(str(Path(folder) / "crowded.json"))
        with contextlib.redirect_stdout(io.StringIO()) as churn:
            for index in range(fighting.MAX_PENDING):
                start = crowd + index * 60
                assert crowded.add(candidate(start=start, end=start + 30, at=start + 30)), \
                    f"interval {index} did not save"
            assert len(crowded.candidates) == fighting.MAX_PENDING, \
                f"{len(crowded.candidates)} intervals are held past the ceiling"
            last = crowd + fighting.MAX_PENDING * 60
            assert crowded.add(candidate(start=last, end=last + 30, at=last + 30)), \
                "the interval that crosses the ceiling did not save"
        assert len(churn.getvalue().splitlines()) == 1, \
            f"the drop of the oldest interval was reported {len(churn.getvalue().splitlines())} times"
        assert len(crowded.candidates) == fighting.MAX_PENDING
        assert crowded.candidates[-1]["start"] == last, "the newest interval is not last"

        # Six hours, and not an instant more, on a file of its own.
        aged_path = str(Path(folder) / "aged.json")
        aged = fighting.Pending(aged_path)
        old = time.time() - fighting.PENDING_TTL_SECONDS - 5
        assert aged.add(candidate(start=old - 400, end=old - 300, at=old)), \
            "an old interval was refused"
        assert aged.expire(), "an interval older than six hours was not dropped"
        assert aged.candidates == [], f"expired intervals are still held: {aged.candidates}"
        assert aged.save(), "the expiry did not save"
        assert fighting.Pending(aged_path).candidates == [], "an expired interval was saved"

        # Every way of being a file this does not read.
        refusals = (
            ("not json", "{not json"),
            ("a bare array", "[]"),
            ("a version of true", '{"version": true, "candidates": []}'),
            ("an unknown version", '{"version": 9, "candidates": []}'),
            ("a third key", '{"version": 1, "candidates": [], "extra": 1}'),
            ("a missing key", '{"version": 1}'),
            ("a candidates object", '{"version": 1, "candidates": {}}'),
            ("a list over the ceiling",
             json.dumps({"version": 1, "candidates": [candidate(at=time.time())] *
                         (fighting.MAX_PENDING + 1)})),
            ("camera_1 in it", json.dumps({"version": 1, "candidates": [candidate("camera_1")]})),
            ("a camera that is not a camera",
             json.dumps({"version": 1, "candidates": [candidate("camera_9")]})),
            ("an end before its start",
             json.dumps({"version": 1, "candidates": [candidate(start=END, end=BASE)]})),
            ("an end equal to its start",
             json.dumps({"version": 1, "candidates": [candidate(start=BASE, end=BASE)]})),
            ("a confirmation before the window ended",
             json.dumps({"version": 1, "candidates": [candidate(at=BASE)]})),
            ("a future confirmation",
             json.dumps({"version": 1,
                         "candidates": [candidate(at=time.time() + 86400)]})),
            ("an infinite score",
             json.dumps({"version": 1, "candidates": [candidate(score=1e400)]})),
            ("a score above one", json.dumps({"version": 1, "candidates": [candidate(score=1.5)]})),
            ("a zero score", json.dumps({"version": 1, "candidates": [candidate(score=0.0)]})),
            ("a string time",
             json.dumps({"version": 1,
                         "candidates": [{"camera": "camera_2", "start": "now", "end": BASE,
                                         "score": 0.9, "at": BASE}]})),
            ("a boolean time",
             json.dumps({"version": 1,
                         "candidates": [{"camera": "camera_2", "start": True, "end": BASE,
                                         "score": 0.9, "at": BASE}]})),
            ("a per-event key left in",
             json.dumps({"version": 1,
                         "candidates": [dict(candidate(), matches=[{"event": "1.0-abcdef"}])]})),
            ("an oversized file", '{"pad": "' + "x" * (fighting.MAX_STATE_BYTES + 64) + '"}'),
        )
        for label, text in refusals:
            Path(path).write_text(text)
            broken = fighting.Pending(path)
            assert broken.candidates == [], f"a file that is {label} was read as intervals"
            assert broken.error == fighting.STATE_UNREADABLE, \
                f"a file that is {label} was not reported as unreadable: {broken.error}"

        # A save that cannot land changes no bytes and says so.
        good = fighting.Pending(path)
        assert good.add(candidate()), "the interval before the blocked save did not save"
        before = Path(path).read_bytes()
        blocked = Path(folder) / "blocked"
        blocked.mkdir()
        refused = fighting.Pending(str(blocked))
        assert not refused.add(candidate()), "a save into a directory reported success"
        assert refused.error, "a refused save left no error note"
        assert list(blocked.iterdir()) == [], \
            f"a refused save left something behind: {list(blocked.iterdir())}"
        assert len(refused.candidates) == 1, "a refused save dropped the interval from memory"
        assert fighting.Pending(path).candidates == good.candidates, \
            "the previous file was not left whole"
        assert Path(path).read_bytes() == before, "the previous bytes changed"
        assert not Path(path).with_name("pending.json.new").exists(), "scratch was left behind"
    print(f"ok  a saved interval is read back by a new process, the ceiling holds at "
          f"{fighting.MAX_PENDING} with the oldest dropped, six hours expires, {len(refusals)} "
          "files this cannot read are refused whole, and a save that cannot land keeps the "
          "previous bytes and says so")


# ---------------------------------------------------------------------------
# Matching, over disposable HTTP
# ---------------------------------------------------------------------------

def gates_fixture():
    """One row per decision the worker has to make for itself, all inside the window.

    Every tag is exactly six lowercase alphanumerics, so a row reaches the check it is
    here for rather than being counted as one this cannot address: a seven character
    tag would be counted at the walk and never reach a gate at all.
    """
    return [
        row("keep01", "camera_2", BASE + 10, BASE + 40),                      # 0  labelled
        row("keep02", "camera_2", BASE + 12, BASE + 44),                      # 1  labelled
        row("open01", "camera_2", BASE + 20),                                 # 2  still running
        row("done01", "camera_2", BASE + 30, BASE + 50,                       # 3  nothing to do
            sub_label=fighting.SUB_LABEL, retain_indefinitely=True),
        row("half01", "camera_2", BASE + 32, BASE + 52,                       # 4  keep only
            sub_label=fighting.SUB_LABEL),
        row("twol01", "camera_2", BASE + 34, BASE + 54,                       # 5  keep only
            sub_label=[fighting.SUB_LABEL, 0.4]),
        row("other1", "camera_2", BASE + 36, BASE + 56,                       # 6  left alone
            sub_label="reviewed_by_hand"),
        row("weird1", "camera_2", BASE + 38, BASE + 58,                       # 7  left alone
            sub_label={"word": "unknown shape"}),
        row("malfor", "camera_2", BASE + 40, BASE + 60,                       # 8  left alone
            sub_label=[fighting.SUB_LABEL, "not a number"]),
        row("quiet1", "camera_2", BASE + 42, BASE + 62),                       # 9  200, no work
        row("wrong1", "camera_3", BASE + 10, BASE + 40),                       # 10 other camera
        row("dog001", "camera_2", BASE + 12, BASE + 44, label="dog"),         # 11 not a person
        row("noclp1", "camera_2", BASE + 14, BASE + 46, snapshot=False),      # 12 no snapshot
        row("early1", "camera_2", BASE - 300, BASE - 200),                    # 13 no overlap
        row("late01", "camera_2", END + 100, END + 160),                      # 14 no overlap
        row("tuch01", "camera_2", END - 1, END + 40),                         # 15 one instant
        {"id": "not-a-native-id", "camera": "camera_2", "start_time": float(BASE + 18),
         "end_time": float(BASE + 48), "label": "person", "has_snapshot": True, "zones": []},
        {"id": f"{BASE + 19}.123456-badtim", "camera": "camera_2", "start_time": "soon",
         "end_time": None, "label": "person", "has_snapshot": True, "zones": []},
    ]


def check_event_gates():
    """Exactly the rows that overlap are written to, with exactly the agreed requests.

    The evidence is the request stream, not the worker's own opinion: which ids were
    labelled, which were kept, the exact body of each, and the fact that a refused
    label produces no keep at all.
    """
    refused = row("keep02", "camera_2", BASE + 12, BASE + 44)["id"]
    quiet = row("quiet1", "camera_2", BASE + 42, BASE + 62)["id"]
    fake = FakeFrigate(gates_fixture(), refusals={refused: 503}, quiet={quiet})
    with tempfile.TemporaryDirectory() as folder, serving(fake) as base:
        worker = make_worker(base, folder)
        assert worker.pending.add(candidate()), "the interval did not save"
        text = clean_log(sweep(worker))
        overlapping = sorted(ids_of(fake, tag) for tag in
                             ("keep01", "keep02", "open01", "quiet1", "tuch01"))
        assert ids(fake) == overlapping, \
            f"labelled {ids(fake)}, expected {overlapping}\n{text}"
        # Both refusals came back as the statuses they were set up with, and neither
        # touched its row: a 503 and a 200 that says success:false are both not done.
        assert ("sub_label", refused, 503) in fake.answers, \
            f"the refused label answered {fake.answers}"
        assert ("sub_label", quiet, 200) in fake.answers, f"the quiet label answered {fake.answers}"
        for event_id in (refused, quiet):
            row_after = next(item for item in fake.rows if item["id"] == event_id)
            assert "sub_label" not in row_after, \
                f"a refused label was written onto the row anyway: {row_after}"
            assert event_id not in [kept for kept, _ in fake.retains], \
                f"an unlabelled row was kept anyway: {fake.retains}"
        # The exact request: the label word, the score, and the camera it belongs to.
        bodies = {event_id: body for event_id, body in fake.sub_labels}
        assert set(bodies[ids_of(fake, "keep01")]) == {"subLabel", "subLabelScore", "camera"}, \
            f"the label body is {bodies[ids_of(fake, 'keep01')]}"
        assert bodies[ids_of(fake, "keep01")] == {"subLabel": fighting.SUB_LABEL,
                                                 "subLabelScore": 0.93,
                                                 "camera": "camera_2"}, \
            f"the label body is {bodies[ids_of(fake, 'keep01')]}"
        assert bodies[ids_of(fake, "open01")]["camera"] == "camera_2", \
            "a running track was labelled on another camera"
        assert [body["retain_indefinitely"] for _id, body in fake.retains] == \
            [True] * len(fake.retains), \
            f"a keep asked for something other than an indefinite keep: {fake.retain_bodies}"
        assert sorted(fake.retain_bodies) == sorted([ids_of(fake, tag) for tag in
                                                     ("keep01", "open01", "tuch01",
                                                      "half01", "twol01")]), \
            f"kept {sorted(fake.retain_bodies)}"
        # Nothing outside the interval, and nothing carrying another label.
        for tag, why in (("wrong1", "another camera"), ("dog001", "an animal"),
                         ("noclp1", "a row without a snapshot"), ("early1", "a row before it"),
                         ("late01", "a row after it"),
                         ("not-a-native-id", "an id this cannot address"),
                         ("badtim", "a start_time that is not a number"),
                         ("other1", "a row another person had labelled"),
                         ("weird1", "a sub-label shape this cannot read"),
                         ("malfor", "a sub-label whose score is not a number"),
                         ("done01", "a row already labelled and already kept")):
            for name in ids(fake) + ids(fake, "retains"):
                assert not name.endswith(tag), f"{why} was written to: {name}"
        assert ids_of(fake, "half01") not in ids(fake), \
            "a row already carrying our label was labelled again"
        assert ids_of(fake, "twol01") not in ids(fake), \
            "our label as [label, score] was not read as already labelled"

        # Both refusals are retried, land once, are kept, and are never attempted again.
        clean_log(sweep(worker))
        for event_id in (refused, quiet):
            assert ids(fake).count(event_id) == 2, \
                f"{event_id} was attempted {ids(fake).count(event_id)} times, not twice"
            assert event_id in ids(fake, "retains"), f"{event_id} was retried but not kept"
            row_after = next(item for item in fake.rows if item["id"] == event_id)
            assert row_after["sub_label"] == fighting.SUB_LABEL, f"the retry did not land on {event_id}"
            assert row_after["retain_indefinitely"] is True, f"{event_id} was not kept upstream"
        clean_log(sweep(worker))
        for event_id in (refused, quiet):
            assert ids(fake).count(event_id) == 2, "a label that had landed was written again"
        assert len(fake.deletes) == 0, f"a DELETE was sent: {fake.deletes}"
    print("ok  every row that overlaps is written to and nothing else: another camera, an animal, "
          "a row without a snapshot, rows outside the interval, two unaddressable ids, a running "
          "track and four rows carrying a label this must not touch are all left alone")


def ids_of(fake, tag):
    """The one id in the fixture whose tag is this, or a list of what is there."""
    matches = [row["id"] for row in fake.rows if row["id"].endswith(tag)]
    assert matches, f"no row carries the tag {tag}: {sorted(row['id'] for row in fake.rows)}"
    return matches[0]


def check_retry_and_late_snapshot():
    """A refused keep is retried on its own, and a track snapshotted later is caught."""
    late = row("late01", "camera_2", BASE + 14, BASE + 44, snapshot=False)
    fake = FakeFrigate([late])
    with tempfile.TemporaryDirectory() as folder, serving(fake) as base:
        worker = make_worker(base, folder)
        assert worker.pending.add(candidate()), "the interval did not save"

        # First sweep: the row has no snapshot, so it is refused and nothing is written.
        clean_log(sweep(worker))
        assert fake.sub_labels == [] and fake.retains == [], \
            f"a row without a snapshot was written to: {fake.sub_labels}"
        # Frigate finalises the snapshot afterwards, which is the ordinary case.
        late["has_snapshot"] = True
        late["snapshot"] = "camera_2/snapshot.jpg"
        clean_log(sweep(worker))
        assert ids(fake) == [late["id"]], f"the finalised snapshot was not caught: {ids(fake)}"
        assert [event_id for event_id, _ in fake.retains] == [late["id"]]

        # A keep that is refused is retried, and the label is not written again.
        second = row("keep99", "camera_2", BASE + 16, BASE + 46)
        fake.rows.append(second)
        fake.retain_refusals = {second["id"]: 500}
        clean_log(sweep(worker))
        assert [event_id for event_id, _ in fake.retains].count(second["id"]) == 1, \
            f"the refused keep was attempted {len(fake.retains)} times over"
        assert ids(fake).count(second["id"]) == 1, "the label was written more than once"
        assert second["sub_label"] == fighting.SUB_LABEL, "the label did not land"
        clean_log(sweep(worker))
        assert [event_id for event_id, _ in fake.retains].count(second["id"]) == 2, \
            f"the refused keep was not retried: {fake.retains}"
        assert ids(fake).count(second["id"]) == 1, "the label was written again on the retry"
        assert fake.retain_bodies[second["id"]] == [{"retain_indefinitely": True},
                                                    {"retain_indefinitely": True}], \
            f"a keep asked for something else: {fake.retain_bodies}"
        assert fake.deletes == [], f"a DELETE was sent: {fake.deletes}"
    print("ok  a track snapshotted after the window that named it is caught on a later sweep, a "
          "refused label produces no keep and is retried, and a refused keep is retried on its own")


def check_overlapping_intervals():
    """Two intervals over one track in one sweep write to that track once."""
    shared = row("share1", "camera_2", BASE + 20, BASE + 60)
    other = row("other1", "camera_2", BASE + 25, BASE + 65)
    fake = FakeFrigate([shared, other])
    with tempfile.TemporaryDirectory() as folder, serving(fake) as base:
        worker = make_worker(base, folder)
        assert worker.pending.add(candidate(start=BASE, end=BASE + 30)), "the first did not save"
        assert worker.pending.add(candidate(start=BASE + 35, end=BASE + 90)), \
            "the second did not save"
        clean_log(sweep(worker))
        assert ids(fake).count(shared["id"]) == 1, \
            f"the shared track was labelled {ids(fake).count(shared['id'])} times"
        assert [event_id for event_id, _ in fake.retains].count(shared["id"]) == 1, \
            "the shared track was kept more than once"
        assert sorted(ids(fake)) == sorted([shared["id"], other["id"]]), \
            f"labelled {ids(fake)}"
        clean_log(sweep(worker))
        assert ids(fake) == sorted([shared["id"], other["id"]]), "the second sweep wrote again"
        assert [event_id for event_id, _ in fake.retains] == sorted([shared["id"], other["id"]]), \
            "the second sweep kept something again"
        assert len(fake.deletes) == 0, f"a DELETE was sent: {fake.deletes}"
        assert all(body == {"retain_indefinitely": True}
                   for bodies in fake.retain_bodies.values() for body in bodies), \
            f"a keep asked for something else: {fake.retain_bodies}"
    print("ok  two pending intervals over one track in one sweep write to that track once, and a "
          "second sweep writes nothing at all")


def check_no_pending_no_walk():
    """With nothing pending the archive is not read at all."""
    fake = FakeFrigate(gates_fixture())
    with tempfile.TemporaryDirectory() as folder, serving(fake) as base:
        worker = make_worker(base, folder)
        clean_log(sweep(worker))
        assert [path for _method, path in fake.calls] == [], \
            f"an idle worker called the upstream: {fake.calls}"
    print("ok  a worker with nothing pending sends no request at all")


def watching(window, now):
    """Put one camera in the state it is in while it is genuinely being watched:
    frames arriving, a window scored, and a fresh score behind it."""
    window.started(now)
    for index in range(fighting.WINDOW_FRAMES):
        window.offer(f"frame{index}".encode(), now + index / 3)
    taken = window.take(now + 5.2)
    window.offer("after", now + 5.4)
    window.scored(0.95, taken[0], taken[1], taken[3], now + 5.6)
    return window


def states_of(worker, now):
    return {camera: worker.status(now)["cameras"][camera]["state"] for camera in fighting.WATCHED}


def check_stats_gate():
    """A camera is only reported as watched while Frigate says it is running.

    Frigate keeps returning its last JPEG after a camera or the detector stops, so
    fresh frames are not evidence that anything is being processed. Every camera here
    has fresh frames, a fresh score and a usable model, and the only thing that
    changes is the stats route: one camera drops to unavailable, both do when the
    route is not answering at all, and both do once the sample itself is older than
    the staleness bound even though fresh frames and a fresh score are still there.
    """
    with tempfile.TemporaryDirectory() as folder:
        for state, expected in (("ok", {camera: fighting.ANALYZING for camera in fighting.WATCHED}),
                                ("zero", {"camera_2": fighting.UNAVAILABLE,
                                          "camera_3": fighting.ANALYZING}),
                                ("missing", {camera: fighting.UNAVAILABLE
                                             for camera in fighting.WATCHED}),
                                ("empty", {camera: fighting.UNAVAILABLE
                                           for camera in fighting.WATCHED}),
                                ("error", {camera: fighting.UNAVAILABLE
                                           for camera in fighting.WATCHED}),
                                ("not-json", {camera: fighting.UNAVAILABLE
                                              for camera in fighting.WATCHED})):
            fake = FakeFrigate(stats=state)
            with serving(fake) as base:
                worker = make_worker(base, folder)
                worker.model_state = fighting.READY
                now = time.time()
                for camera in fighting.WATCHED:
                    watching(worker.windows[camera], now)
                worker.live_cameras(now)  # the one sample the inference loop takes
                assert states_of(worker, now) == expected, \
                    f"stats {state} reported {states_of(worker, now)}, expected {expected}"
                # The same answer is given again with no further request at all.
                assert not [call for call in fake.calls[1:] if call[1] == "/api/stats"], \
                    "the status read asked for stats again"
        # A sample that has aged out gates every camera shut, frames or no frames.
        fake = FakeFrigate(stats="ok")
        with serving(fake) as base:
            worker = make_worker(base, folder)
            worker.model_state = fighting.READY
            now = time.time()
            for camera in fighting.WATCHED:
                watching(worker.windows[camera], now)
            worker.live_cameras(now)
            later = now + fighting.STALE_SECONDS + 1
            for camera in fighting.WATCHED:  # frames and a score that are both current
                watching(worker.windows[camera], later)
            aged = {camera: fighting.UNAVAILABLE for camera in fighting.WATCHED}
            assert states_of(worker, later) == aged, \
                f"a stats sample older than {fighting.STALE_SECONDS}s reported {states_of(worker, later)}"
    print("ok  a camera is reported as watched only while both of its frame rates are positive, a "
          "route that is not answering gates both shut, and so does a sample older than its own "
          "bound, whatever the frames and the score say")


# ---------------------------------------------------------------------------
# The one status read
# ---------------------------------------------------------------------------

def ask(base, path, method="GET", host="127.0.0.1:8088", headers=None):
    """One request to the status listener with an exact Host, and its whole reply."""
    address = base.split("//", 1)[1]
    connection = http.client.HTTPConnection(address, timeout=5)
    try:
        connection.request(method, path, headers={"Host": host, **(headers or {})})
        response = connection.getresponse()
        return response.status, {key.lower(): value for key, value in response.getheaders()}, \
            response.read()
    finally:
        connection.close()


@contextlib.contextmanager
def status_listener(worker):
    server = fighting.status_server(worker, ("127.0.0.1", 0))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def check_status():
    """One exact GET, one allowlist, no query, no write, and nothing leaky in the answer."""
    fake = FakeFrigate(gates_fixture())
    with tempfile.TemporaryDirectory() as folder, serving(fake) as base, \
            contextlib.redirect_stdout(io.StringIO()) as printed:
        worker = make_worker(base, folder)
        assert worker.pending.add(candidate()), "the interval did not save"
        with status_listener(worker) as status_base:
            good, headers, body = ask(status_base, fighting.STATUS_PATH)
            assert good == 200, f"GET {fighting.STATUS_PATH} answered {good}"
            payload = json.loads(body)
            assert set(payload) == STATUS_KEYS, f"the answer holds {sorted(payload)}"
            assert payload["experimental"] is True, "the answer does not say it is experimental"
            assert payload["threshold"] == fighting.DEFAULT_THRESHOLD
            assert payload["model_revision"] == fighting.MODEL_REVISION
            assert payload["pending"] == 1, f"the answer says {payload['pending']} pending"
            assert set(payload["cameras"]) == set(fighting.WATCHED), \
                f"the answer names {sorted(payload['cameras'])}"
            for camera, row in payload["cameras"].items():
                assert set(row) == CAMERA_KEYS, f"{camera} holds {sorted(row)}"
                assert row["state"] == fighting.UNAVAILABLE, \
                    f"{camera} reported {row['state']} with no frames and no model"
            assert headers["content-type"] == "application/json"
            assert "access-control-allow-origin" not in headers, "a CORS header was sent"
            for leak in ("frigate:", "/api/", CREDENTIAL_MARKER, "config", "Traceback",
                         "/models", "/state"):
                assert leak not in body.decode(), f"{leak!r} reached the status answer"

            for path, method, host, expected in (
                ("/status?camera=camera_2", "GET", "127.0.0.1:8088", 400),
                ("/status", "POST", "127.0.0.1:8088", 405),
                ("/status", "DELETE", "127.0.0.1:8088", 405),
                ("/", "GET", "127.0.0.1:8088", 404),
                ("/api/snapshots/delete", "GET", "127.0.0.1:8088", 404),
                (fighting.STATUS_PATH, "GET", "evil.example:8088", 403),
                (fighting.STATUS_PATH, "GET", "localhost:9999", 403),
            ):
                refused, _headers, refused_body = ask(status_base, path, method=method, host=host)
                assert refused == expected, \
                    f"{method} {path} from {host} answered {refused}, expected {expected}"
                assert json.loads(refused_body)["error"], "a refusal was not explained"
            # The dashboard's own other address is answered too.
            answered, _headers, _body = ask(status_base, fighting.STATUS_PATH,
                                            host="localhost:8088")
            assert answered == 200, f"the dashboard's own Host answered {answered}"
        # A refused model changes the answer and removes the score from it.
        worker.model_state = fighting.REFUSED
        worker.windows["camera_2"].offer("frame", time.time())
        worker.model = None
        with status_listener(worker) as status_base:
            _status, _headers, body = ask(status_base, fighting.STATUS_PATH)
        refused_payload = json.loads(body)
        assert refused_payload["model_state"] == fighting.REFUSED, \
            f"the model reads {refused_payload['model_state']}"
        assert refused_payload["cameras"]["camera_2"]["state"] == fighting.UNAVAILABLE, \
            "a camera reported something other than unavailable under a refused model"
        assert refused_payload["cameras"]["camera_2"]["score"] is None, \
            "a score was reported under a refused model"
        assert fake.deletes == [], f"a DELETE was sent: {fake.deletes}"
        clean_log(printed.getvalue())
    print("ok  one exact GET with the dashboard's Host answers, a query, every other method, every "
          "other path and every other Host are refused, no CORS header is sent, and a refused model "
          "reports unavailable with no score")


def main():
    parser = argparse.ArgumentParser(description="fighting sidecar boundary check")
    parser.parse_args()
    for package in ("torch", "transformers", "PIL", "numpy"):
        assert package not in sys.modules, f"importing fighting pulled in {package}"
    check_model_contract()
    check_cli()
    check_temporal_rule()
    check_capture_rules()
    check_window_states()
    check_pending_file()
    check_event_gates()
    check_retry_and_late_snapshot()
    check_overlapping_intervals()
    check_no_pending_no_walk()
    check_stats_gate()
    check_status()
    print("\nPASS  every assertion held")
    print("note  none of this says anything about whether the model detects a fight")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as err:
        print(f"\nFAIL  {err}")
        sys.exit(1)