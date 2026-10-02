# Specification

Minimal surveillance detection, in phases. Each phase has an exit condition. Later
phases do not start until the earlier one is accepted by hand.

Nothing in this document has been validated against real hardware. See
[`validation.md`](validation.md).

## What is actually known

- **Deployment hardware is unknown; the current development host uses macOS ARM64.** One camera is an authenticated RTSP device reachable
  from a workstation. Four more devices exist but are unverified: firmware
  versions, RTSP paths, codecs, and authentication are unconfirmed; HTTP/RTSP reachability was observed.
- **No real IPs or credentials exist in this repository.** `.env` is gitignored,
  `chmod 600`, and never committed.
- **Detection model:** bundled SSDLite MobileNetV2 on OpenVINO, CPU, `person`
  only. No custom YOLO training, export, or inference code until a measured
  failure justifies it. That trigger is a number, not a hunch: a specific class of
  false negative or false positive that tuning `zones`, `objects`, `motion`, and
  `detect.fps` cannot resolve.

## Phase 1 — one camera, one zone, person loitering

Implemented by the files in this repository.

- One camera, `courtyard`, detect role only at `detect.fps: 5`.
- One zone, `courtyard_area`, `objects: [person]`.
- `loitering_time: 3` — a person must remain in the zone for 3 seconds.
- `inertia: 3` — 3 consecutive frames inside the zone are required for zone presence.
- Alerts are gated on `required_zones: [courtyard_area]` so events anywhere else
  in frame do not create alerts.
- Snapshots on, timestamped, bounding box drawn, gated on the same zone.
- `record.enabled: false`. MQTT disabled. No notification provider configured.

**Latency target: 5 seconds from loiter qualification to a visible alert.** This is a target measured by hand, not a guarantee and not a
product claim. The three-second loitering threshold precedes that target. Measure alert visibility
and snapshot availability separately: a final best snapshot may only be saved when
object tracking ends. It excludes time before the person becomes trackable, and it will be worse
at night, in heavy rain, or when the host is loaded.

**Exit condition:** all checks in [`validation.md`](validation.md) pass by hand,
including night operation and false-positive review.

## Phase 2 — email snapshot alerts

Not started. No notifications exist today. When this phase begins, the design is:

- **Trigger:** a Frigate event whose alert is gated on the zone, not merely any
  detection.
- **Payload:** the snapshot image, timestamp, camera name, zone name, and event
  link. No continuous video, no RTSP stream in the message.
- **Event ID dedup.** Use Frigate MQTT state updates (or an existing notification integration),
  enabling a broker in Phase 2. Repeated updates can refer to the same event. Every outbound message is keyed on the Frigate event ID and a
  send-state marker. A message already sent for an event ID is not sent again,
  including across a container restart, so the dedup record has to survive
  restarts.
- **Cooldown.** A per-zone quiet period after each successful send, plus a hard
  daily cap per zone. Without the daily cap, a person standing in the zone for an
  hour with the dedup key churning produces an unbounded mail flood. Exact values
  are a decision for the owner, not a default.
- **Bounded retries.** A send failure retries a small fixed number of times with
  capped backoff, then the event is marked failed and dropped. No unbounded retry
  loop, no queue that grows while the host is offline. A failed send must never
  block detection.
- **Secrets handling.** SMTP credentials in `.env` or Docker secrets, never in
  `config/config.yml`, never in the repository.
- **Quiet hours are a decision, not an assumption.** See open questions.

**Exit condition:** a deliberate flood scenario produces a bounded number of
messages, and killing the network mid-send does not produce a runaway retry.

## Phase 3 — all cameras

Deliberately blocked on hardware truth. Four additional streaming devices are unverified; do not assume each is a camera.

Before any config is written for them, each needs: confirmed RTSP URL and stream
path, confirmed resolution and framerate, confirmed whether the host can pull
that many streams at `detect.fps: 5`, and a decision on whether each camera gets
its own zone or shares one schema.

**Multi-camera acceptance:**

- Per-camera detector load is measured, not assumed. If adding a fifth camera
  pushes detection past the 5 second target on the cameras already accepted, that
  camera waits rather than degrading the working ones.
- Per-camera latency is measured independently. One slow camera must not be hidden
  by an average across five.
- Zones are drawn per camera. A normalized polygon from camera `courtyard` means
  nothing on camera `driveway`.
- Overnight soak with all cameras live, to catch thermal throttling and USB or
  network drops that do not appear in a ten minute test.
- A single camera failing must not stop the others. Restart behaviour, log noise,
  and alert suppression for the failed camera are all checked explicitly.
- False positives are reviewed per camera, not pooled, because lighting and scene
  differ.

**Exit condition:** every camera independently passes the Phase 1 checks.

## Phase 4 — remote access

Last, and only after notifications are proven.

VPN is the intended route. Exposing 8971 directly to the internet is not the plan.

- The UI stays on `127.0.0.1` in `compose.yaml`. Use an authenticated VPN plus an explicit proxy or tunnel to loopback;
  a subnet VPN alone cannot reach a loopback-only listener.
- Reachability from outside the LAN is verified without any port forward to the
  Frigate container.
- Access is read-mostly by default: watching and reviewing, not reconfiguring.
- Access logs are reviewed after go-live. If nobody can say who looked at the
  footage, that is a finding.

## Risks

- **Retention and disk.** Snapshots accumulate. `storage/` is bind-mounted to the
  host and will fill a disk if nothing prunes it. Snapshot retention is configured to three days. Host disk
  alerting and disk-full behaviour still need validation. A full disk can
  take the whole host down, including detection.
- **Camera outages.** A camera that stops streaming is silent. Nothing in this
  prototype distinguishes "nobody is in the zone" from "the camera is dead and
  therefore reports nobody is in the zone." Both look like quiet. Camera
  availability monitoring is required before anyone trusts a quiet result.
- **Host availability.** `restart: unless-stopped` handles a crash but not a
  power loss, a kernel panic, a full disk, or a host that is simply off. This
  prototype is a prototype, not a monitored service. There is no uptime alerting.
- **Notification flood.** Phase 2 is the highest-risk phase for real-world damage.
  A dedup or cooldown bug mails a real person repeatedly. That is why the bounded
  retry and daily cap are exit conditions, not nice-to-haves.
- **Privacy and access.** Footage of a private property is sensitive. Credentials
  stay local, ports stay on loopback, and who can view is a decision to make
  explicitly rather than by whoever first asks for access.

## Open questions blocking later phases

- **Recipients.** Who receives alerts, and how many? One address or a list.
- **Hours.** Is this 24/7, or only certain hours? Quiet hours need a timezone, and
  a decision about what happens to an event that fires during them.
- **Criteria.** What actually counts as worth an email: any 3 second loiter, only
  longer dwell times, only a subset of zones? "A person was briefly in the yard" may
  be noise rather than signal, and the threshold is the owner's call.
- **Response expectation.** If alerts imply someone will act on them, that is a
  human process commitment. If nobody is expected to respond, the design should
  lean toward fewer, higher-confidence alerts.

## Official references

- <https://docs.frigate.video/frigate/installation/>
- <https://docs.frigate.video/guides/getting_started/>
- <https://docs.frigate.video/configuration/zones/>
- <https://docs.frigate.video/configuration/notifications/>

## Decisions and acceptance record

- Initial criterion: person remains in the courtyard zone for three seconds, at any hour.
- Snapshots retained three days; continuous recordings disabled.
- No footage or camera credentials are sent to OpenCode or committed to Git.
- Detection stays on the home host; email requires internet only for delivery.
- Before deployment choose an always-on host, confirm accelerator support, reserve camera IPs via DHCP, and define disk/availability alerts.
- Measure at least ten inside-zone and ten outside-zone passages in daylight and darkness; record misses and false alerts rather than assuming perfect detection.
- Proposed detection acceptance: at least 9/10 sustained inside-zone passages detected per lighting condition and zero zone alerts for the ten outside-zone passages. Tune or reject deployment if these fail.
- Measure UI alert latency separately from final snapshot completion and future email delivery.
