# Demo guide

Everything needed to set up and run the crater-detection orbiter demo, in the
order you'll do it. The [command reference](#8-command-reference) at the end
lists every command and option.

- [Demo day in 5 commands](#demo-day-in-5-commands)
- [1. Before you start: hardware](#1-before-you-start-hardware)
- [2. Connect to the Pi](#2-connect-to-the-pi)
- [3. Get the latest code onto the Pi](#3-get-the-latest-code-onto-the-pi)
- [4. One-time setup](#4-one-time-setup-new-board-position-or-camera)
- [5. Run the demo](#5-run-the-demo)
- [6. Fault injection demo](#6-fault-injection-demo)
- [7. Look at the results and tidy up](#7-look-at-the-results-and-tidy-up)
- [8. Command reference](#8-command-reference)
- [9. Settings in config.toml](#9-settings-in-configtoml)
- [10. Troubleshooting](#10-troubleshooting)

> **Which branch?** Fault injection (`pantilt fi`, `--fault`) is on the
> `fault-injection` branch, not on `main` yet. Everything else is on both.
> Check with `git status -sb`; the first line names the branch.

---

## Demo day in 5 commands

Board and camera already set up, corners and calibration already saved:

```bash
ssh -Y reem@reem-rpi.local
```
```bash
cd ~/main-orbiter && source .venv/bin/activate
```
```bash
python3 -m pantilt check
```
```bash
python3 -m pantilt board-scan --threshold 0.5 --gui
```
```bash
python3 -m pantilt off
```

Add the fault-injection part from [section 6](#6-fault-injection-demo) if
you're showing it.

---

## 1. Before you start: hardware

- [ ] **Servo power:** external 5-6 V supply into the servo driver's **V+**
      terminal. Never from the Pi's 5 V pins.
- [ ] **Servo driver wiring:** SDA to pin 3, SCL to pin 5, VCC to pin 1, GND
      to pin 6. Tilt servo on channel 0, pan on channel 1.
- [ ] **Camera and AI HAT+** ribbons seated.
- [ ] **Board:** prints flat, trimmed, matte if possible, at about 1.5 m from
      the camera. Corners taped.
- [ ] **Light:** bright and even, no glare. Scores drop noticeably in dim or
      fading daylight.
- [ ] **Nothing has moved** since the corners were taught. If the board or the
      camera moved, redo [section 4](#4-one-time-setup-new-board-position-or-camera).

---

## 2. Connect to the Pi

The Pi is `reem-rpi.local`, user `reem`. The project is in `~/main-orbiter`.

**Terminal only:**
```bash
ssh reem@reem-rpi.local
```

**With windows (`--gui`) shown on your Mac:** open **XQuartz** first
(Applications → Utilities), then:
```bash
ssh -Y reem@reem-rpi.local
```
Check it worked: `echo $DISPLAY` should print something like `localhost:10.0`.

**Or** use the Pi's own monitor and keyboard and open a terminal there. That
gives the smoothest video in the windows.

**Every time, after connecting:**
```bash
cd ~/main-orbiter && source .venv/bin/activate
```

**Browser live view:** while a command runs, open
`http://reem-rpi.local:8000/` on the Mac. If the terminal prints
`http://reem-rpi:8000/`, add `.local` yourself; the Mac doesn't know the bare name.

> Always run commands as `python3 -m pantilt ...`, never `python3 pantilt ...`.
> If `pip install -e .` has been run, plain `pantilt ...` works too.

---

## 3. Get the latest code onto the Pi

**On the Mac**, commit and push your changes:
```bash
cd ~/Desktop/main-orbiter-main && git status -sb
```
```bash
git add -A && git commit -m "describe the change" && git push
```

**On the Pi, if it has internet** (`ping -c 2 github.com` works):
```bash
git stash && git pull && git stash pop
```
The stash keeps your local `config.toml` edits (such as `crop = 0.2`).

**First time on a branch the Pi doesn't have yet:**
```bash
git fetch origin fault-injection:fault-injection && git switch fault-injection
```

**If the Pi has no internet,** send the branch straight from the Mac. Git won't
push into the branch the Pi has open, so step off it first.

On the Pi:
```bash
git switch main
```
On the Mac:
```bash
git push reem@reem-rpi.local:main-orbiter fault-injection:fault-injection
```
On the Pi again:
```bash
git switch fault-injection && git log --oneline -1
```

---

## 4. One-time setup (new board position or camera)

Do these in order. Skip any step whose "when" doesn't apply.

**a. Set the crop size** (once). Edit `config.toml`:
```bash
nano config.toml
```
Under `[model]` set `crop = 0.2`. Save with **Ctrl+O**, **Enter**, exit with **Ctrl+X**.

**b. Check the hardware** (every session):
```bash
python3 -m pantilt check
```
Expect `[ok]` for the I2C bus, the PCA9685 and the Hailo.

**c. Teach the board corners** (when the board or camera moved):
```bash
python3 -m pantilt teach --gui
```
Put the green crosshair on the **outer** corner of each outermost print, in
order around the edge. **m** marks, **u** undoes, **g** revisits the marks,
**s** saves. Without a window: `python3 -m pantilt teach`, with the same keys.

**d. Calibrate the servo step** (new camera, lens or servos, or if a command
says the calibration has no direction info):
```bash
python3 -m pantilt calibrate-step
```
Keep the board still and lit. Repeats should agree within a few percent.

**e. Check the plan and the coverage:**
```bash
python3 -m pantilt board-scan --dry-run
```
```bash
python3 -m pantilt board-scan --trace
```
The red boxes should cover the board and not hang off it.

---

## 5. Run the demo

### Live classification (aim by hand)
```bash
python3 -m pantilt live --gui
```
A window shows the camera, the red box the model judges, and the class with its
confidence. Arrow keys aim, **+/-** change the step, **c** centres, **q** quits.

### The board scan (the main demo)
```bash
python3 -m pantilt board-scan --threshold 0.5 --gui
```
The camera walks the whole board, flags points of interest where the crater
score rises, goes back to each one, and photographs the confirmed craters. The
window shows the camera, the stage, the scores and the photo count. **q** stops it.

**Or the whole thing in one script** (teaches and calibrates only if needed):
```bash
./scripts/demo.sh --threshold 0.5
```
```bash
./scripts/demo.sh --teach --threshold 0.5
```

**Useful variations:**

| Want | Add |
|---|---|
| Stricter / looser photos | `--threshold 0.6` / `--threshold 0.4` |
| Brightness frozen for the whole run | `--lock-exposure` |
| Faster, from one wide view | `--wide` |
| Keep scanning until stopped | `--passes 0` |
| Slower, steadier moves | `--speed 20` |
| Name the output folder | `--out data/runs/demo1` |

---

## 6. Fault injection demo

> On the `fault-injection` branch. Faults are random bit flips in the data
> going **into** the Hailo chip (the 8-bit image) or coming **out** of it (the
> raw 8-bit answer). The model itself is never changed. Every frame is also run
> clean, so each fault is compared with the correct answer.

**Sanity check first: 0 bits flipped must change nothing.**
```bash
python3 -m pantilt fi --fault output --fault-bits 0 --no-keys
```
Expect `changed 0.0%`. Ctrl+C to stop.

**Live, aiming by hand, clean vs faulty side by side:**
```bash
python3 -m pantilt fi --fault output --fault-bits 1 --gui
```

**With true accuracy:** aim at a crater, press **L** until the window says
`label now: crater`, and hold still. Before moving the camera, press **L** until
it says `label now: none`. Or start with a label:
```bash
python3 -m pantilt fi --fault output --fault-bits 1 --gui --label crater
```

**Faults in the input image instead:**
```bash
python3 -m pantilt fi --fault input --fault-bits 4 --gui --label crater
```

**Automatic raster scan with faults, in a window:**
```bash
python3 -m pantilt fi --fault output --scan --gui
```

**A whole board scan with faults, in a window:**
```bash
python3 -m pantilt board-scan --fault output --fault-bits 1 --threshold 0.5 --gui
```

**Clean vs faulty board scan, to compare the photos:**
```bash
python3 -m pantilt board-scan --threshold 0.5 --out data/runs/clean
```
```bash
python3 -m pantilt board-scan --threshold 0.5 --fault output --fault-bits 1 --out data/runs/faulty
```

**How much damage per number of bits** (same seed, so it's repeatable):
```bash
python3 -m pantilt fi --fault output --fault-bits 1 --fault-seed 0 --no-keys --fault-log data/fi/out_1bit.csv
```
```bash
python3 -m pantilt fi --fault output --fault-bits 2 --fault-seed 0 --no-keys --fault-log data/fi/out_2bit.csv
```
```bash
python3 -m pantilt fi --fault output --fault-bits 4 --fault-seed 0 --no-keys --fault-log data/fi/out_4bit.csv
```
```bash
python3 -m pantilt fi --fault output --fault-bits 8 --fault-seed 0 --no-keys --fault-log data/fi/out_8bit.csv
```

### Reading the numbers

| Number | Meaning |
|---|---|
| **changed** | Share of frames where the fault gave a different answer from the clean model |
| **agree** | The opposite: accuracy with the clean model as the reference |
| **conf.** | Average change in the clean answer's confidence, in points. Negative = the faults cost confidence |
| **accuracy clean -> faulty** | Only while a label is set: how often each matches the label, and the drop |
| **crater found or lost** | Frames where a fault made a crater appear or disappear |
| **By bit** (in the report) | For each bit position, how often flipping it changed the answer |
| **flipped: spider bit 5 (+0.13)** | This frame's one flipped bit landed in the spider byte, bit 5, raising spider's score by 0.13 |

- One bit per frame means one bit **in total**, chosen at random from all 64
  output bits (8 classes × 8 bits), not one per class.
- High bits matter most: bit 7 moves a score by about 0.50, bit 0 by 0.004.
- If faulty accuracy comes out **higher** than clean, the label didn't match what
  the camera was looking at. Re-run with the label set only while aimed at that
  feature.

When you stop a run (**q** or Ctrl+C), a **FAULT INJECTION REPORT** is printed.
Every frame is in the log: `data/fi/fi-<time>.csv`, or `faults.csv` in a
board-scan run folder.

---

## 7. Look at the results and tidy up

**Each board scan saves to `data/runs/board-<date>-<time>/`:**

| File | What's in it |
|---|---|
| `photos/` | Each crater photo, plus `_crop.png`, the exact square the model judged |
| `overview_p1_0.jpg` | The board with the outline (blue), scan stops (magenta), photos (green), near misses (orange) |
| `candidates.csv` | Every revisit: scan score, revisit score, seconds watched, result |
| `windows.csv` | Every square scored during the scan |
| `faults.csv` | (with `--fault`) every inference, clean vs faulty, and the bits flipped |
| `run.json` | The settings used |

**See the newest run's images on the Mac:**
```bash
cd ~/main-orbiter/data/runs/$(ls -t ~/main-orbiter/data/runs | head -1) && python3 -m http.server 8080
```
Open `http://reem-rpi.local:8080/` on the Mac. Ctrl+C when done, then `cd ~/main-orbiter`.

**Revisit scores of a run:**
```bash
cut -d, -f2,5,6,8,9 data/runs/<run>/candidates.csv
```

**Changed rate by bit position, from the newest fault log:**
```bash
python3 - <<'EOF'
import csv, glob, collections
f = sorted(glob.glob("data/fi/*.csv"))[-1]
by = collections.defaultdict(lambda: [0, 0])
for r in csv.DictReader(open(f)):
    for flip in filter(None, r["flipped"].split(";")):
        bit = int(flip.split(":")[-1])
        by[bit][0] += 1
        by[bit][1] += int(r["changed"])
for b in sorted(by):
    n, c = by[b]
    print(f"bit {b}: {c}/{n} frames changed ({100 * c / n:.1f}%)")
EOF
```

**Delete old photos** (lists first, asks before deleting):
```bash
python3 -m pantilt clean
```
```bash
python3 -m pantilt clean --keep 3
```

**When you're done, release the servos:**
```bash
python3 -m pantilt off
```

---

## 8. Command reference

Every command takes `--help`, e.g. `python3 -m pantilt board-scan --help`. All
commands also accept `--config path/to/config.toml` **before** the command name.

### Hardware and servos

| Command | What it does |
|---|---|
| `python3 -m pantilt check` | Checks the I2C bus, the PCA9685 at 0x40 and the Hailo |
| `python3 -m pantilt center` | Both servos to 90° |
| `python3 -m pantilt goto 100 90` | Pan 100°, tilt 90° |
| `python3 -m pantilt keys` | Arrow-key aiming in the terminal (`--step 5`) |
| `python3 -m pantilt keys --gui` | Arrow-key aiming in a window with the camera |
| `python3 -m pantilt sweep` | Each servo through its range (`--axis pan`, `--axis tilt`, `--release`) |
| `python3 -m pantilt off` | Release both servos |
| `python3 find_bounds.py --preview` | Free movement past the limits, showing angles |
| `python3 scripts/preview.py` | Camera only, in the browser |

### Board setup

| Command | Options |
|---|---|
| `python3 -m pantilt teach` | `--gui`, `--crop 0.2`, `--file data/board_corners.json` |
| `python3 -m pantilt calibrate-step` | `--crop`, `--delta 5`, `--reps 3`, `--file data/board_step.json` |
| `python3 -m pantilt board-scan --dry-run` | Plan only |
| `python3 -m pantilt board-scan --trace` | Walk every stop (`--hold 1`) |
| `python3 -m pantilt board-scan --trace-edges` | Only the 4 outer stops (`--hold 3`) |

### Live classification

`python3 -m pantilt live` with:

| Option | Meaning |
|---|---|
| `--gui` | Window with camera and buttons |
| `--no-keys` | No keyboard; print one line per second |
| `--scan` | Raster-scan instead of aiming (`--pan 90 120 --tilt 80 100 --cols 5 --rows 3`); add `--gui` to watch it |
| `--crop 0.2` | Square size |
| `--step 2` | Degrees per key press |
| `--log data/live.csv` | Save predictions with pan/tilt |
| `--hef models/mars_landmark.hef` | Model file |
| `--no-preview` / `--port 8001` | Browser live view off / on another port |

### Board scan

`python3 -m pantilt board-scan` with:

| Option | Meaning |
|---|---|
| `--threshold 0.5` | Crater score needed for a photo, after going back |
| `--gui` | Watch the scan in a window; **q** stops it |
| `--crop 0.2` | Square size |
| `--wide` | Find from one wide view instead of walking the board |
| `--grid` | Older scan: one look per square, no revisits (`--one-way`) |
| `--lock-exposure` | Freeze exposure and white balance for the run |
| `--passes 2` | Times over the board (`0` = until Ctrl+C) |
| `--speed 20` | Degrees per second between stops |
| `--max-photos 20` | Photo limit per run |
| `--cooldown-tiles 0.5` / `--cooldown-s 3` | No photo within half a square / 3 s of the last |
| `--out data/runs/demo1` | Output folder |
| `--corners FILE` / `--hef FILE` | Other corners file / model |
| `--no-preview` / `--port 8001` | Browser live view off / on another port |
| `--dry-run` / `--trace` / `--trace-edges` | See [board setup](#board-setup) |
| `--fault input/output --fault-bits K --fault-seed N` | Fault injection during the scan (fault-injection branch) |

**Demo script:** `./scripts/demo.sh [--teach] [any board-scan options]`

### Fault injection (fault-injection branch)

`python3 -m pantilt fi --fault input|output` with any `live` option above, plus:

| Option | Meaning |
|---|---|
| `--fault input` / `output` | Flip bits in the 8-bit image going in, or the 8-bit answer coming out (required) |
| `--fault-bits K` | Random bits flipped per frame (default 1; `0` = clean run) |
| `--fault-seed N` | Seed: same seed, same flips (default 0) |
| `--label crater` | What the camera is really looking at, for true accuracy. **L** cycles it |
| `--fault-log data/fi/name.csv` | Where to save the log |

Labels: `other`, `crater`, `"dark dune"`, `"slope streak"`, `"bright dune"`,
`"impact ejecta"`, `"swiss cheese"`, `spider` (quote the two-word ones).

### Results

| Command | What it does |
|---|---|
| `python3 -m pantilt clean` | List runs and photo counts, delete nothing |
| `python3 -m pantilt clean --keep 3` | Delete photos from all but the newest 3 runs |
| `python3 -m pantilt clean --all` | Delete photos from every run |
| `python3 -m pantilt clean --run NAME --everything` | Delete one run completely |

`clean` also takes `--yes` (don't ask) and `--runs-dir data/runs`.

### Older tools

| Command | What it does |
|---|---|
| `python3 -m pantilt scan --pan 90 120 --tilt 80 100 --cols 5 --rows 3 --capture scans/run1` | Fixed-angle grid of photos |
| `python3 -m pantilt measure-k --separation 1000` | Measure mm per pixel from two marks |
| `python3 -m pantilt survey --dry-run` | Survey using measured board geometry |

### Tests (any computer, no hardware)

```bash
pip install -e ".[dev]"
```
```bash
python3 -m pytest
```

---

## 9. Settings in config.toml

| Section | Setting | Meaning |
|---|---|---|
| `[model]` | `crop` | Square size the model judges (we use **0.2**) |
| `[model]` | `smooth_frames` | Frames averaged in `live` |
| `[camera]` | `rotate_180` | Flip the picture if the camera is upside down |
| `[camera]` | `preview_width` / `preview_height` | Camera picture size (1280 × 720) |
| `[sweep]` | `threshold` | Default photo threshold |
| `[sweep]` | `look_threshold` | Scan score that makes a spot worth going back to (0.15) |
| `[sweep]` | `watch_min_s` / `watch_max_s` / `revisit_watch_max_s` | How long to classify at each stop / while the score climbs / on a revisit |
| `[sweep]` | `find_frames` / `confirm_frames` | Frames averaged per score at a stop / on a revisit |
| `[sweep]` | `edge_margin` | How far a square may hang off the taught outline |
| `[sweep]` | `overlap` / `max_gap` | How the scan squares tile the board |
| `[sweep]` | `speed_dps` / `settle_s` / `settle_max_s` | Move speed and settling time |
| `[sweep]` | `max_photos` / `cooldown_tiles` / `cooldown_s` | Photo limits |

---

## 10. Troubleshooting

| You see | Fix |
|---|---|
| `attempted relative import with no known parent package` | Missing `-m`: use `python3 -m pantilt ...` |
| `pantilt: command not found` | Use `python3 -m pantilt ...`, or run `pip install -e .` once |
| `fatal: invalid reference: <branch>` | Check the spelling; if the Pi doesn't have the branch yet, see [section 3](#3-get-the-latest-code-onto-the-pi) |
| `Could not resolve hostname github.com` | The Pi has no internet: push from the Mac ([section 3](#3-get-the-latest-code-onto-the-pi)), or connect the Pi to Wi-Fi / Mac Internet Sharing |
| `Your local changes ... would be overwritten` | `git stash`, then repeat the command, then `git stash pop` |
| `can't open a window (no display...)` | Use `ssh -Y` with XQuartz open, the Pi's own screen, or prefix `DISPLAY=:0` to show it on the Pi's monitor |
| `tkinter is missing` | `sudo apt install python3-tk` (needs internet) |
| Browser page won't load at `http://reem-rpi:8000/` | Use `http://reem-rpi.local:8000/` |
| `No route to host` from the Mac | The Pi's address changed: use `reem-rpi.local` instead of an IP |
| `Couldn't start the camera` / camera busy | Close any other `pantilt`, `rpicam` or preview program |
| `no board corners` | Run `python3 -m pantilt teach` |
| `no calibration` / `no direction info` | Run `python3 -m pantilt calibrate-step` |
| No photos saved | Look at `overview_p1_0.jpg` and `candidates.csv`: near misses at about 0.5 mean lower `--threshold`; no points of interest mean check the light and the crop |
| Scores lower than earlier in the day | Lighting changed: add a lamp, or try `--lock-exposure` |
| Craters near the edge missed | Re-teach on the outer corners of the prints |
| Faulty accuracy higher than clean | The label didn't match the view: set the label only while aimed at that feature |
| Dates in file names are wrong | The Pi's clock is off without internet; it fixes itself once online |
| Servos buzz or the Pi reboots when they move | Servo power must come from the external V+ supply, not the Pi |
