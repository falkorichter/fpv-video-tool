# white-noise-separator

Split an analog video recording (e.g. FPV goggles / DVR capture) into chunks
wherever the picture turns into white noise for more than a second — for
example every time the battery of the transmitter is swapped.

Detection uses **only the video stream** (the audio of such recordings is
noisy all the time anyway).

## Requirements

- `ffmpeg` / `ffprobe` on `PATH` (`brew install ffmpeg`)
- Python 3.9+ with `numpy` (`pip install numpy`)

## Usage

```sh
# just look at what would be cut
./noisesplit.py --dry-run recording.mp4

# split into recording_chunks/recording_001.mp4, _002.mp4, ...
./noisesplit.py recording.mp4

# fast lossless split (cuts snap to keyframes, so a bit of noise may remain)
./noisesplit.py --copy recording.mp4
```

Next to the chunks, a `segments.json` lists the detected noise sections and
chunk boundaries.

By default the chunks are re-encoded with x264 (`--crf 18`), so the cuts are
frame-accurate. Use `--preset veryfast` to speed that up.

### All options

```
usage: noisesplit.py [-h] [-o OUTDIR] [--min-noise MIN_NOISE]
                     [--min-chunk MIN_CHUNK] [--merge-gap MERGE_GAP]
                     [--min-density MIN_DENSITY] [--extend-gap EXTEND_GAP]
                     [--max-extend MAX_EXTEND] [--pad PAD] [--tc-max TC_MAX]
                     [--vc-max VC_MAX] [--band-min BAND_MIN]
                     [--weak-tc WEAK_TC] [--copy] [--crf CRF]
                     [--preset PRESET] [-n] [--features-csv FEATURES_CSV] [-q]
                     input

Split an analog video recording at white-noise sections.

positional arguments:
  input                 input video (e.g. .mp4)

options:
  -h, --help            show this help message and exit
  -o, --outdir OUTDIR   output directory (default: <input>_chunks)
  --min-noise MIN_NOISE
                        minimum noise duration in seconds to split at (default
                        1.0)
  --min-chunk MIN_CHUNK
                        drop chunks shorter than this many seconds (default
                        2.0)
  --merge-gap MERGE_GAP
                        merge noise sections separated by <= this many seconds
                        (default 1.0)
  --min-density MIN_DENSITY
                        fraction of a section that must be clear noise;
                        rejects interference during normal footage (default
                        0.5)
  --extend-gap EXTEND_GAP
                        when growing a noise section over its ragged edges,
                        stop after this many seconds of clean picture (default
                        0.3)
  --max-extend MAX_EXTEND
                        grow a noise section by at most this many seconds per
                        side (default 3)
  --pad PAD             extra seconds cut away on both sides of each noise
                        section (default 0.1)
  --tc-max TC_MAX       max frame-to-frame correlation for noise (default 0.2)
  --vc-max VC_MAX       max row-to-row correlation for noise (default 0.5)
  --band-min BAND_MIN   frames whose variance is at least this much explained
                        by row means count as sync-loss bands (default 0.8)
  --weak-tc WEAK_TC     frames below this frame-to-frame correlation count as
                        disturbed when growing noise sections (default 0.35)
  --copy                stream-copy instead of re-encoding (fast, but cuts
                        snap to keyframes)
  --crf CRF             x264 CRF when re-encoding (default 18)
  --preset PRESET       x264 preset (default medium)
  -n, --dry-run         only detect and report, do not write video chunks
  --features-csv FEATURES_CSV
                        write per-frame features to this CSV for tuning
  -q, --quiet
```

## How it works

Every frame is decoded (at its real timestamp — analog captures are often
variable frame rate), scaled down to 160×120 grayscale and measured:

| feature | snow | sync-loss bands | real picture |
|---|---|---|---|
| correlation with the previous frame | ~0 | medium | high |
| correlation between neighbouring rows | ~0 | high | high |
| share of variance explained by row means | low | ~1 | low |

A frame is *noise* if it is snow (both correlations low) or bands
(row share > `--band-min`). Noise frames are grouped into sections
(gaps up to `--merge-gap` are bridged); a section counts if it is at least
`--min-noise` seconds long **and** at least `--min-density` of it is noise —
that rejects interference/breakup during normal flight. Sections are then
grown over their ragged on/off transitions and padded by `--pad`.

## Tuning

Write the per-frame measurements to a CSV and look at them:

```sh
./noisesplit.py -n recording.mp4 --features-csv features.csv
```

All thresholds are options; see [All options](#all-options).

## Tests

```sh
python3 -m unittest -v
```

The test builds a synthetic clip with ffmpeg (test pattern, snow, bands) and
checks detection and splitting.
