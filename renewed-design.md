# Renewed Design — Live Hand / Loop Hand

A rethink of what the two gloves do. This is a spec, not an implementation:
nothing in this branch changes behaviour yet.

## The problem with the current two-hand mapping

The voice hand was built by taking the instrument hand's vocabulary and
pointing it at a second sound source:

```
Instrument hand:  tilt=pitch   roll=filter   yaw=pan   flex=volume
Voice hand:       tilt=pitch   roll=reverb   yaw=pan   flex=volume
```

That is the same instrument twice. It is symmetric, and symmetric is dull to
play — you learn one hand and the second teaches you nothing new. It also
duplicates parameters that should be singular: two pans, two volumes, two
pitches, when the instrument only ever needs one of each.

The test it fails: **could you swap the hands and have it still make sense?**
For that mapping, yes. That is the whole problem.

## The principle

Real two-handed instruments split by **role**, not by target.

- **Violin** — left hand selects pitch (discrete, precise); right hand does
  everything expressive (bow pressure, speed, attack). Neither could do the
  other's job.
- **Theremin** — one hand is pitch, one is volume. Completely different, which
  is exactly why it is playable.
- **Wind instruments** — fingers select, breath shapes.

In none of them do both hands do the same thing to different things.

## The design: one hand makes sound, the other shapes what was made

You sing or play a phrase, capture it, and the second hand sculpts that
recording while the first plays something new over the top. One hand is
*making*, the other is *processing*. They are never doing the same kind of
work.

### Live hand (dominant)

Everything about the note happening right now. This hand never touches an
effect.

| Gesture | Controls |
| --- | --- |
| Tilt | Musical pitch, quantized to the scene's scale |
| Roll | Brightness (filter cutoff) |
| Index bend | Volume / expression |
| Middle bend | Vibrato |
| Fist / Open hand | Sound on / off |
| Point | Next instrument |
| Wrist flick | Drum hit / accent |
| Button hold | **Capture this phrase into the loop** |
| Button tap | Next scene |

### Loop hand (non-dominant)

Everything about time and space. This hand never plays a note.

| Gesture | Controls |
| --- | --- |
| Roll | **Scrub position** — move the playhead through the recording |
| Tilt | Loop speed / pitch |
| Yaw | Where the loop sits in the stereo field |
| Index bend | Blend between live and looped |
| Middle bend | Grain size / vowel character |
| Fist | **Freeze** — hold the current grain, time stops |
| Open hand | Release, let it run |
| Point | Cycle loop mode: normal -> granular -> slices |
| Wrist flick | Fire a slice / stutter |
| Button hold | Overdub another layer |

### Together

| Gesture | Effect | Why it earns a slot |
| --- | --- | --- |
| Relative roll (live − loop) | Stereo width — level is normal, twisting widens or narrows | The only thing two IMUs give that one cannot |
| Both fists | Total freeze | A big moment no continuous axis can express |
| Both open hands | Panic mute | The gesture you want when something feeds back |

Deliberately **not** included: relative tilt and relative yaw. One combination
axis is expressive; three is unplayable, because you would have to hold both
hands' absolute angles steady while varying their difference.

Also not included: "hands apart / together". The BNO08x reports orientation,
not position, so distance between the hands is not measurable. Only relative
rotation is real.

## Why this holds up

There is exactly **one** of each parameter in the instrument, and each belongs
to exactly one hand. One reverb — loop hand. One filter — live hand. One
scrub — loop hand. One pitch — live hand.

Swap the hands now and it stops making sense: scrubbing a recording is
meaningless on the hand that is playing live. That is the test the previous
mapping failed.

## What a performance looks like

1. Sing a phrase. Hold the live hand's button to capture it.
2. The loop hand takes over: roll to scrub through your own voice, fist to
   freeze it mid-syllable.
3. The live hand starts playing the synth over the top.
4. Flick the loop hand to stutter the voice as punctuation.
5. Hold the loop hand's button to overdub a harmony.

Each hand is visibly doing different work, which matters more than it sounds
for a demo — an audience can see the division.

## What already exists

Most of this is moving built features onto a hand that has room for them,
rather than inventing DSP. Already working and tested:

- Granular scrub (`target_scrub`), variable-rate playback (`target_rate`)
- Slice mode, overdub, stutter grab, reverb, delay
- Freeze, approximately — granular mode already stops time

Today these are buried behind keyboard toggles (`g`, `b`, `p`) and crammed
onto the instrument hand's roll, where they compete with the filter for the
same axis.

## What is missing

- **Independent loop volume.** The loop currently mixes at a fixed
  `0.6 + amp`; the blend between live and looped needs its own control.
- **Mode cycling on a posture** rather than a keypress.
- **A real freeze** distinct from granular mode.
- **Relative-roll axis** — needs both hands' frames in one place, which
  `main.py` already has.
- **Per-hand assignment of which glove is which**, so the dominant hand is a
  setting rather than a wiring decision.

## Two problems worth naming

**The loop hand has nothing to do before anything is recorded.** Mitigation:
with no loop, it shapes the live microphone instead — reverb, delay, pitch. It
is always the "space and time" hand; it simply has more to do once material
exists. That degrades gracefully rather than sitting dead.

**It is harder to learn.** Two asymmetric hands means two vocabularies instead
of one applied twice. That is the cost of it being more interesting to play.

## A further idea: let scenes redefine the hands

Six scenes already exist. A scene could switch which *model* is active — one
scene is Live/Loop, another is a simpler symmetric mapping for a quiet
section. Same hardware, different instrument, one button press.

Probably the strongest version of all of this: not one perfect mapping, but a
small set of deliberately different ones you move between mid-performance.
