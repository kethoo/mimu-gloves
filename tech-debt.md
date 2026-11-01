# Technical debt

What the project promised or needs and does not yet have. Kept here so it is
written down somewhere other than a conversation, and so the report's
evaluation section has something to point at.

Last reviewed: 2026-11-01.

---

## 1. The fourth metric is not measured

The proposal set four success criteria. Three are now measured by
`measure.py`, which runs against the real mapping and synthesis code and
needs no glove attached:

| Criterion | Status |
| --- | --- |
| Gesture recognition | **100%** — 90 of 90 attempts, 0 fired unasked |
| Response time | **35.8 ms** worst case (20 + 10 + 5.8) |
| Stability under load | **0 missed deadlines** in 5,167 blocks, 5.5× headroom |
| **Subjective usability** | **outstanding** |

The fourth is the only one that cannot be automated: it needs people who
have not used the instrument before. Everything required to run it already
exists — the protocol is the fifteen-step sequence in the testing guide,
with an expected outcome written against each step.

**What it takes:** three to five testers, twenty minutes each. Record which
steps succeeded without help, where they hesitated, and what they said about
how natural the gesture-to-sound relationship felt. Then fill in §4.2.4 of
the report and update the status in Table 7.

**Blocked by:** the hardware faults below. A tester cannot do steps 10–14
with a dead flex channel.

---

## 2. Two components named in the proposal were never built

### Vibration motor (haptic feedback)

The proposal lists a 1034 DC vibration motor, and an NPN transistor and
diode to drive it. None of the three is used; `grep -i vibrat` over the
firmware returns nothing but the *vibrato* audio effect, which is a
different thing.

This is the one missing item the instrument plays perfectly well without,
so it is last in any order of work.

### Battery and charging

The proposal lists a battery, a charging board and a power switch, and names
the battery in the system architecture. The gloves still run from USB — a
wearable instrument on a cable, which argues against its own premise.

A USB power bank solves it today with no electronics work at all. A LiPo
with an IP5306 module (charge, boost and protection on one board) is the
lighter answer; a bare LiPo cannot be wired to the 3V3 pin, because a full
cell is 4.2 V and the ESP32's maximum is 3.6 V.

---

## 3. Hardware faults, open

| Fault | Effect | Likely cause |
| --- | --- | --- |
| BNO08x does not answer on I2C | that glove sends no orientation; the link drops and reconnects while the firmware retries | measured: VIN 3.3 V, RST 3.15 V, ADD 3.29 V, PS0/PS1 at GND — all correct, yet SDA and SCL sit at ~1 V, which is a powered chip clamping the bus. Suspect the chip itself, after the smoke |
| One flex channel reads 0 | postures unavailable on that hand: fist, open and point all need both fingers | supply to that sensor |
| Status LED dark from boot | no hardware diagnostic on battery | VDD on 5 V instead of 3V3, DIN on DOUT, or the LED died in the same event |

**Fastest way to split the first one:** swap the known-good BNO08x from the
other glove onto this board. If it works, the chip is dead; if it does not,
the fault is in the board or the wiring.

---

## 4. Smaller things

- **The report has no generator any more.** It was built by a script that
  was not kept; `senior-project-report.docx` is now the source and is edited
  directly. Fine, but worth knowing before planning a large restructure.
- **The Gantt chart was never filled in.** The proposal's work-breakdown
  section contains only the instruction text. The report's §4.4, on who did
  what, is still a placeholder and the guideline requires it for group work.
- **Figures 3 to 5 are not drawn** — the control panel, the flex divider's
  usable band, and the spectral similarity result. Figures 1 and 2 are in.
- **Harmony transposes in parallel.** Every voice in the buffer moves by the
  same interval, so on a single note it is a chord and on an already-layered
  take it is parallel chords, which can be muddy. Bounce is the way round
  it. Diatonic harmony per note would need pitch detection, which is the
  level 3 described in the report and was deliberately not attempted.
- **Reverb tails are cut at the loop boundary when bouncing.** Inherent to
  bouncing a loop; a crossfade would soften it.

---

## Order of work

1. **Fix the BNO08x** — nothing can be demonstrated without it.
2. **Run the usability sessions** — the largest gain for the least work, and
   it closes the last of the four criteria.
3. **Battery** — a power bank counts; it removes the cable from the demo.
4. **Vibration motor** — last. It is the only item whose absence costs the
   instrument nothing.
