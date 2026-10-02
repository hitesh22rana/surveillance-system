#!/usr/bin/env python3
"""Experimental: watch cameras 2 and 3 for what might be two people fighting.

Where the retention worker keeps one person snapshot for review, this looks at the
whole frame of two rooms at 3 FPS, in memory, and asks one small local model
whether a 16 frame clip looks violent. Two consecutive windows on one camera at or
above the threshold confirm an interval, and every native person event overlapping
that interval is labelled possible_fighting and kept indefinitely, so the archive
and the operator's own deletion path see these snapshots like any other.

The label is a possibility and never a finding. A hug, a dance and a fight can
look the same to this model, nothing here has been measured on these two rooms,
and the threshold is a number somebody chose rather than a calibrated accuracy. A
person looks at every snapshot this labels. docs/fighting.md has the rest.

Cameras 2 and 3 only, whatever anybody has armed: this is not part of Home and
Away, nothing on the dashboard reaches it, and no file can switch it off.

Two capture threads read each camera's latest.jpg at 3 FPS. One inference thread
holds one model on two CPU threads and takes whole nonoverlapping windows out of
a buffer of the newest 32 frames. Frames live in RAM, no video file is written,
and a gap over a second, a failed read, a dropped buffer or a window of sixteen
identical stills discards the window and the votes with it. A window is scored
only while Frigate's own /api/stats reports that camera's frame rates positive.

The model comes from a local directory and nowhere else: local_files_only,
safetensors, no remote code, no download, and a refusal rather than a guess if one
weight is missing, unexpected or mismatched, if the model is not 16 frames of the
two classes its model card means, or if a score is not a finite number. No frame,
clip, score or credential leaves this host.

Pending intervals live in one small JSON file in the one writable volume, 40 at
most and six hours old at most, written atomically and validated field by field on
the way back in, and a save that does not land leaves the previous file whole.
Nothing per-event is saved: what an event still needs is read from Frigate's own
row on every sweep, which is what makes a retry harmless.

Events are only ever added to. No DELETE, no un-pin, no configuration edit and no
synthetic event. The only two writes this process can make are a sub-label on an
event that already exists and the retain flag that keeps it. A row carrying some
other sub-label is left exactly as it is.

    python3 scripts/fighting.py

Log lines carry camera names, states, counts, scores and status codes only.
"""

import argparse
import contextlib
import http.server
import io
import json
import math
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
from collections import deque

import retain  # upstream client, archive walk, id and time checks, the keep, the refusals

# Camera 2 is the monitored area 2 and camera 3 is the area_3. Camera 1 is an
# approach and camera 4 is the area_4: neither is in scope, and a named list is
# easier to argue with than a rule.
WATCHED = ("camera_2", "camera_3")
SUB_LABEL = "possible_fighting"

# The model, pinned to one revision of one repository under MIT. Root provisions
# the files into the mounted directory; nothing here ever downloads them.
MODEL_NAME = "HappyGook/videomae-violence-detector"
MODEL_REVISION = "975dadb8f6d7c5d3119ad40341c3622ee5f8ff43"
MODEL_LICENSE = "MIT"
MODEL_DIR = "/models/fighting"
VIOLENT_INDEX = 1
CLASSES = 2
# The pinned checkpoint's own config carries the default placeholder pair
# LABEL_0 and LABEL_1, and its model card states what those two mean: index 0
# nonviolent, index 1 violent. Exactly two ordered pairs are accepted, in exactly
# that order. A swapped pair is refused rather than read the other way round,
# because reading it the other way round turns a quiet room into an alert. The
# config is never renamed, reordered or written to.
ACCEPTED_CLASS_PAIRS = (("nonviolent", "violent"), ("label0", "label1"))

WINDOW_FRAMES = 16  # the model's own clip length, and one window
VOTES = 2  # consecutive windows on one camera: the whole of the temporal rule
CAPTURE_FPS = 3.0
FRAME_INTERVAL = 1.0 / CAPTURE_FPS
# At 3 FPS a frame is 0.33 s away, so a second is three missed frames and the
# frames either side of it are no longer one stretch of the room.
GAP_SECONDS = 1.0
# Twice the nominal window span. Longer than that means the bounded buffer dropped
# frames, so what is left has a hole in it and is not one continuous window.
MAX_WINDOW_SPAN = WINDOW_FRAMES / CAPTURE_FPS * 2.0
BUFFER_FRAMES = WINDOW_FRAMES * 2  # the newest 32 frames, and nothing older
DEFAULT_THRESHOLD = 0.9
COOLDOWN_SECONDS = 60.0
INFER_THREADS = 2
MATCH_INTERVAL = 15.0
REPORT_SECONDS = 300.0
STOP_POLL_SECONDS = 5.0  # how often the main thread wakes, so a Ctrl-C is quick
STATS_SECONDS = 2.0  # Frigate's stats are a whole-stack summary, not a per-frame read
# Capture and inference go stale separately: a refused model with frames still
# arriving is not a camera that is working, and an hour-old score is not a fact
# about this room now.
STALE_SECONDS = 10.0
# How far a saved interval's confirmation time may sit ahead of this process's own
# clock before the file is refused rather than trusted.
CLOCK_TOLERANCE = 60.0

WARMING = "warming"  # frames are arriving and no window has been scored yet
ANALYZING = "analyzing"  # a recent score on a camera whose frames are current
UNAVAILABLE = "unavailable"  # no model, no frames, or nothing recent

# Pending intervals only: a camera, four finite numbers and the version. No frame,
# no credential, no event id, nothing per-event.
STATE_FILE = "/state/fighting.json"
MAX_STATE_BYTES = 64 * 1024
STATE_VERSION = 1
MAX_PENDING = 40
PENDING_TTL_SECONDS = 6 * 60 * 60
CANDIDATE_KEYS = frozenset({"camera", "start", "end", "score", "at"})
STATE_UNREADABLE = ("Saved candidate intervals could not be read, so nothing is being "
                    "labelled from them.")

STATUS_LISTEN = "0.0.0.0:8091"
STATUS_PATH = "/status"
STATUS_NOTICE = (
    "Experimental. possible_fighting is what a small model said about sixteen frames from a "
    "three frames per second stream, not a finding: a hug, a game and a fight can look the same "
    "to it, and nothing here has measured accuracy on these two rooms. The threshold is a number, "
    "not a calibrated one. A person has to look at every snapshot this labels."
)

LOADING = "loading"
READY = "ready"
REFUSED = "refused"


def log(message):
    """One line to stdout. Nothing reaching here may hold a body, a URL or a secret."""
    print(f"fighting: {message}", flush=True)


def plain_name(value):
    """A class label as letters and digits alone, so LABEL_0, Label-0 and label0 agree."""
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


class ModelRefused(Exception):
    """The model is not the one this worker scores with. Nothing is scored."""


def check_contract(model):
    """Refuse any model that is not 16 frames of the two classes its card means.

    The frame count and the class count first, because a model that takes a
    different shape answers a different question. Then the two label words in
    order, compared without punctuation or case: either the default placeholder
    pair or the spelled-out pair, index 0 first. A swapped pair is not this
    checkpoint and is refused.
    """
    config = getattr(model, "config", None)
    frames = getattr(config, "num_frames", None)
    if isinstance(frames, bool) or not isinstance(frames, int) or frames != WINDOW_FRAMES:
        raise ModelRefused(f"the model takes {frames!r} frames per clip")
    classes = getattr(config, "num_labels", None)
    if isinstance(classes, bool) or not isinstance(classes, int) or classes != CLASSES:
        raise ModelRefused(f"the model carries {classes!r} classes")
    labels = getattr(config, "id2label", None)
    if not isinstance(labels, dict) or set(labels) != {0, 1}:
        raise ModelRefused(f"the model does not carry exactly two indexed classes: {labels!r}")
    names = (plain_name(labels[0]), plain_name(labels[1]))
    if names not in ACCEPTED_CLASS_PAIRS:
        raise ModelRefused(f"the model's two classes are {names[0]!r} then {names[1]!r}")


def load_model(model_dir):
    """(processor, model), or ModelRefused.

    How the weights arrive is fixed here and no flag can widen it: a local
    directory only, safetensors only, no remote code, no download. The loading
    report must be completely clean, because one missing key is a randomly
    initialised layer and one mismatched key is a model quietly reshaped, and
    either way its scores mean nothing in particular.

    The processor and the classifier come from the same directory as the weights,
    so the resize, the crop and the normalisation are the saved ones rather than
    this script's idea of them.
    """
    import torch
    from transformers import VideoMAEForVideoClassification, VideoMAEImageProcessorPil

    torch.set_num_threads(INFER_THREADS)
    try:
        torch.set_num_interop_threads(1)
    except RuntimeError:
        # Already fixed for this process, which is the same answer. Only a second
        # set after parallel work has begun raises, and that is not worth a restart.
        pass
    processor = VideoMAEImageProcessorPil.from_pretrained(model_dir, local_files_only=True)
    model, loading = VideoMAEForVideoClassification.from_pretrained(
        model_dir,
        local_files_only=True,
        use_safetensors=True,
        trust_remote_code=False,
        attn_implementation="sdpa",
        output_loading_info=True,
    )
    report = loading if isinstance(loading, dict) else {}
    for key in ("missing_keys", "unexpected_keys", "mismatched_keys", "error_msgs"):
        if report.get(key):
            raise ModelRefused(f"{key} was not empty")
    check_contract(model)
    model.eval()
    return processor, model


def decode_frames(frames):
    """The captured bytes as RGB images, in order. A frame that will not decode is a
    failed read rather than a black picture, so this raises instead of substituting."""
    from PIL import Image

    images = []
    for raw in frames:
        with Image.open(io.BytesIO(raw)) as handle:
            images.append(handle.convert("RGB"))
    return images


def score_window(processor, model, frames):
    """How likely this clip is violent, as a finite number, or refuse it.

    The length is checked before anything is decoded, and the images go to the
    processor exactly as they came off the wire. VideoMAEImageProcessorPil is an
    image processor, so the clip is passed as one positional sequence of images
    rather than as videos=.

    The answer is the probability of the violent class out of the model's own two,
    and anything that is not a finite number in [0, 1] is refused rather than
    compared with the threshold.
    """
    import torch

    if len(frames) != WINDOW_FRAMES:
        raise ModelRefused(f"a clip of {len(frames)} frames was not scored")
    inputs = processor(decode_frames(frames), return_tensors="pt")
    with torch.inference_mode():
        logits = model(**inputs).logits
    if logits.shape[-1] != CLASSES:
        raise ModelRefused(f"the model answered with {logits.shape[-1]} classes")
    score = float(torch.softmax(logits.detach().float(), dim=-1)[0][VIOLENT_INDEX].item())
    if not math.isfinite(score) or not 0.0 <= score <= 1.0:
        raise ModelRefused("the model answered with a score that is not a number in [0, 1]")
    return score


def all_identical(frames):
    """True when every frame in the window is the same bytes.

    Sixteen identical stills are what a camera shows when nothing has happened and
    also what it shows when the image has stopped updating, and a model asked about
    those has been asked about the encoder rather than about the room. This is
    exact byte identity, not a motion measure, and it is not a substitute for
    asking whether anything moved.
    """
    return len(set(frames)) == 1


class Streak:
    """Two consecutive above-threshold windows on one camera, and the cooldown.

    Consecutive means continuous as well as adjacent: the next window must start
    within GAP_SECONDS of where the last one ended, so a dropped frame, a dropped
    window or a gap in capture cannot join two stretches of the room that never
    touched. Windows arrive nonoverlapping because the caller consumes each one
    before taking the next, so nothing about overlap is tracked here beyond that
    boundary.
    """

    def __init__(self, camera, threshold):
        self.camera = camera
        self.threshold = threshold
        self.count = 0
        self._start = None
        self._end = None
        self._best = 0.0
        self._until = 0.0

    def reset(self):
        self.count = 0
        self._start = None
        self._end = None
        self._best = 0.0

    def score(self, score, start, end, now):
        """The confirmed interval, or None.

        The interval runs from the first frame of the first window to the last
        frame of the second, which is the stretch those two windows covered, and
        its score is the higher of the pair: the evidence is the pair, and the
        weaker window is not what a person should be shown.

        A pair confirmed inside the cooldown is dropped rather than held for later.
        One camera in a room where something is happening produces many such pairs,
        and a backlog of them would label one moment's snapshots long afterwards.
        """
        if not all(math.isfinite(value) for value in (score, start, end)) or start > end:
            self.reset()
            return None
        if self.count and not 0.0 <= start - self._end <= GAP_SECONDS:
            self.reset()  # not the same stretch of time, so not consecutive
        if score < self.threshold:
            self.reset()
            return None
        if self.count == 0:
            self._start = start
            self._best = score
        self.count += 1
        self._best = max(self._best, score)
        self._end = end
        if self.count < VOTES:
            return None
        interval = {"camera": self.camera, "start": self._start, "end": end,
                    "score": self._best, "at": now}
        self.reset()
        if now < self._until:
            return None
        self._until = now + COOLDOWN_SECONDS
        return interval


class CameraWindow:
    """One camera's newest frames, its votes, and what the dashboard may see.

    The buffer holds the newest 32 frames and nothing older: about eleven seconds
    at 3 FPS.

    Every reset bumps a generation. A window is taken with the generation it was
    taken under, and a score that comes back for a generation that has moved on is
    discarded.
    """

    def __init__(self, camera, threshold):
        self.camera = camera
        self.streak = Streak(camera, threshold)
        self.lock = threading.Lock()
        self._frames = deque(maxlen=BUFFER_FRAMES)
        self.generation = 0
        self.since = time.time()  # the start of the current attempt
        self.last_capture = None  # the newest frame seen, kept even when take() empties
        self.last_scored = None
        self.score = None
        self.candidate = None
        self.matched = None
        self.gaps = 0
        self.failed_reads = 0

    def _reset(self, reason, now):
        """Throw the run away: the buffer, the votes, the held score and the attempt."""
        self.generation += 1
        self._frames.clear()
        self.streak.reset()
        self.score = None  # a score from before this is not a current one
        self.since = now
        setattr(self, reason, getattr(self, reason) + 1)

    def started(self, now):
        """The model has just become usable, so a fresh attempt starts here."""
        with self.lock:
            self.since = now

    def offer(self, raw, now):
        """One captured frame, with the gap and the overflow rules applied first.

        The gap is measured against the last frame of any kind, not the last one in
        the buffer, because taking a window empties the buffer and a window that
        was just taken is still the frame before this one.

        An append into a full buffer drops the oldest frame, so the votes go with
        it: two windows with a frame missing between them are not consecutive, and
        the newest 32 frames are kept rather than starting over, because the drop
        means the model is behind rather than that the room stopped.
        """
        with self.lock:
            if self.last_capture is not None and now - self.last_capture > GAP_SECONDS:
                self._reset("gaps", now)
            if len(self._frames) >= BUFFER_FRAMES:
                self.generation += 1
                self.streak.reset()
                self.score = None  # a score from the run before the drop is not current
                self.gaps += 1
            self._frames.append((now, raw))
            self.last_capture = now

    def fail(self, now):
        """A read that did not come back, or a camera Frigate says is not running.

        last_capture is cleared rather than stamped: nothing was read from this
        camera just now, and stamping the failure would keep a dead camera looking
        alive for as long as the loop keeps trying.
        """
        with self.lock:
            self.last_capture = None
            self._reset("failed_reads", now)

    def take(self, now):
        """(start, end, frames, generation) for the oldest whole window, or None.

        Refused for a stretch too long to be one window, for times that do not run
        forwards, for anything dated ahead of this process's own clock, and for a
        window that has been waiting in the buffer longer than STALE_SECONDS: each
        of those means these 16 frames are not a five second look at a room now.
        """
        with self.lock:
            if len(self._frames) < WINDOW_FRAMES:
                return None
            window = [self._frames[index] for index in range(WINDOW_FRAMES)]
            for _ in range(WINDOW_FRAMES):
                self._frames.popleft()
            start, end = window[0][0], window[-1][0]
            if not (0 < start <= end <= now and now - end <= STALE_SECONDS
                    and end - start <= MAX_WINDOW_SPAN):
                self._reset("gaps", now)
                return None
            return start, end, [raw for _, raw in window], self.generation

    def reject(self, generation):
        """A window that will never be scored: the votes go with it, nothing else."""
        with self.lock:
            if generation == self.generation:
                self.streak.reset()
                self.score = None

    def scored(self, score, start, end, generation, now):
        """The confirmed interval, or None, and this camera's last score either way.

        A result is dropped if the run moved on, if the window has aged out while
        the model was thinking, or if no frame has arrived for longer than the gap
        rule allows. Each of those says the answer is about frames that no longer
        stand.
        """
        with self.lock:
            stale = (generation != self.generation
                     or now - end > STALE_SECONDS
                     or self.last_capture is None
                     or now - self.last_capture > GAP_SECONDS)
            if stale:
                return None
            self.score = score
            self.last_scored = now
            interval = self.streak.score(score, start, end, now)
            if interval is not None:
                self.candidate = dict(interval)
            return interval

    def noted(self, now, note):
        """The previous note for this camera's matched event, and the new one.

        now is recorded so the dashboard can tell a match from last week from one
        this minute, and so a camera nobody has swept in a while shows it.
        """
        with self.lock:
            previous, self.matched = self.matched, dict(note, at=now)
            return previous

    def snapshot(self, now, model_ready):
        """What the dashboard may see of this camera, and nothing else.

        Capture and inference age separately, and so does the warm-up. A model that
        is not usable, a camera nothing has been read from for STALE_SECONDS, and a
        score older than that are all unavailable whatever the attempt was doing. With
        the model usable, frames current and no score yet it is warming, but only for
        STALE_SECONDS from the start of this attempt, so a model that loads and then
        never returns a score reports unavailable rather than warming forever. A
        score is reported only while it is a recent one on a usable model, so a stale
        number and any number at all under a refused model stay out of the answer.
        """
        with self.lock:
            capturing = self.last_capture is not None and now - self.last_capture <= STALE_SECONDS
            scored = (self.score is not None and now - self.last_scored <= STALE_SECONDS)
            if not model_ready or not capturing or (self.score is not None and not scored):
                state = UNAVAILABLE
            elif self.score is not None:
                state = ANALYZING
            elif now - self.since <= STALE_SECONDS:
                state = WARMING
            else:
                state = UNAVAILABLE
            return {
                "state": state,
                "last_capture": self.last_capture if capturing else None,
                "last_scored": self.last_scored if scored and model_ready else None,
                "score": self.score if scored and model_ready else None,
                "last_candidate": self.candidate,
                "last_matched": self.matched,
            }


def valid_candidate(value, now):
    """The interval as this file understands it, or None.

    Closed and field by field: the exact key set, one of the two cameras watched,
    four finite numbers above zero, a strictly forward window (end after start), a
    score a two-class softmax could have produced, a confirmation at or after the
    window ended within the clock tolerance, and nothing dated further ahead than
    that tolerance. A half trusted interval would put a label on the wrong snapshot,
    so anything else is not this file.
    """
    if not isinstance(value, dict) or set(value) != CANDIDATE_KEYS:
        return None
    camera = value["camera"]
    if camera not in WATCHED:
        return None
    numbers = {}
    for key in CANDIDATE_KEYS - {"camera"}:
        if isinstance(value[key], bool) or not isinstance(value[key], (int, float)):
            return None
        numbers[key] = float(value[key])
        if not math.isfinite(numbers[key]) or numbers[key] <= 0.0:
            return None
    score = numbers["score"]
    if not 0.0 < score <= 1.0:
        return None
    if numbers["end"] <= numbers["start"]:
        return None
    if numbers["at"] < numbers["end"] - CLOCK_TOLERANCE:
        return None
    if numbers["at"] > now + CLOCK_TOLERANCE:
        return None
    return {"camera": camera, "start": numbers["start"], "end": numbers["end"],
            "score": score, "at": numbers["at"]}


class Pending:
    """The candidate intervals still waiting to be matched, in one file.

    An interval is never removed for being matched once. It stays for its six
    hours, because the person track it was made for is often snapshotted a moment
    after the window that named it, and an interval dropped on its first match
    would never meet that track. What an event still needs is read from Frigate's
    own row on every sweep instead, so keeping the interval costs a lookup and not
    a risk of writing twice.

    One reentrant lock covers the object. It is taken for the length of a file
    operation and no longer, so the matching sweep walks the network with a copy
    of the list rather than with the lock held. A candidate is never edited once
    it is in the list, which is what makes that copy safe.
    """

    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self.candidates = []
        self.error = None
        self.candidates, self.error = self.read()

    def read(self):
        """(candidates, note): the saved intervals, validated field by field.

        The document is exactly a version and a candidates list, and the list is
        checked for its own length before any of it is trusted. A missing file is a
        fresh start rather than an error. Anything else this cannot read, which is
        not JSON, not that shape, an unknown version, a list over the ceiling, one
        bad interval among the good ones, or a file over its own byte ceiling, is no
        intervals at all and one short note: failing closed is the only safe answer
        when an interval read as something it is not would label a wrong snapshot.
        """
        now = time.time()
        try:
            with open(self.path, "rb") as handle:
                raw = handle.read(MAX_STATE_BYTES + 1)
        except FileNotFoundError:
            return [], None
        except OSError:
            return [], STATE_UNREADABLE
        if len(raw) > MAX_STATE_BYTES:
            return [], STATE_UNREADABLE
        try:
            document = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, RecursionError):
            return [], STATE_UNREADABLE
        if not isinstance(document, dict) or set(document) != {"version", "candidates"}:
            return [], STATE_UNREADABLE
        version = document["version"]
        if isinstance(version, bool) or version != STATE_VERSION:
            return [], STATE_UNREADABLE
        saved = document["candidates"]
        if not isinstance(saved, list) or len(saved) > MAX_PENDING:
            return [], STATE_UNREADABLE
        candidates = []
        for value in saved:
            candidate = valid_candidate(value, now)
            if candidate is None:
                return [], STATE_UNREADABLE
            candidates.append(candidate)
        return candidates, None

    def snapshot(self):
        """A copy of the list, taken under the lock. The rows in it never change."""
        with self.lock:
            return list(self.candidates)

    def add(self, candidate):
        """True when the interval is held and saved, False when the save did not land.

        A save that fails keeps the interval in memory, so this process still acts
        on it now and only a restart would lose it. That is said in the error note
        and in the caller's line rather than being passed off as durable.

        The ceiling is the newest MAX_PENDING. Past that the oldest goes, because
        an interval from six hours ago is the least useful one still to be holding
        and the person looking now needs the recent one.
        """
        with self.lock:
            row = valid_candidate(candidate, time.time())
            if row is None:
                return False
            if len(self.candidates) >= MAX_PENDING:
                self.candidates.pop(0)
                log(f"pending: at the ceiling of {MAX_PENDING}, so the oldest candidate interval "
                    "was dropped")
            self.candidates.append(row)
            return self.save()

    def expire(self):
        """Drop whatever is older than six hours, and say whether anything went.

        Six hours is the retention worker's own archive window, so an interval kept
        longer could not reach the events it was made for anyway.
        """
        with self.lock:
            now = time.time()
            keep = [row for row in self.candidates if now - row["at"] <= PENDING_TTL_SECONDS]
            if len(keep) == len(self.candidates):
                return False
            self.candidates = keep
            return True

    def save(self):
        """Atomic, small, and it never half writes what was there.

        The byte ceiling is checked before anything is opened, so a document that
        could not fit never reaches the filesystem. A failure removes the scratch
        file, leaves the previous file byte for byte as it was, and records why.
        """
        with self.lock:
            document = json.dumps({"version": STATE_VERSION, "candidates": self.candidates},
                                  sort_keys=True)
            if len(document) > MAX_STATE_BYTES:
                self.error = "The candidate intervals were over their own byte ceiling."
                return False
            temporary = f"{self.path}.new"
            try:
                with open(temporary, "w", encoding="utf-8") as handle:
                    handle.write(document)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.path)
            except OSError:
                with contextlib.suppress(OSError):
                    os.unlink(temporary)
                self.error = ("The candidate intervals could not be saved, so a restart would "
                              "not have them.")
                return False
            self.error = None
            return True


def event_span(event, now):
    """(start, end) for this track, or None.

    end_time of exactly None is a track Frigate has not ended, and it covers up to
    now, which is what lets an entry still being tracked overlap a recent interval.
    That is only allowed for a start at or before now. Every other time must be a
    finite number above zero with the end not before the start: a malformed one is
    refused rather than guessed, because a guessed end invents either an overlap or
    a gap.
    """
    start = event.get("start_time")
    end = event.get("end_time")
    if isinstance(start, bool) or not isinstance(start, (int, float)):
        return None
    start = float(start)
    if not math.isfinite(start) or start <= 0.0:
        return None
    if end is None:
        return (start, float(now)) if start <= now else None
    if isinstance(end, bool) or not isinstance(end, (int, float)):
        return None
    end = float(end)
    if not math.isfinite(end) or end <= 0.0 or end < start:
        return None
    return (start, end)


def overlaps(span, candidate):
    """True when a track and a candidate interval share any instant at all."""
    return span[0] <= candidate["end"] and span[1] >= candidate["start"]


def occupied_sub_label(event):
    """(occupied, is_ours): what this row's sub-label field already holds.

    Frigate reports a sub-label as a bare string or as [label, score]. Only the
    label word is read, and only in those two shapes. Anything else, a number, a
    dict, a three element list, a second element that is not a number, is treated
    as occupied by a label this cannot read: writing over a field whose meaning is
    unknown is the one thing never to do here.
    """
    value = event.get("sub_label")
    if value is None:
        return False, False
    if isinstance(value, str):
        return (bool(value), value == SUB_LABEL)
    if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str) and value[0]:
        score = value[1]
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
            return True, False
        return True, value[0] == SUB_LABEL
    return True, False


def tag(api, event_id, camera, score):
    """(ok, detail): label one event that already exists, or refuse the label.

    The camera goes with it because Frigate keys a sub-label per camera, and the
    score is the one the model produced at six decimal places, which is all a JSON
    float carries anyway. A 2xx alone is not success: the body has to say so, the
    same rule the retention worker applies to its own keep.
    """
    path = "/api/events/" + urllib.parse.quote(event_id, safe="") + "/sub_label"
    status, body = api.call(path, method="POST", body={
        "subLabel": SUB_LABEL, "subLabelScore": round(score, 6), "camera": camera,
    })
    if not 200 <= status < 300:
        return False, f"HTTP {status}"
    if not retain.succeeded(body):
        return False, "HTTP 2xx with no success:true in the reply"
    return True, f"HTTP {status}"


class Worker:
    """Two capture threads, one inference thread, one matching sweep, one status read.

    The model is loaded on a thread of its own so the status answer and the capture
    threads are answering while a several hundred megabyte model is read from disk.
    """

    def __init__(self, api, pending, threshold, page_size, max_pages, model_dir):
        self.api = api
        self.pending = pending
        self.threshold = threshold
        self.page_size = page_size
        self.max_pages = max_pages
        self.model_dir = model_dir
        self.windows = {camera: CameraWindow(camera, threshold) for camera in WATCHED}
        self.processor = None
        self.model = None
        self.model_state = LOADING
        self.stop = threading.Event()
        self.reported = time.time()
        self._live = {}
        self._live_at = None

    # -- capture ------------------------------------------------------------

    def capture(self, camera):
        """One camera's frames, three a second, until the process stops.

        The still image Frigate already publishes is read rather than a stream, so
        there is no decoder, no video file and nothing on disk: sixteen stills in a
        row is the clip, and every one is dropped after it is scored. The sleep is
        measured from the start of the read, so a slow read does not push the
        cadence out and two windows cannot drift apart.
        """
        window = self.windows[camera]
        path = f"/api/{camera}/latest.jpg"
        while not self.stop.is_set():
            started = time.monotonic()
            try:
                status, body = self.api.call(path)
                raw = body if 200 <= status < 300 and body else None
            except (urllib.error.URLError, OSError):
                raw = None
            now = time.time()
            if raw is None:
                window.fail(now)
            else:
                window.offer(raw, now)
            self.stop.wait(max(0.0, FRAME_INTERVAL - (time.monotonic() - started)))

    def live_cameras(self, now):
        """{camera: bool}, read from Frigate's own /api/stats at most every other second.

        Fail closed on everything: a transport error, a non-2xx, a body that is not
        JSON, a missing camera, a frame rate that is not a number and a frame rate
        of zero are all the same answer, which is no. A camera Frigate says is not
        running is showing a stale still, and sixteen stale stills are not a five
        second look at a room. The first read is immediate; there is no cached
        answer to begin with.
        """
        if self._live_at is not None and now - self._live_at < STATS_SECONDS:
            return self._live
        self._live_at = now
        self._live = {}
        try:
            status, body = self.api.call("/api/stats")
        except (urllib.error.URLError, OSError):
            return self._live
        if not 200 <= status < 300:
            return self._live
        try:
            document = json.loads(body.decode("utf-8", "replace"))
        except ValueError:
            return self._live
        cameras = document.get("cameras") if isinstance(document, dict) else None
        live = {}
        for camera in WATCHED:
            block = cameras.get(camera) if isinstance(cameras, dict) else None
            live[camera] = (isinstance(block, dict)
                            and retain.positive_stamp(block.get("camera_fps")) is not None
                            and retain.positive_stamp(block.get("process_fps")) is not None)
        self._live = live
        return live

    def gate(self, now):
        """{camera: bool}: the cached Frigate verdict, and false for every camera once
        that sample is older than the staleness bound.

        This is what the status read uses, and it makes no request of its own: it reads
        the sample the inference loop already has. The sample matters here because a
        camera Frigate has stopped processing keeps returning its last JPEG, so fresh
        frames on their own would read as a camera being watched. No sample, or an old
        one, gates every camera shut.
        """
        if self._live_at is None or not self._live or now - self._live_at > STALE_SECONDS:
            return {camera: False for camera in WATCHED}
        return self._live

    # -- the model ----------------------------------------------------------

    def model_loop(self):
        """Load the model off every other thread, or say in one word that it did not."""
        try:
            processor, model = load_model(self.model_dir)
        except Exception as err:
            # Type only: the message would carry the path it was raised for.
            self.model_state = REFUSED
            log(f"the model was refused ({type(err).__name__}), so no window is scored, both "
                "cameras show unavailable and nothing is labelled")
            return
        self.processor = processor
        self.model = model
        self.model_state = READY
        now = time.time()
        for window in self.windows.values():
            window.started(now)  # the warm-up clock starts when the model becomes usable
        log(f"loaded {MODEL_NAME} at revision {MODEL_REVISION[:12]} ({MODEL_LICENSE}) from a local "
            f"directory: {WINDOW_FRAMES} frames, {CLASSES} classes, {INFER_THREADS} CPU threads, "
            "safetensors only, no remote code, no download")

    # -- inference ----------------------------------------------------------

    def analyse(self, camera):
        """Score one window of one camera and act on the answer."""
        taken = self.windows[camera].take(time.time())
        if taken is None:
            return
        start, end, frames, generation = taken
        if all_identical(frames):
            # Nothing moved and the image did not update either. A model asked about
            # that has been asked about the encoder rather than about the room.
            self.windows[camera].reject(generation)
            return
        try:
            score = score_window(self.processor, self.model, frames)
        except Exception as err:
            # A model fault must not take the worker down, and the votes go with it.
            self.windows[camera].reject(generation)
            log(f"{camera}: a window was not scored ({type(err).__name__}); its votes were dropped")
            return
        interval = self.windows[camera].scored(score, start, end, generation, time.time())
        if interval is None:
            return
        self.pending.expire()
        saved = self.pending.add(interval)
        log(f"{camera}: {VOTES} consecutive windows at or above {self.threshold:g}, so an interval "
            f"of {interval['end'] - interval['start']:.1f}s at {interval['score']:.3f} is confirmed "
            f"and {'saved' if saved else 'NOT saved, so a restart would lose it'}; "
            f"{len(self.pending.candidates)} pending, cooldown {COOLDOWN_SECONDS:g}s")
        if not saved and self.pending.error is not None:
            log(f"pending: {self.pending.error}")

    def inference_loop(self):
        """One model, one loop: each camera's next whole window, in turn."""
        while not self.stop.is_set():
            started = time.monotonic()
            live = self.live_cameras(time.time())
            for camera in WATCHED:
                if not live.get(camera):
                    self.windows[camera].fail(time.time())
                    continue
                if self.model is None:
                    continue  # still loading: the frames keep arriving
                self.analyse(camera)
            self.stop.wait(max(0.0, FRAME_INTERVAL - (time.monotonic() - started)))

    # -- matching -----------------------------------------------------------

    def qualifies(self, event, candidate, now):
        """True when this row is one of the person tracks the interval is about.

        Strict and independent of the walk: the same camera, the person label, a
        native id, the retention worker's own listable check (which carries its
        snapshot test), and a real time span sharing an instant with the interval.
        """
        if event.get("camera") != candidate["camera"]:
            return False
        if event.get("label") != retain.TARGET_LABEL:
            return False
        if retain.native_id(event)[0] is None:
            return False
        if retain.disqualification(event) is not None:
            return False
        span = event_span(event, now)
        return span is not None and overlaps(span, candidate)

    def label_candidate(self, candidate, events, now):
        """{tagged, kept}: what one interval's events needed from this sweep.

        Every decision is read from Frigate's own row rather than from what this
        process remembers, which is what makes a sweep idempotent and a half
        finished one repairable by the next: a row already carrying our label needs
        no second label, a row already kept needs no second keep, a row carrying
        some other sub-label is left exactly as it is and never pinned, and a label
        that was refused gets no keep, because an unlabelled event is not one this
        has claimed.
        """
        counts = {"tagged": 0, "kept": 0}
        window = self.windows[candidate["camera"]]
        for event in sorted(events.values(), key=lambda row: row["id"]):
            if not self.qualifies(event, candidate, now):
                continue
            event_id = retain.native_id(event)[0]
            occupied, ours = occupied_sub_label(event)
            if occupied and not ours:
                previous = window.matched
                if previous is None or previous["event"] != event_id:
                    # One line for an event this will never touch, rather than one
                    # every sweep for six hours. Its snapshot sits in the archive
                    # and is deleted by hand if the owner wants it gone, which is the
                    # only way anything here is ever removed.
                    log(f"{candidate['camera']}: {event_id} carries another sub-label and was "
                        "left exactly as it is; this interval is not removed for that")
                continue
            retained = event.get("retain_indefinitely") is True
            labelled = False
            if not ours:
                ok, detail = tag(self.api, event_id, candidate["camera"], candidate["score"])
                if not ok:
                    log(f"{candidate['camera']}: {event_id} could not be labelled ({detail}); it "
                        "is retried, and no keep is sent for an unlabelled event")
                    continue
                # The row now carries our label, so a later interval in this same
                # sweep does not label it a second time. Only a success is mirrored
                # back onto the row: a refused write leaves it as it was.
                event["sub_label"] = SUB_LABEL
                counts["tagged"] += 1
                labelled = True
            if not retained:
                ok, detail = retain.pin(self.api, event_id)
                if not ok:
                    # The label is already there, so only the keep is retried.
                    log(f"{candidate['camera']}: {event_id} was labelled but not kept ({detail}); "
                        "only the keep is retried")
                    outcome = "our label is on it, and its keep is still refused"
                else:
                    event["retain_indefinitely"] = True
                    counts["kept"] += 1
                    retained = True
                    outcome = ("labelled and kept indefinitely" if labelled
                               else "kept indefinitely, and it already carried our label")
            else:
                outcome = "already labelled and kept"
            note = {"event": event_id, "score": candidate["score"], "outcome": outcome,
                    "retained": retained}
            previous = window.noted(now, note)
            if previous is None or previous["outcome"] != outcome:
                log(f"{candidate['camera']}: {event_id} {outcome} at {candidate['score']:.3f}")
        return counts

    def match_pending(self, now):
        """Label and keep every person event that overlaps a pending interval.

        Runs only while something is pending, and reads through the retention
        worker's own bounded walk over its own six hour window, so the project has
        one archive walker rather than two. No interval is removed here for having
        been matched: each one is swept again for its six hours, which is what
        catches a track whose snapshot only appeared after the window that named it.

        Two writes leave this process in total, both on events that already exist:
        the sub-label, and the retain flag that keeps the row. Nothing is deleted,
        un-pinned, configured or created.
        """
        if self.pending.expire() and not self.pending.save():
            log("pending: expired intervals could not be saved; the previous file is unchanged")
        if not self.pending.candidates:
            return
        # ponytail: one bounded walk per sweep, rescanned for each of at most
        # MAX_PENDING intervals, so a sweep is O(pending x events); index the
        # candidate list over the events if that ever shows in a profile.
        try:
            events, _walk = retain.walk_events(self.api, retain.after_cutoff(),
                                               self.page_size, self.max_pages)
        except retain.PassFailed as err:
            # Type only: the message names an upstream route.
            log(f"match: the archive walk could not finish ({type(err).__name__}); every pending "
                "interval stays pending")
            return
        except (urllib.error.URLError, OSError) as err:
            # Type only: a URLError string carries the URL it was raised for.
            log(f"match: {type(err).__name__} reaching Frigate; every pending interval stays "
                "pending")
            return
        totals = {"tagged": 0, "kept": 0}
        failed = 0
        for candidate in self.pending.snapshot():
            try:
                counts = self.label_candidate(candidate, events, now)
            except (urllib.error.URLError, OSError) as err:
                log(f"match: {type(err).__name__} reaching Frigate; this interval is retried")
                failed += 1
                continue
            for key in totals:
                totals[key] += counts[key]
        log(f"match: {totals['tagged']} labelled, {totals['kept']} newly kept, {failed} "
            f"interval(s) could not be swept, {len(self.pending.candidates)} still pending and "
            "none of them removed for having been matched")

    def matching_loop(self):
        """Come back on a timer rather than on an event, while an interval is pending."""
        while not self.stop.wait(MATCH_INTERVAL):
            try:
                self.match_pending(time.time())
            except Exception as err:
                log(f"match: {type(err).__name__}; every pending interval stays pending")

    # -- the status read ----------------------------------------------------

    def status(self, now):
        """The whole answer, and the only thing this server ever returns.

        One word for the model, the threshold this process is using, how many
        intervals are pending, and one row per camera: its state, when a frame was
        last read, when a window was last scored, its last score, its last confirmed
        interval and the last event it matched. No frame, no path, no URL, no
        configuration, no error text, no media.

        A camera is only ever reported under a usable model and a live gate, and it
        makes no request here: the gate is the cached Frigate sample, so a camera that
        has stopped processing is unavailable however recently its stale JPEG arrived.
        """
        model_ready = self.model_state == READY
        gate = self.gate(now)
        return {
            "experimental": True,
            "label": SUB_LABEL,
            "model": MODEL_NAME,
            "model_revision": MODEL_REVISION,
            "model_state": self.model_state,
            "threshold": self.threshold,
            "window_frames": WINDOW_FRAMES,
            "capture_fps": CAPTURE_FPS,
            "votes_required": VOTES,
            "cooldown_seconds": COOLDOWN_SECONDS,
            "pending": len(self.pending.candidates),
            "notice": STATUS_NOTICE,
            "cameras": {camera: self.windows[camera].snapshot(now, model_ready
                                                               and gate.get(camera, False))
                        for camera in WATCHED},
        }

    def report(self, now):
        """One line every few minutes, so a quiet process is not a silent one."""
        gate = self.gate(now)
        ready = self.model_state == READY
        states = " ".join(
            f"{camera}={self.windows[camera].snapshot(now, ready and gate.get(camera, False))['state']}"
            for camera in WATCHED)
        missed = " ".join(f"{camera} {window.gaps} gap(s), {window.failed_reads} refused read(s)"
                          for camera, window in self.windows.items())
        log(f"watching {states}, {missed}, {len(self.pending.candidates)} pending interval(s), "
            f"model {self.model_state}")

    # -- running ------------------------------------------------------------

    def run(self, host, port):
        """Start every loop and the status listener, then stay up until stopped."""
        server = status_server(self, (host, port))
        threads = [
            threading.Thread(target=server.serve_forever, name="status", daemon=True),
            threading.Thread(target=self.model_loop, name="model", daemon=True),
            threading.Thread(target=self.inference_loop, name="inference", daemon=True),
            threading.Thread(target=self.matching_loop, name="match", daemon=True),
        ]
        threads += [threading.Thread(target=self.capture, args=(camera,),
                                     name=f"capture-{camera}", daemon=True)
                    for camera in WATCHED]
        for thread in threads:
            thread.start()
        log(f"watching {', '.join(WATCHED)} at {CAPTURE_FPS:g} frames per second on "
            f"{INFER_THREADS} CPU threads, {VOTES} consecutive windows at or above "
            f"{self.threshold:g} and a {COOLDOWN_SECONDS:g}s cooldown, labelling {SUB_LABEL} on "
            f"person events and keeping them; {len(self.pending.candidates)} pending interval(s) "
            "restored")
        if self.pending.error is not None:
            log(f"pending: {self.pending.error}")
        log(f"serving the status on {host}:{port} for the dashboard's own origin only")
        log(f"the threshold is not a measured accuracy and {SUB_LABEL} is a possibility, never a "
            "finding; read docs/fighting.md and look at every snapshot this labels")
        try:
            while not self.stop.wait(STOP_POLL_SECONDS):
                now = time.time()
                if now - self.reported >= REPORT_SECONDS:
                    self.reported = now
                    self.report(now)
        except KeyboardInterrupt:
            log("stopped")
        finally:
            self.stop.set()
            server.shutdown()
            server.server_close()
        return retain.EXIT_OK


class StatusHandler(http.server.BaseHTTPRequestHandler):
    """One read-only route, and a refusal for everything else.

    The Host is checked because the browser only reaches this through the dashboard,
    exactly as on the retention gateway, and no CORS header is ever sent, so no other
    page can read the answer even if it manages to send one. There is no write here
    at all, and a query string is refused rather than ignored, so nothing a client
    appends can select a camera or widen anything.
    """

    protocol_version = "HTTP/1.0"
    server_version = "surveillance-fighting"
    sys_version = ""  # the base class would otherwise send the interpreter version
    status = 0

    def do_GET(self):
        self._route()

    do_POST = do_HEAD = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_TRACE = do_GET

    def log_message(self, *args):
        pass  # one line per request is written by _route, not by the base class

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
        try:
            if (self.headers.get("Host") or "").strip() not in retain.ALLOWED_HOSTS:
                raise retain.Refused(403, "this answers the dashboard's own origin only")
            if parsed.path != STATUS_PATH:
                raise retain.Refused(404, "no such endpoint")
            if parsed.query:
                raise retain.Refused(400, "this endpoint takes no query string")
            if method not in ("GET", "HEAD"):
                raise retain.Refused(405, "this endpoint answers GET only")
            return self._json(200, self.server.worker.status(time.time()))
        except retain.Refused as err:
            self._json(err.status, {"error": str(err)})
        finally:
            # The route is the one literal or "unknown": a client's own path is never
            # echoed into a log line.
            known = STATUS_PATH if parsed.path == STATUS_PATH else "unknown"
            log(f"status: {method} {known} -> {self.status}")


class StatusServer(http.server.ThreadingHTTPServer):
    """The status listener, holding the one worker it answers for."""

    daemon_threads = True
    allow_reuse_address = True


def status_server(worker, address):
    server = StatusServer(address, StatusHandler)
    server.worker = worker
    return server


def threshold_value(value):
    """argparse type for the vote threshold: a finite number in (0, 1].

    Anything else is refused here rather than discovered later. Zero would make
    every window a vote, and above 1 is outside what a two-class softmax reaches.
    """
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a number")
    if not math.isfinite(number):
        raise argparse.ArgumentTypeError(f"{value!r} is not a finite number")
    if not 0 < number <= 1:
        raise argparse.ArgumentTypeError(f"{value!r} is not above 0 and at most 1")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Experimental: watch cameras 2 and 3 for possible fighting, and label and "
                    "keep the person events of the intervals in which it looked that way"
    )
    parser.add_argument("--api-base", default=retain.API_BASE, help="internal Frigate API base URL")
    parser.add_argument("--model-dir", default=MODEL_DIR, metavar="PATH",
                        help=f"local model directory, mounted read-only, default {MODEL_DIR}")
    parser.add_argument("--state-file", default=STATE_FILE, metavar="PATH",
                        help="where pending candidate intervals are saved between restarts")
    parser.add_argument("--listen", type=retain.listen_address,
                        default=retain.listen_address(STATUS_LISTEN), metavar="HOST:PORT",
                        help=f"where the status read listens (default {STATUS_LISTEN})")
    parser.add_argument("--threshold", type=threshold_value, default=DEFAULT_THRESHOLD,
                        help="score at or above which a window votes, above 0 and at most 1 "
                             f"(default {DEFAULT_THRESHOLD})")
    parser.add_argument("--page-size", type=retain.positive_int, default=retain.PAGE_SIZE,
                        help="rows per events page when matching a pending interval")
    parser.add_argument("--max-pages", type=retain.positive_int, default=retain.MAX_PAGES,
                        help="page cap for one matching sweep")
    parser.add_argument("--timeout", type=retain.positive_float, default=retain.TIMEOUT,
                        help="per-request timeout, seconds")
    args = parser.parse_args(argv)
    # The archive walk, the id and time checks, the keep, the upstream client, the
    # address validator and the dashboard's Host list all come from the retention
    # worker rather than being written out again here.
    worker = Worker(retain.Api(args.api_base, args.timeout), Pending(args.state_file),
                    args.threshold, args.page_size, args.max_pages, args.model_dir)
    host, port = args.listen
    return worker.run(host, port)


if __name__ == "__main__":
    sys.exit(main())