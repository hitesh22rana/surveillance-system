# Surveillance Console

A four-camera Frigate prototype with a small dashboard. The one thing it does:
detect a person **loitering** — 60 seconds inside a named per-camera zone — and
snapshot it. Email and every other outbound notification is **not** wired up.
Nothing leaves the host. The dashboard reads everything it shows, and with one
explicitly authorised exception it can **delete a reviewed snapshot on request**;
everything else about it is read-only. See
[Controlled deletion](#controlled-deletion).

The live view is the camera's own main channel — 2560×1440 HEVC, the source's
reported 25 fps, handed to the browser without re-encoding. That 25 fps is what
the channel reports, not a render rate measured in a browser. Detection runs
separately, on its own low substream at 3 fps.

- Frigate pinned to **0.18.0** (`ghcr.io/blakeblackshear/frigate:0.18.0`).
- Four independent cameras, `camera_1` … `camera_4`, each with **two** zones on
  the same polygon: `camera_N_area` (loitering_time 0 — somebody is on camera) and
  `camera_N_loitering` (loitering_time 60 — tracked in-zone presence reaching a
  minute). Alerts fire on the loitering zone; snapshots are saved on the area
  zone.
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
  (~4 MP) live view** — the two qualities are unrelated and the event image is the
  small one. Neither the 352×288/3 fps detection source nor the 25 fps main
  channel changed with the loitering rule or the deletion endpoint.
- Detector: the bundled SSDLite MobileNetV2 on OpenVINO, CPU, `person` only. No
  custom training or inference code, and no behaviour, anomaly or classification
  model of any kind: a person in a named zone is the only rule that exists.
- Continuous recording disabled (`record.enabled: false`).
- **Snapshots.** An ordinary snapshot is kept **6 hours**
  (`snapshots.retain.default: 0.25`, in days). Frigate's periodic cleanup runs
  about every five minutes and is what actually deletes it, so six hours is the
  nominal cutoff it is measured against, not an exact deletion time. A `person`
  event that loitered **60 seconds in its own camera's loitering zone** is pinned
  by the retention worker through Frigate's native `retain_indefinitely`, which
  cleanup does not touch, so it survives until somebody deletes it. A person who
  merely walked through is **not** pinned: that event still gets its ordinary
  six hours, which is a behaviour change from the previous whole-frame zone. See
  [Retention and review](#retention-and-review).
- **What the 60 seconds counts.** Frigate's loitering counter advances on tracked
  frames with the object inside the zone, and detection runs at 3 fps, so 180
  frames is 60 seconds. That is **tracked presence, not continuous wall-clock
  time**: separate visits by the same tracked object can accumulate toward the
  threshold, and nothing here establishes that it stayed still, stayed in one
  place, or meant anything by it.
- Two loopback ports, nothing else:
  - `127.0.0.1:8971` — Frigate's own authenticated UI.
  - `127.0.0.1:8088` — the dashboard. No login, and now one authorised write
    path: controlled manual deletion of reviewed snapshots.
- The `retention` container publishes **no port**. Its deletion gateway listens
  on `0.0.0.0:8090` on the Docker network only, and refuses any request whose
  `Host` is not the dashboard's own published address.
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
| `scripts/check.py` smoke check | **PASS 2026-10-02, expanded version** — all assertions hold: 11 forbidden paths refused, `POST`/`DELETE` refused on `/api/stats` and `/live/camera_1`, `media-src 'self' blob:`, player module 200, no credentials in the three served assets, stats held plain and against an override, all four detecting, a 50-event list with native ids plus one snapshot JPEG 200 (all 50 listed snapshot URLs separately checked at root: 200 + JPEG, across three cameras), all 48 settled zone events flagged kept. A later live pass against the deployed gateway held too: `camera_4` with no events answered one valid empty page, and raw config, raw logs, `cpu_usages` and a credential-bearing query were all refused |
| `scripts/check_retention.py` boundary check | **PASS 2026-10-02** against a disposable fake Frigate, under the two-zone rule and under the previous whole-frame rule |
| Retention worker pinning kept snapshots | **verified live 2026-10-02, historical, under the old whole-frame rule** — first pass 49 pinned + 27 already retained = 76 events; a later `--once` found all 76 already retained, exit 0 |
| `scripts/check_console_api.py` HTTP contract check | **PASS 2026-10-02** against a disposable fake Frigate: a 60-row archive across three pages of 25, invalid `Origin`/`Host`/content type/ids, a partial failure and a native `success: false`, the fail-closed page cap, and no automatic `DELETE` |
| Static checks on the deployed files | **PASS 2026-10-02** — `docker compose config -q`, the Python compile of both check scripts and `retain.py`, `node --check dashboard/app.js`, and `nginx -t` |
| Deployed deletion gateway | **deployed 2026-10-02** — `retain.py --serve` bound on internal `0.0.0.0:8090` with **no published port**; nginx routes the three exact paths `summary`, `events` and `delete` |
| 60-second loitering zones, alerts gated on them | **loaded and confirmed 2026-10-02** — native `/api/config` reports all four `camera_N_area` at 0 s, all four `camera_N_loitering` at 60 s, and `record.enabled` false on all four. Behaviour is still **unmeasured**: no controlled 60-second walkthrough |
| Live loitering protection | **one event observed 2026-10-02** — a naturally completed loitering snapshot is pinned, and three new ordinary snapshots are not. That is one event, not a rate |
| Controlled deletion through the dashboard | **done once, 2026-10-02, with the owner's explicit authorisation** — one ended experimental snapshot deleted via the dashboard `POST` (`deleted: 1`, `missing: 0`, `failed: []`, `active: 0`), its image URL then 404, no archive wipe, and the Cancel confirmation observed doing nothing |
| Archive pagination source | **API live 2026-10-02** — `GET /api/snapshots/events` returns the archive over 25-row pages with `total`/`pages`; over 100 snapshots across 5 pages were read, including rows older than the legacy 50 |
| Dashboard pager | **observed in a browser 2026-10-02** — the page reads `GET /api/snapshots/events` at 25 rows per page and drives `page`/`camera` from its own controls, including a latest-page jump and a clamp to the last page; the measured behaviour is the final browser validation below |
| Final browser validation | **observed 2026-10-02** — the archive read **133 rows over 6 pages at 25**; one checkbox selected **1**, and **Next** carried that **1 off-page**; **Select all shown** gave **26 including the 1 held off-page**, and the selected-delete dialog correctly named **26**; **Cancel** and **Clear** both worked; jumping to the last page (**6**) showed the oldest rows, **126** and up; **Latest** returned to page 1; the `camera_4` filter returned an empty page 1; the archive-delete dialog counted **131 across all pages** and was then cancelled; no horizontal overflow at **320 px** or **1280 px**, desktop rows **78 px**, and all four players held **2560×1440** throughout. **No destructive button was clicked in this pass** — the one deleted snapshot came earlier from the terminal API under the owner's authorisation |
| Ordinary six-hour expiry, watched end to end | **not done** — applied and confirmed loaded in native `/api/config`; nobody waited the window out |
| Snapshots already kept under the old rule | **kept, and still a manual job** — nothing unpins them; only newly captured ordinary snapshots are on the six-hour timer |
| Zones drawn on real boundaries | **not done** — every zone is the full frame |
| Controlled zone walkthrough, alert timing, night, false positives | **not run**, **not verified** |
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
  which rows each chip keeps is still not evidenced. The delete confirmation's
  **Cancel** was exercised and cancelled. The pager wired to the 25-row source was
  reviewed in a browser on 2026-10-02; the measured result is the **Final browser
  validation** row of the Status table.
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
  the   next pass, the qualification rules, a page cap that fails the pass instead
  of reporting one it did not run, no automatic `DELETE`, and non-native manual
  ids refused before anything is sent. It passed again on 2026-10-02 under the
  two-zone rule. Both are fake-Frigate results: they say nothing about the live
  stack.
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
- **The 60-second rule and the deletion gateway, 2026-10-02.** Both are now
  executed, not just written. `scripts/check_retention.py` and
  `scripts/check_console_api.py` **both PASS** against a disposable fake upstream;
  the contract check covers a 60-row archive across three pages of 25, invalid
  `Origin`, `Host`, content type and ids, a partial failure and a native
  `success: false`, the fail-closed page cap, and **no automatic `DELETE`**.
  `docker compose config -q`, the Python compile of both scripts and `retain.py`,
  `node --check dashboard/app.js` and `nginx -t` all pass. The gateway is
  **deployed** on internal `0.0.0.0:8090` with **no published port**, and nginx
  carries the three exact routes `summary`, `events` and `delete`. `scripts/check.py`
  then **passed live** against that deployed gateway.
- **The zones are loaded, and one qualifying event was seen.** Native `/api/config`
  reports all four `camera_N_area` at `loitering_time: 0`, all four
  `camera_N_loitering` at `60`, and `record.enabled: false`, with detection at
  3 FPS. One naturally completed loitering snapshot is **pinned**, and three new
  ordinary snapshots are **not**. That is a single observation per direction, not
  a measured rate, and **no controlled 60-second walkthrough was performed**, so
  no timing or accuracy claim rests on it.
- **One deletion, performed deliberately, 2026-10-02.** With the owner's explicit
  authorisation, one **ended experimental** snapshot was deleted from the
  dashboard: the reply was `deleted: 1`, `missing: 0`, `failed: []`, `active: 0`,
  the image URL then answered **404**, and no archive scope was used, so nothing
  else moved. It is irreversible and there is no undo.

The suite **is** streaming live video, the detector **is** running, and the
snapshot path **does** return real saved detections — but nothing here shows it
catching a person on request: no controlled zone walkthrough, alert timing,
night, or false-positive check has been run. One snapshot **has** now been
deleted, deliberately and irreversibly. The full record is in
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

Then draw the real zones on each camera in Frigate's own zone editor at
<https://localhost:8971/settings> (**Configuration → Zones and masks**). There are
two per camera: draw `camera_N_loitering` around the place worth watching, and
keep `camera_N_area` identical to it so the snapshot you keep matches the one the
alert is about. The example polygons are the full frame `0,0,1,0,1,1,0,1` so the
stack starts cleanly; until you redraw them, "loitered" means "was on camera for a
minute", which is nobody's idea of a threshold in a real scene.

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

What it is: static HTML/CSS/JS served by nginx, plus an allowlisted view of a
handful of Frigate and go2rtc endpoints — every one of them a read, except the
one controlled deletion described below. Four cards, one per camera, each playing
that camera's **main** channel — 2560×1440 HEVC — through the player go2rtc
already ships, plus a per-camera detector read-out. Below that, the most recent
person events whose entered zones intersect the area zone for the camera,
filterable by camera, with the retained ones badged and selectable for review.

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
  snapshot-bearing, 50 rows, all four cameras, all four area zones) with the
  client's query string discarded. That **50 is the shape of this legacy
  endpoint, not the size of the archive**. The dashboard reads the paginated
  source below instead, so `/api/events` is now kept only for compatibility;
  narrowing by camera happens in the gateway, before the page is cut, rather than
  in the browser.
- It exposes no config, no logs, no credentials, no restart path, and no write
  endpoint other than the one controlled deletion call described below. `GET` and
  `HEAD` on the reads; everything else is refused, and every other `/api` path is
  404. The browser-side code contains exactly one write, the reviewed-deletion
  `POST`, and nothing to sign in to. The page is a viewer plus a delete button a
  person has to aim, so the review workflow around it stays a person's decision.
- Frigate's API port 5000 is never published. nginx reaches it on the internal
  Docker network as `frigate:5000`, and go2rtc as `frigate:1984`. The retention
  container's deletion gateway is reached as `retention:8090`, on the same
  internal network, with no published port of its own.

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
- **A `person` event that loitered 60 seconds in its own camera's loitering zone
  is kept until you delete it.** The `retention` service (`scripts/retain.py`)
  polls Frigate every 30 seconds and sets Frigate's own `retain_indefinitely` flag
  on exactly those events. Periodic cleanup skips a retained event, so it survives
  on its own, with no local state: a restart neither re-pins nor forgets. The
  worker publishes no port, opens no database of its own, holds no credential, and
  reads nothing but the internal API. Both lifetimes are Frigate's own
  bookkeeping; go2rtc restreams the live view and holds nothing.

The two zones per camera are what make that distinction possible.
`camera_N_area` is the whole frame with `loitering_time: 0`, so **every** person
event still gets a snapshot and still gets its ordinary six hours.
`camera_N_loitering` is the same polygon with `loitering_time: 60`, and only
events that carried it are pinned. **This is a behaviour change**: under the
previous single full-frame zone, essentially every person snapshot was kept
forever, and a walk-through is now worth only six hours.

What that does **not** do is clean up the old pile. Events already pinned under
the previous rule keep their `retain_indefinitely` flag — the worker only ever
sets it, never clears it, and there is no migration — so **previously kept
snapshots stay until somebody deletes them by hand.** Only newly captured ordinary
snapshots are on the six-hour timer.

The example loitering zones are still the whole frame, so "loitered" currently
means "somebody was on camera for a minute": waiting for a door, a delivery, a
phone call, or a gate. That is a **geometry prototype, not a judgement about
danger.** 60 seconds is a starting threshold for "worth a human look", long
enough to exclude a passing step and short enough to see inside one shift. It
assesses no risk, infers no intent, and models nothing; narrowing the polygons to
the places that matter is the next piece of work, and no claim of accuracy rests
on the current ones.

**Reviewing.** The dashboard reads the archive through the paginated source,
**25 rows at a time** across the four cameras; each retained row carries a *Kept
for review* badge. On 2026-10-02 that read reported **over 100 snapshots across 5
pages**, so the pager is what reaches rows older than the legacy 50. Page,
camera filter and a jump back to the newest page are part of the page itself, the
page count comes from the source rather than being counted here, and a page past
the last lands on the last rather than erroring. The poll re-reads only the
newest page, so a page being read through history does not shift under the
operator. **Browser review of the pager was completed 2026-10-02** and its
measured results are the **Final browser validation** row of the Status table;
anything further back is in Frigate's own **Explore** view.
While the worker is running,
`docker compose logs -f retention` reports one pass per 30 seconds with pinned,
already-retained and skipped counts — that line is the only sign it is alive.

**Deleting, after you have looked at it.** Two ways, and the second one is the
operator's escape hatch for when the gateway is not running:

```sh
docker compose exec retention python3 /app/retain.py --delete-reviewed EVENT_ID
```

`EVENT_ID` is Frigate's tracker id, the `<timestamp>-<6 lowercase
alphanumerics>` string in the row's snapshot URL on the dashboard, or the one
Frigate's own Explore view shows for that event. The call is **irreversible**: it
deletes the event and its saved image, and there is no undo and no trash. Run it
deliberately, after review, and only under an explicit human authorisation. On
2026-10-02 the owner authorised one such deletion; it was carried out through the
dashboard `POST` below, and **this CLI path has still not been run** against a real
event. An id that is not in that native form is refused before anything is sent.

What retention does **not** protect: if the worker is stopped for longer than the
six-hour window, the events it would have pinned in that time can be cleaned up
before it sees them. Anything it did pin keeps growing until it is deleted by
hand. There is no cap, and no automatic expiry — that is the point of keeping
evidence.

**Not implemented, and not implied anywhere:** any behaviour, anomaly or
classification detection beyond "a person loitering in a named zone". The 60
second threshold is a timer, not a model, and it makes no claim about what
anybody is doing. There is no such model in this repository.

## Controlled deletion

The one write path in the project, added on the owner's explicit instruction. It
replaces the previous "there is no delete button anywhere, by design" position:
deleting a reviewed snapshot is now possible from the page, and **still only
after a person decides to.**

It is not a new service. The `retention` container runs `retain.py --serve`,
which runs the existing 30-second poll loop and a small stdlib HTTP server in one
process, sharing **one lock** so a poll pass and a manual deletion can never
interleave. No new image, no framework, no dependency, no new port published, no
`env_file`, and no storage mount — Frigate's own API does the deleting.

**Three exact paths — two reads and one write — and nothing else answers:**

- `GET /api/snapshots/summary` → `{"count": 812, "before": 1790928400}`.
  `count` is every person snapshot from the four configured cameras in the whole
  archive, **older than `before`, not just the newest 50**; `before` is that whole
  second, so the number on screen is exactly the number a deletion reaches.
- `GET /api/snapshots/events`, the paginated read, taking **optional** `page` and
  `camera` and nothing else. Both are strictly validated before any upstream
  request: a repeated, unknown or blank parameter, a page that is not a positive
  whole number, or a camera that is not `all` or one of the four configured names
  is refused. It answers
  `{"events": [...], "page": 2, "page_size": 25, "total": 118, "pages": 5}` —
  **25 rows per page**, newest first, the camera filter applied **before** the
  page is cut, so `total` and `pages` always describe the filtered set. A page
  past the last is clamped to the last rather than refused, so a page that is one
  behind after deletions still gets an answer. The read walks the archive to count
  it, so it is bounded by the same page cap as everything else here and answers
  503 rather than a short page that reads like a whole one.
- `POST /api/snapshots/delete`, which takes **either** `{"ids": ["<native id>",
  ...]}` (1 to 1000, duplicates folded) **or** `{"all": true, "before":
  <summary cutoff>}`, and answers `{"deleted": 12, "missing": 0, "failed": [],
  "active": 1}`.

`all` walks the archive first and deletes nothing at all if the walk cannot reach
the end of the list, so a bounded run can never become a partial one. `before` is
applied locally, so the newest events are excluded by this code rather than by an
upstream bound. The page offers the two scopes deliberately: **delete the shown
rows** (the ones selected in the list) or **delete the whole archive up to the
count**, each behind an explicit confirmation that says how many, names the
irreversibility, and is not the default.

Every request must carry all of: `Host` exactly `localhost:8088` or
`127.0.0.1:8088`; for the POST, `Origin` exactly `http://` that Host,
`Content-Type: application/json`, and `X-Surveillance-Action: delete`. A body over
64 KiB, a non-native id, a traversal, a future timestamp, an ambiguous body with
both modes, or an unknown key is refused **before any upstream request**. No CORS
header is ever sent, so no other page can read a reply. Nothing in the request
path or query is forwarded: each upstream call is built from literals and validated
ids. Each selected id is looked up on Frigate and checked for a configured camera,
a snapshot and the `person` label **before** its `DELETE` is sent; a 404 counts as
already gone, another camera's event is refused, and a native 2xx carrying
`success: false` is never counted as a deletion. Errors are redacted JSON — never
a Frigate body, URL, config, log or password.

What the `Host` and `Origin` guards are **for**: they stop another site in a
browser from driving this delete, because a page on another origin cannot address
this one or set an `Origin` that matches. They are **not** a defence against a
malicious process on this machine, which can write any header it wants and is
already inside the loopback trust boundary. What actually stands between a header
mistake and lost evidence is validation before any upstream request, and the
confirmation in front of it.

`active` counts selected events Frigate has not ended yet. Those detections can
write a new snapshot afterwards; that is reported, not prevented, and nothing here
restarts a camera or keeps deleting in a loop.

Two things this does **not** change: the poll loop still contains no `DELETE` and
never will — deletion happens only through an explicit HTTP `POST` or the
operator's CLI — and every other dashboard endpoint, along with Frigate's config,
logs, exports and restart paths, stays denied. The gateway is on the internal
Docker network only, so its port is not an entry point from the host; a request
that does not name the dashboard's own address is refused by the gateway itself.

**Run, and used once.** `scripts/check_console_api.py` **passes** (2026-10-02)
against a disposable fake Frigate, and the deployed gateway was then exercised
live: `scripts/check.py` passed with it running, and one **ended experimental**
snapshot was deleted from the dashboard with the owner's explicit authorisation —
`deleted: 1`, `missing: 0`, `failed: []`, `active: 0`, its image URL 404
afterwards, no archive scope. The Cancel path was exercised too and cancelled. See
[`docs/validation.md`](docs/validation.md).

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
The retention worker's own decisions are covered separately, against a fake
Frigate, by two scripts that need no stack, no camera and no network:

```sh
python3 scripts/check_retention.py   # the poll: which events are pinned, and that nothing is deleted
python3 scripts/check_console_api.py # the --serve gateway: refusals, selection, fail-closed walks
```

Both run the real `scripts/retain.py` as a subprocess and read only what left the
process and what reached the fake upstream. **Both pass as of 2026-10-02** —
`check_retention.py` under the two-zone rule as well as the previous whole-frame
one, and `check_console_api.py` against a 60-row archive over three pages of 25.
Neither says anything about the live stack; that evidence is recorded separately in
[`docs/validation.md`](docs/validation.md).

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