"""System-level fault injection around the Hailo-8: the model on the chip is untouched.

Faults hit the data going in to or coming out of the chip, as in hailo_sysfi.py:

  input   flip k random bits in the 8-bit 224 x 224 x 3 frame sent to the chip
  output  flip k random bits in the chip's raw 8-bit output (one byte per class)

Every inference is also run clean (input) or read clean (output) from the same
frame, so each row of the log compares the faulty answer with the golden one.

    pantilt fi --fault output --fault-bits 1          # live: clean vs faulty side by side
    pantilt board-scan --fault input --fault-bits 4   # a whole scan with faults
"""

from __future__ import annotations

import csv
import threading
import time
from pathlib import Path

import numpy as np

from .classify import CLASS_NAMES

MODES = ("input", "output")


def flip_bits(arr: np.ndarray, k: int, rng: np.random.Generator):
    """Copy of a uint8 array with k distinct random bits flipped, and where they were.

    Returns (corrupted copy, [(byte index into the flattened array, bit 0-7), ...]).
    """
    a = np.array(arr, dtype=np.uint8, copy=True)
    flat = a.reshape(-1)
    k = min(k, flat.size * 8)
    flips = []
    for p in rng.choice(flat.size * 8, size=k, replace=False):
        byte, bit = int(p) // 8, int(p) % 8
        flat[byte] ^= np.uint8(1 << bit)
        flips.append((byte, bit))
    return a, flips


def describe_flips(flips, shape, mode: str) -> str:
    """Readable positions: output 'crater:7', input 'y,x,c:bit' (batch axis dropped)."""
    parts = []
    for byte, bit in flips:
        if mode == "output":
            name = CLASS_NAMES[byte] if byte < len(CLASS_NAMES) else str(byte)
            parts.append(f"{name}:{bit}")
        else:
            idx = np.unravel_index(byte, shape)
            parts.append(",".join(str(int(i)) for i in idx[-3:]) + f":{bit}")
    return ";".join(parts)


class FaultLog:
    """One CSV row per faulty inference: golden vs faulty, and the exact bits flipped."""

    FIELDS = ["time", "frame", "mode", "bits", "golden", "pred", "changed",
              "golden_p_target", "pred_p_target", "target_changed", "flipped", "pan", "tilt",
              "stage"]

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._f = open(self.path, "w", newline="")
        self._w = csv.DictWriter(self._f, fieldnames=self.FIELDS, extrasaction="ignore")
        self._w.writeheader()
        self._lock = threading.Lock()
        self._rows = 0

    def write(self, row: dict) -> None:
        with self._lock:
            self._w.writerow(row)
            self._rows += 1
            if self._rows % 50 == 0:
                self._f.flush()

    def close(self) -> None:
        with self._lock:
            if not self._f.closed:
                self._f.close()


class FaultInjector:
    """Wraps a raw-output HailoClassifier; its classify() returns the faulty answer.

    clf needs infer_raw(x) -> uint8 output bytes, and dequant(q) -> probabilities
    (HailoClassifier(..., raw_output=True) has both). context() may return extra
    columns for the log, e.g. pan/tilt or the scan stage.
    """

    def __init__(self, clf, mode: str, bits: int, seed: int = 0, target: str = "crater",
                 log=None, context=None):
        if mode not in MODES:
            raise ValueError(f"fault mode must be one of {', '.join(MODES)}")
        if bits < 0:
            raise ValueError("fault bits must be >= 0")
        self.clf = clf
        self.mode = mode
        self.bits = bits
        self.seed = seed
        self.rng = np.random.default_rng(seed)
        self.target_i = CLASS_NAMES.index(target)
        self.log = log
        self.context = context
        self.frames = 0
        self.changed = 0
        self.target_changed = 0
        self.last: dict | None = None
        self.last_clean: np.ndarray | None = None

    def classify(self, x: np.ndarray) -> np.ndarray:
        """Golden and faulty inference of the same frame; returns the faulty probabilities."""
        golden_q = self.clf.infer_raw(x)
        if self.mode == "input":
            xf, flips = flip_bits(x, self.bits, self.rng)
            faulty_q = self.clf.infer_raw(xf)
            where = describe_flips(flips, np.shape(x), "input")
        else:
            faulty_q, flips = flip_bits(golden_q, self.bits, self.rng)
            where = describe_flips(flips, np.shape(golden_q), "output")
        golden = self.clf.dequant(golden_q)
        faulty = self.clf.dequant(faulty_q)
        self._record(golden, faulty, where)
        return faulty

    def _record(self, golden, faulty, where: str) -> None:
        g, f = int(np.argmax(golden)), int(np.argmax(faulty))
        t = self.target_i
        changed = g != f
        target_changed = (g == t) != (f == t)  # target found or lost because of the fault
        self.frames += 1
        self.changed += changed
        self.target_changed += target_changed
        self.last_clean = golden
        row = {
            "time": f"{time.time():.3f}", "frame": self.frames, "mode": self.mode,
            "bits": self.bits, "golden": CLASS_NAMES[g], "pred": CLASS_NAMES[f],
            "changed": int(changed), "golden_p_target": f"{golden[t]:.4f}",
            "pred_p_target": f"{faulty[t]:.4f}", "target_changed": int(target_changed),
            "flipped": where,
        }
        if self.context:
            row.update(self.context())
        self.last = row
        if self.log:
            self.log(row)

    @property
    def change_rate(self) -> float:
        return self.changed / self.frames if self.frames else 0.0

    def summary(self) -> str:
        if not self.frames:
            return f"faults: {self.mode}, {self.bits} bit(s) per inference - nothing classified"
        return (f"faults: {self.mode}, {self.bits} bit(s) per inference, seed {self.seed} | "
                f"{self.frames} inferences, answer changed on {100 * self.change_rate:.2f}%, "
                f"{CLASS_NAMES[self.target_i]} found/lost by a fault on "
                f"{100 * self.target_changed / self.frames:.2f}%")

    def status(self) -> dict:
        """Fields for the browser live view."""
        d = {"fault": f"{self.mode} x{self.bits}", "fi frames": self.frames,
             "changed %": 100 * self.change_rate}
        if self.last:
            d["golden"] = self.last["golden"]
            d["faulty"] = self.last["pred"]
        return d

    def text(self) -> str:
        """Lines for the terminal screen and the --gui readout."""
        lines = [f"FAULTS  {self.mode}, {self.bits} bit(s) per frame, seed {self.seed}"]
        if self.last:
            lines += [
                f"golden = {self.last['golden']:<14} faulty = {self.last['pred']}",
                f"flipped: {self.last['flipped'][:60]}",
            ]
        lines.append(f"changed on {self.changed}/{self.frames} frames "
                     f"({100 * self.change_rate:.1f}%)")
        return "\n".join(lines)


def add_fault_args(p, required: bool = False) -> None:
    """--fault / --fault-bits / --fault-seed, shared by `fi` and `board-scan`."""
    p.add_argument("--fault", choices=MODES, required=required,
                   help="flip bits in the 8-bit input sent to the chip, or in its raw 8-bit output")
    p.add_argument("--fault-bits", type=int, default=1, metavar="K",
                   help="random bits flipped per inference (default 1)")
    p.add_argument("--fault-seed", type=int, default=0, metavar="N",
                   help="random seed, so a run can be repeated exactly (default 0)")


def fault_settings(args) -> dict | None:
    """What goes into run.json, or None for a clean run."""
    mode = getattr(args, "fault", None)
    if not mode:
        return None
    return {"mode": mode, "bits": args.fault_bits, "seed": args.fault_seed}


def wrap_for_scan(clf, args, cfg, run_dir: Path, context=None):
    """board-scan: (classify function, injector or None, log or None).

    With --fault, every window the scan scores goes through the injector and is
    logged to <run>/faults.csv; without it, this is just clf.classify.
    """
    mode = getattr(args, "fault", None)
    if not mode:
        return clf.classify, None, None
    log = FaultLog(Path(run_dir) / "faults.csv")
    injector = FaultInjector(clf, mode, args.fault_bits, args.fault_seed,
                             target=cfg.sweep.target, log=log.write, context=context)
    print(f"faults: {mode}, {args.fault_bits} bit(s) per inference, seed {args.fault_seed} "
          f"-> {log.path}")
    return injector.classify, injector, log
