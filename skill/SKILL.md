---
name: reel-edit
description: Compose a short vertical reel in DaVinci Resolve from derushed car-shoot selects and a music track - find the beats, choose which select plays where and for how long, and build a timeline with the music on A1 and every cut on the beat. Use after the derush skill, when the user asks to edit, cut, assemble or compose a reel / short / edit to music from the selects, or to sync clips to a track or to the beat in Resolve.
---

# Compose a reel on the beat, in DaVinci Resolve

You turn the selects of the derush step and one music track into a first cut: a Resolve timeline
where the music sits on A1 and each shot starts on a beat. Nothing is rendered. The editor takes
over from a timeline that already plays in time.

Division of labour:

| Job | Tool | Why |
|---|---|---|
| What footage exists | `selects` (reads the derush `segments.json`) | The derush labels, scale and stars are your catalogue |
| Where the beats are | `audio` (ffmpeg + a beat tracker) | You cannot hear; it gives tempo, beats, bars, the drop |
| What a good reel looks like | `playbook` | Rules and cut orders taken from nine reference reels and the shot plan |
| Which moment of a select | You, reading derush `sheet` zooms | A 1 s shot out of a 15 s select: only looking finds the second worth showing |
| Frame arithmetic, validation | `check` | Beats to frames, source ranges against the rejects |
| The deliverable | `apply`, then `verify` and `board` | Places every clip, measures every cut against the music, shows you the cuts |

This skill works in **the derush work dir** (the one holding `inventory.json` and `segments.json`)
and keeps its files under `<work>/reel/`. Define both helpers once per shell call:

```
R() { python3 <skill dir>/scripts/reel.py --work <absolute derush work dir> "$@"; }
D() { python3 <derush skill dir>/scripts/derush.py --work <absolute derush work dir> "$@"; }
```

Two kinds of position, never mixed:
- **On the music**: a beat number as `audio` prints it (`16`, `16.5` for the half beat), or seconds
  of the track as a string (`"7.52s"`). Prefer beat numbers: they stay right if the tempo is refined.
- **In the rushes**: 0-based source frame offsets (`f7040`), exactly as in derush.

## Before you start

- The derush step is done: `segments.json` passes derush `check`. If not, run the derush skill first.
- You need the music file. If the user did not give one, ask for it; do not pick a track yourself.
- Resolve is open on the same project (needed from step 7 on). `ffmpeg` on PATH. No Python packages.
- Unless the user says otherwise: one edit of 12 to 25 s, vertical 1080x1920 with the picture
  letterboxed (`--format letterbox`). State these choices in your report.

## Step 1 - Know the footage

```
R selects              # --min-rating 2 to see only the solid ones
```

Note what the footage can and cannot do: which scales exist, which selects hold an event (corner,
pass-by, pull-out), which are the 3-star ones. A reel needs contrast, so a catalogue that is all
`wide` front tracking is the first thing to tell the user about.

## Step 2 - Know the music

```
R audio /path/to/track.mp3
```

Read the whole output. One line per bar: the beat numbers, the time, how loud the bar is, and how
hard each beat (`hits`) and each half beat (`halves`) is struck, 0 to 9.

| It prints | Do |
|---|---|
| `steady grid` | Trust the beat numbers |
| `tempo drifts` | Fine for live music; beats follow the playing. Say so in the report |
| BPM under 80 or over 170, or `halves` as strong as `hits` everywhere | The tempo may be half or double. Either grid still lands cuts on the music; use `.5` positions or re-run with `--bpm` doubled or halved |
| `beat position: UNSURE` or `taken from the bass` | Go on, and ask the user in the report to check the beat markers against the kicks (step 8) |
| Bar lines `not reliable` / `a guess` | Cut on beats anyway; do not reason about "bar starts". If the user names a bar start, `--downbeat BEAT` |
| `<- DROP` | The bar where the track gets loud. Build the edit around it |

Choose the excerpt (`audio.from` and `audio.to`, both on a beat):
- Track already reel-length (under 30 s): use it whole, `"from": "0s", "to": "end"`.
- Longer track with a drop: start 1 or 2 bars before the drop, so the hook plays over the build and
  the first hard cut lands on the drop. End on a bar line 12 to 25 s later.
- No drop: start on a bar line where a loud passage begins.

Work out the beat length once (`60 / BPM` seconds) and from it the slot sizes you will use:

| Slot | Target | At 128 BPM (0.47 s a beat) | At 90 BPM (0.67 s) |
|---|---|---|---|
| shot | 1 to 1.5 s | 2 or 3 beats | 2 beats |
| insert | 0.3 to 1 s | 1 beat, or half | 1 beat, or half |
| hook, hold | 2 to 6 s | 4 to 8 beats | 4 to 8 beats |

## Step 3 - Choose the shape

```
R playbook             # --shots lists the whole shot plan, --refresh re-reads it from its web page
```

It prints what the nine reference reels share, the three cuts planned for the shoot (slot by slot)
and each reference's pace and ideas. The planned cuts are written in shot-plan ids (`m-wipe`,
`d-light`); the selects are written in derush words (scale, label, tags). Matching them is your job.

1. For each planned cut, go down its slots and name the select that shows that shot. Read labels
   first; when a label could fit but you are not sure, look (step 4).
2. If the user asked for a cut by name, use it. Otherwise take the cut with the most slots filled.
3. A slot with no matching select is dropped or replaced by the nearest thing (same scale, same
   kind of movement). Never fill a slot with a select that merely exists.
4. If under half the slots of every cut can be filled (the rushes come from another kind of day),
   do not force a planned cut. Build the edit from the rules alone, in this order:
   hook, the strongest select on the drop, then shots that alternate scale with one or two inserts,
   the best moving select returning once or twice, a closer: the whole car driving away, or failing
   that the strongest close image, held. Say in the report that no planned cut fitted and why.

The rules, and what they mean when you place selects:

| Rule from the references | In practice |
|---|---|
| Shots last 1 to 1.5 s, inserts 0.3 to 1 s | Use the slot sizes of step 2. A long select is a quarry, not a shot: take its best second |
| The first 2 s are never a plain car shot | Slot 1 has role `hook`: an event, a move, the car arriving or filling the lens. Not a steady tracking shot |
| The best moving clip plays two or three times | Reuse your strongest select at different moments, with other shots between |
| Cuts hide in motion | End a shot while something moves fast (background streaming past, a turn, a whip, a flare); start the next one already moving. Never get that motion from the car sliding out of the frame |
| Framed 16:9, then letterboxed or rotated | `--format letterbox` (default) or `--format rotate` |

## Step 4 - Choose the moment inside each select

**The car is whole and inside the frame in every frame of every slot.** This comes before every
other rule and holds for the hook, the inserts and the closer alike. A shot lasts one second: a car
cut by the frame edge, hidden behind something, or sliding out reads as a mistake, however good the
movement looked on the sheet. A pass-by where the car fills the lens and then leaves is a shot of a
car leaving the frame; use only the part before it touches an edge. The one exception is a select
derush scaled `detail`, where showing a part of the car is the point.

For every select you plan to use, look before you write a frame number:

```
D sheet CLIP --ranges "7000-7180:20,8620-9120:50"     # letters tell the windows apart; 16 cells a sheet
```

- A step of 20 to 50 frames is enough to see how the shot evolves. Pick the source frame where your
  slot starts so that the slot ends on movement, not on a settled frame.
- Then zoom on the exact window you picked at a step of 5 to 10 frames and check the car against
  all four edges in every cell. When it is close to an edge, move the window; do not hope.
- Count what the slot consumes: `slot seconds x clip fps x speed`. It must stay inside the select;
  `check` refuses any frame of a derush reject.
- When one instant must fall on a beat (the car passes the lens, a headlight flashes, the camera
  whips), use `sync` instead of `src`: give that source frame and the beat it belongs on.
- A derush `maybe` is usable only when the frames your slot shows are free of its flaw. If the flaw
  is the car itself (cut by the frame, hidden, leaving) or runs through the whole segment, do not
  use it at all. `check` reminds you of the flaw; say in the report which maybes you used and why
  the frames shown are clean.
- Derush notes name the seconds where something goes wrong inside a keep ("clipped at 235-237s").
  `check` warns when a slot shows such a moment. Treat that warning as a stop: look, then move.

## Step 5 - Write `<work>/reel/<name>.json`

`<name>` is yours (`autumn-duo`, `edit`); every later command takes it as argument.

```json
{
  "title": "Autumn duo",
  "audio": { "file": "/path/to/track.mp3", "from": 8, "to": 48, "fadeOut": 0.5 },
  "slots": [
    { "at": 8,  "role": "hook",   "clip": "PANA2464.MOV", "src": 8700,
      "label": "BMW closes in until it fills the frame", "note": "Two bars of build." },
    { "at": 16, "role": "shot",   "clip": "PANA2464.MOV", "src": 7040,
      "label": "Tight corner, chevrons whip past", "note": "First hard cut, on the drop." },
    { "at": 18, "role": "shot",   "clip": "PANA2464.MOV", "sync": { "frame": 3350, "at": 19 },
      "label": "Junction, passes the obelisk" },
    { "at": 20, "role": "insert", "clip": "PANA2464.MOV", "src": 8960, "speed": 50,
      "label": "Close, headlights, slowed" }
  ]
}
```

- A slot runs from its `at` to the next slot's `at`; the last one runs to `audio.to`. The first
  `at` equals `audio.from`.
- `role`: `hook`, `shot`, `insert`, `hold` (a long locked frame that leaves room for a text overlay),
  `closer`. It sets the length `check` expects and the marker colour.
- `src` is the first source frame shown. `sync` replaces it: `frame` is shown exactly at `at`.
- Optional per slot: `speed` in percent (50 = half speed; only slow down footage shot at a higher
  frame rate than the timeline, or accept stutter), `zoom` (1.2 = punch in 20 %), `pan`, `tilt`,
  `repeat: true` (you mean to show the same frames twice), `note` (why this shot is here).
- `label` under 60 characters, written like a derush label. Avoid `/ \ : * ? " < > | [ ]`.
- `audio.fadeOut` in seconds, optional.

## Step 6 - Check

```
R check NAME
```

Prints the cut list (frame, length, select, source range per slot), the pace, then notes, warnings
and errors. Fix every ERROR. For every WARNING, either fix it or be able to say in the report why it
stands. "The movement is good" is not a reason to keep a slot where the car is cut or leaving. Compare the `pace` line with the reference you are following (`R playbook`).

Done when: `check: OK`.

## Step 7 - Apply to Resolve

```
R apply NAME                       # --format letterbox | rotate | project
```

This creates, without touching anything else in the project:
- a `Reel` bin holding the imported track and the timeline `<project> - Reel - <title>`;
- A1 `Music`: the excerpt, starting on a whole frame;
- V1 `Reel`: one clip per slot, picture only, named `03 b18 SHOT · label`, coloured by scale like
  the derush Selects, with one timeline marker per cut (red hook, blue shot, pink insert, yellow
  hold, green closer) whose note names the select and source range;
- beat markers on the music clip itself (red = bar line, cream = beat), so the editor can re-time
  a cut by snapping to them.

If the timeline exists, a new one is made with a `v2` suffix. Use `--replace` only to redo your own
previous apply of the same edit, or when the user says the old one can go.

## Step 8 - Verify, then look at the cuts

```
R verify NAME
R board NAME
```

`verify` reads the timeline back and prints, per cut, how many milliseconds it sits from its beat.
Done when: `verify: OK` (every cut within half a frame).

`board` renders the first, middle and last frame of every slot from the built timeline. Read every
sheet as a film strip; each `OUT` cell followed by the next `IN` cell is one cut. Look for:

| You see | Do |
|---|---|
| The car cut by a frame edge, hidden, or out of frame, in any cell | Wrong slot, no exceptions. Move the window to frames where the car is whole, or use another select |
| Two neighbours that look alike (same scale, same angle, same place) | Swap one for a select of another scale, or move one elsewhere |
| A shot that ends settled while its neighbour is all movement | Move its `src` so it ends on the movement |
| A flaw the derush note warned about, visible in the slot | Move the window, or use another select |
| The hook reads as an ordinary shot | Choose another hook |

Change the JSON, then `check`, `apply --replace`, `verify`, and `board` again for the slots you
changed. Two rounds are usually enough; the editor will refine by hand.

## Report to the user

Briefly: the timeline name and where it is (`Reel` bin); track, excerpt, BPM and whether the beat
position was sure; which planned cut you followed, or that none fitted; the cut list in one line per
slot; the judgement calls (maybes used, warnings left standing, slots dropped for lack of footage).

Always ask them to play the first bars and check that the red and cream markers on the music clip
sit on the kicks. If they sit between them, the fix is `R audio FILE --shift 0.5` followed by
`check`, `apply --replace`, `verify`.

Say what is left for them, because this skill does not do it: transitions (whip blurs, flashes),
speed ramps, sound effects and engine sound, colour, titles.
