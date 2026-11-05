"""End-to-end test: build a synthetic clip with known noise sections."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import noisesplit  # noqa: E402

SIZE = "320x240"
RATE = 25


def make_clip(path: Path) -> None:
    """video 4s | snow 2s | video 3s | snow 0.5s (too short) | video 3s
    | bands 2s | video 3s"""
    parts = [("video", 4), ("noise", 2), ("video", 3), ("noise", 0.5), ("video", 3),
             ("bands", 2), ("video", 3)]
    inputs, filters = [], []
    for i, (kind, dur) in enumerate(parts):
        if kind == "video":
            src = f"testsrc2=s={SIZE}:r={RATE}:d={dur}"
            inputs += ["-f", "lavfi", "-i", src]
            filters.append(f"[{i}:v]format=yuv420p,setsar=1[v{i}]")
        elif kind == "bands":
            # rolling horizontal black/white stripes like analog sync loss
            src = f"color=c=black:s={SIZE}:r={RATE}:d={dur}"
            inputs += ["-f", "lavfi", "-i", src]
            filters.append(f"[{i}:v]geq=lum='if(lt(mod(Y+N*17\\,60)\\,25)\\,230\\,20)'"
                           f":cb=128:cr=128,noise=alls=20:allf=t,format=yuv420p,setsar=1[v{i}]")
        else:
            src = f"color=c=gray:s={SIZE}:r={RATE}:d={dur}"
            inputs += ["-f", "lavfi", "-i", src]
            filters.append(f"[{i}:v]noise=alls=100:allf=t+u,format=yuv420p,setsar=1[v{i}]")
    concat = "".join(f"[v{i}]" for i in range(len(parts)))
    fc = ";".join(filters) + f";{concat}concat=n={len(parts)}:v=1:a=0[out]"
    subprocess.run(["ffmpeg", "-v", "error", "-y", *inputs, "-filter_complex", fc,
                    "-map", "[out]", "-c:v", "libx264", "-crf", "18", str(path)],
                   check=True)


class EndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.clip = Path(cls.tmp.name) / "clip.mp4"
        make_clip(cls.clip)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_detects_only_long_noise(self):
        feat = noisesplit.analyze(self.clip, progress=False)
        mask = noisesplit.classify(feat, 0.2, 0.5, 0.8)
        weak = mask | (feat[:, 1] < 0.35)
        dur = noisesplit.probe_duration(self.clip)
        noise = noisesplit.find_noise(feat, mask, weak, min_noise=1.0, merge_gap=1.0, min_density=0.5,
                                      extend_gap=0.3, max_extend=3.0, pad=0.0, duration=dur)
        self.assertEqual(len(noise), 2)
        self.assertAlmostEqual(noise[0].start, 4.0, delta=0.1)
        self.assertAlmostEqual(noise[0].end, 6.0, delta=0.1)
        self.assertAlmostEqual(noise[1].start, 12.5, delta=0.1)
        self.assertAlmostEqual(noise[1].end, 14.5, delta=0.1)

    def test_split_writes_chunks(self):
        out = Path(self.tmp.name) / "out"
        rc = noisesplit.main([str(self.clip), "-o", str(out), "-q", "--preset", "ultrafast"])
        self.assertEqual(rc, 0)
        report = json.loads((out / "segments.json").read_text())
        self.assertEqual(len(report["files"]), 3)
        durs = [noisesplit.probe_duration(Path(f)) for f in report["files"]]
        self.assertAlmostEqual(durs[0], 3.9, delta=0.1)
        self.assertAlmostEqual(durs[1], 6.3, delta=0.1)
        self.assertAlmostEqual(durs[2], 2.9, delta=0.1)


if __name__ == "__main__":
    unittest.main()
