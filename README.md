# Frigate Zones Prototype

Minimal single-camera Frigate setup for one thing only: detect a person loitering
in a single named zone, then snapshot it. Notifications are **not** wired up yet —
this prototype records events and snapshots, it does not send anything to anyone.

- Frigate pinned to **0.18.0** (`ghcr.io/blakeblackshear/frigate:0.18.0`).
- Web UI bound to `127.0.0.1:8971` only. Not reachable from the network.
- Target platform is **Linux + Docker**. Docker on macOS works for exploration but
  gives no guaranteed GPU acceleration, so detection latency there is not
  representative of the intended deployment.
- No custom YOLO training or model code. The bundled SSDLite MobileNetV2 OpenVINO
  model is used until a measured accuracy problem justifies something else.

## Setup

```sh
cp config/config.example.yml config/config.yml
cp .env.example .env
chmod 600 .env
```

Edit `.env` and put the real RTSP URL in `FRIGATE_CAMERA_RTSP_URL`.

The camera password must be **URL-encoded**. Characters that break RTSP URLs when
left raw: `@` -> `%40`, `:` -> `%3A`, `/` -> `%2F`, `#` -> `%23`, `?` -> `%3F`.

`config/config.yml` also carries a placeholder `courtyard_area` polygon. Draw the
real shape in the Frigate UI instead of trusting the example coordinates.

Validate, then start:

```sh
docker compose config -q
docker compose up -d
```

`docker compose config -q` checks that Compose can parse the file. It does **not**
validate the Frigate schema inside `config/config.yml`.

Open `https://localhost:8971` and use the Frigate login screen. The console warns
about the self-signed certificate; that is expected for a local prototype. Keep access local for this prototype; remote access is a later VPN phase.

On first startup, Frigate creates an admin account and prints its initial password
in the local logs. Treat those logs as sensitive. Inspect startup and camera errors here:

```sh
docker compose logs -f frigate
```

## What this prototype does not do

- Sends no email, push, or webhook notifications. Nothing leaves the host.
- Records no continuous video (`record.enabled` is false).
- Serves no RTSP stream on 8971 and exposes no ports beyond loopback 8971.
- Runs unprivileged, with no host network mode and no extra capabilities.

## Documentation

- [`docs/spec.md`](docs/spec.md) — phased plan: one camera and zone, then email
  snapshot alerts, then all cameras, then remote access.
- [`docs/validation.md`](docs/validation.md) — manual acceptance checks. Nothing
  in this repo has been validated against a real camera yet.

## Prototype run notes

These are tooling facts about how this prototype was written, kept here because the
repository should not imply more process than it has.

- Written with opencode 1.18.33, model `opencode/space-bunny-free`.
- Smoke test result: `MODEL_OK`.
- The original OpenCode database failed with `no such column project_id`. A separate
  `XDG_DATA_HOME=/tmp/surveillance-opencode-data` allowed a successful writing run.
- Codex reviewed the draft, corrected its YAML structure and added retention.

## Official Frigate references

- <https://docs.frigate.video/frigate/installation/>
- <https://docs.frigate.video/guides/getting_started/>
- <https://docs.frigate.video/configuration/zones/>
- <https://docs.frigate.video/configuration/notifications/>
