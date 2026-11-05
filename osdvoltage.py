#!/usr/bin/env python3
"""Read the per-cell battery voltage from a Betaflight-style analog OSD
(bottom-left "3.52v" element) of a recorded FPV video.

Writes a JSON time series and an HTML page that plays the video next to a
voltage graph - scrub either one and the other follows.

How it reads digits: the three digit cells of "D.DDv" are cut out of every
sampled frame and compared (normalised cross-correlation, small shift
search) against a bank of hand-labelled digit images (osd_digits.npz), which
also contains a "junk" class for noise / missing OSD. Low-confidence or
implausible readings are dropped and the rest are median-filtered over time,
which removes the remaining single-frame misreads.

Requires: ffmpeg/ffprobe on PATH, numpy.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from urllib.parse import quote

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from noisesplit import PTS_RE, probe_duration

HERE = Path(__file__).resolve().parent
BANK = HERE / "osd_digits.npz"
TEMPLATE = HERE / "voltage_template.html"

# Layout at 640x480 (videos are scaled to this before reading).
FRAME_W, FRAME_H = 640, 480
CROP_X, CROP_Y, CROP_W, CROP_H = 0, 300, 260, 180   # region we decode
CELL_Y = 443 - CROP_Y           # top of the digit cells inside the crop
CELL_XS = (46, 91, 113)         # left edge of "D", "D", "D" in "D.DDv"
CH, CW = 22, 14                 # digit cell size
SHIFT = 3                       # per-cell search +-SHIFT px
CAL_DY = range(-30, 13, 3)      # global layout offsets tried by --calibrate
CAL_DX = range(-30, 31, 3)


def _norm(w: np.ndarray) -> np.ndarray:
    mu = w.mean(axis=(-1, -2), keepdims=True)
    sd = w.std(axis=(-1, -2), keepdims=True) + 1e-3
    return (w - mu) / sd


def load_bank(path: Path = BANK) -> tuple[np.ndarray, np.ndarray]:
    d = np.load(path)
    cells = _norm(d["cells"].astype(np.float32)).reshape(len(d["cells"]), -1)
    return cells / (CH * CW), d["labels"].astype(int)


def extract(path: Path, fps: float, progress: bool) -> tuple[np.ndarray, np.ndarray]:
    """Decode the bottom-left OSD region at `fps`. Returns (times, frames)."""
    duration = probe_duration(path)
    vf = (f"fps={fps},scale={FRAME_W}:{FRAME_H},"
          f"crop={CROP_W}:{CROP_H}:{CROP_X}:{CROP_Y},format=gray,showinfo")
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "info",
           "-i", str(path), "-an", "-sn", "-vf", vf, "-f", "rawvideo", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    times: list[float] = []
    errors: list[str] = []

    def read_stderr() -> None:
        for raw in proc.stderr:
            line = raw.decode(errors="replace")
            m = PTS_RE.search(line)
            if m:
                times.append(float(m.group(1)))
            elif "rror" in line:
                errors.append(line.strip())

    th = threading.Thread(target=read_stderr, daemon=True)
    th.start()
    size = CROP_W * CROP_H
    frames = []
    while True:
        buf = proc.stdout.read(size)
        if len(buf) < size:
            break
        frames.append(np.frombuffer(buf, np.uint8).reshape(CROP_H, CROP_W))
        if progress and len(frames) % int(fps * 60) == 0:
            t = len(frames) / fps
            print(f"\rreading OSD {t:7.0f}s / {duration:.0f}s", end="",
                  file=sys.stderr, flush=True)
    proc.stdout.close()
    proc.wait()
    th.join()
    proc.stderr.close()
    if progress:
        print(file=sys.stderr)
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg failed: " + "\n".join(errors[-5:]))
    n = len(frames)
    t = np.array(times[:n]) if len(times) >= n else np.arange(n) / fps
    return t, np.array(frames)


def _cell_windows(frame: np.ndarray, dx: int, dy: int) -> np.ndarray | None:
    """All shifted, normalised digit windows: (3, shifts, CH*CW)."""
    out = []
    for x in CELL_XS:
        y0, x0 = CELL_Y + dy - SHIFT, x + dx - SHIFT
        if y0 < 0 or x0 < 0 or y0 + CH + 2 * SHIFT > CROP_H or x0 + CW + 2 * SHIFT > CROP_W:
            return None
        win = frame[y0:y0 + CH + 2 * SHIFT, x0:x0 + CW + 2 * SHIFT].astype(np.float32)
        out.append(_norm(sliding_window_view(win, (CH, CW))).reshape(-1, CH * CW))
    return np.stack(out)


def calibrate(frames: np.ndarray, bank: np.ndarray, labels: np.ndarray,
              samples: int = 120) -> tuple[int, int]:
    """Find the global OSD offset that makes digits match best."""
    digit_means = np.stack([bank[labels == d].mean(0) for d in range(10)])
    idx = np.linspace(0, len(frames) - 1, min(samples, len(frames))).astype(int)
    best, best_off = -1.0, (0, 0)
    for dy in CAL_DY:
        for dx in CAL_DX:
            scores = []
            for i in idx:
                w = _cell_windows(frames[i], dx, dy)
                if w is None:
                    break
                scores.append((w @ digit_means.T).max(axis=(1, 2)).min())
            if len(scores) != len(idx):
                continue
            # frames without OSD score low everywhere; rank by the good ones
            s = float(np.mean(np.sort(scores)[-len(scores) // 3:]))
            if s > best:
                best, best_off = s, (dx, dy)
    return best_off


def read_digits(frames: np.ndarray, bank: np.ndarray, labels: np.ndarray,
                dx: int, dy: int) -> tuple[np.ndarray, np.ndarray]:
    """Return (digits[n,3], similarity[n,3]); digit -1 = no digit."""
    n = len(frames)
    digits = np.full((n, 3), -1, int)
    sim = np.zeros((n, 3))
    for i in range(n):
        w = _cell_windows(frames[i], dx, dy)
        if w is None:
            continue
        s = (w @ bank.T).max(axis=1)          # (3, bank)
        b = s.argmax(axis=1)
        digits[i] = labels[b]
        sim[i] = s[np.arange(3), b]
    return digits, sim


def to_voltage(t: np.ndarray, digits: np.ndarray, sim: np.ndarray,
               min_sim: float, vmin: float, vmax: float, window: float,
               max_dev: float) -> tuple[np.ndarray, np.ndarray]:
    """Raw per-frame voltage (nan if unreadable) and the filtered series."""
    raw = digits[:, 0] + digits[:, 1] / 10 + digits[:, 2] / 100
    ok = (digits.min(1) >= 0) & (sim.min(1) >= min_sim) & (raw >= vmin) & (raw <= vmax)
    raw = np.where(ok, raw, np.nan)
    med = np.full(len(raw), np.nan)
    lo = np.searchsorted(t, t - window / 2)
    hi = np.searchsorted(t, t + window / 2, side="right")
    for i in range(len(raw)):
        w = raw[lo[i]:hi[i]]
        w = w[~np.isnan(w)]
        if len(w) >= 3:
            med[i] = np.median(w)
    # only report where this frame itself was readable and agrees with
    # its neighbourhood (drops isolated misreads), plus short gaps inside
    # well-read stretches
    good = ~np.isnan(raw) & (np.abs(raw - med) <= max_dev)
    smooth = np.where(good | (~np.isnan(med) & np.isnan(raw)), med, np.nan)
    return raw, smooth


def build_html(report: dict, video_rel: str, out: Path) -> None:
    html = TEMPLATE.read_text()
    payload = json.dumps(report, separators=(",", ":")).replace("</", "<\\/")
    html = html.replace("/*__DATA__*/null", payload)
    html = html.replace("__VIDEO_SRC__", quote(video_rel))
    html = html.replace("__TITLE__", Path(report["input"]).name.replace("<", "&lt;"))
    out.write_text(html)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Read the OSD battery voltage from an analog FPV recording.")
    p.add_argument("input", type=Path, help="input video (e.g. .mp4)")
    p.add_argument("-o", "--output", type=Path,
                   help="output JSON (default: <input>_voltage.json)")
    p.add_argument("--html", type=Path,
                   help="output HTML viewer (default: <input>_voltage.html)")
    p.add_argument("--no-html", action="store_true", help="only write the JSON")
    p.add_argument("--fps", type=float, default=4.0,
                   help="frames per second to read (default 4)")
    p.add_argument("--offset", type=int, nargs=2, metavar=("DX", "DY"),
                   help="OSD position offset in px at 640x480 (default: auto-calibrate)")
    p.add_argument("--min-sim", type=float, default=0.7,
                   help="minimum digit match similarity (default 0.7)")
    p.add_argument("--vmin", type=float, default=2.8,
                   help="lowest plausible per-cell voltage (default 2.8)")
    p.add_argument("--vmax", type=float, default=4.45,
                   help="highest plausible per-cell voltage (default 4.45)")
    p.add_argument("--window", type=float, default=2.0,
                   help="median filter window in seconds (default 2)")
    p.add_argument("--max-dev", type=float, default=0.05,
                   help="drop raw readings further than this from the median (default 0.05 V)")
    p.add_argument("--segments", type=Path,
                   help="noisesplit segments.json to mark noise in the graph "
                        "(default: <input>_chunks/segments.json if present)")
    p.add_argument("-q", "--quiet", action="store_true")
    a = p.parse_args(argv)

    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            p.error(f"{tool} not found on PATH")
    if not a.input.is_file():
        p.error(f"{a.input} does not exist")

    bank, labels = load_bank()
    duration = probe_duration(a.input)
    t, frames = extract(a.input, a.fps, progress=not a.quiet)
    if a.offset:
        dx, dy = a.offset
    else:
        dx, dy = calibrate(frames, bank, labels)
        if not a.quiet:
            print(f"OSD offset: dx={dx} dy={dy}", file=sys.stderr)
    digits, sim = read_digits(frames, bank, labels, dx, dy)
    raw, smooth = to_voltage(t, digits, sim, a.min_sim, a.vmin, a.vmax,
                             a.window, a.max_dev)

    def arr(x: np.ndarray, nd: int) -> list:
        return [None if np.isnan(v) else round(float(v), nd) for v in x]

    noise = []
    seg_path = a.segments or a.input.with_name(a.input.stem + "_chunks") / "segments.json"
    if seg_path.is_file():
        try:
            noise = json.loads(seg_path.read_text()).get("noise", [])
        except (OSError, json.JSONDecodeError):
            noise = []

    readable = ~np.isnan(raw)
    report = {
        "input": str(a.input),
        "duration": duration,
        "fps": a.fps,
        "osd_offset": [int(dx), int(dy)],
        "unit": "V per cell",
        "stats": {
            "samples": int(len(t)),
            "readable": int(readable.sum()),
            "min": None if np.all(np.isnan(smooth)) else round(float(np.nanmin(smooth)), 2),
            "max": None if np.all(np.isnan(smooth)) else round(float(np.nanmax(smooth)), 2),
        },
        "noise": noise,
        "t": [round(float(x), 3) for x in t],
        "raw": arr(raw, 2),
        "confidence": [round(float(x), 3) for x in sim.min(1)],
        "voltage": arr(smooth, 3),
    }
    out = a.output or a.input.with_name(a.input.stem + "_voltage.json")
    out.write_text(json.dumps(report, separators=(",", ":")))
    print(f"wrote {out}  ({report['stats']['readable']}/{len(t)} samples readable, "
          f"{report['stats']['min']}-{report['stats']['max']} V)")

    if not a.no_html:
        html = a.html or a.input.with_name(a.input.stem + "_voltage.html")
        rel = os.path.relpath(a.input.resolve(), html.resolve().parent)
        build_html(report, Path(rel).as_posix(), html)
        print(f"wrote {html}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
