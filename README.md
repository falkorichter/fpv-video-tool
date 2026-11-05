# fpv-video-tool

Splits analog FPV recordings (goggles / DVR captures) into chunks wherever
the picture turns into white noise for more than a second, e.g. at every
battery swap.

- `noisesplit.py`: the splitter (command line).
- `splitserver.py`: a small web front end for a home server. Upload a
  recording from your phone or laptop over Tailscale; the server splits it
  and lists the chunks for download
  (see [Upload server](#upload-server-over-tailscale)).

Detection uses **only the video stream**, because the audio of such
recordings is noisy all the time anyway.

Analysing the recordings or chunks (battery voltage from the OSD, comparing
flights) lives in a separate repository:
[fpv-video-analysis](https://github.com/falkorichter/fpv-video-analysis).

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

Example run on a 40-minute FPV recording (`sample.mp4`, not in the repo):

```
$ python3 noisesplit.py sample.mp4
analyzing   2400.0s / 2408.4s ( 99.6%)

9 noise section(s):
  00:03:56.275 - 00:04:13.041  (  16.8s)
  00:08:07.775 - 00:08:11.953  (   4.2s)
  00:08:36.808 - 00:08:40.446  (   3.6s)
  00:08:48.317 - 00:09:31.973  (  43.7s)
  00:13:49.390 - 00:13:58.054  (   8.7s)
  00:17:38.342 - 00:17:51.884  (  13.5s)
  00:23:34.392 - 00:24:00.916  (  26.5s)
  00:31:13.300 - 00:31:22.387  (   9.1s)
  00:34:42.954 - 00:35:05.441  (  22.5s)

10 chunk(s):
    1  00:00:00.000 - 00:03:56.275  (  236.3s)
    2  00:04:13.041 - 00:08:07.775  (  234.7s)
    3  00:08:11.953 - 00:08:36.808  (   24.9s)
    4  00:08:40.446 - 00:08:48.317  (    7.9s)
    5  00:09:31.973 - 00:13:49.390  (  257.4s)
    6  00:13:58.054 - 00:17:38.342  (  220.3s)
    7  00:17:51.884 - 00:23:34.392  (  342.5s)
    8  00:24:00.916 - 00:31:13.300  (  432.4s)
    9  00:31:22.387 - 00:34:42.954  (  200.6s)
   10  00:35:05.441 - 00:40:08.448  (  303.0s)
[1/10] 00:00:00.000 - 00:03:56.275 -> sample_chunks/sample_001.mp4
[2/10] 00:04:13.041 - 00:08:07.775 -> sample_chunks/sample_002.mp4
[3/10] 00:08:11.953 - 00:08:36.808 -> sample_chunks/sample_003.mp4
[4/10] 00:08:40.446 - 00:08:48.317 -> sample_chunks/sample_004.mp4
[5/10] 00:09:31.973 - 00:13:49.390 -> sample_chunks/sample_005.mp4
[6/10] 00:13:58.054 - 00:17:38.342 -> sample_chunks/sample_006.mp4
[7/10] 00:17:51.884 - 00:23:34.392 -> sample_chunks/sample_007.mp4
[8/10] 00:24:00.916 - 00:31:13.300 -> sample_chunks/sample_008.mp4
[9/10] 00:31:22.387 - 00:34:42.954 -> sample_chunks/sample_009.mp4
[10/10] 00:35:05.441 - 00:40:08.448 -> sample_chunks/sample_010.mp4
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
                     [--preset PRESET] [-n] [--report REPORT]
                     [--features-csv FEATURES_CSV] [-q]
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
  --report REPORT       also write the detected sections as JSON to this file
                        (works with --dry-run)
  --features-csv FEATURES_CSV
                        write per-frame features to this CSV for tuning
  -q, --quiet
```

## Upload server (over Tailscale)

`splitserver.py` runs on a machine at home and accepts uploads over HTTP.
Each upload becomes a job; jobs are split one after another with
`noisesplit.py` in the background. The page shows upload and split progress,
and links to the chunks, `segments.json` and the log. Chunks are served with
range support, so they play right in the browser. Jobs survive a restart:
finished ones stay listed, interrupted ones start over.

```sh
./splitserver.py --data ~/fpv-uploads --split-args "--preset veryfast"
tailscale serve --bg 8080        # https://<machine>.<tailnet>.ts.net -> 127.0.0.1:8080
```

Then open `https://<machine>.<tailnet>.ts.net` on any device in your
tailnet. Drop videos on the page, or upload from a terminal:

```sh
curl -T recording.mp4 https://<machine>.<tailnet>.ts.net/upload/recording.mp4
curl -T recording.mp4 "https://<machine>.<tailnet>.ts.net/upload/recording.mp4?copy=1"  # lossless, faster
```

**There is no authentication.** By default the server only listens on
`127.0.0.1`. `tailscale serve` (not `tailscale funnel`, which would put it on
the internet) makes it reachable from your tailnet only, with HTTPS.
Alternatively, bind it directly to the machine's Tailscale address with
`--host 100.x.y.z`.

Storage: every job gets a directory `<data>/<job-id>/` holding the upload,
`<name>_chunks/` (the chunks and `segments.json`), `log.txt` and `job.json`.
Uploads are streamed to disk. Before accepting one, the server checks that
there is room for about twice its size (upload plus chunks). Use
`--delete-input` to keep only the chunks.

API, for scripts:

| | |
|---|---|
| `PUT /upload/<name>[?copy=1]` | upload a video (raw request body), starts a job, returns it |
| `GET /api/jobs`, `GET /api/jobs/<id>` | job status (`status`, `progress`, `message`, `chunks`, `noise`, `files`) |
| `DELETE /api/jobs/<id>` | delete a finished job and its files |
| `GET /files/<id>/<path>` | download a file of the job (`?download` for an attachment) |

To keep it running on a Linux home server, use a systemd unit, e.g.
`/etc/systemd/system/splitserver.service`:

```ini
[Unit]
Description=noisesplit upload server
After=network-online.target

[Service]
User=fpv
ExecStart=/usr/bin/python3 /opt/fpv-video-tool/splitserver.py --data /srv/fpv-uploads --split-args "--preset veryfast" -q
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```sh
sudo systemctl enable --now splitserver
sudo tailscale serve --bg 8080
```

```
usage: splitserver.py [-h] [--data DATA] [--host HOST] [--port PORT]
                      [--split-args SPLIT_ARGS] [--delete-input]
                      [--max-upload GIB] [--min-free GIB] [-q]

HTTP upload front end for noisesplit (for use over Tailscale).

options:
  -h, --help            show this help message and exit
  --data DATA           where uploads and chunks are stored (default
                        ./uploads)
  --host HOST           address to listen on (default 127.0.0.1; use
                        `tailscale serve` or your Tailscale IP - there is no
                        authentication)
  --port PORT           port (default 8080)
  --split-args SPLIT_ARGS
                        extra noisesplit options for every job, e.g. "--preset
                        veryfast"
  --delete-input        delete the uploaded video once it is split (keep only
                        the chunks)
  --max-upload GIB      largest accepted upload in GiB (default 64)
  --min-free GIB        keep at least this much disk free in GiB (default 1)
  -q, --quiet           no request log
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

The tests build a synthetic clip with ffmpeg (test pattern, snow, bands).
They check detection and splitting, and run the upload server end to end:
upload, split, range download, delete, rejected file names and paths, and
jobs surviving a restart.

## How this was built

This tool was written by Claude Code (model `claude-opus-5-5`) in a single
session on 2026-11-05, tuned against a 40-minute FPV recording.

### Original prompt

> build a tool that reads an mp4 file and identifies white noise areas (more
> than one second) and splits the file into individual chunks. use case: a
> recording of an analog video, every time I change the battery on the
> device, i have white noise. use the video stream, the audio is always quite
> noisy

(Mid-session follow-up: "I added a sample file (sample.mp4)".)

The same session then added `osdvoltage.py` (battery voltage from the OSD),
which has since moved to
[fpv-video-analysis](https://github.com/falkorichter/fpv-video-analysis).

Follow-up prompt that added `splitserver.py` and split the repository:

> can we separte the readme into the splitting og the video and the analysis
> of the chunks eventually those are 2 use cases and should propably be 2
> repositories. actually split the two usecases, the analysis should go in
> git@github.com:falkorichter/fpv-video-analysis.git and add a little http
> interface so I could upload a video via tailscale and let my home server
> split the videos into chunks

### Usage / cost

From Claude Code `/usage` at the end of the first part of the session
(`noisesplit.py`, `osdvoltage.py` with the HTML viewer, tests and README),
before the flight analysis, the upload server and the repository split.

| | |
|---|---:|
| **Total cost** | **$9.45** |
| API time | 20 min 34 s |
| Wall-clock time | 1 h 21 min |
| Code changes | 871 lines added, 0 removed |

| model | input | output | cache read | cache write | cost |
|---|---:|---:|---:|---:|---:|
| `claude-opus-5-5` | 3.1k | 122.0k | 18.2M | 420.5k | $9.45 |
| `claude-haiku-5-5` | 1.2k | 19 | 0 | 0 | $0.0001 |

98% of input tokens were served from the prompt cache (123 requests).
By usage share, the biggest add-ons were browser automation
(`/claude-in-chrome`, 16%), used to test the HTML viewer and take the
screenshot, and the `/dataviz` charting guidelines (6%).
