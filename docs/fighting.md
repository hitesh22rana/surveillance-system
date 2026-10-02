# The experimental fighting watch

`scripts/fighting.py` watches **cameras 2 and 3** (the monitored area 2 and the area_3)
for what might be two people fighting, and keeps the person snapshots of the interval
in which it looked that way. It is an experiment, not a finished feature. Read this
page before believing anything it labels.

`possible_fighting` is **a possibility a model reported, never a finding**. The model may confuse fighting with a hug, dance, game, a child being lifted,
a person helping another up, or gesturing. Every
snapshot it labels is a snapshot a person has to open and judge. Nothing here has been
measured on these two rooms, so **its accuracy is unknown**: the threshold is a number
somebody chose, not a calibrated accuracy, and an inference time is not an accuracy
either.

## The model

| | |
| --- | --- |
| Repository | [HappyGook/videomae-violence-detector](https://huggingface.co/HappyGook/videomae-violence-detector) |
| Revision | `975dadb8f6d7c5d3119ad40341c3622ee5f8ff43` |
| Licence | MIT |
| Architecture | VideoMAE, a video classification transformer |
| Input | 16 frames per clip, its own `num_frames` |
| Output | 2 classes: index 0 nonviolent, index 1 violent |
| Runtime | CPU, two threads |
| Measured on this host | 1.5 s cold, 0.864 s warm per 16 frame clip |

The two classes come from the model card, not from the checkpoint's own config: that
config carries the default placeholders `LABEL_0` and `LABEL_1`, and the worker reads
that pair as nonviolent then violent, the same as a spelled-out pair. A swapped pair is
refused rather than guessed at, because reading it the other way round turns a quiet
room into an alert.

The weights are **not in this repository**. Root provisions them into
`.models/fighting`, which compose mounts read-only at `/models/fighting`. The worker
loads from that directory with `local_files_only=True`, `use_safetensors=True` and
`trust_remote_code=False`, and refuses to score anything if a single weight is missing,
unexpected or mismatched, if the clip length or class count is not what the card says,
or if a score is not a finite number. Model loading is offline. The worker contacts only Frigate on the local Docker network.

## Provisioning the model, once, by hand

This is a one-off the operator does on the host, never something the container does at
startup. There is no download step in `Dockerfile.fighting`, no `snapshot_download`, no
`from_pretrained` without `local_files_only`, and the image sets the hub offline and
telemetry off besides. Fetch the three runtime files at the pinned revision, plus its model card:

```bash
mkdir -p .models/fighting
cd .models/fighting
REV=975dadb8f6d7c5d3119ad40341c3622ee5f8ff43
BASE=https://huggingface.co/HappyGook/videomae-violence-detector/resolve/$REV
for file in config.json model.safetensors preprocessor_config.json; do
  curl --fail --location --silent --show-error "$BASE/$file" -o "$file"
done
curl --fail --location --silent --show-error \
  "https://huggingface.co/HappyGook/videomae-violence-detector/raw/$REV/README.md" \
  -o MODEL_CARD.md
```

`huggingface_hub.snapshot_download(repo_id=..., revision=REV, local_dir=".models/fighting",
allow_patterns=["config.json", "model.safetensors", "preprocessor_config.json", "README.md"])`
does the same thing if you would rather use the library, from a host that has it. Either
way, `.models/` is in `.gitignore`: the weights are several hundred megabytes, they are
not this repository's business, and no commit should ever carry them.

No `trust_remote_code` file is fetched and no pickle is loaded: the worker asks for
safetensors and refuses anything else, so a checkpoint that needs remote code to run is a
checkpoint this worker will not run.

`transformers` is pinned to **5.8.1** for a reason: 4.57.6 drops the trained biases of
this architecture, so its weights do not load cleanly and it cannot be used with this checkpoint. The clean-loading check is what catches that, which is why it is a refusal
rather than a warning.

## What it does, in order

1. **Capture.** Two threads read `camera_2/latest.jpg` and `camera_3/latest.jpg` from
   Frigate's internal API at 3 FPS. Nothing is decoded into a video file: sixteen stills
   in a row is the clip, and frames live in a bounded buffer of the newest 32.
2. **Window.** Whole, nonoverlapping 16 frame windows: about 5.3 s of the real room each.
3. **Score.** One model, one loop, two CPU threads. A window is only scored while
   Frigate's own `/api/stats` reports that camera's `camera_fps` and `process_fps` as
   positive.
4. **Confirm.** Two consecutive windows on one camera at or above the threshold (default
   `0.9`, configurable with `--threshold`) confirm one candidate interval, and then a
   60 second cooldown stops that camera confirming another.
5. **Match.** Every 15 s, while an interval is pending, the worker's own bounded archive
   walk finds every native `person` event with a snapshot on that camera whose track
   overlaps the interval, and labels it `possible_fighting` with the model's score.
6. **Keep.** Each labelled event is pinned with `retain_indefinitely`, the same flag the
   retention worker sets, so Frigate's own pruning cannot take the snapshot first.

The label is Frigate's own native `sub_label` field. The existing archive and the
existing manual deletion path handle these snapshots exactly as they handle every other
one: they are listed, they can be deleted by hand, and nothing here deletes anything.

## What it refuses, and why it matters here

- **A gap over a second, a failed read, or a dropped frame** throws away the window and
  the votes with it, so two windows that never touched cannot be joined into a pair.
- **Sixteen identical frames** are refused before the model is called. That is what a
  camera shows when nothing has happened *and* what it shows when the image has stopped
  updating, and a model asked about those has been asked about its encoder.
- **A result that arrives late**, after the buffer moved on or after the window has aged
  out, is discarded rather than counted.
- **A row already carrying `possible_fighting`** needs no second label. **A row already
  kept** needs no second keep. **A row carrying any other sub-label**, including a shape
  this cannot read, is left exactly as it is and never pinned. **A refused label** gets
  no keep at all.
- **A candidate interval is never dropped for having been matched.** It stays for its
  six hours, because a person track is often snapshotted a moment after the window that
  named it. What an event still needs is read from Frigate's own row every sweep, so
  retries are harmless and nothing is written twice.

## Its honest limits

- **Low resolution and slow.** Cameras 2 and 3 are 352x288. Three frames a second is
  about the ceiling of what a CCTV stream gives away, and it means a fast movement or a
  fight that happens mostly between frames is invisible to this. A clip that contains a
  fifth of a second of violence and 4.8 seconds of two people standing there is a clip
  of two people standing there.
- **A standard center crop.** The processor's own saved configuration resizes and centre
  crops to the model's 224x224. Whatever happens at the edges of the frame is cropped
  away before the model sees it, so a struggle that starts in a doorway or behind a
  chair may never reach the model at all.
- **Domain shift.** This model was trained on video that is not a monitored area 2 at 352x288
  from a cheap camera, in bad light, with people at a distance. How it behaves on this
  footage is unknown, and the labels it produces here are exactly the case where its
  training and this room disagree most.
- **No measured accuracy.** Not on this house, not on these two rooms, not on anything
  else. There is no confusion matrix, no false positive rate and no true positive rate
  in this repository, because producing one needs labelled real footage of real fights
  and that has not been done. Treat every label as a request to look, not as a detection.
  **A snapshot cannot prove a fight either.** A kept image is evidence that a person was
  there and that a model said something about sixteen frames, and nothing more.
- **Fast and edge activity can be missed.** Three frames a second will miss a shove that
  takes a fifth of a second, and the centre crop throws away whatever happens at the
  edges of the frame before the model sees it at all. A missed fight leaves no trace in
  the archive and nothing on the dashboard. There is no "clear" state: an hour with no
  labels is an hour in which nothing was above the threshold, not an hour in which
  nothing happened.
- **It is not armable and it is not disarmable.** Cameras 2 and 3 are watched whatever
  anybody has armed. Home and Away do not reach it. That is deliberate, and it also means
  there is no switch to turn it off while you look into what it labels: stop the service
  with `docker compose stop fighting`.

## What it can never do

The worker's whole write surface is two requests on events that already exist: a
sub-label, and the retain flag. There is no DELETE anywhere in it, no un-pin, no
configuration read or write, no created event and no notification to anybody. It publishes
no port, holds no credential, and answers one read-only `GET /status` on the internal
network, refusing any other Host, path, method or query string.

## Running and checking it

```bash
docker compose up -d fighting                  # start
docker compose logs -f fighting                # one line per confirmation and per sweep
docker compose stop fighting                   # stop it, whatever the dashboard says
python3 scripts/check_fighting.py              # the boundary check, no ML packages needed
```

`scripts/check_fighting.py` runs the real worker against a fake Frigate on loopback and
asserts only on the requests that left it and the one status answer it serves. It covers
the temporal rule, the capture and freshness rules, the pending file, the exact label and
keep requests, every refusal above, and the status read. **It proves nothing about
whether this model detects a fight.** That needs a person's eyes on real footage, and the
result would still be one house, one pair of cameras and one evening.
