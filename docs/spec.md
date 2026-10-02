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
`scripts/check.py` suite passes every assertion. What is still unverified is
the detection work itself: zone walkthrough, accuracy measurement, night
behaviour, and alert timing — and, on retention, an actual six-hour expiry, a
retained event followed past six hours, and the hand-delete path. See
[`validation.md`](validation.md) for exactly what was and was not run.

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
- **There is exactly one detection rule: a person in a named zone.** No
  behaviour model, no anomaly detection, no classification beyond `person`, and
  no fall-over, package or line-crossing rule. Nothing in the repository should
  be read as implying otherwise, because none of it exists.
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
  event that entered its own camera's zone is pinned by `scripts/retain.py`
  through Frigate's native `retain_indefinitely`, which that cleanup skips. The
  example zones are the full frame, so most saved person snapshots are pinned and
  therefore kept until a person deletes them; growth is bounded by review, not by
  a clock. Both lifetimes are Frigate's own bookkeeping — go2rtc restreams and
  keeps nothing. The worker's boundary behaviour is verified against a fake
  Frigate, and its pinning is **observed on the live stack** (49 pinned plus 27
  already retained across 76 events; a later pass found all 76 already retained
  and exited 0). No expiry has been waited out, and no retained event has been
  confirmed to survive past six hours.

## Phase 1 — one camera, one zone, person loitering

Superseded. The original single-camera `courtyard` configuration is replaced by
the four-camera layout below; the detection rules carried over unchanged per
camera.

- One zone per camera, `objects: [person]`.
- `loitering_time: 3` — a person must remain in the zone for 3 seconds.
- `inertia: 3` — 3 consecutive frames inside the zone are required.
- Alerts gated on `required_zones: [<that camera's zone>]` so events elsewhere in
  frame do not create alerts.
- Snapshots on, timestamped, bounding box drawn, gated on the same zone.
- `record.enabled: false`. MQTT disabled. No notification provider.

## Phase 2 — four cameras and a read-only console

Implemented in this repository. **Accepted: no.** The code exists; the exit
conditions below have not been met.

### Configuration

- Four independent cameras, `camera_1` … `camera_4`, each `enabled: true` in
  `config/config.example.yml`, each with its own RTSP URL variable.
- Four independent zones, `camera_1_area` … `camera_4_area`. A normalised
  polygon drawn on one camera means nothing on another; each must be redrawn.
- The example polygon for each zone is the full frame `0,0,1,0,1,1,0,1`. This
  is a deliberate whole-view default: every view is covered, at the cost of more
  events than you want to read. Narrow each zone natively when the scene is
  known.
- `required_zones` is per camera and is **not** hoisted into a shared block. A
  shared zone label across cameras would silently gate every camera on one
  camera's geometry.
- A camera that cannot be reached degrades alone: Frigate retries that feed and
  the other three carry on. `enabled: false` removes a camera from the suite
  deliberately; it is not a workaround for a camera that is down.

### Console

- nginx serves static HTML/CSS/JS and proxies exactly four read-only Frigate
  endpoint shapes: `/api/stats`, `/api/events`, `/api/camera_1..4/latest.jpg`,
  and `/api/events/<native-id>/snapshot.jpg`, where the id is Frigate's own
  `<frame_time>-<rand_id>` tracker form. `GET` and `HEAD` only.
- Live video is pinned just as tightly: one exact path serves go2rtc's bundled
  player (`/video-rtc.js`), and `/live/camera_1` … `/live/camera_4` are WebSocket
  upgrades, each naming one main stream with no query string at all. go2rtc's own
  config, stream list and control WebSocket are not proxied, and there is no
  generic `/live/` path — a fifth camera, or any suffix on a camera path, is a
  404 rather than a different stream.
- The console code contains **no write path of any kind**: no `POST`, no
  `DELETE`, no state mutation, no login. Everything it shows, it can only read.
  That is what makes review a person's decision rather than the page's.
- `/api/stats` is rewritten to a **fixed** upstream URI with a **fixed** key
  allowlist, and `/api/events` to a **fixed, bounded** upstream URI. Both discard
  the client's query string, so no override can widen the surface.
- Frigate's API port is never published. Both published ports are loopback.
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
  **that camera's** required zone — by setting Frigate's own
  `retain_indefinitely` every 30 seconds. It holds no state of its own, so a
  restart neither re-pins nor forgets.
- It runs as a `retention` service with **no ports, no `env_file`, no credential
  and no database of its own**, reading only `http://frigate:5000` on the
  internal network.
- Its automated pass contains no `DELETE`. Deleting is one named event id, typed
  by a person, after review, and it is irreversible:
  `docker compose exec retention python3 /app/retain.py --delete-reviewed EVENT_ID`.
  There is deliberately no delete button in the browser and no undo.
- Consequence, stated rather than hidden: with the default full-frame zones,
  **most saved person snapshots are retained until a person deletes them.** Disk
  growth is bounded by review, not by a timer, and there is no cap. If the worker
  is stopped longer than the six-hour window, events it never saw can be cleaned
  up first — which is why the worker has to be watched running, not just written.

**Latency target: 5 seconds from loiter qualification to a visible alert.** A
target measured by hand, not a guarantee. The three-second loitering threshold
precedes it. Measure alert visibility and snapshot availability separately: a
final best snapshot may only be saved when tracking ends. It excludes time
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
  twice are observed on real events. The hand-delete path is not, and nothing has
  been watched surviving the six-hour window. Until that is recorded, retention
  is runtime-verified for pinning only.

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
  daily cap per zone. Without the daily cap, a person standing in the zone for an
  hour produces an unbounded mail flood. Exact values are an owner decision.
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

The rule that exists today — a `person` in a full-frame zone — keeps routine
detections and **assesses no risk at all**: with the example zones it means "a
person was on camera", and the retention worker then pins most of those
snapshots indefinitely. The owner's stated direction for what comes next is
activity-based suspicious detection, covering activity that suggests weapons or
harm. That is a design direction recorded here, **not a built feature**: no
model has been selected, evaluated, or trained, and nothing in this repository
implements it.

What such a phase would have to define before any of it counts: zones narrowed
from the full frame to the actual areas of interest, the hours it applies, dwell
or persistence thresholds, and temporal evidence rather than a single frame —
suspicion is a sequence, not a still. Output would be a labelled
"worth reviewing" signal with the evidence a person checks, so validation is a
**labelled review set plus manual human review**, never an automatic threat
claim. In that future strategy, **ordinary person presence alone must not
qualify**.

Until those criteria are implemented, the existing runtime retention stays
exactly as it is: six hours for an ordinary snapshot, and pinned-until-deleted
for a `person` event in its own camera's zone. The cleanup that renamed this
console and removed the header's Frigate links changed no retention behaviour and
no detection rule.

## Risks

- **Retention and disk.** Snapshots accumulate, and after the retention worker is
  running the accumulation is deliberate: retained person events have **no
  automatic expiry** and are removed only by a person, by hand. With the default
  full-frame zones that is most saved person events. Nobody reviewing means the
  disk fills, and a full disk takes the host down, detection included. Untreated
  events — anything the worker never saw — age out instead, at the first cleanup
  pass after they pass six hours. Disk growth therefore has to be watched as an
  operator routine, and `scripts/check_retention.py` proves nothing about how
  this behaves live.
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
- **Criteria.** What actually counts as worth an email: any 3 second loiter,
  only longer dwell times, only a subset of zones?
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
- Acceptance criterion, per camera: person remains in that camera's zone for
  three seconds, at any hour.
- Live view is the camera's own main channel, un-re-encoded, with no quality
  ladder: full resolution or an explicit "this browser cannot decode HEVC".
- Retention: six hours for an ordinary snapshot (`0.25` days), and Frigate's own
  `retain_indefinitely` for a person event that entered its own camera's zone.
  Continuous recordings stay disabled. Both lifetimes are Frigate's own; go2rtc
  restreams and retains nothing.
- Deleting a reviewed event is a named CLI call by the operator, never a button
  in the browser and never something an agent runs.
- Only "person in a named zone" is detected. No behaviour, anomaly or
  classification detection is in scope, and none is built.
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