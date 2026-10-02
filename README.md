# Surveillance Console

A four-camera Frigate prototype with a small read-only dashboard. The one thing
it does: detect a person loitering in a named per-camera zone, then snapshot it.
Email and every other outbound notification is **not** wired up. Nothing leaves
the host.

The live view is the camera's own main channel — 2560×1440 HEVC, the source's
reported 25 fps — handed to the browser without re-encoding. That 25 fps is what
the channel reports, not a render rate measured in a browser. Detection runs
separately, on its own low substream at 3 fps.

- Frigate pinned to **0.18.0** (`ghcr.io/blakeblackshear/frigate:0.18.0`).
- Four independent cameras, `camera_1` … `camera_4`, each with its own zone
  `camera_1_area` … `camera_4_area`.
- Live view: each camera's **main** channel, restreamed by the go2rtc bundled in
  the Frigate image and served over four fixed WebSocket paths, one per camera.
  Full resolution, no re-encode, no silent downgrade — a browser that cannot
  decode HEVC is told so instead of being handed a lesser stream.
- Detection input is **352×288 at 3 fps per camera** (the low channel, recorder
  channels 1–4). That is a starting point chosen to keep a CPU-only four-camera
  suite within reach, and on 2026-10-02 it did run at 3.0–3.1 fps on all four —
  but it has not been shown to be the right rate for detection quality. Tune
  `detect.width` / `detect.height` / `detect.fps` in `config/config.yml`. **Not**
  `model.width` / `model.height`: those must stay **300×300** to match the
  bundled SSDLite MobileNetV2 input tensor, and frames are resized into it.
  **A saved snapshot is cut from this 352×288 substream, not from the 2560×1440
  live view** — the two qualities are unrelated and the event image is the small
  one.
- Detector: the bundled SSDLite MobileNetV2 on OpenVINO, CPU, `person` only. No
  custom training or inference code, and no behaviour, anomaly or classification
  model of any kind: a person in a named zone is the only rule that exists.
- Continuous recording disabled (`record.enabled: false`).
- **Snapshots.** An ordinary snapshot is kept **6 hours**
  (`snapshots.retain.default: 0.25`, in days). Frigate's periodic cleanup runs
  about every five minutes and is what actually deletes it, so six hours is the
  nominal cutoff it is measured against, not an exact deletion time. A `person`
  event that entered **its own camera's** zone is pinned by the retention worker
  through Frigate's native `retain_indefinitely`, which cleanup does not touch,
  so it survives until somebody deletes it by hand. Because the example zones are
  the whole frame, **most saved person snapshots are kept**, not rationed to six
  hours. See [Retention and review](#retention-and-review).
- Two loopback ports, nothing else:
  - `127.0.0.1:8971` — Frigate's own authenticated UI.
  - `127.0.0.1:8088` — the dashboard. Read-only, **no login**.
- Target platform is **Linux + Docker**. Docker on macOS gives no guaranteed GPU
  acceleration, so detection latency measured there is not representative.

## Status

| Area | State |
| --- | --- |
| Four-camera compose + example config | implemented |
| nginx read-only proxy with pinned stats keys, pinned live paths and bounded events | implemented |
| Dashboard UI (live video, per-camera status, zone events) | implemented |
| Camera authentication | four low channels and four main channels, re-probed 2026-10-02 |
| Runtime camera access | **recovered 2026-10-02** — the recorder lockout expired; all four channels stream |
| Live capture and detector inference | **verified 2026-10-02** — 3.0–3.1 fps in, detection enabled; 9.45 ms inference is exploratory only |
| Live main-stream video in a browser | **verified 2026-10-02, re-verified after the Frigate restart** — four players, 2560×1440 HEVC, no errors, detectors 2.9–3.1 fps |
| `scripts/check.py` smoke check | **PASS 2026-10-02, expanded version** — all assertions hold: 11 forbidden paths refused, `POST`/`DELETE` refused on `/api/stats` and `/live/camera_1`, `media-src 'self' blob:`, player module 200, no credentials in the three served assets, stats held plain and against an override, all four detecting, a 50-event list with native ids plus one snapshot JPEG 200 (all 50 listed snapshot URLs separately checked at root: 200 + JPEG, across three cameras), all 48 settled zone events flagged kept |
| `scripts/check_retention.py` boundary check | **PASS 2026-10-02** — every assertion held |
| Retention worker pinning kept snapshots | **verified live 2026-10-02** — first pass 49 pinned + 27 already retained = 76 events; a later `--once` found all 76 already retained, exit 0 |
| Ordinary six-hour expiry, watched end to end | **not done** — the window is applied and confirmed in native `/api/config`, but nobody waited it out |
| Zones drawn on real boundaries | **not done** — every zone is the full frame |
| Zone walkthrough, alert timing, night, false positives | **not run**, **not verified** |
| Email notifications | **not started**, still a later phase |

What was actually run, and what it does not show:

- `docker compose config` parses, and the containers start. Cameras were
  temporarily disabled during the lockout to avoid a login-retry loop while
  access was 401; they are enabled again.
- `nginx -t` on the dashboard config passes; `node --check dashboard/app.js`
  passes (syntax only).
- Browser review: all four players actually playing 2560×1440 while the
  detectors read 3 FPS, and no horizontal overflow measured at 320, 375, 414 or
  768 px. The `Camera1` and `All` chips change the filter label and selection;
  which rows each chip keeps is still not evidenced.
- Root security headers, the denied routes, `POST`/`DELETE` on `/api/stats`, and
  the stats history key allowlist all hold, plain and against an override attempt,
  with no credential leaks.
- **Recovery, 2026-10-02.** After VLC was closed and both containers were
  stopped, no login was attempted until past the recorder's 240-second
  `remainLockSecond`. The recorder's native Web3 login then **succeeded with
  unchanged credentials**, `getBlockUserInfoList` returned `0`, and the
  diagnostic session was logged out. `ffprobe` then succeeded sequentially on all
  four low channels, each HEVC 352×288, and then on all four **main** channels,
  each **HEVC 2560×1440 at 25 fps**. Recovery on the same credentials confirms
  the lockout — not a credential problem — was what blocked access. The trigger
  is not established: Frigate's earlier double-encoded-password retries are a
  likely trigger, but recorder audit logs were not examined, so exact trigger is
  not proven.
- **Four cameras live in Frigate, 2026-10-02.** The git-ignored `config/config.yml`
  was restored from `config/config.example.yml` and **only Frigate** was started,
  with raw passwords in the detect RTSP URLs. Live stats: all four `camera_fps`
  3.0–3.1, `process_fps` 3.0–3.1, `detection_enabled` true. The four
  `latest.jpg` responses were `200` with valid JPEGs — latest-frame previews,
  **not** saved detection snapshots. Two minutes of fresh Frigate logs contain
  **no 401 entries**.
- **Live video in the browser, 2026-10-02.** All four players ran: 2560×1440
  decoded off the main channels, no errors in the page, no dropped players.
  go2rtc's bundled MP4 for each main channel answered `200`, and `nginx -t`
  passed with the live locations in place.
- **`scripts/check.py` passes every assertion, expanded version, 2026-10-02.** All
  four cameras detecting, the 11 forbidden paths refused, `POST`/`DELETE` refused
  on `/api/stats` and on `/live/camera_1`, `media-src 'self' blob:` present, the
  player served as the module the page imports, no credential-bearing `rtsp://`
  URL in `index.html`, `app.js` or `video-rtc.js`, the stats allowlist held both
  plain and against a `?keys=` override, a 50-event list with native ids and one
  snapshot JPEG 200, plus a separate root diagnostic in which all 50 listed
  snapshot URLs returned 200 + JPEG across three cameras, and all 48 settled zone
  events still flagged kept.
- **Post-restart replay, 2026-10-02.** After the Frigate restart that applied the
  new retention window, all four players again actually played 2560×1440 with no
  page errors, and the detectors read 2.9–3.1 fps. After a dashboard badge
  signature fix and reload, 49 rows carried a visible **Kept for review** badge
  out of the latest 50.
- **`scripts/check_retention.py` passes every assertion.** Strict `before`/
  `after` pagination over a tied group with no `offset` ever sent, a refused pin
  (HTTP 500, and a `200` carrying `success: false`) left unpinned and retried on
  the next pass, the qualification rules, a page cap that fails the pass instead
  of reporting one it did not run, no automatic `DELETE`, and non-native manual
  ids refused before anything is sent. It is a fake-Frigate result: it says
  nothing about the live stack.
- **The worker has run against the live stack.** `docker compose up -d retention`
  succeeded, and its first pass reported **49 pinned and 27 already retained
  across 76 events**. A later `--once` reported all **76 already retained** and
  exited 0; a further `--once` after the Frigate restart reported **78 already
  retained** and exited 0, so a pinned event is not pinned twice. What that is
  **not**: nothing
  has been left for six hours, so **no ordinary expiry has actually been
  observed**, and no retained event has been followed past six hours to confirm
  empirically that it survived.
- **The retention window is applied.** The git-ignored `config/config.yml` sets
  `snapshots.retain.default: 0.25` on all four cameras, zones and detection
  preserved. The Frigate restart that applied it **completed**, and native
  `/api/config` now reports `snapshots.retain.default * 24 == 6.0` for all four,
  with `record.enabled` false on all four. The installed cleanup code was read to
  confirm the model: it **selects the unretained events — `retain_indefinitely`
  false — for expiry** on a `timedelta(days=...)` cutoff, **excludes the
  retained-true ones, which therefore persist**, and then waits 300 s. Six hours
  is therefore configured and confirmed in effect — it has still not been watched
  to expiry.
  Retention is Frigate's alone here: go2rtc only restreams the live view and keeps
  nothing.
- **Inference latency is exploratory.** OpenVINO reported `inference_speed`
  9.45 ms in a snapshot: one sample on macOS Docker, not a benchmark and not the
  accepted figure.

The suite **is** streaming live video, the detector **is** running, and the
snapshot path **does** return real saved detections — but nothing here shows it
catching a person on request: no zone walkthrough, alert timing, night, or
false-positive check has been run. The full record is in
[`docs/validation.md`](docs/validation.md).

## Setup

```sh
cp config/config.example.yml config/config.yml
cp .env.example .env
chmod 600 .env
```

Edit `.env` and put the real RTSP URL for **each** camera in
`FRIGATE_CAMERA_1_RTSP_URL` … `FRIGATE_CAMERA_4_RTSP_URL`, and the same camera's
main channel in `FRIGATE_CAMERA_1_MAIN_RTSP_URL` …
`FRIGATE_CAMERA_4_MAIN_RTSP_URL`. Four lines naming one host are four cameras only
if each names a different channel; four byte-identical URLs are one feed copied
four times.

**The two kinds of value are encoded differently, on purpose.**

- The four **detect** URLs take the password **raw**. Frigate 0.18 encodes it for
  you (`escape_special_characters`), so a pre-encoded value is encoded twice —
  `@` becomes `%40` and then `%2540` — and the camera answers 401.
- The four **main** URLs take the password **URL-encoded**. go2rtc only
  substitutes the value into an RTSP source and applies no encoder, so a raw
  password containing `@ # / : ?` truncates or misparses the URL it builds.
  Encode it yourself: `@` → `%40`, `:` → `%3A`, `/` → `%2F`, `#` → `%23`,
  `?` → `%3F`, and a literal `%` → `%25`.

Quote either value if it contains `#` or spaces, or `.env` will treat the rest of
the line as a comment. Encoding is also needed for URLs you open yourself in
VLC, ffprobe, or curl, which apply no encoder of their own.

A camera you cannot reach does **not** stop Frigate: that feed degrades to a
reconnecting stream while the other three keep detecting, and it comes back on
its own once the camera is reachable again. Use `enabled: false` in
`config/config.yml` only to remove a camera from the suite deliberately.

### Avoiding another lockout

This recorder locks an account out after repeated failed logins — 240 seconds,
`remainLoginTimes` 5 — and it took the whole timer to clear on 2026-10-02.

- **Encode each URL exactly once, by the right rule for its kind.** Detect raw
  for Frigate, main encoded for go2rtc. Double-encoding the detect URL, or
  leaving the main URL raw, both answer 401 — indistinguishable from a wrong
  password.
- **Validate one channel before enabling four.** Probe a single RTSP URL with
  `ffprobe` first, then enable `camera_1`, confirm it in `/api/stats`, and only
  then bring up cameras 2–4. Four simultaneous bad credentials reach the retry
  limit four times faster than one.
- **Stop retrying the moment you see 401.** Frigate reconnects on a timer and
  will burn the remaining attempts on its own. Stop the container instead of
  waiting it out, then verify the credentials by hand before restarting.
- **Leave illegal-login security enabled** on the recorder. Disabling it to make
  a flaky feed connect trades a lockout you can recover from for an account that
  is open to guessing.

Then draw the real zone on each camera in Frigate's own zone editor at
<https://localhost:8971/settings> (**Configuration → Zones and masks**). The
example polygons are the full frame `0,0,1,0,1,1,0,1` so the stack starts
cleanly; until you redraw them, "entered the zone" means "was on camera".

Validate, then start:

```sh
docker compose config -q
docker compose up -d
```

`docker compose config -q` proves Compose can parse `compose.yaml` and that `.env`
resolves. It does **not** validate the Frigate schema inside `config/config.yml`.

Inspect startup and camera errors:

```sh
docker compose logs -f frigate
```

On first startup Frigate creates an admin account and prints its initial password
in the logs. Treat those logs as sensitive.

## The dashboard

Open <http://127.0.0.1:8088>.

What it is: static HTML/CSS/JS served by nginx, plus an allowlisted read-only
view of a handful of Frigate and go2rtc endpoints. Four cards, one per camera,
each playing that camera's **main** channel — 2560×1440 HEVC — through the player
go2rtc already ships, plus a per-camera detector read-out. Below that, the most
recent person events whose entered zones intersect the required zone for the
camera, filterable by camera.

The player is code the browser runs, so it is worth being exact about what
reaches the page: `/video-rtc.js` is the copy bundled inside the Frigate image,
proxied from a fixed upstream path, and the four live paths
(`/live/camera_1` … `/live/camera_4`) are WebSocket upgrades pinned to one named
main stream each with no query string at all. Nothing about a request can aim the
player at a different stream, and go2rtc's own config, stream list and control
WebSocket are not reachable.

What it deliberately does not do:

- It is **unauthenticated**, because it binds to `127.0.0.1` only. Any process or
  person with access to this machine can read it. Extending it beyond loopback —
  a VPN, a tunnel, a reverse proxy — means adding authentication first.
- It never shows Frigate's raw `/api/stats`. That endpoint embeds
  `cpu_usages.cmdline`, which contains the RTSP URLs, credentials included. The
  proxy pins a fixed upstream URI with a fixed key allowlist
  (`cameras.camera_fps`, `cameras.process_fps`, `cameras.detection_enabled`,
  `detectors.inference_speed`, `service.last_updated`) and discards the client's
  query string, so `?keys=cpu_usages` cannot widen it.
- `/api/events` is bounded the same way: a fixed upstream URI (people only,
  snapshot-bearing, 50 rows, all four cameras, all four zones) with the client's
  query string discarded. Narrowing to one camera is done in the browser.
- It exposes no config, no logs, no credentials, no write endpoint, and no
  restart path. `GET` and `HEAD` only; everything else is 405, and every other
  `/api` path is 404. The browser-side code contains **no write path at all** —
  no `POST`, no `DELETE`, no mutation of Frigate state, nothing to sign in to and
  nothing to click that changes anything. The page is a viewer, so the review
  workflow around it is a person's, not the page's.
- Frigate's API port 5000 is never published. nginx reaches it on the internal
  Docker network as `frigate:5000`, and go2rtc as `frigate:1984`.

Zones are drawn in Frigate's own UI, not here. The dashboard does not link to it:
open `127.0.0.1:8971` directly to draw zones or browse older archives.

Recorder channels versus individual feeds: both are supported. Pointing all four
`FRIGATE_CAMERA_*_RTSP_URL` values at channels 1–4 of one recorder is four
distinct feeds and is what the current configuration describes. Pointing them at
four separate devices is equally fine. What duplicates a view is four values
whose entire URLs are identical.

## Retention and review

Two different lifetimes, and it matters which is which.

- **An ordinary snapshot is kept six hours.** `snapshots.retain.default: 0.25` is
  in days. Frigate's periodic cleanup — about every five minutes — is what
  actually deletes it, so six hours is a **nominal cutoff, not an exact
  ceiling**: a file goes at the first cleanup after it passes 0.25 days.
- **A `person` event that entered its own camera's zone is kept until you delete
  it.** The `retention` service (`scripts/retain.py`) polls Frigate every 30
  seconds and sets Frigate's own `retain_indefinitely` flag on those events.
  Periodic cleanup skips a retained event, so it survives on its own, with no
  local state: a restart neither re-pins nor forgets. The worker publishes no
  port, opens no database of its own, holds no credential, and reads nothing but
  the internal API. Both lifetimes are Frigate's own bookkeeping; go2rtc
  restreams the live view and holds nothing.

Because the example zones are the whole frame, "entered the zone" currently
means "was on camera", so **most saved person snapshots end up retained**. They
are not rationed to six hours; they accumulate until somebody reviews and deletes
them. That is the intended trade — evidence is not thrown away unread — and it
means disk growth is bounded by review, not by a timer.

**Reviewing.** The dashboard lists the most recent 50 rows across the four
cameras; each retained row carries a *Kept for review* badge. Anything older is
in Frigate's own **Explore** view, which is where the archive is browsed and where
anything older than the dashboard's window is read. While the worker is running,
`docker compose logs -f retention` reports one pass per 30 seconds with pinned,
already-retained and skipped counts — that line is the only sign it is alive.

**Deleting, after you have looked at it.** There is no delete button anywhere, by
design. One named id, typed by a person, inside the worker container:

```sh
docker compose exec retention python3 /app/retain.py --delete-reviewed EVENT_ID
```

`EVENT_ID` is Frigate's tracker id, the `<timestamp>-<6 lowercase
alphanumerics>` string in the row's snapshot URL on the dashboard, or the one
Frigate's own Explore view shows for that event. The call is **irreversible**: it
deletes the event and its saved image, and there is no undo and no trash. Run it
deliberately, after review; an agent or a script must not run it for you. An id
that is not in that native form is refused before anything is sent.

What this does **not** protect: if the worker is stopped for longer than the
six-hour window, the events it would have pinned in that time can be cleaned up
before it sees them. Anything it did pin keeps growing until it is deleted by
hand. There is no cap, and no automatic expiry — that is the point of keeping
evidence.

**Not implemented, and not implied anywhere:** any behaviour, anomaly or
classification detection beyond "a person in a named zone". There is no such
model in this repository.

## Smoke check

One script, standard library only, against a running stack:

```sh
python3 scripts/check.py
python3 scripts/check.py --base http://127.0.0.1:8088   # or $ZONE_CONSOLE_URL
```

It exercises the proxy trust boundary over HTTP: the UI and its security
headers, a list of paths that must be denied including go2rtc's own, write
methods that must be refused, the pinned stats shape against override attempts,
the bundled player module and the absence of any credential-bearing `rtsp://`
URL in what is served, real event snapshots, and that settled zone snapshots
arrive flagged kept. With Frigate down it **fails**, rather than passing a skipped
check: an unreachable camera service is not a passed validation.

It is a transport check. It never opens a WebSocket, so it **cannot** prove a
frame was decoded — the live video result is a browser observation recorded in
[`docs/validation.md`](docs/validation.md), not something this script reports.
Retention checks are covered separately, against a fake Frigate, by
`python3 scripts/check_retention.py`, which passes every assertion; it says
nothing about the live stack, where only the worker's pinning has been observed.

There is no CI workflow in this repository.

## Documentation

- [`docs/spec.md`](docs/spec.md) — the phased plan, and what is implemented.
- [`docs/validation.md`](docs/validation.md) — what was actually run, and the
  manual checklist including UI screenshot checks.

## Run notes

Tooling facts about how this repository was written, kept because the repository
should not imply more process than it has.

- Written with opencode 1.18.33, model `opencode/space-bunny-free`.
- Codex plans and reviews; OpenCode Space Bunny Free writes the files.
- The original OpenCode database failed with `no such column project_id`. A
  separate `XDG_DATA_HOME=/tmp/surveillance-opencode-data` allowed a successful
  writing run.
- `nginx:1.28.0-alpine` was pulled successfully on the review host. A Docker Hub
  tag listing queried during implementation did not show `1.28.0-alpine` among the
  published `1.28.x` tags, so the pin is kept on the review host's successful pull
  and is worth re-confirming on any other host before relying on it.

## Official Frigate references

- <https://docs.frigate.video/frigate/installation/>
- <https://docs.frigate.video/guides/getting_started/>
- <https://docs.frigate.video/configuration/zones/>
- <https://docs.frigate.video/configuration/notifications/>