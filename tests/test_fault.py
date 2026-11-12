"""Hardware-free tests for input/output fault injection (pantilt fi, board-scan --fault)."""

import csv
import sys
import types

import numpy as np
import pytest

import pantilt.classify as classify
import pantilt.cli as cli
import pantilt.controller as controller
from pantilt import Config, PanTilt, PCA9685
from pantilt.classify import CLASS_NAMES, HailoClassifier
from pantilt.cli import build_parser
from pantilt.fault import FaultInjector, FaultLog, describe_flips, flip_bits

CRATER = CLASS_NAMES.index("crater")


def popcount(a, b) -> int:
    return int(np.unpackbits(np.bitwise_xor(a, b).reshape(-1)).sum())


# -- flip_bits ---------------------------------------------------------------
@pytest.mark.parametrize("k", [0, 1, 2, 8, 64])
def test_flips_exactly_k_distinct_bits(k):
    x = np.random.default_rng(1).integers(0, 256, (1, 224, 224, 3), dtype=np.uint8)
    xf, flips = flip_bits(x, k, np.random.default_rng(7))
    assert popcount(x, xf) == k and len(set(flips)) == k
    assert xf.dtype == np.uint8 and xf.shape == x.shape


def test_flip_bits_leaves_the_original_alone_and_repeats_with_the_seed():
    x = np.zeros(8, np.uint8)
    a, fa = flip_bits(x, 3, np.random.default_rng(42))
    b, fb = flip_bits(x, 3, np.random.default_rng(42))
    assert not x.any()                      # copy, not in place
    assert fa == fb and np.array_equal(a, b)


def test_flips_are_recorded_where_they_landed():
    x = np.zeros(8, np.uint8)
    xf, flips = flip_bits(x, 5, np.random.default_rng(3))
    for byte, bit in flips:
        assert xf[byte] >> bit & 1


def test_describe_flips_names_classes_and_pixels():
    assert describe_flips([(1, 7), (0, 0)], (8,), "output") == "crater:7;other:0"
    byte = np.ravel_multi_index((0, 10, 20, 2), (1, 224, 224, 3))
    assert describe_flips([(byte, 3)], (1, 224, 224, 3), "input") == "10,20,2:3"


# -- the injector ------------------------------------------------------------
class FakeRaw:
    """Stands in for HailoClassifier(raw_output=True): the 'chip' says crater at 90%."""

    def __init__(self):
        self.inputs = []

    def infer_raw(self, x):
        self.inputs.append(np.array(x, copy=True))
        q = np.zeros(len(CLASS_NAMES), np.uint8)
        q[CRATER], q[0] = 230, 25          # crater 0.90, other 0.10
        return q

    def dequant(self, q):
        return np.asarray(q, np.float32) / 255


def frame():
    return np.random.default_rng(0).integers(0, 256, (1, 224, 224, 3), dtype=np.uint8)


def test_output_faults_flip_the_returned_bytes_only():
    rows = []
    inj = FaultInjector(FakeRaw(), "output", 2, seed=5, log=rows.append)
    p = inj.classify(frame())
    golden = FakeRaw().dequant(FakeRaw().infer_raw(None))
    assert popcount((p * 255).round().astype(np.uint8), (golden * 255).round().astype(np.uint8)) == 2
    assert len(inj.clf.inputs) == 1                    # one inference: the chip ran clean
    r = rows[0]
    assert r["mode"] == "output" and r["bits"] == 2 and r["golden"] == "crater"
    assert r["flipped"].count(":") == 2 and r["golden_p_target"] == "0.9020"


def test_input_faults_reach_the_chip_with_exactly_k_bits():
    fake = FakeRaw()
    x = frame()
    FaultInjector(fake, "input", 4, seed=1).classify(x)
    clean, faulty = fake.inputs
    assert np.array_equal(clean, x) and popcount(clean, faulty) == 4


def test_zero_bits_never_change_anything():
    inj = FaultInjector(FakeRaw(), "output", 0)
    for _ in range(20):
        inj.classify(frame())
    assert inj.frames == 20 and inj.changed == 0 and inj.target_changed == 0


def test_a_top_bit_flip_can_lose_the_crater():
    # Flipping bit 7 of the crater byte (230 -> 102, 0.90 -> 0.40) keeps crater on top;
    # flipping bit 7 of another class (0 -> 128, 0.50) doesn't beat 0.90 either. With
    # 8 bits flipped across 8 bytes some runs must change the answer.
    inj = FaultInjector(FakeRaw(), "output", 8, seed=0)
    for _ in range(200):
        inj.classify(frame())
    assert 0 < inj.changed < inj.frames
    assert inj.target_changed == inj.changed           # golden is crater, so any change loses it
    assert "changed on" in inj.summary() and "crater found/lost" in inj.summary()


def test_status_and_text_for_the_screens():
    inj = FaultInjector(FakeRaw(), "output", 1)
    assert "nothing classified" in inj.summary()
    inj.classify(frame())
    st = inj.status()
    assert st["fault"] == "output x1" and st["golden"] == "crater" and "faulty" in st
    assert st["agree %"] == 100 and st["label"] == "none"
    t = inj.text()
    assert "This frame    clean:  crater 90%" in t and "flipped:" in t and "same answer" in t
    assert "1 random bit per frame in the chip output, seed 0" in t
    assert "no label set - press L" in t


def test_bad_settings_are_refused():
    with pytest.raises(ValueError):
        FaultInjector(FakeRaw(), "weights", 1)
    with pytest.raises(ValueError):
        FaultInjector(FakeRaw(), "input", -1)


def test_fault_log_csv(tmp_path):
    log = FaultLog(tmp_path / "sub" / "f.csv")
    inj = FaultInjector(FakeRaw(), "output", 1, log=log.write, context=lambda: {"pan": "90.0"})
    for _ in range(3):
        inj.classify(frame())
    log.close()
    rows = list(csv.DictReader((tmp_path / "sub" / "f.csv").open()))
    assert len(rows) == 3 and rows[0]["pan"] == "90.0" and rows[2]["frame"] == "3"
    assert set(FaultLog.FIELDS) == set(rows[0])


# -- HailoClassifier raw mode (no chip needed for these parts) ---------------------
def test_raw_output_needs_raw_mode_and_dequant_uses_the_chip_values():
    clf = HailoClassifier("x.hef")
    with pytest.raises(RuntimeError, match="raw_output"):
        clf.infer_raw(np.zeros(1))
    raw = HailoClassifier("x.hef", raw_output=True)
    raw.out_scale, raw.out_zp = 0.00390625, 0.0       # 1/256, as the chip reports
    assert raw.dequant(np.array([0, 128, 255], np.uint8)) == pytest.approx([0, 0.5, 0.99609375])


# -- the commands ---------------------------------------------------------------
def test_fi_needs_a_fault_mode_and_has_live_options():
    p = build_parser()
    with pytest.raises(SystemExit):
        p.parse_args(["fi"])
    a = p.parse_args(["fi", "--fault", "input", "--fault-bits", "4", "--gui", "--crop", "0.2"])
    assert (a.fault, a.fault_bits, a.fault_seed, a.gui, a.crop) == ("input", 4, 0, True, 0.2)
    assert a.func.__name__ == "cmd_live"
    assert not hasattr(p.parse_args(["live"]), "fault")      # plain live stays clean
    b = p.parse_args(["board-scan", "--fault", "output", "--fault-bits", "2", "--fault-seed", "9"])
    assert (b.fault, b.fault_bits, b.fault_seed) == ("output", 2, 9)
    assert p.parse_args(["board-scan"]).fault is None


class Bus:
    def __init__(self): self.regs = {0x00: 0x11}
    def write_byte_data(self, a, r, v): self.regs[r] = v
    def read_byte_data(self, a, r): return self.regs.get(r, 0)
    def write_i2c_block_data(self, a, r, d): pass
    def close(self): pass


def test_pantilt_fi_end_to_end(monkeypatch, tmp_path, capsys):
    """`pantilt fi --scan` on fake hardware: every frame is logged golden vs faulty."""
    for name in ("picamera2", "hailo_platform"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    monkeypatch.setattr(controller.time, "sleep", lambda s: None)
    monkeypatch.setattr(cli, "PanTilt", lambda cfg: PanTilt(cfg, driver=PCA9685(smbus=Bus())))

    class Hailo(FakeRaw):
        def __init__(self, hef, raw_output=False):
            super().__init__()
            assert raw_output                             # fi must ask for the raw output
        def __enter__(self): return self
        def __exit__(self, *e): pass
        def classify(self, x): return self.dequant(self.infer_raw(x))

    class Camera:
        url = None
        def __init__(self, *a, **k): self.status = k.get("status")
        def grab(self):
            self.status()                                 # what the browser HUD polls
            return np.full((720, 1280, 4), 128, np.uint8)
        def close(self): pass

    monkeypatch.setattr(classify, "HailoClassifier", Hailo)
    monkeypatch.setattr(cli, "_Camera", Camera)
    hef = tmp_path / "m.hef"
    hef.write_bytes(b"")
    log = tmp_path / "fi.csv"
    args = build_parser().parse_args(["fi", "--fault", "output", "--fault-bits", "3", "--scan",
                                      "--cols", "1", "--rows", "1", "--no-preview",
                                      "--hef", str(hef), "--fault-log", str(log)])
    assert cli.cmd_live(args, Config()) == 0
    rows = list(csv.DictReader(log.open()))
    assert len(rows) >= 2          # fi judges frames one at a time: 2 fresh frames per pose
    assert all(r["mode"] == "output" and r["flipped"].count(":") == 3 for r in rows)
    out = capsys.readouterr().out
    assert "faults: output, 3 bit(s)" in out and "Per-frame log" in out
    assert "FAULT INJECTION REPORT" in out
    assert "time  frames  this frame: clean -> faulty" in out         # --scan prints the table


# -- accuracy, labels and the readable output ------------------------------------------
class CloseCall(FakeRaw):
    """crater 0.55 vs other 0.39: only a top-bit flip can swap them."""

    def infer_raw(self, x):
        self.inputs.append(np.array(x, copy=True))
        q = np.zeros(len(CLASS_NAMES), np.uint8)
        q[CRATER], q[0] = 140, 100
        return q


def run(inj, n=400):
    for _ in range(n):
        inj.classify(frame())
    return inj


def test_true_accuracy_and_its_drop_with_a_label():
    inj = run(FaultInjector(CloseCall(), "output", 1, seed=2, label="crater"))
    clean, faulty = inj.accuracy()
    assert clean == 1.0                                   # the clean model is right every time
    assert faulty == pytest.approx(1 - inj.change_rate)   # every change loses the crater
    assert 0 < inj.changed < inj.frames
    assert f"{100 * (faulty - clean):+.1f} pts" in inj.report()
    wrong = run(FaultInjector(CloseCall(), "output", 0, label="spider"), 5)
    assert wrong.accuracy() == (0.0, 0.0)                 # label says spider, model says crater


def test_without_a_label_accuracy_is_against_the_clean_model():
    inj = run(FaultInjector(CloseCall(), "output", 1, seed=2))
    assert inj.accuracy() is None
    assert inj.agreement == pytest.approx(1 - inj.changed / inj.frames)
    assert "no label set" in inj.report()


def test_only_frames_with_a_label_count_toward_accuracy():
    inj = FaultInjector(CloseCall(), "output", 0)
    run(inj, 3)
    inj.set_label("crater")
    run(inj, 2)
    assert inj.labelled == 2 and inj.accuracy() == (1.0, 1.0)


def test_label_cycling_and_bad_labels():
    inj = FaultInjector(FakeRaw(), "output", 1)
    seen = [inj.label_name]
    for _ in range(len(CLASS_NAMES) + 1):
        inj.cycle_label()
        seen.append(inj.label_name)
    assert seen == ["none", *CLASS_NAMES, "none"]
    with pytest.raises(ValueError):
        inj.set_label("volcano")


def test_confidence_change_is_signed_and_matches_the_log():
    assert run(FaultInjector(CloseCall(), "output", 0), 10).conf_change == 0
    rows = []
    inj = run(FaultInjector(CloseCall(), "output", 8, seed=1, log=rows.append), 200)
    expected = -100 * np.mean([float(r["conf_drop"]) for r in rows])
    assert inj.conf_change == pytest.approx(expected, abs=1e-3) and inj.conf_change != 0
    # Clearing a set bit of the clean answer's byte always costs confidence.
    lose = FaultInjector(CloseCall(), "output", 1, seed=0)
    lose.rng = type("R", (), {"choice": lambda self, n, size, replace: np.array([CRATER * 8 + 7])})()
    lose.classify(frame())
    assert lose.conf_change == pytest.approx(-100 * 128 / 255, abs=0.1)


def test_per_bit_breakdown_shows_only_the_top_bit_matters_here():
    inj = run(FaultInjector(CloseCall(), "output", 1, seed=4), 600)
    assert sum(inj.bit_frames) == inj.frames
    assert inj.bit_changed[7] > 0 and sum(inj.bit_changed[:7]) == 0
    report = inj.report()
    assert "bit 7:" in report and "Most common    crater -> other" in report


def test_flips_are_described_for_people():
    out = FaultInjector(CloseCall(), "output", 1, seed=4)
    for _ in range(50):
        out.classify(frame())
        f = out.last_flips[0]
        assert " bit " in f and f.split()[-1].startswith(("(+", "(-"))
    inp = FaultInjector(CloseCall(), "input", 1, seed=4)
    inp.classify(frame())
    assert inp.last_flips[0].startswith("pixel (") and " ch" in inp.last_flips[0]
    change = int(inp.last_flips[0].split("(")[-1].rstrip(")"))
    assert abs(change) == 2 ** int(inp.last_flips[0].split(" bit ")[1].split()[0])


def test_table_lines_up():
    inj = FaultInjector(CloseCall(), "output", 1, label="crater")
    header = inj.table_header().splitlines()
    assert "Fault injection: 1 random bit per frame in the chip output" in header[0]
    assert set(header[2]) == {"-"} and len(header[2]) == len(header[1])
    assert "waiting" in inj.table_row()
    run(inj, 5)
    row = inj.table_row()
    assert "crater -> " in row and "%" in row and "pt" in row


# -- the scan windows ----------------------------------------------------------------
def _fake_tk(monkeypatch, frames=4, fail=False):
    """Just enough tkinter to run a window's loop for a few frames, no screen."""
    tk = types.ModuleType("tkinter")
    tk.TclError = RuntimeError
    shown = []

    class Widget:
        def __init__(self, *a, **k): self.photo = None
        def grid(self, *a, **k): pass
        def pack(self, *a, **k): pass
        def create_image(self, *a, **k): return 1
        def create_line(self, *a, **k): pass
        def create_rectangle(self, *a, **k): pass
        def itemconfig(self, *a, **k): pass
        def config(self, **k): shown.append(k.get("text", ""))

    class Tk(Widget):
        def __init__(self):
            if fail:
                raise RuntimeError("no display name and no $DISPLAY environment variable")
            super().__init__()
            self.pending, self.alive = [], True
        def title(self, t): self.t = t
        def bind(self, *a): pass
        def protocol(self, *a): pass
        def focus_force(self): pass
        def after(self, ms, fn): self.pending.append(fn)
        def destroy(self): self.alive = False
        def mainloop(self):
            import time as _t
            for _ in range(frames):
                if not self.alive or not self.pending:
                    break
                _t.sleep(0.02)
                self.pending.pop(0)()

    tk.Tk, tk.Canvas, tk.Frame, tk.Label, tk.Button = Tk, Widget, Widget, Widget, Widget
    tk.PhotoImage = lambda data=None: object()
    monkeypatch.setitem(sys.modules, "tkinter", tk)
    return shown


class GrabCamera:
    def grab(self):
        return np.zeros((720, 1280, 4), np.uint8)


def test_watch_window_shows_text_and_returns_when_the_scan_finishes(monkeypatch):
    from pantilt.gui import run_watch

    shown = _fake_tk(monkeypatch)
    ran = []
    ok = run_watch(Config(), camera=GrabCamera(), crop=0.2, title="t",
                   text=lambda: "BOARD SCAN stage: scan 3/15", work=lambda stop: ran.append(1))
    assert ok and ran == [1]
    assert any("BOARD SCAN" in t for t in shown)
    assert any("FINISHED" in t for t in shown)


def test_closing_the_window_stops_the_scan(monkeypatch):
    from pantilt.gui import run_watch

    _fake_tk(monkeypatch, frames=2)
    steps = []

    def work(stop):
        while True:                       # a scan that never ends on its own
            if stop.is_set():
                raise KeyboardInterrupt
            steps.append(1)
            stop.wait(0.005)

    with pytest.raises(KeyboardInterrupt):
        run_watch(Config(), camera=GrabCamera(), crop=0.2, title="t", text=lambda: "", work=work)
    assert steps


def test_no_display_means_nothing_starts(monkeypatch, capsys):
    from pantilt.gui import run_watch

    _fake_tk(monkeypatch, fail=True)
    ran = []
    assert run_watch(Config(), camera=GrabCamera(), crop=0.2, title="t", text=lambda: "",
                     work=lambda stop: ran.append(1)) is False
    assert ran == [] and "can't open a window" in capsys.readouterr().out


def test_fi_scan_gui_runs_the_scan_behind_the_window(monkeypatch, tmp_path, capsys):
    import pantilt.gui as gui

    for name in ("picamera2", "hailo_platform"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    monkeypatch.setattr(controller.time, "sleep", lambda s: None)
    monkeypatch.setattr(cli, "PanTilt", lambda cfg: PanTilt(cfg, driver=PCA9685(smbus=Bus())))

    class Hailo(CloseCall):
        def __init__(self, hef, raw_output=False): super().__init__()
        def __enter__(self): return self
        def __exit__(self, *e): pass
        def classify(self, x): return self.dequant(self.infer_raw(x))

    class Camera:
        url = None
        def __init__(self, *a, **k): pass
        def grab(self): return np.full((720, 1280, 4), 128, np.uint8)
        def close(self): pass

    seen = {}

    def fake_watch(cfg, *, camera, crop, work, text, title, extra_actions=None):
        import threading
        work(threading.Event())
        seen.update(text=text(), title=title, actions=[a[0] for a in extra_actions or []])
        return True

    monkeypatch.setattr(classify, "HailoClassifier", Hailo)
    monkeypatch.setattr(cli, "_Camera", Camera)
    monkeypatch.setattr(gui, "run_watch", fake_watch)
    hef = tmp_path / "m.hef"
    hef.write_bytes(b"")
    args = build_parser().parse_args(["fi", "--fault", "output", "--scan", "--gui", "--cols", "2",
                                      "--rows", "1", "--no-preview", "--label", "crater",
                                      "--hef", str(hef), "--fault-log", str(tmp_path / "f.csv")])
    assert cli.cmd_live(args, Config()) == 0
    assert seen["title"] == "Fault injection scan" and seen["actions"] == ["Next label"]
    assert "SCANNING  pose done" in seen["text"] and "FAULT INJECTION" in seen["text"]
    assert "labelled frames: clean 100.0%" in capsys.readouterr().out
