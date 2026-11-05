#!/usr/bin/env python3
"""Detect analog white-noise ("snow") sections in a video and split the file
into chunks at those sections.

Detection uses the video stream only. A frame is classed as noise when
  * it is snow: uncorrelated with the previous frame (temporal correlation)
    AND neighbouring rows are uncorrelated (vertical correlation), or
  * it shows sync-loss bands: horizontal stripes, i.e. nearly all of the
    picture's variance is explained by per-row means.
Runs of noise frames are then merged, required to be >= --min-noise long
and mostly (--min-density) noise, so short interference during normal
footage is ignored, and finally grown over their ragged edges.

Requires: ffmpeg/ffprobe on PATH, numpy.
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import re
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np

W, H = 160, 120  # analysis resolution
BORDER = 8  # ignore edges (overscan, OSD frames, black bars)
PTS_RE = re.compile(r"pts_time:\s*(-?[0-9.]+)")


@dataclass
class Segment:
    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start


def probe_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    return float(out)


def _corr(a: np.ndarray, b: np.ndarray) -> float:
    a = a - a.mean()
    b = b - b.mean()
    den = np.sqrt((a * a).mean() * (b * b).mean())
    if den < 1e-3:  # flat frame (e.g. pure black) - not noise
        return 1.0
    return float((a * b).mean() / den)


def analyze(path: Path, progress: bool = True) -> np.ndarray:
    """Decode every frame and return rows of
    (pts_time, temporal_corr, vertical_corr, mean, row_band)."""
    duration = probe_duration(path)
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "info",
           "-i", str(path), "-an", "-sn", "-fps_mode", "passthrough",
           "-vf", f"scale={W}:{H}:flags=area,format=gray,showinfo",
           "-f", "rawvideo", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    times: list[float] = []
    errors: list[str] = []

    def read_stderr() -> None:
        # showinfo prints one line per frame with its presentation timestamp
        for raw in proc.stderr:
            line = raw.decode(errors="replace")
            m = PTS_RE.search(line)
            if m:
                times.append(float(m.group(1)))
            elif "Error" in line or "error" in line:
                errors.append(line.strip())

    th = threading.Thread(target=read_stderr, daemon=True)
    th.start()
    size = W * H
    rows = []
    prev = None
    next_report = 30.0
    while True:
        buf = proc.stdout.read(size)
        if len(buf) < size:
            break
        f = np.frombuffer(buf, np.uint8).reshape(H, W).astype(np.float32)
        c = f[BORDER:-BORDER, BORDER:-BORDER]
        vc = _corr(c[1:, :], c[:-1, :])
        tc = _corr(c, prev) if prev is not None else 1.0
        prev = c
        var = c.var()
        # share of the variance explained by row means: ~1 for horizontal
        # sync-loss bands, low for real pictures
        rb = float(c.mean(axis=1).var() / var) if var > 1e-3 else 0.0
        rows.append((tc, vc, float(c.mean()), rb))
        if progress and times and times[-1] >= next_report:
            next_report += 30.0
            pct = min(100.0, 100.0 * times[-1] / duration) if duration else 0
            print(f"\ranalyzing {times[-1]:8.1f}s / {duration:.1f}s ({pct:5.1f}%)",
                  end="", file=sys.stderr, flush=True)
    proc.stdout.close()
    proc.wait()
    th.join()
    proc.stderr.close()
    if progress:
        print(file=sys.stderr)
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg failed: " + "\n".join(errors[-5:]))
    n = min(len(rows), len(times))
    if n < len(rows):
        # should not happen; fall back to evenly spaced timestamps
        t = np.linspace(0, duration, len(rows), endpoint=False)
    else:
        t = np.array(times[:n])
    return np.column_stack([t[:len(rows)], np.array(rows, dtype=np.float64).reshape(-1, 4)])


def classify(feat: np.ndarray, tc_max: float, vc_max: float,
             band_min: float) -> np.ndarray:
    snow = (feat[:, 1] < tc_max) & (feat[:, 2] < vc_max)
    bands = feat[:, 4] > band_min
    return snow | bands


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """Return [start, end) index pairs of True runs."""
    d = np.diff(np.concatenate(([0], mask.astype(np.int8), [0])))
    return list(zip(np.where(d == 1)[0], np.where(d == -1)[0]))


def find_noise(feat: np.ndarray, core: np.ndarray, weak: np.ndarray,
               min_noise: float, merge_gap: float, min_density: float,
               extend_gap: float,
               max_extend: float, pad: float, duration: float) -> list[Segment]:
    """Group noise frames into segments.

    core: frames that are clearly noise; weak: frames that look disturbed
    (used to grow segments over the ragged on/off transitions).
    """
    t = feat[:, 0]
    n = len(t)
    # how long each frame is on screen (variable frame rate safe)
    shown = np.diff(np.append(t, duration))

    def end_time(i: int) -> float:  # time where frame i stops being shown
        return t[i + 1] if i + 1 < n else duration

    merged: list[list[int]] = []  # inclusive frame indices
    for s, e in _runs(core):
        if merged and t[s] - end_time(merged[-1][1]) <= merge_gap:
            merged[-1][1] = e - 1
        else:
            merged.append([s, e - 1])

    segs: list[Segment] = []
    for s, e in merged:
        length = end_time(e) - t[s]
        if length < min_noise:
            continue
        # battery-swap snow is solid; in-flight breakup only sprinkles noise
        if shown[s:e + 1][core[s:e + 1]].sum() / length < min_density:
            continue
        # grow backwards across disturbed frames
        first, j = s, s - 1
        while j >= 0 and t[s] - t[j] <= max_extend:
            if weak[j]:
                first = j
            elif t[first] - t[j] > extend_gap:
                break
            j -= 1
        last, j = e, e + 1
        while j < n and t[j] - end_time(e) <= max_extend:
            if weak[j]:
                last = j
            elif t[j] - end_time(last) > extend_gap:
                break
            j += 1
        start = max(0.0, t[first] - pad)
        end = min(duration, end_time(last) + pad)
        if segs and start <= segs[-1].end + merge_gap:
            segs[-1].end = max(segs[-1].end, end)
        else:
            segs.append(Segment(start, end))
    return segs


def chunks_between(noise: list[Segment], duration: float,
                   min_chunk: float) -> list[Segment]:
    chunks = []
    t = 0.0
    for n in noise:
        if n.start - t >= min_chunk:
            chunks.append(Segment(t, n.start))
        t = max(t, n.end)
    if duration - t >= min_chunk:
        chunks.append(Segment(t, duration))
    return chunks


def fmt(t: float) -> str:
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:06.3f}"


def split(path: Path, chunks: list[Segment], outdir: Path, copy: bool,
          crf: int, preset: str) -> list[Path]:
    outdir.mkdir(parents=True, exist_ok=True)
    outputs = []
    for i, c in enumerate(chunks, 1):
        out = outdir / f"{path.stem}_{i:03d}{path.suffix}"
        cmd = ["ffmpeg", "-v", "error", "-y", "-ss", f"{c.start:.3f}",
               "-i", str(path), "-t", f"{c.duration:.3f}", "-map", "0:v:0",
               "-map", "0:a?"]
        if copy:
            cmd += ["-c", "copy", "-avoid_negative_ts", "make_zero"]
        else:
            cmd += ["-c:v", "libx264", "-crf", str(crf), "-preset", preset,
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k"]
        cmd += ["-movflags", "+faststart", str(out)]
        print(f"[{i}/{len(chunks)}] {fmt(c.start)} - {fmt(c.end)} -> {out}",
              file=sys.stderr)
        subprocess.run(cmd, check=True)
        outputs.append(out)
    return outputs


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Split an analog video recording at white-noise sections.")
    p.add_argument("input", type=Path, help="input video (e.g. .mp4)")
    p.add_argument("-o", "--outdir", type=Path,
                   help="output directory (default: <input>_chunks)")
    p.add_argument("--min-noise", type=float, default=1.0,
                   help="minimum noise duration in seconds to split at (default 1.0)")
    p.add_argument("--min-chunk", type=float, default=2.0,
                   help="drop chunks shorter than this many seconds (default 2.0)")
    p.add_argument("--merge-gap", type=float, default=1.0,
                   help="merge noise sections separated by <= this many seconds (default 1.0)")
    p.add_argument("--min-density", type=float, default=0.5,
                   help="fraction of a section that must be clear noise; rejects "
                        "interference during normal footage (default 0.5)")
    p.add_argument("--extend-gap", type=float, default=0.3,
                   help="when growing a noise section over its ragged edges, stop after "
                        "this many seconds of clean picture (default 0.3)")
    p.add_argument("--max-extend", type=float, default=3.0,
                   help="grow a noise section by at most this many seconds per side (default 3)")
    p.add_argument("--pad", type=float, default=0.1,
                   help="extra seconds cut away on both sides of each noise section (default 0.1)")
    p.add_argument("--tc-max", type=float, default=0.2,
                   help="max frame-to-frame correlation for noise (default 0.2)")
    p.add_argument("--vc-max", type=float, default=0.5,
                   help="max row-to-row correlation for noise (default 0.5)")
    p.add_argument("--band-min", type=float, default=0.8,
                   help="frames whose variance is at least this much explained by row "
                        "means count as sync-loss bands (default 0.8)")
    p.add_argument("--weak-tc", type=float, default=0.35,
                   help="frames below this frame-to-frame correlation count as disturbed "
                        "when growing noise sections (default 0.35)")
    p.add_argument("--copy", action="store_true",
                   help="stream-copy instead of re-encoding (fast, but cuts snap to keyframes)")
    p.add_argument("--crf", type=int, default=18, help="x264 CRF when re-encoding (default 18)")
    p.add_argument("--preset", default="medium", help="x264 preset (default medium)")
    p.add_argument("-n", "--dry-run", action="store_true",
                   help="only detect and report, do not write video chunks")
    p.add_argument("--features-csv", type=Path,
                   help="write per-frame features to this CSV for tuning")
    p.add_argument("-q", "--quiet", action="store_true")
    a = p.parse_args(argv)

    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            p.error(f"{tool} not found on PATH")
    if not a.input.is_file():
        p.error(f"{a.input} does not exist")

    duration = probe_duration(a.input)
    feat = analyze(a.input, progress=not a.quiet)
    mask = classify(feat, a.tc_max, a.vc_max, a.band_min)
    weak = mask | (feat[:, 1] < a.weak_tc) | (feat[:, 4] > a.band_min * 0.75)

    if a.features_csv:
        with a.features_csv.open("w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["time", "temporal_corr", "vertical_corr", "mean",
                        "row_band", "noise"])
            for row, m in zip(feat, mask):
                w.writerow([f"{row[0]:.3f}", f"{row[1]:.4f}", f"{row[2]:.4f}",
                            f"{row[3]:.1f}", f"{row[4]:.4f}", int(m)])

    noise = find_noise(feat, mask, weak, a.min_noise, a.merge_gap, a.min_density, a.extend_gap,
                       a.max_extend, a.pad, duration)
    chunks = chunks_between(noise, duration, a.min_chunk)

    print(f"\n{len(noise)} noise section(s):")
    for n in noise:
        print(f"  {fmt(n.start)} - {fmt(n.end)}  ({n.duration:6.1f}s)")
    print(f"\n{len(chunks)} chunk(s):")
    for i, c in enumerate(chunks, 1):
        print(f"  {i:3d}  {fmt(c.start)} - {fmt(c.end)}  ({c.duration:7.1f}s)")

    outdir = a.outdir or a.input.with_name(a.input.stem + "_chunks")
    report = {
        "input": str(a.input),
        "duration": duration,
        "noise": [{"start": n.start, "end": n.end} for n in noise],
        "chunks": [{"start": c.start, "end": c.end} for c in chunks],
    }
    if a.dry_run:
        return 0

    outs = split(a.input, chunks, outdir, a.copy, a.crf, a.preset)
    report["files"] = [str(o) for o in outs]
    (outdir / "segments.json").write_text(json.dumps(report, indent=2))
    print(f"\nwrote {len(outs)} file(s) to {outdir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
