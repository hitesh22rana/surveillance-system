# Surveillance Console

A four-camera Frigate prototype with a small dashboard. What it does now: save a
snapshot of every person and every watched animal that comes into frame, and keep
extra ones **for review** - on Camera 4's area_4 area always, and on Cameras 1–3
while a person has armed them by hand. Email and every other outbound notification
is **not** wired up. Nothing leaves the host. The dashboard reads everything it
shows, and with two explicitly authorised exceptions it can **delete a reviewed
snapshot on request** and **arm or disarm person review**; everything else about it
is read-only. See [Controlled deletion](#controlled-deletion) and
[Entry monitoring](#entry-monitoring).

The live view is the camera's own main channel - 2560×1440 HEVC, the source's
reported 25 fps, handed to the browser without re-encoding. That 25 fps is what
the channel reports, not a render rate measured in a browser. Detection runs
separately, on its own low substream at 3 fps.

- Frigate pinned to **0.18.0** (`ghcr.io/blakeblackshear/frigate:0.18.0`).
- Four independent cameras, `camera_1` … `camera_4`, each with **one** zone on the
  full normalised frame: `camera_N_area`, `loitering_time: 0`, so somebody on
  camera is somebody snapshotted. This is the snapshot trigger, not a rule.
- **Native person alerts are off on all four cameras** (`review.alerts.enabled:
  false`). Retaining a person is now a decision a person makes on the dashboard,
  not something Frigate fires. Alerting on every person on every frame was never
  the point.
- **`camera_1_restricted` and `camera_4_restricted`** are the two areas entry
  review is armed for. They are **not** in `config/config.example.yml`: the
  polygons are deployment-specific and have been drawn and validated directly in the
  running instance, and a generic full-frame placeholder in a public example
  could end up armed. One commented example, and what arming does, are in
  [`docs/restricted-zone.md`](docs/restricted-zone.md). Cameras 2 and 3 need no
  second zone: arming them watches the full-frame `camera_N_area` they already
  have, so it keeps anyone in view.
- **The 60-second full-frame person rule is gone, on every camera.** Under it any
  tracked person on any camera for a minute was kept forever, which with
  full-frame zones meant essentially every walk-through. Nothing pins a person
  now unless that camera's person review is armed. **Records kept under the old
  rule are left exactly as they are** - the worker only ever sets
  `retain_indefinitely`, and it has no un-pin path and no migration, so the old
  pile stays until somebody deletes it by hand.
- **Animals, `camera_1` … `camera_3` only.** Those three cameras also track the ten
  COCO 91 animal classes - `bird` `cat` `dog` `horse` `sheep` `cow` `elephant`
  `bear` `zebra` `giraffe` - and those labels are in `camera_N_area`, which is
  already `loitering_time: 0`, so an animal is snapshotted the moment it is in
  frame, and the retention worker pins it on sight. That rule is **unchanged** and
  needs no arming. `camera_4` tracks `person` and nothing else, and the worker
  refuses an animal event on `camera_4` on its own, without consulting Frigate.
  See [Animals](#animals).
- Live view: each camera's **main** channel, restreamed by the go2rtc bundled in
  the Frigate image and served over four fixed WebSocket paths, one per camera.
  Full resolution, no re-encode, no silent downgrade - a browser that cannot
  decode HEVC is told so instead of being handed a lesser stream.
- Detection input is **352×288 at 3 fps per camera** (the low channel, recorder
  channels 1–4). That is a starting point chosen to keep a CPU-only four-camera
  suite within reach, and on 2026-10-02 it did run at 3.0–3.1 fps on all four -
  but it has not been shown to be the right rate for detection quality. Tune
  `detect.width` / `detect.height` / `detect.fps` in `config/config.yml`. **Not**
  `model.width` / `model.height`: those must stay **300×300** to match the
  bundled SSDLite MobileNetV2 input tensor, and frames are resized into it.
  **A saved snapshot is cut from this 352×288 substream, not from the 2560×1440
  (~4 MP) live view** - the two qualities are unrelated and the event image is the
  small one. Neither the 352×288/3 fps detection source nor the 25 fps main
  channel changed with the entry-review rule, the arming control or the deletion
  endpoint.
- Detector: the bundled SSDLite MobileNetV2 on OpenVINO, CPU, tracking `person` on
  all four cameras plus the ten COCO 91 animal classes on `camera_1` … `camera_3`.
  No custom training or inference code for the object detector, and no behaviour,
  anomaly or classification model built on it: a person entering an **armed**
  camera's own restricted area, or an animal on one of the first three cameras, are
  the rules that exist on top of it. A separate, explicitly experimental video model
  exists for one narrow question; see
  [Possible fighting](#possible-fighting-experimental).
- Continuous recording disabled (`record.enabled: false`).
- **Snapshots.** An ordinary snapshot is kept **6 hours**
  (`snapshots.retain.default: 0.25`, in days). Frigate's periodic cleanup runs
  about every five minutes and is what actually deletes it, so six hours is the
  nominal cutoff it is measured against, not an exact deletion time. A `person`
  event that entered an **armed** camera's own restricted area is pinned by the
  retention worker through Frigate's native `retain_indefinitely`, which cleanup
  does not touch, so it survives until somebody deletes it. Everything else about
  a person ages out on the six-hour timer. See
  [Retention and review](#retention-and-review).
- **What a kept person snapshot says.** While armed, it means one thing: a person
  was inside that camera's watched area during the window you armed - always, on
  Camera 4. It is a **candidate for a person to look at**, and nothing more. It is
  not a direction - a zone says where somebody was, not which side of a boundary
  they came from. It is not an identity - the detector reports one class,
  `person`, and cannot tell family from anyone else. And the object model itself is
  COCO 91, which has no behaviour, anomaly or fighting class: the one place a
  separate video model is used, it is the experimental sidecar below, not the
  detector. On Cameras 2 and 3 the area is the whole frame,
  so arming one keeps every person in view for as long as it stays armed -
  presence, not an incident, and a deliberate limit of what a zone can say.
- Two loopback ports, nothing else:
  - `127.0.0.1:8971` - Frigate's own authenticated UI.
  - `127.0.0.1:8088` - the dashboard. No login, and now **two** authorised write
    paths: controlled manual deletion of reviewed snapshots, and manual arming or
    disarming of person review on Cameras 1–3.
- The `retention` container publishes **no port**. Its gateway listens
  on `0.0.0.0:8090` on the Docker network only, serves both writes, and refuses
  any request whose `Host` is not the dashboard's own published address. Its root
  filesystem stays read-only; the one writable path is the named volume holding
  the operator's arming.
- The `fighting` container publishes **no port** either. nginx reaches it as
  `fighting:8091` on the same internal network, on one exact read-only path,
  `GET /api/activity/fighting`. Its model weights are provisioned into a mounted
  directory from **outside this repository** and are never downloaded at runtime.
- Target platform is **Linux + Docker**. Docker on macOS gives no guaranteed GPU
  acceleration, so detection latency measured there is not representative.

## Status

| Area | State |
| --- | --- |
| Four-camera compose + example config | implemented |
| nginx read-only proxy with pinned stats keys, pinned live paths and bounded events | implemented |
| Dashboard UI (live video, per-camera status, zone events) | implemented |
| Camera authentication | four low channels and four main channels, re-probed 2026-10-02 |
| Runtime camera access | **recovered 2026-10-02** - the recorder lockout expired; all four channels stream |
| Live capture and detector inference | **verified 2026-10-02** - 3.0–3.1 fps in, detection enabled; 9.45 ms inference is exploratory only |
| Live main-stream video in a browser | **verified 2026-10-02, re-verified after the Frigate restart** - four players, 2560×1440 HEVC, no errors, detectors 2.9–3.1 fps |
| `scripts/check.py` smoke check | **PASS 2026-10-02, expanded version** - all assertions hold: 11 forbidden paths refused, `POST`/`DELETE` refused on `/api/stats` and `/live/camera_1`, `media-src 'self' blob:`, player module 200, no credentials in the three served assets, stats held plain and against an override, all four detecting, a 50-event list with native ids plus one snapshot JPEG 200 (all 50 listed snapshot URLs separately checked at root: 200 + JPEG, across three cameras), all 48 settled zone events flagged kept. A later live pass against the deployed gateway held too: `camera_4` with no events answered one valid empty page, and raw config, raw logs, `cpu_usages` and a credential-bearing query were all refused |
| `scripts/check_retention.py` boundary check | **PASS 2026-10-02, re-run under the animal rule** - 14 assertions against a disposable fake Frigate, now including the ten animal labels on cameras 1–3 (pinned with no zone), the same labels refused on `camera_4` before any POST, an untracked `car` refused on label, and the listing/deleting gate agreeing with the pin gate in both directions |
| Retention worker pinning kept snapshots | **verified live 2026-10-02, historical, under the old whole-frame rule** - first pass 49 pinned + 27 already retained = 76 events; a later `--once` found all 76 already retained, exit 0 |
| `scripts/check_console_api.py` HTTP contract check | **PASS 2026-10-02, re-run under the animal rule** - against a disposable fake Frigate: a 61-row archive across three pages of 25, invalid `Origin`/`Host`/content type/ids, a partial failure and a native `success: false`, the fail-closed page cap, no automatic `DELETE`, plus the animal rule on both sides: a `dog` on `camera_4` refused before any request, and a `cat` on `camera_3` deleting like any other saved snapshot |
| Static checks on the deployed files | **PASS 2026-10-02** - `docker compose config -q`, the Python compile of both check scripts and `retain.py`, `node --check dashboard/app.js`, and `nginx -t` |
| Deployed deletion gateway | **deployed 2026-10-02** - `retain.py --serve` bound on internal `0.0.0.0:8090` with **no published port**; nginx routes the three exact paths `summary`, `events` and `delete` |
| 60-second full-frame person rule | **removed** - pins nothing on any camera now; records kept under it are left exactly as they are |
| Native person alerts | **disabled on all four cameras** (`review.alerts.enabled: false`) in the public example |
| Restricted areas `camera_1_restricted`, `camera_4_restricted` | **polygons drawn and validated in the running instance; not in the public example**, whose zone is a commented placeholder |
| Manual person review, camera_4 always and cameras 1–3 by hand | **implemented, PASS in the disposable-upstream checks and on the live stack** - camera_4 armed at startup with no control and no state, Home/Away for the other three, one saved file that survives a restart, a failed save that changes nothing, an unreadable file that falls back to Home, an unconfirmed area that pauses without losing the setting, and a confirmation before reducing person review from Away |
| Turning review off from Away | **implemented, PASS in `node scripts/check_entry_ui.mjs` and observed live** - one native dialog in the existing confirm classes, Cancel focused first, Cancel and Escape send nothing and keep Away, Confirm sends one request, and a repeated Away never asks |
| Entry arming on the live stack | **run live 2026-10-02** - a live `POST` arm, a repeat arm and a disarm each returned a valid state and the page displayed each one; the repeat arm changed nothing. **Both cameras were left disarmed**. Historical: this was the two-camera rule, replaced by the row above |
| `scripts/check_retention.py` boundary check | **PASS** - a fresh install is Home and keeps only camera_4's person row, a saved arming is read back by the next process, eight malformed or unreadable files each fall back to Home with camera_4 unaffected, a save that cannot land changes neither the file nor memory, an entry before the arm is never pinned while camera_4 has no such boundary, an unconfirmed area pauses without losing the setting, animals unchanged, nothing deleted |
| `scripts/check_console_api.py` HTTP contract check | **PASS** - 10 refusal shapes and 17 bodies refused on the arming POST including both camera_4 writes, Away arming all three at one moment, an idempotent repeat, Custom from one toggle, Away surviving a process restart, camera_4 immutable through a file that names it, a real blocked write answering 503 with the valid Away file byte-identical, readiness gating and recovery, and the archive/delete scope identical Home and armed |
| `scripts/evaluate.py --self-check` | **PASS** - the four outcomes counted separately, undefined precision/recall printed as undefined, 16 bad values refused with nothing printed |
| **Detection accuracy, every rule, every camera** | **unmeasured - nothing has been run.** Protocol and empty templates in [`docs/evaluation.md`](docs/evaluation.md) |
| Live loitering protection - **historical, under the removed 60-second whole-frame rule** | **one event observed 2026-10-02** - a naturally completed loitering snapshot is pinned, and three new ordinary snapshots are not. That is one event, not a rate. The rule itself no longer exists |
| Controlled deletion through the dashboard | **done once, 2026-10-02, with the owner's explicit authorisation** - one ended experimental snapshot deleted via the dashboard `POST` (`deleted: 1`, `missing: 0`, `failed: []`, `active: 0`), its image URL then 404, no archive wipe, and the Cancel confirmation observed doing nothing |
| Archive pagination source | **API live 2026-10-02** - `GET /api/snapshots/events` returns the archive over 25-row pages with `total`/`pages`; over 100 snapshots across 5 pages were read, including rows older than the legacy 50 |
| Dashboard pager | **observed in a browser 2026-10-02** - the page reads `GET /api/snapshots/events` at 25 rows per page and drives `page`/`camera` from its own controls, including a latest-page jump and a clamp to the last page; the measured behaviour is the final browser validation below |
| Saved detections review filter | **implemented, PASS in `python3 scripts/check_console_api.py`** - against the same disposable fake Frigate, `review=kept` narrows rows, `total` and `pages` before the page is cut, so flagged rows beyond page 1 and the combined camera filter are both covered; the kept set is `retain_indefinitely` alone, so a legacy flagged row and a flagged animal are in and an unflagged person inside its own watched area is out; the four camera totals add up to the all-camera one, a page past the end clamps, and the archive count behind the archive-wide delete is still the whole archive. Eleven unrecognised, repeated, blank or no-value `review` values are refused with 400 before any upstream request, and seven query strings are refused with 400 on the delete path. **Observed live by Codex on 2026-10-03, owner has not reviewed it yet**: 796 all detections against 137 natively retained rows over 6 pages, page 2 / reload / Back working, Camera 2 + kept answering 85 rows over 4 pages, all four players still 2560×1440, and 320 px with no overflow and 44 px review buttons |
| Experimental possible-fighting watch | **implemented, UI lane only, static checks PASS and live run still owed** - `scripts/fighting.py` and its own checks belong to another lane. This lane added `GET /api/activity/fighting` (one exact path, fixed `/status`, no forwarded query, 4 s timeouts, variable upstream so the dashboard starts without it, every other `/api/activity` path 404), the Possible fighting section, the Possible fighting activity row chip and the viewer caption, and the Home/disarm copy that names what keeps running. Static: `node --check`, `python3 -m py_compile` on all five scripts, and `node scripts/check_entry_ui.mjs` all pass. **Not yet run**: `scripts/check.py` against the deployed route, and any browser review. **No accuracy figure exists or is claimed** |
| Final browser validation | **observed 2026-10-02** - the archive read **133 rows over 6 pages at 25**; one checkbox selected **1**, and **Next** carried that **1 off-page**; **Select all shown** gave **26 including the 1 held off-page**, and the selected-delete dialog correctly named **26**; **Cancel** and **Clear** both worked; jumping to the last page (**6**) showed the oldest rows, **126** and up; **Latest** returned to page 1; the `camera_4` filter returned an empty page 1; the archive-delete dialog counted **131 across all pages** and was then cancelled; no horizontal overflow at **320 px** or **1280 px**, desktop rows **78 px**, and all four players held **2560×1440** throughout. **No destructive button was clicked in this pass** - the one deleted snapshot came earlier from the terminal API under the owner's authorisation |
| Ordinary six-hour expiry, watched end to end | **not done** - applied and confirmed loaded in native `/api/config`; nobody waited the window out |
| Snapshots already kept under the old rule | **kept, and still a manual job** - nothing unpins them; only newly captured ordinary snapshots are on the six-hour timer |
| Zones drawn on real boundaries | **not done** - every zone is the full frame |
| COCO 91 animal rule on `camera_1` … `camera_3` | **loaded and verified 2026-10-02** - native internal `/api/config` reports person plus all ten animal classes in tracking and area zones on cameras 1–3; camera 4 stays person-only; the two restricted zones are person-only at `inertia: 3`, `loitering_time: 0`; **no loitering zone remains on any camera**, the `camera_N_loitering` zones at 60 seconds having been removed; recording stays disabled. Runtime config was schema-validated before saving; private backup is ignored by Git. Real animal accuracy is not yet measured |
| Retention worker pinning animals | **verified against a fake Frigate 2026-10-02** - `scripts/check_retention.py` pins all ten labels on cameras 1–3 with no zone at all, refuses the same labels on `camera_4` before any POST, and confirms the listing/deleting gate accepts the pinned rows and refuses the camera-4 ones. Not exercised against a real animal event, because none exists yet |
| Controlled zone walkthrough, alert timing, night, false positives | **not run**, **not verified** |
| Animal detection accuracy | **not verified** - no animal has been on any of these four feeds under the new rule; misses are expected at 352×288 |
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
  the lockout - not a credential problem - was what blocked access. The trigger
  is not established: Frigate's earlier double-encoded-password retries are a
  likely trigger, but recorder audit logs were not examined, so exact trigger is
  not proven.
- **Four cameras live in Frigate, 2026-10-02.** The git-ignored `config/config.yml`
  was restored from `config/config.example.yml` and **only Frigate** was started,
  with raw passwords in the detect RTSP URLs. Live stats: all four `camera_fps`
  3.0–3.1, `process_fps` 3.0–3.1, `detection_enabled` true. The four
  `latest.jpg` responses were `200` with valid JPEGs - latest-frame previews,
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
  confirm the model: it **selects the unretained events - `retain_indefinitely`
  false - for expiry** on a `timedelta(days=...)` cutoff, **excludes the
  retained-true ones, which therefore persist**, and then waits 300 s. Six hours
  is therefore configured and confirmed in effect - it has still not been watched
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
- **The zones are loaded, and one qualifying event was seen - historical, under the
  removed 60-second rule.** Native `/api/config` reported all four
  `camera_N_area` at `loitering_time: 0`, all four `camera_N_loitering` at `60`,
  and `record.enabled: false`, with detection at 3 FPS. One naturally completed
  loitering snapshot was **pinned**, and three new ordinary snapshots were
  **not**. That is a single observation per direction, not a measured rate, and
  **no controlled 60-second walkthrough was performed**, so no timing or accuracy
  claim rests on it. **No loitering zone exists now**; that rule is gone and the
  entry-review rule below is the one in force.
- **One deletion, performed deliberately, 2026-10-02.** With the owner's explicit
  authorisation, one **ended experimental** snapshot was deleted from the
  dashboard: the reply was `deleted: 1`, `missing: 0`, `failed: []`, `active: 0`,
  the image URL then answered **404**, and no archive scope was used, so nothing
  else moved. It is irreversible and there is no undo.

The suite **is** streaming live video, the detector **is** running, and the
snapshot path **does** return real saved detections - but nothing here shows it
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
  you (`escape_special_characters`), so a pre-encoded value is encoded twice -
  `@` becomes `%40` and then `%2540` - and the camera answers 401.
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

This recorder locks an account out after repeated failed logins - 240 seconds,
`remainLoginTimes` 5 - and it took the whole timer to clear on 2026-10-02.

- **Encode each URL exactly once, by the right rule for its kind.** Detect raw
  for Frigate, main encoded for go2rtc. Double-encoding the detect URL, or
  leaving the main URL raw, both answer 401 - indistinguishable from a wrong
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
<https://localhost:8971/settings> (**Configuration → Zones and masks**). Leave
`camera_N_area` as the full-frame `loitering_time: 0` snapshot trigger it already
is, and draw the place worth watching as `camera_N_restricted` on **Camera 1 and
Camera 4 only** - person-only, `loitering_time: 0`, so crossing the line is the
whole event. There is no second, timed zone to draw: the 60-second
`camera_N_loitering` rule has been removed, and Camera 2 and Camera 3 get no
restricted area at all. The example polygon is the full frame
`0,0,1,0,1,1,0,1`, which is right for the area zone and **wrong** for a restricted
one - a full-frame restricted area arms every pixel. See
[`docs/restricted-zone.md`](docs/restricted-zone.md).

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
handful of Frigate and go2rtc endpoints - every one of them a read, except the
one controlled deletion described below. Four cards, one per camera, each playing
that camera's **main** channel - 2560×1440 HEVC - through the player go2rtc
already ships, plus a per-camera detector read-out. Below that, the most recent
person events whose entered zones intersect the area zone for the camera,
filterable by camera and by review status, with the retained ones badged and
selectable for review.

The player is code the browser runs, so it is worth being exact about what
reaches the page: `/video-rtc.js` is the copy bundled inside the Frigate image,
proxied from a fixed upstream path, and the four live paths
(`/live/camera_1` … `/live/camera_4`) are WebSocket upgrades pinned to one named
main stream each with no query string at all. Nothing about a request can aim the
player at a different stream, and go2rtc's own config, stream list and control
WebSocket are not reachable.

What it deliberately does not do:

- It is **unauthenticated**, because it binds to `127.0.0.1` only. Any process or
  person with access to this machine can read it. Extending it beyond loopback -
  a VPN, a tunnel, a reverse proxy - means adding authentication first.
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
  endpoint other than the two controlled calls described below - the reviewed
  deletion and the entry arming. `GET` and `HEAD` on the reads; everything else is
  refused, and every other `/api` path is 404. The browser-side code contains
  exactly two writes, the reviewed-deletion `POST` and the arming `POST`, and
  nothing to sign in to. The page is a viewer plus two buttons a person has to
  aim, so the review workflow around it stays a person's decision.
- Frigate's API port 5000 is never published. nginx reaches it on the internal
  Docker network as `frigate:5000`, and go2rtc as `frigate:1984`. The retention
  container's gateway is reached as `retention:8090`, and the experimental
  fighting sidecar as `fighting:8091`, both on the same internal network and both
  with no published port of their own. No raw Frigate stats, config, logs or frame
  bytes reach the browser through the fighting route: it serves one status
  document and nothing else.

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
  in days. Frigate's periodic cleanup - about every five minutes - is what
  actually deletes it, so six hours is a **nominal cutoff, not an exact
  ceiling**: a file goes at the first cleanup after it passes 0.25 days.
- **A `person` event inside a watched area is kept until you delete it.** On Camera 4
  that is its area_4 area, always. On Cameras 1–3 it is the area that camera's Arm
  control watches: the monitored area 1 on Camera 1, and the whole-frame monitored area 2 on
  Camera 2 and area_3 on Camera 3 while they are armed. **So is an animal on
  `camera_1` … `camera_3`**, unchanged and needing no arming. The `retention` service
  (`scripts/retain.py`) polls Frigate every 30 seconds and sets Frigate's own
  `retain_indefinitely` flag on exactly those events. Periodic cleanup skips a
  retained event, so it survives on its own: a restart neither re-pins nor forgets.
  The worker publishes no port, opens no database of its own, holds no credential,
  and reads nothing but the internal API. The only thing it writes is the operator's
  arming, in `/state/entry.json` on a named volume. Both lifetimes are Frigate's
  own bookkeeping; go2rtc restreams the live view and holds nothing.

`camera_N_area` is the whole frame with `loitering_time: 0`, so **every** person
event still gets a snapshot and still gets its ordinary six hours. It is a
snapshot trigger and nothing more.

What that does **not** do is clean up the old pile. Events pinned under the
removed 60-second rule keep their `retain_indefinitely` flag - the worker only
ever sets it, never clears it, and there is no migration - so **previously kept
snapshots stay until somebody deletes them by hand.** Only newly captured
ordinary snapshots are on the six-hour timer.

## Entry monitoring

**Camera 4 is always armed.** A person inside `camera_4_restricted` - the area_4
area within the configured boundary - is kept for review with no control at all: no button, no
request that turns it off, and nothing written to disk that can override it. It is
armed from startup, it is not in the saved state file, and Home and Away do not
touch it. It still needs the camera service to report its zone, so a zone that
stops being configured shows it unavailable and keeps nothing until it returns.

**Cameras 1–3 are armed by hand.** Camera 1 keeps a person inside its left-side
`camera_1_restricted`. The monitored area 2 (Camera 2) and the area_3 (Camera 3) keep
anyone in view while armed, because their monitored area is the whole frame.

- **Home** keeps no person snapshot on Cameras 1–3. **Away** arms all three at one
  moment, in one write. Switching any single camera yourself overrides a preset,
  and the status reads **Custom** while they disagree.
- **Home and a disarm stop armed person-presence review, and only that.** They do
  **not** turn everything off: Camera 4's area_4 stays armed, the animal rule on
  Cameras 1–3 keeps running with no arming needed, and the
  [possible-fighting watch](#possible-fighting-experimental) keeps running on
  Cameras 2 and 3 whatever these controls say. Three separate things keep
  snapshots here; the page names all three wherever a disarm could be read as a
  total stop.
- **Turning review off from Away is confirmed**, in the same native dialog the
  deletion uses: *Disarm Camera N?* or *Switch to Home?*, naming exactly what stops
  and what carries on. **Cancel and Escape send nothing** and leave every setting
  as it was. Arming a camera, and leaving Home, are never confirmed. Snapshots
  already kept are untouched either way.
- **Your choice is saved**, in one small JSON file on the named volume
  (`--state-file`, default `/state/entry.json`) holding three flags and three
  timestamps and nothing else. A restart comes back as you left it; a fresh
  install comes back Home. A save that does not land leaves the running state and
  the previous valid file exactly as they were, and answers 503 rather than
  reporting a change a restart would lose.
- **A file that cannot be read is Home, not Away**, and the dashboard says so. A
  camera whose area the camera service does not report keeps its setting and keeps
  nothing, then resumes by itself - still measured from the moment you armed it.
- **A kept snapshot is a candidate, not a finding.** A person was inside that
  camera's watched area. Nothing more. Not a direction: Camera 1's left-side entry
  and Camera 4's area_4 approach are both boundary facts. Not an identity: one
  class, `person`. Family is not distinguishable.
- **Arming reaches forwards on Cameras 1–3, never backwards.** An event that began
  before the arm is not kept, so an entry already under way can be missed, because a
  track has one start time for the whole time somebody was on camera. Camera 4 has
  no arming moment and no such boundary.
- **Disarming stops further snapshots being kept and never releases one already
  kept**, and works even when the area cannot be confirmed. The pile from the
  removed 60-second rule is untouched.
- **A camera can only be armed once the camera service reports its area** - the
  restricted polygon person-only at `loitering_time: 0` for Cameras 1 and 4, and
  person among the labels it already carries for the full-frame areas on Cameras 2
  and 3. The worker reads `/api/config` internally and keeps four booleans.
- **Arming here assesses no risk, intent or conflict.** Nothing infers a direction
  of travel, identifies a person, or scores danger. The one separate model in this
  repository is the experimental possible-fighting watch, it is not part of this
  rule, and it is not a finding either; see
  [Possible fighting](#possible-fighting-experimental).

See [`docs/restricted-zone.md`](docs/restricted-zone.md) for the commented zone
example and the arming details, and
[`docs/evaluation.md`](docs/evaluation.md) for how accuracy is to be measured.

**Reviewing.** The dashboard reads the archive through the paginated source,
**25 rows at a time** across the four cameras; each retained row carries a *Kept
for review* badge, and a person row kept under the old rule reads *Previously
kept*. Two tab groups narrow it: **All cameras** … **Camera 4**, and **All
detections** … **Kept for review**. The second is the camera service's own kept
flag and nothing else - `retain_indefinitely` - so it includes the *Previously
kept* rows and kept animals, reads no class and no arming state, and still means
the same thing after the retention policy changes. It is applied **before** the
rows are counted and paged, so `total` and the page count always describe the
narrowed set, and it combines with the camera filter rather than replacing it.
Both choices live in the URL beside the page, so a reload and the back button both
come back to the view, and changing either returns to page 1. The archive-wide
count and the delete scopes are unchanged by any of this, and follow neither
filter:
a `person` row on **all four** cameras is listable and deletable whatever the
retention policy is doing, so an old kept record can always be removed. Page,
camera filter and a jump back to the newest page are part of the page itself, the
page count comes from the source rather than being counted here, and a page past
the last lands on the last rather than erroring. The poll re-reads only the
newest page, so a page being read through history does not shift under the
operator. While the worker is running,
`docker compose logs -f retention` reports one pass per 30 seconds with pinned,
already-retained and skipped counts, and which cameras are armed - that line is
the only sign it is alive.

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
hand. There is no cap, and no automatic expiry - that is the point of keeping
evidence.

## Possible fighting (experimental)

One separate, explicitly **experimental** model exists, and it answers exactly one
narrow question. It is not part of the rules above, it is not armed by them, and it
cannot be switched off from anywhere.

`scripts/fighting.py` runs as its own container with **no published port**. It looks
at the whole frame of two rooms - **Camera 2** (monitored area 2) and **Camera 3**
(area_3) - at 3 FPS, in memory, and asks one small local video model whether a
16-frame window looks violent. **Two consecutive windows** on one camera at or above
a threshold confirm an interval, and every Frigate `person` event overlapping that
interval gets the native sub-label **`possible_fighting`** and is **kept
indefinitely**. Cameras 2 and 3 only, whatever anybody has armed.

**What it is not, and this is the whole point.**

- **It is not a finding.** `possible_fighting` is a possibility a model reported.
  A hug, a dance and a fight can look the same to it.
- **Its accuracy has not been measured on these two rooms.** No precision, recall,
  false-positive rate or confidence figure exists anywhere in this repository, and
  none may be quoted. The threshold is a number somebody chose, not a calibrated
  one. A person has to look at every snapshot it labels.
- **An inference timing is not an accuracy.** Timings taken on this host are
  timings and nothing more; they are not a score and not a confidence.
- **It has nothing to do with weapons, intent, identity or anomaly.** Nothing here
  says a weapon was present, who anybody is, who started anything, or whether
  anything unusual happened at all beyond what one small model said about sixteen
  frames.

**Three lifetimes, not one.** An ordinary snapshot is kept **six hours**. A snapshot
kept by armed person review or by the animal rule is kept **until a person deletes
it**, as is one the fighting watch labels. All three are the same Frigate flag
(`retain_indefinitely`) and the same cleanup that skips it, so *Kept for review* on
the dashboard means the flag and only the flag.

**The model is pinned and provisioned from outside.** Weights come from a local
mounted directory only (`local_files_only`, safetensors, no remote code, **no
download at runtime**), at one pinned revision of one MIT-licensed model. Nothing
is trained here, and the model file is gitignored.

**What the browser may see of it: one safe read.** `GET /api/activity/fighting`
proxies the sidecar's own read-only `/status` and nothing else of it: one exact
path, a fixed upstream URI, **no forwarded client query**, no CORS, and the server's
own 4 s timeouts. The upstream is a variable so Docker's resolver, not nginx's
start-time lookup, resolves `fighting:8091`, so **the dashboard still starts and
still serves everything else when the sidecar is absent**. Every other
`/api/activity` path is 404. The page checks the status field by field and shows
**Monitoring** only when the model is ready, that camera is analysing, and its
capture and score are recent; a loading model, a refused model, no frames, and
stale data all read as Starting or Unavailable. The page has **no control** over
this watch, and the status carries no frame, path, score or credential.

**Rows.** A labelled snapshot carries **Possible fighting activity** in the row's own
tag row and in the viewer caption, read from the camera service's `sub_label` and
never inferred from the camera or the armed state. The kept filter is
`retain_indefinitely` and nothing else, so that label can never widen what *Kept for
review* means.

The sidecar is authorised to do exactly two things, both on events that already
exist: write that sub-label, and set the retain flag. It never deletes, never
un-pins, never edits configuration and never creates an event. **The browser's two
writes are unchanged**: the reviewed deletion and the entry arming.

Full model and runtime detail: [`docs/fighting.md`](docs/fighting.md).

## Animals

`camera_1`, `camera_2` and `camera_3` track the ten COCO 91 animal classes as well
as `person`. `camera_4` tracks `person` and nothing else.

| | person | animal |
|---|---|---|
| Tracked on | all four cameras | `camera_1` … `camera_3` |
| In `camera_N_area` (`loitering_time: 0`) | yes | yes - snapshotted on sight |
| Kept by `retain.py` when | a person is inside that camera's watched area: `camera_4_restricted` always, `camera_1_restricted` / `camera_2_area` / `camera_3_area` while armed | it was seen, no zone and no arming needed |
| Row text reads | `Monitored area` | `Dog · Monitored area` - the class, then the area |
| Dashboard row reads | `Kept for review` | `Kept for review` |

The zones carry the whole rule. An animal label sits in `camera_N_area`, which is
already `loitering_time: 0`, so it is snapshotted the instant it is in frame, and
the worker pins it on sight - no arming, no restricted area, no person rule.

Three things this is **not**.

- **It is not a harm judgement.** SSDLite MobileNetV2 reads `coco_91cl_bkgr.txt`
  and reports a species. "Harmful" is decided by a human afterwards, never by the
  model. No class here means dangerous.
- **Rats, snakes and scorpions are not in it.** COCO 91 has no rodent, snake or
  scorpion class, so those cannot be detected here at all - they need a different
  model, which this configuration deliberately does not have.
- **It is not verified against a real animal.** Detection runs at 352×288 and
  3 fps. A person fills the frame; an animal at the edge of one is far smaller in
  those pixels, so misses are expected. Nothing here has been measured on these
  four feeds, and no claim is made about which class is the worst behaved here.

**Cost, which is not measured.** Tracking eleven classes rather than one adds
detector and tracker work this repository has not quantified, and its effect on
detection accuracy is unmeasured too - the live figures in the Status table were
all taken with `person` only. One structural fact is readable from the
configuration, and it is not a cost figure: the model scores the whole COCO 91
label set on every frame and `objects.track` filters afterwards, so the forward
pass is the same one either way. That does not make the extra work free, and
nothing here measures it.

**One consequence to know about.** Because an animal is pinned on sight, a false
positive is **permanent** - it holds `retain_indefinitely` until somebody deletes
  it, exactly like a person kept by armed person review. All ten classes stay
  tracked on cameras 1–3,
as asked. Which of them misfire on a 352×288 frame, and how often, is unmeasured
on these feeds, so no class is named here as the likely offender. If the archive
fills with one you do not want, the fix is one line in `config/config.example.yml`
and one in `scripts/retain.py`; the worker counts what it refused, so it will tell
you they were arriving.

`camera_4` is gated twice over: it does not track the labels in Frigate, and
`retain.py` refuses an animal event on it before any request is made. The second
gate does not depend on Frigate having been reconfigured.

## Controlled deletion

One of the two write paths in the project, added on the owner's explicit
instruction. It
replaces the previous "there is no delete button anywhere, by design" position:
deleting a reviewed snapshot is now possible from the page, and **still only
after a person decides to.**

It is not a new service. The `retention` container runs `retain.py --serve`,
which runs the existing 30-second poll loop and a small stdlib HTTP server in one
process, sharing **one lock** so a poll pass and a manual deletion can never
interleave. No new image, no framework, no dependency, no new port published, no
`env_file`, and no storage mount - Frigate's own API does the deleting.

**Three exact paths - two reads and one write - and nothing else answers:**

- `GET /api/snapshots/summary` → `{"count": 812, "before": 1790928400}`.
  `count` is every person snapshot from the four configured cameras in the whole
  archive, **older than `before`, not just the newest 50**; `before` is that whole
  second, so the number on screen is exactly the number a deletion reaches.
- `GET /api/snapshots/events`, the paginated read, taking **optional** `page`,
  `camera` and `review` and nothing else. All three are strictly validated before
  any upstream request: a repeated, unknown or blank parameter, a page that is not
  a positive whole number, a camera that is not `all` or one of the four configured
  names, or a `review` that is not `all` or `kept` is refused. It answers
  `{"events": [...], "page": 2, "page_size": 25, "total": 118, "pages": 5}` -
  **25 rows per page**, newest first, both filters applied **before** the
  page is cut, so `total` and `pages` always describe the filtered set. A page
  past the last is clamped to the last rather than refused, so a page that is one
  behind after deletions still gets an answer. The read walks the archive to count
  it, so it is bounded by the same page cap as everything else here and answers
  503 rather than a short page that reads like a whole one. `review=kept` is
  exactly the rows carrying `retain_indefinitely` true, which is why a record
  left over from the removed 60 second rule and a kept animal are both in it and
  an unflagged person inside a watched area is not; no class list, zone list or
  armed state is consulted.
- `POST /api/snapshots/delete`, which takes **either** `{"ids": ["<native id>",
  ...]}` (1 to 1000, duplicates folded) **or** `{"all": true, "before":
  <summary cutoff>}`, and answers `{"deleted": 12, "missing": 0, "failed": [],
  "active": 1}`. It takes **no query string on any method**, so the scope comes
  from that body alone and no argument can narrow a delete the confirmation did not
  describe.

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
`success: false` is never counted as a deletion. Errors are redacted JSON - never
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
never will - deletion happens only through an explicit HTTP `POST` or the
operator's CLI - and every other dashboard endpoint, along with Frigate's config,
logs, exports and restart paths, stays denied. The gateway is on the internal
Docker network only, so its port is not an entry point from the host; a request
that does not name the dashboard's own address is refused by the gateway itself.

**Run, and used once.** `scripts/check_console_api.py` **passes** (2026-10-02)
against a disposable fake Frigate, and the deployed gateway was then exercised
live: `scripts/check.py` passed with it running, and one **ended experimental**
snapshot was deleted from the dashboard with the owner's explicit authorisation -
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
headers, a list of paths that must be denied including go2rtc's own and every
`/api/activity` path except the one fighting read, write
methods that must be refused, the pinned stats shape against override attempts,
the bundled player module and the absence of any credential-bearing `rtsp://`
URL in what is served, real event snapshots, that settled zone snapshots
arrive flagged kept, the `kept` review scope and its refusals, a query on the
delete path being refused, and the experimental fighting status route: its core
shape, that no query is forwarded, that only GET and HEAD answer, and that it
carries no path, credential, media URL or frame. It reports **`NOT READY`**, never
a pass, when the fighting model is loading or refused or when no room is being
monitored, so a clean run cannot be read as a working model. With Frigate down it
**fails**, rather than passing a skipped
check: an unreachable camera service is not a passed validation.

It is a transport check. It never opens a WebSocket, so it **cannot** prove a
frame was decoded - the live video result is a browser observation recorded in
[`docs/validation.md`](docs/validation.md), not something this script reports.
The retention worker's own decisions are covered separately, against a fake
Frigate, by two scripts that need no stack, no camera and no network:

```sh
python3 scripts/check_retention.py   # the poll: which events are pinned, and that nothing is deleted
python3 scripts/check_console_api.py # the --serve gateway: refusals, selection, fail-closed walks
node scripts/check_entry_ui.mjs      # the confirmation: which change asks, and that Cancel writes nothing
```

All three run the real code - the two Python ones over real processes and a real HTTP
boundary, the Node one against the page's own functions lifted out of `dashboard/app.js`
with only the dialog and the dispatcher replaced. **All three pass.** None of them
says anything about the live stack; that evidence is recorded separately in
[`docs/validation.md`](docs/validation.md).

Accuracy is scored from CSVs a person filled in by hand, not from a live stack:

```sh
python3 scripts/evaluate.py --self-check
python3 scripts/evaluate.py --trials docs/evaluation/trials.csv \
                            --windows docs/evaluation/windows.csv
```

There is no CI workflow in this repository.

## Documentation

- [`docs/spec.md`](docs/spec.md) - the phased plan, and what is implemented.
- [`docs/validation.md`](docs/validation.md) - what was actually run, and the
  manual checklist including UI screenshot checks.
- [`docs/restricted-zone.md`](docs/restricted-zone.md) - the commented zone
  example, which camera watches which area, and what arming does and does not say.
- [`docs/evaluation.md`](docs/evaluation.md) - the accuracy protocol, and the two
  empty CSV templates it is recorded in.
- [`docs/fighting.md`](docs/fighting.md) - the experimental possible-fighting model:
  the pinned revision, how it is provisioned, and what its label does and does not
  mean.

## Known limitations

Stated plainly, because nothing below has been measured and nothing below is
fixed by it.

- **No accuracy figure exists** for any rule, on any camera, in any lighting.
  The protocol is written down and the templates are empty.
- **The saved snapshots cannot show what was missed.** Frigate only writes an
  image for something it detected, so the archive is a list of hits by
  construction. Recall needs an observer's ground truth recorded separately.
- **A kept entry is not a direction.** Camera 1's monitored area 1 and
  Camera 4's area_4 approach are boundary questions a zone cannot answer; that
  needs an ordered track across frames, which does not exist here.
- **Family is not distinguishable.** One class, `person`.
- **The possible-fighting watch has no measured accuracy on these two rooms**, and
  that is the limitation, not a footnote. It is a possibility a model reported, its
  threshold is a chosen number, and no false-positive rate, precision, recall or
  confidence figure exists for it anywhere. Two systems may read it as either kind
  of behaviour, and both would be kept for a person to look at. Inference timings
  have been taken on this host and they are timings, not accuracy.
- **There is no weapon, intent, identity or anomaly detection anywhere**, and the
  COCO 91 object detector has no such class. Camera 2 and Camera 3 keep anyone in
  view while they are armed, because their area is the whole frame - presence, not
  an incident.
- **An entry already under way when you arm can be missed** on Cameras 1–3, because
  a track has one start time. Camera 4 has no arming moment and no such boundary.
- **Arming survives a restart now**, which the old "every restart comes back
  disarmed" rule got the wrong way round for cameras 1–3. What still holds: a fresh
  installation is Home, arming is never automatic, and no command line arms
  anything.
- **An animal false positive is permanent**, because that pin needs no arming
  and has no expiry.
- **Detection is 352×288 at 3 fps** and has not been shown to be the right rate.
  A saved snapshot is cut from that small frame, not from the 2560×1440 live
  view.

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
### Latest live verification - 2026-10-02

After applying animal tracking, `scripts/check.py` passed against the running stack: four cameras detecting, paginated archive and camera filters responding, snapshots returning JPEG, and all refusal checks holding. Retention and deletion animal-policy checks passed separately against a fake Frigate; no live animal event was used to prove pinning or deletion.

Browser verification showed four 2560×1440 players running. Expanding Camera 1 produced a 1264×984 dialog and minimizing preserved its exact source; a 320px viewport had no horizontal overflow, pagination buttons on one row, and a 304×784 camera dialog closed with Escape. Snapshot preview opened in a dialog without changing the URL and Escape restored scrolling. Next set `?page=6`, reload retained that page, and Latest returned to `/`. The viewport override was reset. These are observed UI checks, not animal accuracy measurements.
