# The restricted area and arming

`camera_1_restricted` and `camera_4_restricted` are the two areas somebody can
switch entry review on for, by hand, on the dashboard. **They are not in
`config/config.example.yml`**, and the polygons are not in this repository: they
are deployment-specific and have been drawn and validated directly in the running
Frigate instance. The public example stays generic and inactive on purpose, so
that a full-frame placeholder cannot end up armed by accident.

## The commented example

One copy, for `camera_1`. When the zone is drawn for real, it goes in that
camera's `zones:` block alongside `camera_1_area`, and the other camera does the
same with its own name.

```yaml
    zones:
      camera_1_restricted:
        # Person only, and no dwell. loitering_time 0 means an event carries this
        # zone as soon as inertia is met, so crossing the line is the whole event
        # rather than a timer — which is what separates this from the 60 second
        # rule that was removed.
        coordinates: "0,0,1,0,1,1,0,1"   # PLACEHOLDER — replace with the real floor boundary
        inertia: 3
        loitering_time: 0
        objects:
          - person
```

Those coordinates are a **placeholder, not a recommendation**. The full frame is
what `config/config.example.yml` uses for `camera_N_area`, and it is the one
shape that must not reach a restricted zone: a full-frame restricted area arms
every pixel, and every person in the house becomes a candidate.

`review.alerts` stays `enabled: false` on all four cameras. Retaining a person
is now the dashboard's decision, not a native alert, and turning alerts back on
would fire on every person on every frame regardless of any of this.

## Manual arming, and only that

Manual arming is the chosen design. Arming and disarming go through the
dashboard's controlled API only — `POST /api/security/entry`, behind the same Host,
Origin, content-type and action-header refusals as the delete. There is no
schedule, no arming from a detector signal, and no command-line route to set the
flag. The dashboard gateway runs with `--serve`; other worker modes start
disarmed.

- **Arming is in memory only.** A restart of the retention service disarms both
  cameras, and has to be armed again by hand. That is the intended direction to
  forget in.
- **A camera can only be armed once Frigate reports its zone** as person-only at
  `loitering_time: 0`. The worker reads Frigate's own `/api/config` for that and
  keeps only two booleans. If the config cannot be read, both cameras read *not
  ready* and the Arm button is disabled — turning something on is never allowed
  on the strength of a failed check.
- **Arming reaches forwards, never backwards.** An event that started before the
  arm is not kept, whatever it is. An entry that was already under way when you
  armed is therefore **missed**, because a track has one start time for the whole
  time somebody was on camera. Catching those needs a current-zone-entry signal
  on MQTT, which is not built.
- **Disarming always works**, including on a camera that has stopped being
  ready, so a camera can never get stuck armed.
- **Disarming never releases anything.** Records kept under this rule, and the
  whole pile kept under the removed 60 second rule, stay exactly as they are
  until a person deletes them. This worker only ever sets `retain_indefinitely`;
  it has no un-pin path, no sweep and no migration.

## What a kept entry is, and is not

A kept entry is a **candidate for a person to look at**: somebody was inside
that area while the camera was armed. Nothing more.

- **Not a direction.** A zone says where somebody was, not which side of the
  boundary they came from. Camera 1's left-side entry and Camera 4's area_4
  approach are both boundary facts that need an ordered trajectory across frames
  to establish, and no such tracking exists here.
- **Not an identity.** The detector reports one class, `person`. It cannot tell
  family from anyone else, and nothing in this repository attempts to.
- **Not a risk score.** No threat, no intent, no risk.
- **Not a fight or behaviour detector.** There is no such model here. The object
  model is COCO 91, and it has no class for a fight, a scuffle or anything
  similar. Camera 2 and Camera 3 — the internal views — are deliberately
  given no restricted area and no automatic person retention at all, and nothing
  about their contents is assessed by anything in this repository.

## The animals, unchanged

`camera_1` … `camera_3` still track the ten COCO 91 animal classes and still pin
an animal **on sight**, with no arming involved. `camera_4` still tracks
`person` and nothing else, and the worker still refuses an animal event on it
without consulting Frigate. Because that pin is immediate, an animal false
positive is permanent — see [`evaluation.md`](evaluation.md).
