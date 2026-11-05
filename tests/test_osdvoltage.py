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


if __name__ == "__main__":
    unittest.main()
