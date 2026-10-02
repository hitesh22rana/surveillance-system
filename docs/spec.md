# Specification

Minimal surveillance detection, in phases. Each phase has an exit condition. Later
phases do not start until the earlier one is accepted by hand.

As of 2026-10-02, live camera acceptance is still **pending**. The 401 on camera
access was a confirmed recorder-side lockout that expired once the clients were
stopped. Credentials were not changed and work again as written. All four RTSP
channels are live, Frigate capture and process are verified at roughly 3 FPS,
each main channel serves HEVC 2560×1440 at 25 fps, and all four players were
watched running full-resolution video in a browser before and after the Frigate
restart that applied the six-hour snapshot window. The expanded
`scripts/check.py` suite passes every assertion, against the deployed gateway as
well as before it. The earlier 60-second loitering zones have been removed; see the
current policy below. The controlled deletion gateway is **deployed and
exercised**: `scripts/check_retention.py` and `scripts/check_console_api.py` both
PASS against a disposable fake upstream, and one ended experimental snapshot was
deleted through the dashboard on the owner's explicit authorisation. What is still
unverified is the detection work itself: a controlled zone walkthrough, accuracy
measurement, night behaviour, and alert timing — and, on retention, an actual
six-hour expiry, a retained event followed past six hours, and the CLI deletion
path. See [`validation.md`](validation.md) for exactly what was and was not run.

## Current policy — 2026-10-02 update

This update supersedes the earlier 60-second loitering requirements below. Those
zones have been removed. Camera 4 always watches its area_4 restricted zone;
Camera 1 watches the monitored area 1 while manually armed. Camera 2 is the monitored area 2
and Camera 3 is the area_3: while armed, each keeps person events in its existing
full-frame area. Home disarms Cameras 1–3; Away arms them together; individual
changes produce Custom when their settings differ. Camera 4 cannot be disarmed.
Manual settings persist across service restarts.

While Away is active, the dashboard asks for confirmation before disarming one
camera or switching to Home. Cancel and Escape preserve the settings. Animal
monitoring on Cameras 1–3 continues independently of arming. Ordinary snapshots
retain the six-hour window; kept snapshots remain until manually deleted. These
rules preserve review candidates, and do not identify intent, family members,
fighting or weapons. Live detection accuracy still requires controlled evaluation.

## What is actually known

- **Deployment hardware is unknown; the current development host uses macOS
  ARM64.** Four independent camera views exist as HEVC 352×288 substreams on
  recorder channels 1–4 for detection, and as HEVC 2560×1440 main channels for
  the live view. That 25 fps is the channel's reported source rate; no browser
  render frame rate has been measured. Channels on one recorder and separate
  devices are both supported; only byte-identical URLs duplicate a view.
- **Live quality and detection quality are unrelated.** The browser gets the
  main channel; the detector runs on the 352×288 low channel at 3 fps, and every
  saved snapshot is cut from *that* frame. A perfect live picture does not make
  an event image sharp, and raising `detect.*` is what would.
- **No real IPs or credentials exist in this repository.** `.env` is gitignored,
  `chmod 600`, and never committed.
- **Detection model:** bundled SSDLite MobileNetV2 on OpenVINO, CPU, `person`
  only, at 352×288 and 3 fps per camera. The model input tensor is fixed at
  300×300 and is not a tuning knob; frames are resized into it. The capture size
  and rate are a starting point, chosen so a CPU-only four-camera suite is
  plausible to run. No custom YOLO training, export, or inference code until a
  measured failure justifies it. That trigger is a number, not a hunch: a
  specific class of false negative or false positive that tuning `zones`,
  `objects`, `motion`, and `detect.fps` cannot resolve.
- **There is exactly one detection rule: a person loitering in a named zone.** No
  behaviour model, no anomaly detection, no classification beyond `person`, and
  no fall-over, package or line-crossing rule. Nothing in the repository should
  be read as implying otherwise, because none of it exists. As of 2026-10-02 the
  rule is **60 seconds of tracked in-zone presence**, with the zone polygon still
  the full frame: a geometry prototype chosen to be tunable, and explicitly not a
  claim that anybody was doing anything wrong. The counter advances on tracked
  frames with the object inside the zone, and detection runs at 3 fps, so 180
  frames is 60 seconds. It is **not** continuous wall-clock time: separate visits
  by the same tracked object can accumulate toward the threshold, and nothing here
  establishes that it stayed still, stayed in one place, or meant anything by it.
- **Passwords are encoded twice, by different rules, on purpose.** The four
  detect URLs in `.env` take the password raw, because Frigate applies
  `escape_special_characters` itself and a pre-encoded value is double-encoded
  and rejected with 401. The four main URLs take it URL-encoded, because go2rtc
  substitutes the value as-is and applies no encoder.
- **Frigate's raw `/api/stats` contains RTSP credentials.** `cpu_usages.cmdline`
  carries the full process command line, including every camera's RTSP URL. Any
  browser-facing surface must therefore never proxy raw stats. The dashboard
  pins a fixed key allowlist server-side for exactly this reason, and
  `scripts/check.py` asserts the pinned shape.
- **Retention has two lifetimes, and only one of them is a timer.** An ordinary
  snapshot is kept six hours (`snapshots.retain.default: 0.25` days) and is
  deleted by Frigate's periodic cleanup, about every five minutes, so six hours
  is a nominal cutoff rather than an exact deletion time. A `person`
  event that reached 60 seconds of tracked presence in its own camera's
  **loitering** zone is pinned
  by `scripts/retain.py` through Frigate's native `retain_indefinitely`, which
  that cleanup skips. The example zones are the full frame, so any tracked person
  who stays in view for a minute qualifies; that is a geometry prototype, not a
  judgement about risk, and it is **narrowing the polygons that remains to be
  done**. Both lifetimes are Frigate's own bookkeeping — go2rtc restreams and
  keeps nothing. The worker's boundary behaviour is verified against a fake
  Frigate, and its pinning was **observed on the live stack under the previous
  single-zone rule** (49 pinned plus 27 already retained across 76 events; a later
  pass found all 76 already retained and exited 0). Under the two-zone rule, one
  naturally completed loitering snapshot was seen pinned and three new ordinary
  snapshots not pinned — one observation each way, not a rate. **No six-hour
  expiry has been waited out**, no retained event has been confirmed to survive
  past six hours, and the CLI deletion path has not been run against a real event.
- **The dashboard has exactly one write path, and the owner authorised it.**
  `retain.py --serve` runs the poll loop and a stdlib HTTP server in one process
  behind one lock, and answers three exact paths: two reads,
  `GET /api/snapshots/summary` and `GET /api/snapshots/events`, and the single
  write, `POST /api/snapshots/delete`.
  It is **not** a new service: no new image, no framework, no dependency, no
  published port, no `env_file`, no storage mount. It is bounded by the exact Host
  the dashboard is published on, an Origin equal to that Host on the write, a JSON
  content type, an action header, and full body validation before any upstream
  request. Deletion is **never automatic**: the poll loop contains no `DELETE`.

## Phase 1 — one camera, one zone, person loitering

Superseded twice over. The original single-camera `courtyard` configuration is
replaced by the four-camera layout below, and the single full-frame zone is
replaced by two per camera. The detection rules carried over unchanged per camera
apart from the presence threshold, which is now 60 seconds and not 3.

- One zone per camera, `objects: [person]`.
- `loitering_time: 3` — tracked in-zone presence of 3 seconds.
- `inertia: 3` — 3 consecutive frames inside the zone are required.
- Alerts gated on `required_zones: [<that camera's zone>]` so events elsewhere in
  frame do not create alerts.
- Snapshots on, timestamped, bounding box drawn, gated on the same zone.
- `record.enabled: false`. MQTT disabled. No notification provider.

## Phase 2 — four cameras and a read-only console

Implemented in this repository, with one authorised exception to the read-only
part, recorded under **Phase 2b**. **Accepted: no.** The code exists; the exit
conditions below have not been met.

### Configuration

- Four independent cameras, `camera_1` … `camera_4`, each `enabled: true` in
  `config/config.example.yml`, each with its own RTSP URL variable.
- **Two independent zones per camera**, `camera_N_area` and
  `camera_N_loitering`, on the same polygon and with the same `inertia: 3` and
  `objects: [person]`. They differ only in `loitering_time`: **0** for the area
  zone, **60** for the loitering zone. A normalised polygon drawn on one camera
  means nothing on another, so all eight must be redrawn; and the two on one
  camera must be redrawn together or a snapshot will describe a different place
  from the alert.
- The example polygon for every zone is the full frame `0,0,1,0,1,1,0,1`. This
  is a deliberate whole-view default: every view is covered, at the cost of more
  events than you want to read. A full-frame *loitering* zone is worse than a
  full-frame area zone — it fires on anybody who waits for a door — so narrowing
  is the first thing to do, natively, once the scene is known.
- **60 seconds is a geometry prototype, not a danger judgement.** It is a timer
  chosen so that a passing step does not qualify and a shift-longer presence
  cannot hide. Nothing in this repository scores threat, intent or risk, and no
  accuracy claim may rest on the threshold.
- `required_zones` is per camera and is **not** hoisted into a shared block. A
  shared zone label across cameras would silently gate every camera on one
  camera's geometry.
- `review.alerts.required_zones` names the **loitering** zone, so an ordinary
  walk-through never becomes an alert. `snapshots.required_zones` stays on the
  **area** zone: saving an image of everybody is cheap at six hours, and keeping
  it is what the loitering rule exists to decide. `record.enabled: false`.
- A camera that cannot be reached degrades alone: Frigate retries that feed and
  the other three carry on. `enabled: false` removes a camera from the suite
  deliberately; it is not a workaround for a camera that is down.

### Console

- nginx serves static HTML/CSS/JS and proxies a bounded view of Frigate and
  go2rtc: `/api/stats`, `/api/events`, `/api/camera_1..4/latest.jpg`,
  `/api/events/<native-id>/snapshot.jpg`, where the id is Frigate's own
  `<frame_time>-<rand_id>` tracker form. The reads are `GET` and `HEAD` only, and
  every other `/api` path is denied.
- `/api/events` keeps a **fixed 50-row upstream URI** — people only,
  snapshot-bearing, all four cameras, all four area zones. That **50 is the
  legacy endpoint's shape, not the size of the archive**; it is kept for
  compatibility, and the dashboard reads the paginated source below instead,
  which is where rows older than 50 are reachable.
- Live video is pinned just as tightly: one exact path serves go2rtc's bundled
  player (`/video-rtc.js`), and `/live/camera_1` … `/live/camera_4` are WebSocket
  upgrades, each naming one main stream with no query string at all. go2rtc's own
  config, stream list and control WebSocket are not proxied, and there is no
  generic `/live/` path — a fifth camera, or any suffix on a camera path, is a
  404 rather than a different stream.
- **The console's only write is the reviewed-deletion `POST` described in Phase
  2b.** Everything it shows it can otherwise only read. There is no config, log,
  export, credential or restart path, and no way to alter a zone, a camera or a
  detection rule from the browser.
- `/api/stats` is rewritten to a **fixed** upstream URI with a **fixed** key
  allowlist, and `/api/events` to a **fixed, bounded** upstream URI. Both discard
  the client's query string, so no override can widen the surface.
- **Three exact snapshot paths, two reads and one write**, are proxied to the
  retention worker on the internal network: `GET /api/snapshots/summary`,
  `GET /api/snapshots/events` and `POST /api/snapshots/delete`. Only the events
  read forwards a client query, and only to the local gateway, which validates
  `page` and `camera` strictly before touching the archive. See Phase 2b.
- Frigate's API port is never published. Both published ports are loopback, and
  the retention gateway publishes none.
- The console is unauthenticated by construction, because loopback-only is the
  boundary. Remote extension requires authentication and is Phase 4.
- Per-camera status is derived from `camera_fps`, `process_fps` and
  `detection_enabled` in the newest stats sample, and requires a recent picture
  before a camera is called healthy. A failed or stale read clears the sample,
  so an unreadable backend reads as "no data", never as healthy. Live playback is
  judged separately, from frames this browser has actually decoded, so a camera
  can be detecting perfectly while the picture here is refused or stalled.
- The event list contains only person events whose `zones` intersect the required
  zone of a camera in the current filter. It is never presented as "every person
  detection"; an empty list is explicitly labelled as not proving the cameras
  were watching.
- **The live view is 2560×1440; a saved snapshot is 352×288.** The page says
  which is which, because a full-resolution live picture beside a low-resolution
  event image otherwise reads as one camera running at two qualities at once.

### Retention

- Two lifetimes, deliberately: `snapshots.retain.default: 0.25` (six hours, in
  days) for everything ordinary, deleted by Frigate's periodic cleanup. Because
  that cleanup runs about every five minutes, six hours is the cutoff an ordinary
  snapshot is measured against, not the minute it disappears.
- `scripts/retain.py` pins qualifying events — `person`, snapshot-bearing, in
  **that camera's loitering** zone — by setting Frigate's own
  `retain_indefinitely` every 30 seconds. It holds no state of its own, so a
  restart neither re-pins nor forgets. An ordinary walk-through, which carries
  only the area zone, is **not** pinned and ages out on the six-hour timer.
- It runs as a `retention` service with **no ports, no `env_file`, no credential
  and no database of its own**, reading only `http://frigate:5000` on the
  internal network.
- Its automated pass contains no `DELETE` and never will. Deleting is one
  explicit HTTP `POST` from the dashboard (Phase 2b) or one named event id typed
  by a person:
  `docker compose exec retention python3 /app/retain.py --delete-reviewed EVENT_ID`.
  Both are irreversible, and neither runs without an explicit human decision.
- Snapshots **already retained under the previous single-zone rule are not
  unpinned**. The worker only ever sets `retain_indefinitely`; there is no
  migration and no sweep, so the old pile stays on disk until somebody deletes
  it. Only newly captured ordinary snapshots are on the six-hour timer, which
  means the transition is gradual, not a clean cut.
- Consequence, stated rather than hidden: with the default full-frame loitering
  zones, **every tracked person who stays in view for a minute is retained until a
  person deletes it.** Disk growth is bounded by review, not by a timer, and there
  is no cap. If the worker is stopped longer than the six-hour window, events it
  never saw can be cleaned up first — which is why the worker has to be watched
  running, not just written.

**Latency target: 5 seconds from loiter qualification to a visible alert.** A
target measured by hand, not a guarantee, and not yet measured at all. The
sixty seconds of tracked presence precede it by a minute.
 Measure alert visibility and snapshot availability separately: a final
best snapshot may only be saved when tracking ends. It excludes time
before the person becomes trackable, and it will be worse at night, in heavy
rain, or when the host is loaded.

**Exit conditions:**

- The 401 on camera access is resolved and all four channels stream again.
  **Met as of 2026-10-02:** the lockout expired with clients stopped, unchanged
  credentials work, and all four channels stream at roughly 3 FPS. This unblocks
  measurement; it does not satisfy the zone criteria below.
- All four main channels play live video in a browser at their own resolution.
  **Met as of 2026-10-02:** four players, 2560×1440 HEVC decoded, no errors. This
  says the live path works, not that detection is any good.
- Every camera independently passes the Phase 1 checks in
  [`validation.md`](validation.md). Zone walkthrough, accuracy, night, and alert
  timing are all still pending.
- Per-camera detector load is measured, not assumed. If a camera pushes
  detection past the 5 second target on cameras already accepted, that camera
  waits rather than degrading the working ones.
- Per-camera latency is recorded independently. An average across cameras hides
  a slow one.
- Zones are drawn per camera.
- Overnight soak with all four live, to catch thermal throttling and network
  drops that do not appear in a ten minute test.
- A single camera failing must not stop the others, and must be distinguishable
  from a quiet zone in the console.
- False positives reviewed per camera, not pooled, because lighting and scene
  differ.
- `scripts/check.py` passes against the live stack, with its Frigate-dependent
  checks actually run rather than skipped. **Met 2026-10-02**: every assertion in
  the expanded script holds — all four cameras detecting, the 11 forbidden paths
  refused, `POST`/`DELETE` refused on `/api/stats` and `/live/camera_1`,
  `media-src 'self' blob:`, the player module served 200, no credentials in the
  three served assets, the stats allowlist held plain and against an override, 50
  events serving real snapshot JPEGs, and all 48 settled zone events kept. Note
  that this script opens no WebSocket: playback is a browser observation and
  cannot be closed here.
- **The retention worker is observed working on the live stack**, not only
  against a fake Frigate: a zone snapshot visibly gains `retain_indefinitely`,
  survives a worker restart without being re-pinned, and an event deleted by hand
  with `--delete-reviewed` is really gone. **Partly met 2026-10-02:** the worker
  ran against the live stack and pinned 49 of 76 qualifying events, a later pass
  found all 76 already retained and exited 0, and a pass after the Frigate
  restart found 78 already retained and exited 0, so pinning and not pinning
  twice are observed on real events — under the **previous single-zone rule**.
  Under the two-zone rule, one naturally completed loitering snapshot was observed
  pinned and three new ordinary snapshots observed not pinned: one in each
  direction, not a rate. What is still not met: a genuine service restart with a
  retained event watched past six hours, and the hand-delete CLI path. Until that
  is recorded, retention is runtime-verified for pinning, not for expiry.
- Both zones per camera are drawn, and the two on each camera describe the same
  place. **Not done:** all eight polygons are still the full frame.

## Phase 2b — controlled manual deletion

Implemented, **deployed and exercised**, on 2026-10-02. Recorded separately
because it is the one place the console is not read-only, and the owner authorised
it explicitly.

- **Reuses the retention container and script.** `retain.py --serve` runs the
  existing poll loop and a `http.server` in one process, serialised by **one
  lock**, so a pass and a manual deletion never interleave. No new service, no
  framework, no dependency, no published port, no `env_file`, no storage mount:
  Frigate's own API deletes the media, the event and the timeline entry.
- `GET /api/snapshots/summary` → `{"count": <int>, "before": <unix seconds>}`.
  `count` is every `person` snapshot from the four configured cameras in the
  **whole archive**, not the newest 50. `before` is a whole second taken before
  the walk, and `count` is exactly what a deletion naming that second would
  reach, so the number shown is never a number the deletion cannot deliver.
  Events newer than that second are excluded: they are live detection, not
  archive.
- `GET /api/snapshots/events` is the **paginated read** that the fixed-50
  `/api/events` cannot replace, because the archive is deeper than 50 rows, and
  it is the source the dashboard's pager reads. It takes **optional** `page` and
  `camera` and nothing else: a repeated, unknown or
  blank parameter, a page that is not a positive whole number, or a camera that is
  not `all` or one of the four configured names is refused before any upstream
  request. It answers `{"events": [...], "page": <int>, "page_size": 25,
  "total": <int>, "pages": <int>}` — **25 rows per page**, newest first, with the
  camera filter applied **before** the page is cut, so `total` and `pages` always
  describe the filtered set. A page past the last is clamped to the last rather
  than refused, because a page that is one behind after deletions still needs an
  answer. The read walks the archive in order to count it, so it is bounded by the
  same page cap as the deletion and answers 503 rather than a short page that reads
  like a whole one.
- `POST /api/snapshots/delete` accepts **either** `{"ids": [<native id>, ...]}`
  (1 to 1000, non-empty, duplicates folded) **or** `{"all": true, "before":
  <the summary cutoff>}` — never both, never neither, and no other key. It
  answers `{"deleted": <int>, "missing": <int>, "failed": [<id>, ...],
  "active": <int>}`, and the three counts always add up to what was selected.
- **`all` is fail-closed.** The whole archive is walked first, from the beginning
  of time, and if the walk cannot reach the end of the list — the page cap, an
  upstream failure, a malformed page — **nothing is deleted at all** and the
  request answers 503. `before` is applied locally after the walk, so the newest
  events are excluded by this code rather than by an upstream bound that could
  drift between the count and the deletion.
- **Every request is validated before one upstream byte moves.** Body at most
  64 KiB, JSON object, one mode, ids matching the native pattern exactly
  (uppercase, a path segment or a trailing newline is refused), a `before` that
  is finite, non-negative and not in the future.
- **Every request must also be the dashboard's own.** `Host` exactly
  `localhost:8088` or `127.0.0.1:8088`; for the `POST`, `Origin` exactly
  `http://` that Host, `Content-Type: application/json`, and
  `X-Surveillance-Action: delete`. No CORS header is ever sent, so no other page
  can read a reply, and the gateway publishes no port: nginx reaches it as
  `retention:8090` on the internal network. **These two headers guard against
  cross-site requests from a browser.** A page on another origin cannot address
  this one or present an `Origin` that matches it. They are not aimed at a
  malicious process on this machine, which can forge any header and is already
  inside the loopback trust boundary.
- **No path or query is forwarded.** Each upstream call is built from literals
  and validated ids. Unknown paths and methods are 404/405/501; every refusal is
  redacted JSON, never a Frigate body, URL, config, log or password.
- **Each selected id is verified before it is deleted.** The event is looked up
  on Frigate and must be a configured camera's, must carry a snapshot and must be
  a `person`; another camera's event is refused, not deleted. 404 counts as
  already gone, so a repeat is idempotent. A native 2xx carrying `success: false`
  is never counted as a deletion — both the status and the body must say so.
- **Active detections are reported, not managed.** `active` counts selected
  events Frigate has not ended yet; those can write a new snapshot afterwards.
  Nothing here restarts a camera, and nothing here deletes in a loop. Deletion
  happens only through this explicit `POST` or the operator's CLI.
- **The UI offers two scopes and neither is the default**: the rows selected in
  the visible list, or the whole archive up to the summary count, each behind an
  confirmation that states the number and the irreversibility. The Cancel path was
  exercised and cancelled without a request. Anything older than the current page
  is reached by the pager or by the archive scope, which is why it exists.
- **The operator's CLI still works** with the gateway stopped. Both are
  irreversible.

**Exit conditions: recorded on 2026-10-02; the phase is still not accepted.**
`scripts/check_console_api.py` **passes** against a disposable fake Frigate,
covering a 60-row archive over three pages of 25, invalid `Origin`/`Host`/content
type/ids, a partial failure and a native `success: false`, the fail-closed page
cap, and no automatic `DELETE`. `docker compose config -q`, the Python compile of
both scripts and `retain.py`, `node --check dashboard/app.js` and `nginx -t` all
pass. The gateway is **deployed** on internal `0.0.0.0:8090` with no published
port, and nginx routes the three exact paths. `scripts/check.py` **passed live**
with it deployed. One **ended experimental** snapshot was then deleted through the
dashboard on the owner's explicit authorisation: `deleted: 1`, `missing: 0`,
`failed: []`, `active: 0`, its image URL 404 afterwards, and **no archive scope**,
so nothing else moved. Still open: the archive scope against real footage, the
CLI delete path, and any sign-off. The phase stays unaccepted because a
destructive path that has been exercised once is not the same as a phase meeting
every exit condition.

## Phase 3 — email snapshot alerts

Not started. No notifications exist today. When this phase begins, the design is:

- **Trigger:** a Frigate event whose alert is gated on a zone, not merely any
  detection.
- **Payload:** the snapshot image, timestamp, camera name, zone name, and event
  link. No continuous video, no RTSP stream in the message.
- **Event ID dedup.** Use Frigate MQTT state updates (or an existing
  notification integration), enabling a broker in this phase. Repeated updates
  can refer to the same event. Every outbound message is keyed on the Frigate
  event ID and a send-state marker, so a message already sent for an event ID is
  not sent again, including across a container restart.
- **Cooldown.** A per-zone quiet period after each successful send, plus a hard
  daily cap per zone. Without the daily cap, tracked presence in one zone running
  for an hour produces an unbounded mail flood. Exact values are an owner decision.
- **Bounded retries.** A send failure retries a small fixed number of times with
  capped backoff, then the event is marked failed and dropped. A failed send must
  never block detection.
- **Secrets handling.** SMTP credentials in `.env` or Docker secrets, never in
  `config/config.yml`, never in the repository.
- **Recipients are still an open question.** See below.

**Exit condition:** a deliberate flood scenario produces a bounded number of
messages, and killing the network mid-send does not produce a runaway retry.

## Phase 4 — remote access

Last, and only after notifications are proven.

VPN is the intended route. Exposing 8971 or 8088 directly to the internet is not
the plan.

- The dashboard is unauthenticated. Any remote exposure must add authentication
  first, or tunnel into loopback without ever binding publicly.
- A subnet VPN alone cannot reach a loopback-only listener; use an authenticated
  VPN plus an explicit proxy or tunnel to loopback.
- Reachability from outside the LAN is verified without any port forward to the
  Frigate container.
- Access logs are reviewed after go-live. If nobody can say who looked at the
  footage, that is a finding.

## Next phase — activity-based detection (not started)

The rule that exists today — a `person` reaching **60 seconds of tracked
in-zone presence** in a full-frame zone — is a presence counter, and it
**assesses no risk at all**: with the example polygons it means "somebody was on
camera for a minute". It **measures nothing about how the object behaved**: not
whether it moved, not whether it left and came back, and not intent. Whether it
excludes an ordinary crossing is **unmeasured** — no controlled walkthrough has been
run — so no claim either way is recorded here. The owner's stated direction for
what comes next is activity-based suspicious detection, covering activity that
suggests weapons or harm. That is a design direction recorded here, **not a built
feature**: no model has been selected, evaluated, or trained, and nothing in this
repository implements it.

What such a phase would have to define before any of it counts: zones narrowed
from the full frame to the actual areas of interest, the hours it applies,
persistence thresholds, and temporal evidence rather than a single frame —
suspicion is a sequence, not a still. Output would be a labelled
"worth reviewing" signal with the evidence a person checks, so validation is a
**labelled review set plus manual human review**, never an automatic threat
claim. In that future strategy, **ordinary person presence alone must not
qualify**, and neither may "was in the zone for a minute".

Until those criteria are implemented, the runtime stays what it is: six hours for
an ordinary snapshot, pinned-until-deleted for a `person` event that reached the
threshold in its own camera's loitering zone, and controlled manual deletion from
the dashboard. The 60-second threshold and the deletion endpoint changed no
classification rule and added none.

## Risks

- **Retention and disk.** Snapshots accumulate, and after the retention worker is
  running the accumulation is deliberate: loitering events have **no automatic
  expiry** and are removed only by a person, by hand or through the controlled
  deletion. With the default full-frame loitering zones that is anyone whose
  tracked presence in view reaches a minute. Nobody reviewing means the disk fills,
  and a full disk takes the host down, detection included. Ordinary events —
  anything that did not loiter, plus anything the worker never saw — age out
  instead, at the
  first cleanup pass after they pass six hours. Snapshots already pinned under the
  previous whole-frame rule are still on disk and are **not** released by this
  change. Disk growth therefore has to be watched as an operator routine, and
  neither `check_retention.py` nor `check_console_api.py` proves anything about
  how this behaves live.
- **Deletion is irreversible, and it is now one click from a page.** A person who
  deletes the wrong scope loses evidence that cannot be recovered from this
  system. The two scopes, the confirmation, the `failed` and `active` counts and
  the fail-closed walk are there to make that unlikely; none of them makes it
  undoable. The archive scope is the sharper edge: it is bounded by the summary's
  own cutoff, so it cannot reach the newest events, but it can still be far larger
  than the page it was started from.
- **The gateway is the only write path, so it is the only thing to break.** Its
  refusals (exact Host, matching Origin, JSON content type, action header, full
  body validation) are the whole defence, and the dashboard is unauthenticated on
  loopback. Stated precisely: they block **cross-site requests from a browser**,
  where the requesting page cannot control `Host` or present a matching `Origin`.
  They do **not** block a malicious local process, which can forge both headers
  and which, on this machine, can already read every frame. So the property that
  actually holds is narrower and worth keeping: a request is refused before any
  upstream request is made, and the person still has to confirm the scope. A bug
  or a future edit that loosens one of those checks is a data-loss bug.
  `scripts/check_console_api.py` is the guard, and it **passes** (2026-10-02) —
  which is a statement about a disposable upstream, not a live one.
- **The worker is a single point of evidence loss.** Nothing pins zone events but
  a 30-second poll. Stopped or crash-looping longer than the six-hour window, it
  silently loses events Frigate then cleans up, and nothing in the stack reports
  that. The operational fix is watching the worker's log lines, which report
  pinned, already-retained and skipped counts each pass.
- **The live view depends on a component nobody here controls.** It runs through
  go2rtc, bundled in the Frigate image, with its own player script. A go2rtc
  upgrade can change the codec string the browser must support, the WebSocket
  shape, or the module's exports, and the dashboard's honest failure modes
  ("cannot decode HEVC", "no picture") are what keep that from reading as a
  healthy camera. It is a dependency, not a guarantee.
- **Camera outages.** A camera that stops streaming is silent. Nothing in this
  prototype fully distinguishes "nobody is in the zone" from "the camera is dead
  and therefore reports nobody is in the zone." The console narrows this with
  per-camera `camera_fps` / `process_fps` and explicit fail/stale labelling, but
  camera availability monitoring is still a separate requirement before anyone
  trusts a quiet result.
- **Credential leakage through stats.** Any future proxy or dashboard that
  exposes raw `/api/stats` exposes camera passwords through `cpu_usages.cmdline`.
  The pinned key allowlist and `scripts/check.py` are the guard. Do not relax
  either for convenience.
- **Host availability.** `restart: unless-stopped` handles a crash but not power
  loss, a kernel panic, a full disk, or a host that is simply off. There is no
  uptime alerting.
- **Notification flood.** Phase 3 is the highest-risk phase for real-world
  damage. A dedup or cooldown bug mails a real person repeatedly.
- **Privacy and access.** Footage of a private property is sensitive.
  Credentials stay local, ports stay on loopback, and who can view is a decision
  to make explicitly rather than by whoever first asks for access.
- **Unauthenticated local surface.** Anything running on this host can read the
  dashboard, including other browser profiles and local extensions with host
  permissions. Acceptable for a single-user appliance on loopback; not
  acceptable off-host.

## Open questions blocking later phases

- **Recipients.** Who receives alerts, and how many? One address or a list.
- **Hours.** Is this 24/7, or only certain hours? Quiet hours need a timezone.
- **Criteria.** What actually counts as worth an email: any 60 second tracked
  presence, only longer thresholds, only a subset of zones?
- **Response expectation.** If alerts imply someone will act on them, that is a
  human process commitment.

## Official references

- <https://docs.frigate.video/frigate/installation/>
- <https://docs.frigate.video/guides/getting_started/>
- <https://docs.frigate.video/configuration/zones/>
- <https://docs.frigate.video/configuration/notifications/>

## Decisions and acceptance record

- Suite is four cameras, confirmed by the owner. `camera_1` … `camera_4` are
  neutral names until physical cameras are assigned.
- Acceptance criterion, per camera: tracked presence in that camera's loitering
  zone reaching sixty seconds, at any hour. **Revised 2026-10-02** from three
  seconds. Whether that excludes an ordinary crossing is **unmeasured** — no
  controlled walkthrough has been run — so it is recorded as a target, not a
  verified behaviour.
- Live view is the camera's own main channel, un-re-encoded, with no quality
  ladder: full resolution or an explicit "this browser cannot decode HEVC".
  Detection stays on 352×288 at 3 fps; the live channel stays at its source's
  reported 25 fps. Neither changed with the loitering rule or the deletion
  endpoint.
- Retention: six hours for an ordinary snapshot (`0.25` days), and Frigate's own
  `retain_indefinitely` for a person event that reached the threshold in its own
  camera's loitering zone. Continuous recordings stay disabled. Both lifetimes are
  Frigate's own; go2rtc restreams and retains nothing.
- **Deletion, revised 2026-10-02 on the owner's explicit instruction.** It was
  "a named CLI call by the operator, never a button in the browser". It is now
  that plus one authorised controlled deletion in the UI: the reviewed rows or the
  whole archive up to a counted cutoff, behind an explicit irreversible
  confirmation, through `retain.py --serve`. It is still never automatic and still
  unreachable by anything but the dashboard's own origin. **Revised again
  2026-10-02:** it may now be exercised on the owner's explicit authorisation, and
  one ended experimental snapshot was deleted that way. The safeguards are
  unchanged and are not negotiable — exact Host, matching Origin, JSON content
  type, action header, full validation before any upstream request, and a person
  deciding to do it.
- Only "a person loitering in a named zone" is detected. No behaviour, anomaly or
  classification detection is in scope, and none is built. The 60-second counter
  measures tracked presence, not behaviour and not risk.
- No footage or camera credentials are sent to OpenCode or committed to Git.
- Detection stays on the home host; email requires internet only for delivery.
- Measure at least ten inside-zone and ten outside-zone passages per camera in
  daylight and darkness; record misses and false alerts rather than assuming
  perfect detection.
- Proposed detection acceptance per camera: at least 9/10 sustained inside-zone
  passages detected in each lighting condition and zero zone alerts for the ten
  outside-zone passages. Tune or reject deployment if these fail.
- Measure UI alert latency separately from final snapshot completion and future
  email delivery.
- 352×288 at 3 fps is a starting configuration, not a measured optimum.