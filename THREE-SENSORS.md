# Three sensors

This branch runs the instrument with **three flex sensors instead of four**:
both on the left hand, one on the right.

It exists because the right glove already has a dead flex channel, and
because a build with one fewer sensor is cheaper, simpler to sew and has one
less thing to fail. `main` assumes four and loses all three postures on a
hand the moment one channel goes; this branch degrades instead.

Reproduce every number here with `python measure.py`.

---

## What changes on the one-sensor hand

| | Four sensors | Three |
| --- | --- | --- |
| Fist → freeze | ✅ | ✅ |
| Open → release | ✅ | ✅ |
| **Point → cycle loop mode** | ✅ | ❌ **gone** |
| Index bend → live/loop blend | ✅ | ✅ if that is the one fitted |
| Middle bend → reverb | ✅ | falls back to the preset's value |

Point cannot survive: it *is* the two fingers disagreeing, so with one finger
there is nothing to compare. Cycling the loop mode moves to the panel or the
keyboard — `g`, `b`, `h` — and the six effect presets still cover reverb from
the glove's own button.

Everything the IMU drives is untouched: scrub, rate, stereo placement, delay
and the wrist flick all work exactly as before.

---

## The difficulty, and what it cost to solve

With two sensors a fist needs **both** fingers curled, so a performer
sweeping the index as a volume or blend control can never fire one. With one
sensor that protection is gone: the same finger is the control *and* the
posture, and nothing distinguishes them except how far it bends and whether
it stays there.

Two numbers do the separating:

```python
POSTURE_SOLO_HI   = 0.85   # bent past where the control range is useful
POSTURE_HOLD_SOLO = {"fist": 0.90, "open": 1.00}   # and held
```

A first attempt put the threshold at 0.90 and detected **0 deliberate fists
out of 30**. The reason was not the threshold itself but the dwell: a finger
held at 0.97 with the measured sensor noise dips below 0.90 about one frame
in eight, and every dip restarted the timer, so the hold never accumulated.

The fix is a five-frame majority vote on the reading — 50 ms, shorter than
any gesture and longer than any noise spike — and a threshold of 0.85, which
is the highest value that still detects reliably. Both were chosen by
measuring, not by taste:

```
thresh  dwell  vote   fist ok  sweep FP
  0.90   0.90     5     18/30      0/30
  0.85   0.90     5     30/30      0/30     <- chosen
  0.80   0.90     5     30/30      0/30
  0.75   0.90     5     30/30      0/30
```

---

## Measured result

```
case                         fires    wanted
deliberate fist              30/30      fist
deliberate open              30/30      open
sweeping the control          0/30   nothing
parked at maximum            30/30   nothing   <- the limitation
```

**Parking a control at maximum reads as a fist.** Holding the blend at full
for about a second freezes the loop. That is the honest price of one finger
doing two jobs, and no choice of threshold removes it: the gesture and the
control occupy the same end of the same range.

It is survivable — maximum blend is a place you pass through more often than
one you park at — but it is the reason the fourth sensor is worth fitting
wherever there is room.

The four-sensor path is unchanged and still measures 90 of 90.

---

## This build

The right glove's one sensor is on the **middle finger** and measures
**7.9k straight, 8.8k bent**. The left glove keeps both of its sensors.

| Right hand | |
| --- | --- |
| Middle bend | reverb |
| Blend | fixed at 0.6, since the index has no sensor |
| Bent fully and held | fist: freeze |
| Straight and held | open: release |
| Change loop mode | `g`, `b`, `h` on the keyboard or panel |

Through the 15k leg of the divider that resistance range is 2682 to 2581
counts, a swing of about 101. That is too small to wait for the ordinary
self-calibration, which wants 80 counts before it believes a finger, so the
channel starts from the known range and needs no bending first.

It does not depend on which pin the sensor landed on: half a second after
connecting it takes the pin that reads like a sensor and reports it as the
middle finger. If the reading disagrees with 7.9-8.8k it says so, in ohms, and
learns the range from the finger instead; `c` does the same on request. A
reading stuck near 4095 means the 15k resistor to ground is missing or open,
and nothing in software can make that finger bend.

The panel draws this: only the middle finger curls, Point is gone from that
hand, and a badge shows what the sensor measures right now.

`--loop-flex-both` puts the right glove back on two sensors.

---

## Merging

Nothing here is specific to three sensors. The posture step now handles a
missing channel wherever one appears, so the same code is correct for a
four-sensor glove whose sensor has died — which is the state the right glove
is in today. It is a candidate for `main` on that basis alone.

It also removes a duplicate: both hands ran their own copy of the posture
state machine, and they now share `mapping.step_posture`.
