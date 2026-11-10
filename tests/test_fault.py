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
    assert "golden = crater" in inj.text() and "flipped:" in inj.text()


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
    assert len(rows) >= Config().model.smooth_frames + 1
    assert all(r["mode"] == "output" and r["flipped"].count(":") == 3 for r in rows)
    out = capsys.readouterr().out
    assert "faults: output, 3 bit(s)" in out and "per-frame log" in out
