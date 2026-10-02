# Specification

Minimal surveillance detection, in phases. Each phase has an exit condition. Later
phases do not start until the earlier one is accepted by hand.

Live camera acceptance is **pending**: the four channels authenticated at one
point, but camera access now returns 401 and the suite is not confirmed running.
See [`validation.md`](validation.md) for exactly what was and was not run.

## What is actually known

- **Deployment hardware is unknown; the current development host uses macOS
  ARM64.** Four independent camera views exist as HEVC 352×288 substreams on
  recorder channels 1–4. Channels on one recorder and separate devices are both
  supported; only byte-identical URLs duplicate a view.
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
- **Passwords go into `.env` raw.** Frigate encodes the password component
  itself; a pre-encoded value is double-encoded and rejected with 401.
- **Frigate's raw `/api/stats` contains RTSP credentials.** `cpu_usages.cmdline`
  carries the full process command line, including every camera's RTSP URL. Any
  browser-facing surface must therefore never proxy raw stats. The dashboard
  pins a fixed key allowlist server-side for exactly this reason, and
  `scripts/check.py` asserts the pinned shape.

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
- `/api/stats` is rewritten to a **fixed** upstream URI with a **fixed** key
  allowlist, and `/api/events` to a **fixed, bounded** upstream URI. Both discard
  the client's query string, so no override can widen the surface.
- Frigate's API port is never published. Both published ports are loopback.
- The console is unauthenticated by construction, because loopback-only is the
  boundary. Remote extension requires authentication and is Phase 4.
- Per-camera status is derived from `camera_fps`, `process_fps` and
  `detection_enabled` in the newest stats sample, and requires a recent picture
  before a camera is called healthy. A failed or stale read clears the sample,
  so an unreadable backend reads as "no data", never as healthy.
- The event list contains only person events whose `zones` intersect the required
  zone of a camera in the current filter. It is never presented as "every person
  detection"; an empty list is explicitly labelled as not proving the cameras
  were watching.

**Latency target: 5 seconds from loiter qualification to a visible alert.** A
target measured by hand, not a guarantee. The three-second loitering threshold
precedes it. Measure alert visibility and snapshot availability separately: a
final best snapshot may only be saved when tracking ends. It excludes time
before the person becomes trackable, and it will be worse at night, in heavy
rain, or when the host is loaded.

**Exit conditions:**

- The 401 on camera access is resolved and all four channels stream again.
  Nothing below is measurable until they do.
- Every camera independently passes the Phase 1 checks in
  [`validation.md`](validation.md).
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
  checks actually run rather than skipped.

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

## Risks

- **Retention and disk.** Snapshots accumulate. `storage/` is bind-mounted and
  will fill a disk if nothing prunes it. Snapshot retention is three days. A
  full disk can take the host down, including detection.
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
- Snapshots retained three days; continuous recordings disabled.
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