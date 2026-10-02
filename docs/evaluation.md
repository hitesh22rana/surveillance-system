# Evaluation

Nothing here has been measured. This document is the protocol, and the two CSV
files next to it are empty headers. There is no accuracy figure for any rule in
this repository, and the first person to run this will be the first.

## The one thing to know first

**The saved snapshots cannot show you what was missed.** Frigate only writes a
snapshot for something it detected, so the archive is by construction a list of
hits. A person who walked past every camera and was never detected leaves no
trace in it — no row, no absence, nothing. Measuring recall therefore needs a
second, independent record: somebody standing there, writing down what they saw.
The `expected` column is that person's ground truth. Without it, only precision
can be looked at, and half of the question stays unasked.

Detection here is `person` and the ten COCO 91 animal classes at 352×288 and 3
fps. It does not tell family from anyone else, and it has no fighting, behaviour
or anomaly class at all. Do not score those; there is nothing to score.

## Record first, score later

Fill the CSVs in **while standing there**, before looking at any result. A row
written afterwards from what the dashboard showed is a record of the detector,
not of the scene.

```sh
python3 scripts/evaluate.py --trials docs/evaluation/trials.csv \
                            --windows docs/evaluation/windows.csv
```

Stdlib only, no network, no image export. An invalid or missing value is refused
with the file, line and column, and the run prints nothing rather than scoring
half a file. `python3 scripts/evaluate.py --self-check` proves the counting and
the refusals against disposable files of its own.

## trials.csv — one row per observed crossing

`camera,lighting,expected,detected`, with `camera` one of `camera_1` …
`camera_4`, `lighting` exactly `day` or `night`, and `expected` and `detected`
exactly `0` or `1`. `expected` is what the observer saw; `detected` is what the
suite produced, including no snapshot at all.

`lighting` is what it was like at the time, not what the clock says. A dusk
crossing is `day` or `night` by whether the frame needed the lights.

## windows.csv — one row per stretch of watched quiet

`camera,lighting,hours,false_alerts`, `hours` a finite positive number and
`false_alerts` a non-negative whole number.

This is the file that cannot be derived. An empty stage looks exactly like a
missed object, so "no trials" is not "no false alerts" — it is nobody looking.
A window is a period you watched a camera with nobody deliberately in it, and
`false_alerts` is what the suite produced during that time.

The rate is `false_alerts ÷ hours`, per camera-hour. With no windows file, or no
rows for a group, it prints **`unmeasured`** — which is a different thing from
zero, and is never printed as zero.

## Two rules, and they are never scored in the same run

The same walk-through is a **positive** under one rule and a **negative** under the
other, so "the family trials" cannot be called negatives without saying which rule
is being scored. There are two rules, and they are run as separate CSV runs:

- **Object detection — was a person on camera detected at all?** Every observed
  crossing counts, anywhere in the view: `expected: 1`, and `detected` says whether
  a snapshot came out. Family crossing the kitchen is a **positive** trial here.
- **Entry review — was somebody inside the armed restricted area kept?** Only
  crossings of `camera_1_restricted` or `camera_4_restricted`, and only while that
  camera is armed. An ordinary family crossing that stays **outside** the restricted
  area is `expected: 0`; a crossing **into** the armed restricted area is
  `expected: 1`. Arming is by hand, so each of these rows is recorded with the arm
  window it was scored in, and the watched-quiet windows for that camera belong to
  this run rather than to the other one.

Keep two sets of files and score them separately, with the rule named in the run
notes for each. `false_alerts` follows the rule being evaluated rather than the
archive: an animal pinned on sight on `camera_1` … `camera_3` is not a false alert
for the entry-review rule, and an ordinary family snapshot is not a false alert for
the object-detection rule. One number for both rules is one meaningless number.

## What to run

Each of these separately, per camera, in daylight and at night, and each scored
as its own run:

- **ordinary family walk-through** — people crossing the view on their way
  somewhere. For **object detection** these are positives: `expected: 1`, and
  `detected` says what happened. For **entry review** the same people are
  negatives **only while they stay outside the armed restricted area**
  (`expected: 0`) — and an ordinary crossing that produces a kept candidate means
  the armed area is too big.
- **monitored boundary, Camera 1** — entering from the left of the floor. This is an
  **entry review** trial, `expected: 1`. Record it, but understand what the tool can
  answer: a zone says somebody was inside it. Which side of the boundary they came
  from needs an ordered track across frames, and there is none here.
- **area_4 approach, Camera 4** — entering the area_4 area within the configured boundary.
  Also an **entry review** trial, `expected: 1`. Same limit: presence in the zone is
  a candidate for review, not a finding.
- **day and night** — every trial twice, once each. Night is where this is
  expected to be worst, and it is the one most often skipped.
- **animals on cameras 1–3** — a dog, a cat, a bird. They are pinned on sight,
  so a false positive here is **permanent** until somebody deletes it. Record how
  many arrive per hour.
- **camera_4** is person-only and must stay a negative for every animal class.
  An animal event there is refused before anything is kept.

Do **not** stage anything for the camera. If you want to see how the suite
responds to ordinary activity, do ordinary things. There is no classifier here
that a staged performance would tell you anything about.

## Scoring scope

One run = one rule, one model, one configuration, declared in the run notes
alongside the numbers. A result from the armed entry rule and a result from
anything else are not combinable: two rules scored together are one meaningless
number. The report says so at the top of every run.

Precision and recall come out of staged observations of scenes somebody watched.
They are **not deployment precision** — they describe those scenes in that
lighting, with that geometry, on that day.

## Recorded results

None. Recording the first one means filling in a real run's rows and stating the
date, the host, the rule, the geometry version and the lighting split. Until then
this table has no entry, and no document in this repository should imply one.

| Run | Date | Rule | Camera | Lighting | Trials | TP | FP | FN | TN | Precision | Recall | False alerts/camera-hour |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |