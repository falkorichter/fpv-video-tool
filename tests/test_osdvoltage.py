"""Tests for the OSD voltage reader using cells from the shipped digit bank."""
import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import osdvoltage as ov  # noqa: E402


def synthetic_frame(raw_cells, digits, dx=0, dy=0):
    """Paste one bank cell per digit at the OSD positions on a black frame."""
    frame = np.full((ov.CROP_H, ov.CROP_W), 12, np.uint8)
    for x, d in zip(ov.CELL_XS, digits):
        cell = raw_cells[d]
        frame[ov.CELL_Y + dy:ov.CELL_Y + dy + ov.CH, x + dx:x + dx + ov.CW] = cell
    return frame


class OsdVoltage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bank, cls.labels = ov.load_bank()
        raw = np.load(ov.BANK)
        # pick the first un-shifted example (centre of each 3x3 augmentation block)
        cls.raw = {}
        for d in range(10):
            idx = np.where(raw["labels"] == d)[0]
            cls.raw[d] = raw["cells"][idx[len(idx) // 2]]

    def test_reads_digits(self):
        for digits in [(3, 5, 2), (4, 2, 0), (3, 6, 9), (3, 0, 8), (4, 1, 7)]:
            f = synthetic_frame(self.raw, digits)
            got, sim = ov.read_digits(f[None], self.bank, self.labels, 0, 0)
            self.assertEqual(tuple(got[0]), digits)
            self.assertGreater(sim.min(), 0.9)

    def test_calibrate_finds_offset(self):
        rng = np.random.default_rng(0)
        frames = np.stack([synthetic_frame(self.raw, rng.integers(0, 10, 3), dx=-9, dy=-12)
                           for _ in range(10)])
        self.assertEqual(ov.calibrate(frames, self.bank, self.labels), (-9, -12))

    def test_filter_drops_outliers_and_unreadable(self):
        t = np.arange(40) / 4
        digits = np.tile([3, 5, 0], (40, 1))
        digits[10] = [3, 9, 0]        # single misread -> 3.90
        digits[20] = [-1, 5, 0]       # noise frame
        sim = np.full((40, 3), 0.9)
        raw, smooth = ov.to_voltage(t, digits, sim, 0.7, 2.8, 4.45, 2.0, 0.05)
        self.assertAlmostEqual(raw[10], 3.9)
        self.assertTrue(np.isnan(smooth[10]))
        self.assertTrue(np.isnan(raw[20]))
        self.assertAlmostEqual(smooth[20], 3.5)   # gap filled by the median
        self.assertAlmostEqual(np.nanmedian(smooth), 3.5)


def synthetic_session():
    """Two batteries at 4 fps.

    Pack 1: 20 s resting at 4.30, takeoff sag to 3.70, 100 s discharging
    linearly to 3.40 with a 20 s OSD gap in the middle after which the rested
    battery reads 0.2 V high and decays back over ~8 s, then a landing (fast
    rise to 3.75) and 10 s resting. 30 s without OSD (battery swap), then
    pack 2: 10 s resting at 4.25, 80 s from 3.65 to 3.45.
    """
    t, v = [], []
    def add(t0, dur, f):
        for k in range(int(dur * 4)):
            tt = t0 + k / 4
            t.append(tt); v.append(f(tt))
    add(0, 20, lambda x: 4.30)
    add(20, 50, lambda x: 3.70 - 0.003 * (x - 20))          # 3.70 -> 3.55
    # 20 s gap (70..90): resumes 0.2 high, decaying
    add(90, 50, lambda x: 3.55 - 0.003 * (x - 90) + 0.2 * np.exp(-(x - 90) / 3))
    add(140, 10, lambda x: 3.75)                            # landed
    add(180, 10, lambda x: 4.25)                            # pack 2 resting
    add(190, 80, lambda x: 3.65 - 0.0025 * (x - 190))
    t = np.array(t); v = np.round(np.array(v), 2)
    jitter = np.where(np.arange(len(v)) % 2, 0.01, -0.01)
    flying = ((t >= 20) & (t < 140)) | (t >= 190)
    raw = np.where(flying, v + jitter, v)                  # load jitters
    return t, raw, v


class FlightAnalysis(unittest.TestCase):
    def test_split_and_rest(self):
        t, raw, v = synthetic_session()
        flights = ov.split_flights(t, v, [])
        self.assertEqual(len(flights), 2)
        self.assertAlmostEqual(t[flights[1][0]], 180.0)
        rest = ov.rest_mask(t, raw, v, flights)
        self.assertTrue(rest[t < 19].all())                 # before takeoff
        self.assertFalse(rest[(t > 22) & (t < 68)].any())   # flying
        self.assertTrue(rest[(t > 142) & (t < 150)].all())  # after landing
        self.assertTrue(rest[(t >= 180) & (t < 189)].all())
        self.assertFalse(rest[t > 192].any())

    def test_flight_axis_removes_pauses(self):
        t, raw, v = synthetic_session()
        a = ov.analyse_flights(t, raw, v, [])
        f1, f2 = a["flights"]
        self.assertTrue(f1["valid"] and f2["valid"])
        # 50 s + 50 s under load; the 20 s OSD gap does not count
        self.assertAlmostEqual(f1["flight_time"], 100, delta=3)
        self.assertAlmostEqual(f2["flight_time"], 80, delta=3)
        self.assertEqual(f1["pauses"], 1)
        self.assertAlmostEqual(f1["rest_v"], 4.30, places=2)
        self.assertAlmostEqual(f1["sag"], 0.60, delta=0.05)

    def test_lowpass_suppresses_post_pause_spike(self):
        t, raw, v = synthetic_session()
        a = ov.analyse_flights(t, raw, v, [])
        f1 = a["flights"][0]
        ft, curve = np.array(f1["ft"]), np.array(f1["v"])
        after = (ft >= 50) & (ft <= 60)                     # right after the gap
        truth = 3.55 - 0.003 * (ft[after] - 50)
        # the raw readings are up to 0.2 V high here; the curve stays close
        self.assertLess(np.max(np.abs(curve[after] - truth)), 0.04)
        self.assertTrue(np.all(np.diff(curve[ft > 5]) < 0.005))  # no bump up


if __name__ == "__main__":
    unittest.main()
