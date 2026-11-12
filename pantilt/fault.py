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
from collections import Counter
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
              "golden_conf", "pred_conf_of_golden", "conf_drop",
              "golden_p_target", "pred_p_target", "target_changed",
              "label", "golden_correct", "pred_correct", "flipped", "pan", "tilt", "stage"]

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


def _pct(n: int, d: int) -> str:
    return f"{100 * n / d:.1f}%" if d else "-"


class FaultInjector:
    """Wraps a raw-output HailoClassifier; its classify() returns the faulty answer.

    clf needs infer_raw(x) -> uint8 output bytes, and dequant(q) -> probabilities
    (HailoClassifier(..., raw_output=True) has both). context() may return extra
    columns for the log, e.g. pan/tilt or the scan stage.

    Two kinds of accuracy are tracked. Against the clean answer, always: how
    often the faulty answer agrees with it, and how much confidence in it the
    fault cost. Against the truth, only while `label` says what the camera is
    really looking at: clean accuracy, faulty accuracy, and the drop between them.
    """

    def __init__(self, clf, mode: str, bits: int, seed: int = 0, target: str = "crater",
                 log=None, context=None, label: str | None = None,
                 label_hint: str = "no label set - press L, or use --label CLASS"):
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
        self.label: int | None = None
        self.label_hint = label_hint
        self.set_label(label)
        self.started = time.monotonic()
        self.frames = 0
        self.changed = 0
        self.target_changed = 0
        self.conf_change_sum = 0.0           # faulty minus clean confidence in the clean answer
        self.labelled = self.clean_right = self.faulty_right = 0
        self.bit_frames = [0] * 8            # frames whose highest flipped bit was b
        self.bit_changed = [0] * 8
        self.swaps: Counter = Counter()      # (clean, faulty) class pairs when they differ
        self.last: dict | None = None
        self.last_flips: list[str] = []      # readable, e.g. "impact ejecta bit 7 (+0.50)"
        self.last_clean: np.ndarray | None = None
        self.last_faulty: np.ndarray | None = None

    # -- the true label --------------------------------------------------------
    def set_label(self, name: str | None) -> None:
        """What the camera is really looking at, or None when unknown."""
        if name is None:
            self.label = None
        elif name in CLASS_NAMES:
            self.label = CLASS_NAMES.index(name)
        else:
            raise ValueError(f"label must be one of: {', '.join(CLASS_NAMES)}")

    def cycle_label(self) -> None:
        """none -> other -> crater -> ... -> spider -> none (the L key)."""
        nxt = 0 if self.label is None else self.label + 1
        self.label = nxt if nxt < len(CLASS_NAMES) else None

    @property
    def label_name(self) -> str:
        return CLASS_NAMES[self.label] if self.label is not None else "none"

    # -- injecting -----------------------------------------------------------------
    def classify(self, x: np.ndarray) -> np.ndarray:
        """Golden and faulty inference of the same frame; returns the faulty probabilities."""
        golden_q = self.clf.infer_raw(x)
        if self.mode == "input":
            xf, flips = flip_bits(x, self.bits, self.rng)
            faulty_q = self.clf.infer_raw(xf)
            where = describe_flips(flips, np.shape(x), "input")
            src, dst = np.asarray(x).reshape(-1), xf.reshape(-1)
        else:
            faulty_q, flips = flip_bits(golden_q, self.bits, self.rng)
            where = describe_flips(flips, np.shape(golden_q), "output")
            src, dst = golden_q.reshape(-1), faulty_q.reshape(-1)
        golden = self.clf.dequant(golden_q)
        faulty = self.clf.dequant(faulty_q)
        self.last_flips = [self._readable(b, bit, int(dst[b]) - int(src[b]), np.shape(x))
                           for b, bit in flips]
        self._record(golden, faulty, where, max((bit for _, bit in flips), default=None))
        return faulty

    def _readable(self, byte: int, bit: int, change: int, shape) -> str:
        if self.mode == "output":
            name = CLASS_NAMES[byte] if byte < len(CLASS_NAMES) else str(byte)
            step = float(self.clf.dequant(np.array([1], np.uint8))[0]
                         - self.clf.dequant(np.array([0], np.uint8))[0])
            return f"{name} bit {bit} ({change * step:+.2f})"
        y, x, c = (int(i) for i in np.unravel_index(byte, shape)[-3:])
        return f"pixel ({y},{x}) ch{c} bit {bit} ({change:+d})"

    def _record(self, golden, faulty, where: str, top_bit) -> None:
        g, f = int(np.argmax(golden)), int(np.argmax(faulty))
        t = self.target_i
        changed = g != f
        target_changed = (g == t) != (f == t)  # target found or lost because of the fault
        drop = float(golden[g] - faulty[g])   # > 0: the fault cost confidence; < 0: it added some
        self.frames += 1
        self.changed += changed
        self.target_changed += target_changed
        self.conf_change_sum -= drop
        if changed:
            self.swaps[(g, f)] += 1
        if top_bit is not None:
            self.bit_frames[top_bit] += 1
            self.bit_changed[top_bit] += changed
        row_label = {"label": "", "golden_correct": "", "pred_correct": ""}
        if self.label is not None:
            self.labelled += 1
            self.clean_right += g == self.label
            self.faulty_right += f == self.label
            row_label = {"label": CLASS_NAMES[self.label], "golden_correct": int(g == self.label),
                         "pred_correct": int(f == self.label)}
        self.last_clean, self.last_faulty = golden, faulty
        row = {
            "time": f"{time.time():.3f}", "frame": self.frames, "mode": self.mode,
            "bits": self.bits, "golden": CLASS_NAMES[g], "pred": CLASS_NAMES[f],
            "changed": int(changed), "golden_conf": f"{golden[g]:.4f}",
            "pred_conf_of_golden": f"{faulty[g]:.4f}", "conf_drop": f"{drop:.4f}",
            "golden_p_target": f"{golden[t]:.4f}", "pred_p_target": f"{faulty[t]:.4f}",
            "target_changed": int(target_changed), **row_label, "flipped": where,
        }
        if self.context:
            row.update(self.context())
        self.last = row
        if self.log:
            self.log(row)

    # -- numbers -----------------------------------------------------------------
    @property
    def change_rate(self) -> float:
        return self.changed / self.frames if self.frames else 0.0

    @property
    def agreement(self) -> float:
        """Faulty answer == clean answer: 'accuracy' with the clean model as the reference."""
        return 1 - self.change_rate if self.frames else 1.0

    @property
    def conf_change(self) -> float:
        """Average change in confidence in the clean answer, in percentage points.

        Negative means the faults cost confidence. It can be positive: a flipped
        bit that turns a 0 into a 1 raises that class's score.
        """
        return 100 * self.conf_change_sum / self.frames if self.frames else 0.0

    def accuracy(self) -> tuple[float, float] | None:
        """(clean, faulty) accuracy against the label, over labelled frames only."""
        if not self.labelled:
            return None
        return self.clean_right / self.labelled, self.faulty_right / self.labelled

    def _setting(self) -> str:
        where = "input frame" if self.mode == "input" else "chip output"
        return f"{self.bits} random bit{'' if self.bits == 1 else 's'} per frame in the {where}, seed {self.seed}"

    def _accuracy_line(self) -> str:
        acc = self.accuracy()
        if acc is None:
            return f"Accuracy      {self.label_hint}"
        c, f = acc
        return (f"Accuracy      over {self.labelled} labelled frames: clean {100 * c:.1f}% -> "
                f"faulty {100 * f:.1f}%  ({100 * (f - c):+.1f} pts)")

    # -- output for people -------------------------------------------------------
    TABLE = "{:>7}  {:>6}  {:<29} {:<32} {:>7}  {:>7}  {:>8}  {}"

    def table_header(self) -> str:
        top = f"Fault injection: {self._setting()}"
        cols = self.TABLE.format("time", "frames", "this frame: clean -> faulty", "bits flipped",
                                 "changed", "agree", "conf.", "accuracy clean -> faulty")
        return f"{top}\n{cols}\n{'-' * len(cols)}"

    def table_row(self) -> str:
        """One aligned line: what just happened, and the running totals."""
        if not self.last:
            return "  (waiting for the first frame)"
        r = self.last
        now = f"{r['golden']} -> {r['pred']}" + ("  *" if r["changed"] else "")
        flips = ", ".join(self.last_flips) or "none"
        acc = self.accuracy()
        acc_s = f"{100 * acc[0]:.0f}% -> {100 * acc[1]:.0f}% ({100 * (acc[1] - acc[0]):+.1f})" if acc else "no label"
        return self.TABLE.format(f"{time.monotonic() - self.started:.0f}s", self.frames, now[:29],
                                 flips[:32], f"{100 * self.change_rate:.1f}%",
                                 f"{100 * self.agreement:.1f}%", f"{self.conf_change:+.1f}pt", acc_s)

    def text(self) -> str:
        """The --gui readout and the terminal screen: plain sentences, newest frame first."""
        lines = [f"FAULT INJECTION  {self._setting()}", ""]
        if self.last:
            r, g, f = self.last, self.last_clean, self.last_faulty
            gi, fi = int(np.argmax(g)), int(np.argmax(f))
            verdict = "CHANGED by the fault" if r["changed"] else "same answer"
            lines += [
                f"This frame    clean:  {CLASS_NAMES[gi]} {100 * g[gi]:.0f}%",
                f"              faulty: {CLASS_NAMES[fi]} {100 * f[fi]:.0f}%   -> {verdict}",
                f"              flipped: {', '.join(self.last_flips) or 'nothing (0 bits)'}",
                "",
            ]
        lines += [
            f"So far        {self.frames} frames, answer changed on {self.changed} ({_pct(self.changed, self.frames)})",
            f"              agrees with the clean model {100 * self.agreement:.1f}% of the time",
            f"              confidence in the clean answer: {self.conf_change:+.1f} pts on average",
            f"              {CLASS_NAMES[self.target_i]} found or lost by a fault: {self.target_changed} frames",
            self._accuracy_line(),
            f"              label now: {self.label_name}",
        ]
        return "\n".join(lines)

    def report(self) -> str:
        """The summary printed when a run ends."""
        if not self.frames:
            return f"Fault injection: {self._setting()} - nothing was classified."
        lines = [
            "",
            "=" * 72,
            "FAULT INJECTION REPORT",
            "=" * 72,
            f"Fault          {self._setting()}",
            f"Frames         {self.frames}",
            f"Changed        {self.changed} frames ({_pct(self.changed, self.frames)}) gave a different answer",
            f"Agreement      {100 * self.agreement:.1f}% same answer as the clean model",
            f"Confidence     {self.conf_change:+.1f} pts on average in the clean answer "
            f"(negative = the faults cost confidence)",
            f"{CLASS_NAMES[self.target_i].capitalize():<15}"
            f"{self.target_changed} frames ({_pct(self.target_changed, self.frames)}) where a fault "
            f"made one appear or disappear",
            self._accuracy_line().replace("Accuracy     ", "Accuracy      "),
        ]
        hit = [(b, self.bit_frames[b], self.bit_changed[b]) for b in range(7, -1, -1) if self.bit_frames[b]]
        if hit:
            lines.append("By bit         highest bit flipped -> how often the answer changed")
            for b, n, c in hit:
                lines.append(f"                 bit {b}: {c:>4} of {n:<5} ({_pct(c, n):>6})")
        if self.swaps:
            top = ", ".join(f"{CLASS_NAMES[g]} -> {CLASS_NAMES[f]} ({n})"
                            for (g, f), n in self.swaps.most_common(4))
            lines.append(f"Most common    {top}")
        lines.append("=" * 72)
        return "\n".join(lines)

    def summary(self) -> str:
        """One line, kept for logs and scripts."""
        if not self.frames:
            return f"faults: {self.mode}, {self.bits} bit(s) per inference - nothing classified"
        return (f"faults: {self.mode}, {self.bits} bit(s) per inference, seed {self.seed} | "
                f"{self.frames} inferences, answer changed on {100 * self.change_rate:.2f}%, "
                f"{CLASS_NAMES[self.target_i]} found/lost by a fault on "
                f"{100 * self.target_changed / self.frames:.2f}%")

    def status(self) -> dict:
        """Fields for the browser live view."""
        d = {"fault": f"{self.mode} x{self.bits}", "fi frames": self.frames,
             "changed %": 100 * self.change_rate, "agree %": 100 * self.agreement,
             "conf change": f"{self.conf_change:+.1f} pts", "label": self.label_name}
        acc = self.accuracy()
        if acc:
            d["acc clean"] = f"{100 * acc[0]:.1f}%"
            d["acc faulty"] = f"{100 * acc[1]:.1f}%"
        if self.last:
            d["golden"] = self.last["golden"]
            d["faulty"] = self.last["pred"]
        return d


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
                             target=cfg.sweep.target, log=log.write, context=context,
                             label_hint="n/a in board-scan (many classes on the board) - "
                                        "use agreement with the clean model")
    print(f"faults: {mode}, {args.fault_bits} bit(s) per inference, seed {args.fault_seed} "
          f"-> {log.path}")
    return injector.classify, injector, log
