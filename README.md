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

All thresholds are options; see `./noisesplit.py --help`.

## Tests

```sh
python3 -m unittest -v
```

The test builds a synthetic clip with ffmpeg (test pattern, snow, bands) and
checks detection and splitting.
