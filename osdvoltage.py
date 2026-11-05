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


# --- flight analysis -------------------------------------------------------
#
# No readable OSD voltage = no flight. The readable samples are split into
# flights (one per battery), resting stretches (quad on the ground, battery
# unloaded and therefore reading higher) are removed, and what is left is put
# on a "flight time" axis on which the pauses do not exist. On that axis a
# weighted Gaussian low-pass smooths the curve; samples right after a pause get
# a weight that ramps up from 0, so the recovery spike of the rested battery
# does not pull the curve up.


def _level(v: np.ndarray, t: np.ndarray, i0: int, i1: int, span: float,
           from_end: bool = False) -> float:
    """Median voltage over the first (or last) `span` seconds of [i0, i1)."""
    if from_end:
        sel = slice(np.searchsorted(t[i0:i1], t[i1 - 1] - span) + i0, i1)
    else:
        sel = slice(i0, np.searchsorted(t[i0:i1], t[i0] + span, side="right") + i0)
    return float(np.nanmedian(v[sel]))


def split_flights(t: np.ndarray, v: np.ndarray, noise: list[dict],
                  gap: float = 3.0, swap_rise: float = 0.3,
                  full: float = 4.1) -> list[tuple[int, int]]:
    """Split readable samples into flights (one per battery pack).

    Readable stretches separated by more than `gap` seconds without OSD are
    pauses. A pause starts a new flight when the voltage afterwards is at
    least `swap_rise` higher than before and either looks like a full pack
    (>= `full` V/cell) or a noise section (battery swap) lies in the pause.
    Returns [start, end) index ranges into t/v.
    """
    ok = ~np.isnan(v)
    idx = np.where(ok)[0]
    if len(idx) == 0:
        return []
    breaks = np.where(np.diff(t[idx]) > gap)[0]          # pause after idx[b]
    stretches = np.split(idx, breaks + 1)
    flights: list[tuple[int, int]] = []
    start = stretches[0][0]
    for prev, nxt in zip(stretches, stretches[1:]):
        before = _level(v, t, prev[0], prev[-1] + 1, 5.0, from_end=True)
        after = _level(v, t, nxt[0], nxt[-1] + 1, 5.0)
        t0, t1 = t[prev[-1]], t[nxt[0]]
        swap_noise = any(n["start"] < t1 and n["end"] > t0 for n in noise)
        if after - before >= swap_rise and (after >= full or swap_noise):
            flights.append((start, prev[-1] + 1))
            start = nxt[0]
    flights.append((start, idx[-1] + 1))
    return flights


def _rolling_median(t: np.ndarray, v: np.ndarray, window: float) -> np.ndarray:
    lo = np.searchsorted(t, t - window / 2)
    hi = np.searchsorted(t, t + window / 2, side="right")
    return np.array([np.median(v[a:b]) for a, b in zip(lo, hi)])


def takeoff_index(t: np.ndarray, v: np.ndarray, drop: float = 0.15,
                  steady: float = 0.06, fast: float = 10.0,
                  min_rest: float = 3.0, plateau_frac: float = 0.8,
                  hold: float = 5.0, gap: float = 3.0) -> int:
    """Index (into t/v, both without NaN) of the first sample under load.

    A flight that starts on the ground (or in the hand) shows a plateau -
    the unloaded battery, which only ever creeps *up* while resting - and
    then a sag of a few tenths of a volt within seconds when the motors spin
    up, which persists. Everything before the returned index (plateau and
    spin-up transition) is not under load. "Within seconds" is measured in
    readable time - OSD gaps count as at most 1 s - because the video signal
    often drops out at takeoff. Returns 0 if there is no such plateau (OSD
    came on mid-flight, or a slowly decaying post-pause spike).

    Called on a time-reversed stretch it finds landings: the plateau is then
    at the end and the in-flight voltage before it is lower.

    The plateau may span OSD gaps (longer than `gap`), but every stretch
    between gaps that it covers must itself be mostly on the plateau - so a
    whole flight can never pass as "plateau" just because a rest follows.
    """
    if len(v) < 4:
        return 0
    stretch = np.r_[0, np.cumsum(np.diff(t) > gap)]
    t = t[0] + np.r_[0.0, np.cumsum(np.minimum(np.diff(t), 1.0))]
    m = _rolling_median(t, v, 3.0)
    start = float(np.median(v[t <= t[0] + 3.0]))
    below = np.where(m < start - drop)[0]
    if len(below) == 0:
        return 0
    k = int(below[0])                       # clearly under load from here
    if np.median(m[(t >= t[k]) & (t < t[k] + hold)]) >= start - drop:
        return 0                            # just a short dip, e.g. a punch
    on = m[:k] >= start - steady
    if not on.any():
        return 0
    end = int(np.where(on)[0][-1]) + 1      # last plateau sample + 1
    # the drop must be quick, the plateau long enough and mostly level
    # (short arming blips or misreads are fine, a slow discharge is not)
    if (t[k] - t[end - 1] > fast or t[end - 1] - t[0] < min_rest
            or on[:end].mean() < plateau_frac):
        return 0
    # the sag must happen right at the edge of the plateau: a decaying
    # post-pause spike also drops `drop` below where it started, but only
    # slowly relative to wherever it leaves the plateau band
    near = (t > t[end - 1]) & (t <= t[end - 1] + fast)
    if not np.any(m[near] < m[end - 1] - drop):
        return 0
    # a resting battery only recovers, so the plateau ends near its highest
    # level; a slow discharge (still flying) ends well below it
    if np.percentile(m[:end], 90) - m[end - 1] > 2 * steady:
        return 0
    for sid in np.unique(stretch[:end]):
        if on[:end][stretch[:end] == sid].mean() < plateau_frac:
            return 0
    return k


def rest_mask(t: np.ndarray, raw: np.ndarray, v: np.ndarray,
              flights: list[tuple[int, int]], window: float = 8.0,
              flat: float = 0.015, gap: float = 3.0) -> np.ndarray:
    """True where the battery is not under load (quad resting).

    Two signals: plateaus before takeoff / after landing (see
    takeoff_index), and any stretch where the OSD reading is flat (10-90 %
    spread over `window` s <= `flat` V) - under load the reading jitters.
    """
    rest = np.zeros(len(t), bool)
    ok = ~np.isnan(v) & ~np.isnan(raw)
    idx = np.where(ok)[0]
    tt, rr = t[idx], raw[idx]
    lo = np.searchsorted(tt, tt - window / 2)
    hi = np.searchsorted(tt, tt + window / 2, side="right")
    for k, (a, b) in enumerate(zip(lo, hi)):
        if b - a >= 8:
            p10, p90 = np.percentile(rr[a:b], [10, 90])
            if p90 - p10 <= flat:
                rest[idx[k]] = True
    for i0, i1 in flights:
        seg = np.where(~np.isnan(v[i0:i1]))[0] + i0
        if len(seg) == 0:
            continue
        # every stretch between OSD pauses may start with a rest plateau
        # (takeoff) and end with one (landing - same test, time reversed)
        cut = np.where(np.diff(t[seg]) > gap)[0] + 1
        bounds = list(zip(np.r_[0, cut], np.r_[cut, len(seg)]))
        for s0, e in bounds:
            k = takeoff_index(t[seg[s0:]], v[seg[s0:]], gap=gap)
            if k:
                rest[seg[s0]:seg[s0 + k]] = True
        for s0, e in bounds:
            k = takeoff_index(-t[seg[:e]][::-1], v[seg[:e]][::-1], gap=gap)
            if k:
                rest[seg[e - k]:seg[e - 1] + 1] = True
    return rest & ~np.isnan(v)


def flight_axis(t: np.ndarray, load: np.ndarray, rest: np.ndarray,
                gap: float = 3.0) -> tuple[np.ndarray, np.ndarray]:
    """Cumulative flight time (pauses removed) for each sample under load.

    Returns (ft, resumed): ft is NaN where not under load; resumed marks the
    first sample after a pause (gap longer than `gap` or a rest).
    """
    ft = np.full(len(t), np.nan)
    resumed = np.zeros(len(t), bool)
    idx = np.where(load)[0]
    if len(idx) == 0:
        return ft, resumed
    dt = np.diff(t[idx])
    contiguous = dt <= gap
    # a step counts as flight time only if no rest sample lies in between
    between_rest = np.diff(np.cumsum(rest)[idx]) > 0
    step = np.where(contiguous & ~between_rest, dt, 0.0)
    ft[idx] = np.r_[0.0, np.cumsum(step)]
    resumed[idx[1:][step == 0]] = True
    resumed[idx[0]] = True
    return ft, resumed


def lowpass(ft: np.ndarray, v: np.ndarray, resumed: np.ndarray,
            sigma: float = 4.0, settle: float = 15.0) -> np.ndarray:
    """Weighted Gaussian low-pass of v over the flight-time axis.

    Samples within `settle` s of flight time after a pause are weighted
    linearly from 0 to 1, so the rested-battery spike is bridged by the
    readings around it instead of being smoothed into the curve.
    """
    out = np.full(len(v), np.nan)
    idx = np.where(~np.isnan(ft) & ~np.isnan(v))[0]
    if len(idx) == 0:
        return out
    x, y = ft[idx], v[idx]
    last = np.maximum.accumulate(np.where(resumed[idx], x, -np.inf))
    w = np.clip((x - last) / settle, 0.0, 1.0) if settle > 0 else np.ones(len(x))
    w[np.isnan(w)] = 1.0
    # keep a tiny weight so a curve exists even if a stretch is all "settling"
    w = np.maximum(w, 1e-3)
    reach = 3 * sigma
    lo = np.searchsorted(x, x - reach)
    hi = np.searchsorted(x, x + reach, side="right")
    for k in range(len(x)):
        xs = x[lo[k]:hi[k]]
        g = np.exp(-0.5 * ((xs - x[k]) / sigma) ** 2) * w[lo[k]:hi[k]]
        out[idx[k]] = float(np.dot(g, y[lo[k]:hi[k]]) / g.sum())
    return out


def analyse_flights(t: np.ndarray, raw: np.ndarray, v: np.ndarray,
                    noise: list[dict], gap: float = 3.0, sigma: float = 4.0,
                    settle: float = 15.0, min_flight: float = 30.0,
                    resample: float = 1.0) -> dict:
    """Per-flight load curves and stats plus the pauses-removed series."""
    flights = split_flights(t, v, noise, gap=gap)
    rest = rest_mask(t, raw, v, flights, gap=gap)
    load = ~np.isnan(v) & ~rest
    ft_all, resumed = flight_axis(t, load, rest, gap)
    lp_all = np.full(len(t), np.nan)
    out_flights = []
    for n, (i0, i1) in enumerate(flights, 1):
        sl = slice(i0, i1)
        seg_ok = np.where(~np.isnan(v[sl]))[0] + i0
        seg_load = np.where(load[sl])[0] + i0
        info = {
            "n": n,
            "start": round(float(t[seg_ok[0]]), 2),
            "end": round(float(t[seg_ok[-1]]), 2),
        }
        k = takeoff_index(t[seg_ok], v[seg_ok])
        info["rest_v"] = round(float(np.median(v[seg_ok[:k]])), 3) if k else None
        if len(seg_load) < 2:
            info.update(flight_time=0.0, valid=False, reason="no flight under load")
            out_flights.append(info)
            continue
        ft = ft_all[seg_load] - ft_all[seg_load[0]]
        # takeoff counts as a resume too: it smooths the spin-up transient
        res = resumed[seg_load].copy()
        res[0] = True
        lp = lowpass(np.r_[ft], v[seg_load], res, sigma, settle)
        lp_all[seg_load] = lp
        dur = float(ft[-1])
        info["takeoff"] = round(float(t[seg_load[0]]), 2)
        info["flight_time"] = round(dur, 1)
        info["pauses"] = int(res.sum()) - 1
        info["valid"] = dur >= min_flight
        if not info["valid"]:
            info["reason"] = f"shorter than {min_flight:.0f} s"
        # resample to a regular flight-time grid for plotting / comparing
        grid = np.arange(0.0, dur + 1e-9, resample)
        curve = np.interp(grid, ft, lp)
        vt = np.interp(grid, ft, t[seg_load])
        info["ft"] = [round(float(x), 1) for x in grid]
        info["v"] = [round(float(x), 3) for x in curve]
        info["t"] = [round(float(x), 2) for x in vt]
        head = curve[grid <= min(10.0, dur)]
        info["v_start"] = round(float(np.median(head)), 3)
        info["v_end"] = round(float(curve[-1]), 3)
        info["v_min"] = round(float(curve.min()), 3)
        info["v_mean"] = round(float(curve.mean()), 3)
        info["sag"] = (round(info["rest_v"] - info["v_start"], 3)
                       if info["rest_v"] is not None else None)
        info["drop_per_min"] = (round(float(np.polyfit(grid, curve, 1)[0] * 60), 4)
                                if len(grid) >= 3 else None)
        below = np.where(curve < 3.5)[0]
        info["t_to_3_5"] = round(float(grid[below[0]]), 1) if len(below) else None
        out_flights.append(info)

    def arr(x: np.ndarray, nd: int) -> list:
        return [None if np.isnan(a) else round(float(a), nd) for a in x]

    valid = [f for f in out_flights if f.get("valid")]
    summary = {
        "flights": len(valid),
        "total_flight_time": round(sum(f["flight_time"] for f in valid), 1),
    }
    if valid:
        for k in ("flight_time", "v_start", "v_end", "drop_per_min"):
            vals = [f[k] for f in valid if f.get(k) is not None]
            if vals:
                summary[k + "_mean"] = round(float(np.mean(vals)), 3)
        sags = [f["sag"] for f in valid if f.get("sag") is not None]
        if sags:
            summary["sag_mean"] = round(float(np.mean(sags)), 3)
    return {
        "params": {"gap": gap, "sigma": sigma, "settle": settle,
                   "min_flight": min_flight},
        "summary": summary,
        "flights": out_flights,
        "rest": [int(x) for x in rest],
        "ft": arr(ft_all, 2),
        "lowpass": arr(lp_all, 3),
    }


def build_html(report: dict, video_rel: str, out: Path) -> None:
    html = TEMPLATE.read_text()
    payload = json.dumps(report, separators=(",", ":")).replace("</", "<\\/")
    html = html.replace("/*__DATA__*/null", payload)
    html = html.replace("__VIDEO_SRC__", quote(video_rel))
    html = html.replace("__TITLE__", Path(report["input"]).name.replace("<", "&lt;"))
    out.write_text(html)


def _nan(x: list) -> np.ndarray:
    return np.array([np.nan if v is None else v for v in x], float)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Read the OSD battery voltage from an analog FPV recording.")
    p.add_argument("input", type=Path,
                   help="input video (e.g. .mp4), or a previously written "
                        "<input>_voltage.json to only redo the flight analysis")
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
    g = p.add_argument_group("flight analysis")
    g.add_argument("--gap", type=float, default=3.0,
                   help="seconds without OSD that count as a pause (default 3)")
    g.add_argument("--lowpass", type=float, default=4.0, metavar="SIGMA",
                   help="Gaussian low-pass sigma in seconds of flight time (default 4)")
    g.add_argument("--settle", type=float, default=15.0,
                   help="seconds after a pause/takeoff over which readings are "
                        "faded in, to suppress the rested-battery spike (default 15)")
    g.add_argument("--min-flight", type=float, default=30.0,
                   help="flights shorter than this (s under load) are not compared (default 30)")
    p.add_argument("-q", "--quiet", action="store_true")
    a = p.parse_args(argv)

    if not a.input.is_file():
        p.error(f"{a.input} does not exist")

    from_json = a.input.suffix.lower() == ".json"
    if from_json:
        report = json.loads(a.input.read_text())
        video = Path(report["input"])
        if not video.is_absolute() and not video.exists():
            video = a.input.parent / video
        t = np.array(report["t"], float)
        raw, smooth = _nan(report["raw"]), _nan(report["voltage"])
        noise = report.get("noise", [])
        stem_path = a.input.with_name(a.input.name.removesuffix(".json")
                                      .removesuffix("_voltage"))
    else:
        for tool in ("ffmpeg", "ffprobe"):
            if not shutil.which(tool):
                p.error(f"{tool} not found on PATH")
        video = a.input
        stem_path = a.input.with_suffix("")
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

    seg_path = a.segments or video.with_name(video.stem + "_chunks") / "segments.json"
    if a.segments or not from_json:
        if seg_path.is_file():
            try:
                noise = json.loads(seg_path.read_text()).get("noise", [])
            except (OSError, json.JSONDecodeError):
                noise = []
        report["noise"] = noise

    report["analysis"] = analyse_flights(
        t, raw, smooth, noise, gap=a.gap, sigma=a.lowpass, settle=a.settle,
        min_flight=a.min_flight)

    out = a.output or Path(str(stem_path) + "_voltage.json")
    out.write_text(json.dumps(report, separators=(",", ":")))
    sm = report["analysis"]["summary"]
    print(f"wrote {out}  ({report['stats']['readable']}/{report['stats']['samples']} "
          f"samples readable, {report['stats']['min']}-{report['stats']['max']} V, "
          f"{sm['flights']} flights, {sm['total_flight_time'] / 60:.1f} min under load)")

    if not a.no_html:
        html = a.html or Path(str(stem_path) + "_voltage.html")
        rel = os.path.relpath(video.resolve(), html.resolve().parent)
        build_html(report, Path(rel).as_posix(), html)
        print(f"wrote {html}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
