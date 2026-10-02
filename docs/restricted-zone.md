# The watched areas and arming

`camera_1_restricted` and `camera_4_restricted` are the two restricted areas
used for person monitoring. Camera 1 is manually armed; Camera 4 is always armed. **Neither is in
`config/config.example.yml`**, and the polygons are not in this repository: they are
deployment-specific and have been drawn and validated directly in the running Frigate
instance. The public example stays generic and inactive on purpose, so that a
full-frame placeholder cannot end up armed by accident. Camera 1's is armed by
hand; **Camera 4's area_4 area is always armed** and has no control at all.

Cameras 2 and 3 have no restricted area and never will. Their monitored area is the
whole frame - the monitored area 2 on Camera 2, the area_3 on Camera 3 - so arming one
of them means "keep a person while they are in view" rather than "keep a crossing".
Nothing about that is a judgement of the room.

## Who is armed, and by whom

| Camera | Zone a person must be inside | Armed by | Boundary |
| --- | --- | --- | --- |
| `camera_4` | `camera_4_restricted` (area_4) | **always**, internally | none: no arming moment, so a track already under way is kept |
| `camera_1` | `camera_1_restricted` (monitored area 1) | by hand | from the moment it was armed |
| `camera_2` | `camera_2_area` (monitored area 2, whole frame) | by hand | from the moment it was armed |
| `camera_3` | `camera_3_area` (area_3, whole frame) | by hand | from the moment it was armed |

**Camera 4 is armed from startup and cannot be turned off.** There is no control
for it, no request that writes to it, and no file that can override it: it is not
in the saved state at all, so `{"camera":"camera_4","armed":false}` is refused and
`{"camera":"camera_4","armed":true}` is refused too. Home and Away do not touch it.
It is still gated on Frigate reporting its zone, so a zone that stops being
configured shows it unavailable and pins nothing, and it recovers by itself.

**Home, Away, and one camera at a time.** Home keeps no person snapshot on cameras
1–3. Away arms all three at one moment, in one write. Switching any single camera
yourself overrides a preset, and the status reads *Custom* while they disagree. The
mode is derived from the three flags, never set independently.

**Turning review off is confirmed, and only from Away.** From Away the only changes
on offer are ones that stop keeping people - one camera, or the whole set - so the
page asks first, in the same native dialog and the same classes as the deletion
confirmation: *Disarm Camera 2?* or *Switch to Home?*, naming what stops and that
Camera 4 stays armed. Cancel and Escape send nothing at all and leave every setting
as it was; Confirm sends one request. Arming a camera, and leaving Home, keep more
than they have and are never confirmed. Snapshots already kept are unaffected either
way, and animals stay monitored on cameras 1–3 regardless.

## The commented example

One copy, for `camera_1`. When the zone is drawn for real, it goes in that
camera's `zones:` block alongside `camera_1_area`, and the other camera does the
same with its own name.

```yaml
    zones:
      camera_1_restricted:
        # Person only, and no dwell. loitering_time 0 means an event carries this
        # zone as soon as inertia is met, so crossing the line is the whole event
        # rather than a timer - which is what separates this from the 60 second
        # rule that was removed.
        coordinates: "0,0,1,0,1,1,0,1"   # PLACEHOLDER - replace with the real floor boundary
        inertia: 3
        loitering_time: 0
        objects:
          - person
```

Those coordinates are a **placeholder, not a recommendation**. The full frame is
what `config/config.example.yml` uses for `camera_N_area`, and it is the one
shape that must not reach a restricted zone: a full-frame restricted area arms
every pixel, and every person in the house becomes a candidate.

`review.alerts` stays `enabled: false` on all four cameras. Keeping a person is
now the dashboard's decision, not a native alert, and turning alerts back on
would fire on every person on every frame regardless of any of this.

## Manual arming, and only that

Manual arming is the chosen design for cameras 1–3. Arming and disarming go
through the dashboard's controlled API only - `POST /api/security/entry`, behind
the same Host, Origin, content-type and action-header refusals as the delete. There
is no schedule, no arming from a detector signal, and no command-line route to set
the flag.

- **Your choice is saved, in one small file.** `--state-file`, default
  `/state/entry.json`, in the one writable named volume; the container's root
  filesystem stays read-only. It holds three flags and three timestamps and
  nothing else - no credential, no configuration, no upstream body - under a
  deliberate 4 KiB ceiling, written and flushed beside itself and then renamed
  over it. A restart comes back as you left it, and a fresh installation comes
  back Home.
- **A change is durable before it is believed.** The write lands first and the
  process's own flags move only after it, so a save that fails leaves the running
  state exactly as it was and answers 503 rather than reporting an arming that a
  restart would lose. The previous valid file is never left half-written.
- **A file this cannot read is Home, not Away.** Not JSON, not this shape, a
  version that is not this version, a timestamp that is not a number: it comes back
  with every manual camera off, the dashboard told the setting could not be read,
  and camera 4 still armed. The direction to fall back in is the one that keeps
  nothing.
- **A camera can only be armed once the camera service reports its area** - the
  restricted polygon person-only at `loitering_time: 0` for cameras 1 and 4, and
  person among the labels it already carries at `loitering_time: 0` for the
  full-frame areas on cameras 2 and 3. The worker reads `/api/config` for that and
  keeps four booleans. Away needs all three; one camera needs its own. If the
  config cannot be read, every camera reads *not ready*, arming is refused, and
  **the settings are left alone**: they resume pinning by themselves once the
  config answers, still measured from the moment you armed.
- **Arming reaches forwards, never backwards, on cameras 1–3.** An event that
  started before the arm is not kept, whatever it is. An entry already under way
  when you armed is therefore **missed**, because a track has one start time for
  the whole time somebody was on camera. Catching those needs the
  current-zone-entry signal on MQTT, which is not built. Camera 4 has no arming
  moment, so it has no such boundary.
- **Disarming always works**, including on a camera whose area is not confirmed,
  so a camera can never get stuck armed.
- **Disarming never releases anything.** Records kept under this rule, and the
  whole pile kept under the removed 60 second rule, stay exactly as they are
  until a person deletes them. This worker only ever sets `retain_indefinitely`;
  it has no un-pin path, no sweep and no migration.

## What a kept snapshot is, and is not

A kept person snapshot is a **candidate for a person to look at**: somebody was
inside that camera's watched area during the window it was armed. On camera 4 that
is always. Nothing more.

- **Not a direction.** A zone says where somebody was, not which side of the
  boundary they came from. Camera 1's left-side entry and Camera 4's area_4
  approach are both boundary facts that need an ordered trajectory across frames
  to establish, and no such tracking exists here.
- **Not an identity.** The detector reports one class, `person`. It cannot tell
  family from anyone else, and nothing in this repository attempts to.
- **Not a risk score.** No threat, no intent, no risk.
- **Not a fight or behaviour detector.** The object model here is COCO 91, and it
  has no class for a fight, a scuffle or anything similar: a zone cannot produce
  one. Cameras 2 and 3 are the monitored area 2 and the area_3, internal views whose
  monitored area is the whole frame, so arming one of them keeps **every person in
  view** for as long as it stays armed - presence, not an incident, and a
  deliberate limit of what the zone can say.
- **But a kept snapshot is no longer only ever a zone candidate.** A separate,
  explicitly **experimental** sidecar watches cameras 2 and 3 for
  `possible_fighting` and keeps what it labels, on its own authority and whatever
  the arming above says. That label is **a possibility a model reported about
  sixteen frames, never a finding**, its accuracy on these two rooms is
  **unmeasured**, and it says nothing about direction, identity, intent or a
  weapon. It is a separate system with its own documentation
  ([`fighting.md`](fighting.md)), and arming or disarming above neither starts nor
  stops it. What this file describes is the zone rule only.

## The animals, unchanged

`camera_1` … `camera_3` still track the ten COCO 91 animal classes and still keep
an animal **on sight**, with no arming involved. `camera_4` still tracks
`person` and nothing else, and the worker still refuses an animal event on it
without consulting the camera service. Because that pin is immediate, an animal
false positive is permanent - see [`evaluation.md`](evaluation.md).
