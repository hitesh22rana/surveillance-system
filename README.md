# Zone Console

A four-camera Frigate prototype with a small read-only dashboard. The one thing
it does: detect a person loitering in a named per-camera zone, then snapshot it.
Email and every other outbound notification is **not** wired up. Nothing leaves
the host.

- Frigate pinned to **0.18.0** (`ghcr.io/blakeblackshear/frigate:0.18.0`).
- Four independent cameras, `camera_1` … `camera_4`, each with its own zone
  `camera_1_area` … `camera_4_area`.
- Detection input is **352×288 at 3 fps per camera** (the HEVC substream from
  recorder channels 1–4). That is a starting point chosen to keep a CPU-only
  four-camera suite within reach, and on 2026-10-02 it did run at 3.0–3.1 fps on
  all four — but it has not been shown to be the right rate for detection
  quality. Tune `detect.width` / `detect.height` / `detect.fps` in
  `config/config.yml`. **Not** `model.width` / `model.height`: those must stay
  **300×300** to match the bundled SSDLite MobileNetV2 input tensor, and frames
  are resized into it.
- Detector: the bundled SSDLite MobileNetV2 on OpenVINO, CPU, `person` only. No
  custom training or inference code.
- Continuous recording disabled (`record.enabled: false`). Snapshots retained
  three days.
- Two loopback ports, nothing else:
  - `127.0.0.1:8971` — Frigate's own authenticated UI.
  - `127.0.0.1:8088` — the dashboard. Read-only, **no login**.
- Target platform is **Linux + Docker**. Docker on macOS gives no guaranteed GPU
  acceleration, so detection latency measured there is not representative.

## Status

| Area | State |
| --- | --- |
| Four-camera compose + example config | implemented |
| nginx read-only proxy with pinned stats keys and bounded events | implemented |
| Dashboard UI (previews, per-camera status, zone events) | implemented |
| Camera authentication | four channels, HEVC 352×288, re-probed 2026-10-02 |
| Runtime camera access | **recovered 2026-10-02** — the recorder lockout expired; all four channels stream |
| Live capture and detector inference | **verified 2026-10-02** — 3.0–3.1 fps in, detection enabled; 9.45 ms inference is exploratory only |
| `scripts/check.py` smoke check | last run passed the proxy boundary checks, then reported `FAILED` at `camera_1` (`fps 0`) while captures were paused; that run predates the recovery, and no full pass is claimed |
| Zones drawn on real boundaries | **not done** — every zone is the full frame |
| Zone walkthrough, alert timing, night, false positives | **not run**, **not verified** |
| Email notifications | **not started**, still a later phase |

What was actually run, and what it does not show:

- `docker compose config` parses, and both containers start. Cameras were
  temporarily disabled during the lockout to avoid a login-retry loop while
  access was 401; they are enabled again.
- `nginx -t` on the dashboard config passes; `node --check dashboard/app.js`
  passes (syntax only).
- Browser review: desktop and 375 px screenshots, and no horizontal overflow at
  320, 414, or 768 px. The `Camera1` and `All` chips change the filter label and
  selection; no events were available, so the filtering itself is not
  evidenced.
- Root security headers, the six denied routes, and `POST`/`DELETE` on
  `/api/stats` all hold, and the stats history key allowlist holds both plain and
  against an override attempt, with no credential leaks. This is the historical
  proxy validation, from while the dashboard was running.
- The last `scripts/check.py` run passed all of the above, then reported
  `FAILED` at `camera_1` (`fps 0`) while captures were paused. That run predates
  the recovery below and is not a current result. The dashboard is intentionally
  stopped, so no full smoke pass is claimed.
- With Frigate stopped, the UI showed "Camera service unreachable" and "Recent
  detections unavailable (HTTP 502)" instead of an empty event list, and the
  denied paths still refused. Frigate was restarted afterwards with captures
  still disabled; captures are enabled again now.
- **Recovery, 2026-10-02.** After VLC was closed and both containers were
  stopped, no login was attempted until past the recorder's 240-second
  `remainLockSecond`. The recorder's native Web3 login then **succeeded with
  unchanged credentials**, `getBlockUserInfoList` returned `0`, and the
  diagnostic session was logged out. `ffprobe` then succeeded sequentially on all
  four channels, each HEVC 352×288. Recovery on the same credentials confirms
  the lockout — not a credential problem — was what blocked access. The trigger
  is not established: Frigate's earlier double-encoded-password retries are a
  likely trigger, but recorder audit logs were not examined, so exact trigger is
  not proven.
- **Four cameras live in Frigate, 2026-10-02.** The git-ignored `config/config.yml`
  was restored from `config/config.example.yml` and **only Frigate** was started,
  with raw passwords in the RTSP URLs. Live stats: all four `camera_fps` 3.0–3.1,
  `process_fps` 3.0–3.1, `detection_enabled` true. The four `latest.jpg`
  responses were `200` with valid JPEGs — latest-frame previews, **not** saved
  detection snapshots. Two minutes of fresh Frigate logs contain **no 401
  entries**.
- **Inference latency is exploratory.** OpenVINO reported `inference_speed`
  9.45 ms in a snapshot: one sample on macOS Docker, not a benchmark and not the
  accepted figure.
- **Dashboard intentionally stopped**, at the user's request. It is the only
  service not running.

The suite **is** streaming and the detector **is** running, but nothing here shows
it catching a person: no zone walkthrough, alert timing, night, or false-positive
check has been run, and no detection has been observed. The full record is in
[`docs/validation.md`](docs/validation.md).

## Setup

```sh
cp config/config.example.yml config/config.yml
cp .env.example .env
chmod 600 .env
```

Edit `.env` and put the real RTSP URL for **each** camera in
`FRIGATE_CAMERA_1_RTSP_URL` … `FRIGATE_CAMERA_4_RTSP_URL`. Four lines naming one
host are four cameras only if each names a different channel; four byte-identical
URLs are one feed copied four times.

Write the password **raw**. Frigate 0.18 encodes it for you
(`escape_special_characters`), so a pre-encoded value is encoded twice — `@`
becomes `%40` and then `%2540` — and the camera answers 401. Quote the value if
it contains `#` or spaces, or `.env` will treat the rest of the line as a
comment. Encoding is only needed for URLs you open yourself in VLC, ffprobe, or
curl, which apply no encoder of their own.

A camera you cannot reach does **not** stop Frigate: that feed degrades to a
reconnecting stream while the other three keep detecting, and it comes back on
its own once the camera is reachable again. Use `enabled: false` in
`config/config.yml` only to remove a camera from the suite deliberately.

### Avoiding another lockout

This recorder locks an account out after repeated failed logins — 240 seconds,
`remainLoginTimes` 5 — and it took the whole timer to clear on 2026-10-02.

- **Write the password raw and let Frigate encode it.** A pre-encoded value is
  encoded twice and the camera answers 401, which is indistinguishable from a
  wrong password.
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
view of four Frigate endpoints. Four cards, one per camera, each showing the
latest JPEG every two seconds — an **image preview, not live video**. Below that,
person events whose entered zones intersect the required zone for the camera,
filterable by camera.

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
  `/api` path is 404.
- Frigate's API port 5000 is never published. nginx reaches it on the internal
  Docker network as `frigate:5000`.

Zones are drawn, reviewed, and deleted in Frigate's own UI, not here. The
dashboard links to it.

Recorder channels versus individual feeds: both are supported. Pointing all four
`FRIGATE_CAMERA_*_RTSP_URL` values at channels 1–4 of one recorder is four
distinct feeds and is what the current configuration describes. Pointing them at
four separate devices is equally fine. What duplicates a view is four values
whose entire URLs are identical.

## Smoke check

One script, standard library only, against a running stack:

```sh
python3 scripts/check.py
python3 scripts/check.py --base http://127.0.0.1:8088   # or $ZONE_CONSOLE_URL
```

It exercises the proxy trust boundary over HTTP: the UI and its security
headers, a list of paths that must be denied, write methods that must be
refused, the shape of the stats response, and a set of query-string attempts to
widen the key allowlist. With Frigate down it **fails**, rather than passing a
skipped check: an unreachable camera service is not a passed validation.

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