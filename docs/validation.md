# Validation

Live camera acceptance checks below are pending. Static validation results are
recorded separately; they do not establish camera detection accuracy.

There are no tests, no scripts, and no CI in this repository by design. A passing
test suite would tell you the YAML parses. It would not tell you the camera
streams, the night footage is usable, or the alert is worth reading.

## What static validation does and does not prove

`docker compose config -q` proves Compose can parse `compose.yaml` and that
`.env` resolves. That is all it proves.

It does **not** validate the Frigate schema in `config/config.yml`. A misspelled
key, a bad zone polygon, or a detector path that does not exist inside the image
will pass static validation and fail at container start. Validate against the pinned image schema, then start the container and inspect
`docker compose logs -f frigate`.

Similarly, the file starting is not the same as detection working. Both must pass.

## Platform note

Target platform is **Linux + Docker**. On macOS, Docker runs in a VM: no GPU
acceleration is guaranteed, file and volume performance differs, and detection
latency is not representative of the intended deployment.

macOS results are **exploratory only**. Use them to confirm the camera streams and
the zone logic are sound. Do not record a macOS latency number as the accepted
figure, and do not treat a macOS pass as hardware acceptance for deployment.

## Acceptance checks

Record the date, the host, and the camera for each. All pending.

### Startup and connectivity

- [ ] `docker compose up -d` completes without error.
- [ ] Logs show the detector initializing with the expected model.
- [ ] `https://localhost:8971` loads and the login screen accepts credentials.
- [ ] The camera appears live in the UI.
- [ ] No repeated FFmpeg reconnect or authentication errors in the logs.

### Inside the zone

- [ ] A person enters `courtyard_area` and remains.
- [ ] On a fresh track, no alert fires before roughly 3 seconds of dwell time.
- [ ] An alert fires shortly after the dwell threshold is met.
- [ ] The snapshot exists, has a visible timestamp, and has a bounding box drawn.
- [ ] The bounding box matches the person, not the background.
- [ ] Record tracking IDs and review grouping for continuous presence; track loss or
      re-entry can generate new events.

### Outside the zone

- [ ] A person walks through frame entirely outside the polygon: no alert.
- [ ] A person moves from outside to inside: an alert fires once they qualify.
- [ ] A person moves from inside to outside before 3 seconds: no alert.
- [ ] A non-person object in the zone (animal, vehicle, shadow, headlight) does not
      produce an alert.
- [ ] The example placeholder polygon from `config.example.yml` has been replaced
      with a real one drawn in the UI.

### Day and night

This is the check most likely to fail and the one most often skipped.

- [ ] Daytime walk-through passes with correct detection.
- [ ] Night walk-through passes: person is detected, not lost to darkness.
- [ ] Glare, headlights crossing the zone, and shadows do not produce alerts.
- [ ] IR switchover at dusk does not cause a burst of false alerts or a stall.
- [ ] Person is still detected when partially occluded, for example behind a post
      or a parked vehicle.
- [ ] Detection quality is compared against the camera's own night footage, since
      the camera may be tuning itself.

### Latency

Target: **5 seconds from loiter qualification to a visible alert.** Record snapshot
availability separately because final best snapshots can wait until tracking ends. A target for a hand measurement, not a guarantee and not a claim
about performance under load.

- [ ] Time zone entry to alert, subtract the three-second loiter threshold, day.
- [ ] Repeat at night. Expect it to be worse; record the actual number.
- [ ] Repeat with the host under normal load.
- [ ] Latency is measured per camera once more than one camera exists. An average
      across cameras hides a slow one.
- [ ] If latency exceeds the target, the recorded cause is motion before the
      person is trackable, detector cadence, snapshot write, or host load. Guessing
      at the cause does not close this check.

### False positives

- [ ] Review a full day of events. Record the total count and which were real.
- [ ] Identify the recurring sources. For each, decide whether to adjust the zone,
      raise `loitering_time`, or accept it.
- [ ] Re-review after adjustment and confirm the count dropped without losing real
      detections.
- [ ] Confirm that a person repeatedly crossing the zone does not produce an
      unreadable volume of events.

### Retention and disk

- [ ] Confirm snapshots are under `storage/`; the database is in the writable `config/` mount.
- [ ] Confirm retention is behaving as expected and files are pruned.
- [ ] Measure disk growth per day of operation.
- [ ] Set a disk warning threshold and verify it in an isolated test volume; do not fill
      the host disk to test failure.
- [ ] Confirm nothing sensitive is written outside the bind mounts.

### Reconnect and outage

- [ ] Power-cycle the camera: Frigate reconnects on its own with no restart of the
      container.
- [ ] Note how long reconnection takes and whether alerts resume afterwards.
- [ ] Briefly drop network connectivity between host and camera, then restore.
- [ ] Confirm a dead camera is distinguishable from a quiet zone. It currently is
      not — this check is expected to fail and is the reason camera availability
      monitoring is listed as a risk in [`spec.md`](spec.md).
- [ ] Confirm a camera failure does not stop detection on the working cameras.

### Host restart

- [ ] `docker compose restart frigate` restores detection without manual config
      edits.
- [ ] A host reboot brings the container back up.
- [ ] Config survives restart and the zone is unchanged.

## Sign-off

A phase is accepted only when its checks are all recorded with real results and
signed off by a person, not by the existence of this document. Until then the
status of every line above is **pending**.

## Verified during repository creation — 2026-10-02

Host: macOS; Docker Engine reports Linux aarch64.

- PASS: `docker compose config -q` with placeholder `.env`.
- PASS: Frigate 0.18.0 schema parsing of `config/config.example.yml`.
- PASS: OpenVINO lists CPU and compiles the bundled SSDLite model on this host.
- PASS: local `.env` and runtime config ignored; example files included.
- PASS: Space Bunny Free answered `MODEL_OK` and wrote the starter files via OpenCode.
- Not performed: Frigate service startup, authenticated camera ingestion, zone walkthroughs, latency, night testing, reconnect and retention soak.
- No CI workflow is configured in this starter repository.

Repeat schema validation without connecting to a camera:

```sh
docker run --rm --entrypoint python3 \
  -e FRIGATE_CAMERA_RTSP_URL=rtsp://example.invalid:554/stream \
  -v "$PWD/config/config.example.yml:/tmp/prototype.yml:ro" \
  ghcr.io/blakeblackshear/frigate:0.18.0 \
  -c 'from frigate.config import FrigateConfig; FrigateConfig.parse(open("/tmp/prototype.yml").read()); print("FRIGATE_SCHEMA_OK")'
```
