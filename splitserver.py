#!/usr/bin/env python3
"""Small HTTP front end for noisesplit: upload recordings, get chunks back.

Meant for a home server reachable over Tailscale. Upload a video from the
browser (or `curl -T`), the server splits it with noisesplit.py in the
background - one job at a time - and lists the chunks for download.

    ./splitserver.py --data ~/fpv-uploads            # listens on 127.0.0.1:8080
    tailscale serve --bg 8080                         # tailnet-only HTTPS

    curl -T recording.mp4 https://homeserver.<tailnet>.ts.net/upload/recording.mp4

There is no authentication: bind it to localhost and let `tailscale serve`
(not `funnel`) expose it to your tailnet, or bind it to the Tailscale IP.

Endpoints:
    GET    /                         upload page + job list
    PUT    /upload/<name>[?copy=1]   upload a video (raw body), starts a job
    GET    /api/jobs                 all jobs (JSON)
    GET    /api/jobs/<id>            one job (JSON)
    DELETE /api/jobs/<id>            delete a finished job and its files
    GET    /files/<id>/<path>        download upload / chunks / segments.json / log

Requires: Python 3.9+, plus what noisesplit.py needs (ffmpeg, numpy).
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit, parse_qs

HERE = Path(__file__).resolve().parent
NOISESPLIT = HERE / "noisesplit.py"
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".avi", ".ts", ".m4v", ".mpg", ".mpeg"}
NAME_RE = re.compile(r"[^A-Za-z0-9._ -]+")
BLOCK = 1 << 20
ANALYZE_RE = re.compile(r"analyzing\s+([0-9.]+)s\s*/\s*([0-9.]+)s")
WRITE_RE = re.compile(r"^\[(\d+)/(\d+)\]")
TYPES = {".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime",
         ".mkv": "video/x-matroska", ".json": "application/json",
         ".txt": "text/plain; charset=utf-8", ".csv": "text/csv"}


def safe_name(name: str) -> str | None:
    """Reduce an uploaded file name to a harmless basename, or None."""
    name = NAME_RE.sub("_", Path(unquote(name)).name).strip(" .")
    if not name or Path(name).suffix.lower() not in VIDEO_EXT:
        return None
    return name[:120]


class Jobs:
    """Job store (one directory per job with a job.json) and the worker."""

    def __init__(self, data: Path, split_args: list[str], delete_input: bool,
                 python: str = sys.executable):
        self.data = data
        self.split_args = split_args
        self.delete_input = delete_input
        self.python = python
        self.lock = threading.Lock()
        self.jobs: dict[str, dict] = {}
        self.q: queue.Queue[str] = queue.Queue()
        self.proc: subprocess.Popen | None = None
        self.stopping = False
        data.mkdir(parents=True, exist_ok=True)
        for f in sorted(data.glob("*/job.json")):
            try:
                job = json.loads(f.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if job.get("status") in ("queued", "running"):
                job.update(status="queued", progress=0.0, message="re-queued after restart")
                self.jobs[job["id"]] = job
                self._save(job)
                self.q.put(job["id"])
            else:
                self.jobs[job["id"]] = job
        self.worker = threading.Thread(target=self._work, daemon=True)
        self.worker.start()

    # -- store
    def dir(self, job_id: str) -> Path:
        return self.data / job_id

    def _save(self, job: dict) -> None:
        tmp = self.dir(job["id"]) / "job.json.tmp"
        tmp.write_text(json.dumps(job, indent=1))
        tmp.replace(self.dir(job["id"]) / "job.json")

    def update(self, job_id: str, **kw) -> None:
        with self.lock:
            job = self.jobs[job_id]
            job.update(kw)
            self._save(job)

    def get(self, job_id: str) -> dict | None:
        with self.lock:
            job = self.jobs.get(job_id)
            return None if job is None else self._public(job)

    def all(self) -> list[dict]:
        with self.lock:
            return [self._public(j) for j in
                    sorted(self.jobs.values(), key=lambda j: j["created"], reverse=True)]

    def _public(self, job: dict) -> dict:
        out = dict(job)
        d = self.dir(job["id"])
        out["files"] = [
            {"name": str(p.relative_to(d)), "size": p.stat().st_size}
            for p in sorted(d.rglob("*"))
            if p.is_file() and p.name not in ("job.json", "job.json.tmp")
            and not p.name.endswith(".part")
        ]
        return out

    def new(self, name: str, copy: bool) -> dict:
        job_id = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
        self.dir(job_id).mkdir()
        job = {"id": job_id, "name": name, "created": time.time(), "copy": copy,
               "status": "uploading", "progress": 0.0, "message": "", "size": 0}
        with self.lock:
            self.jobs[job_id] = job
            self._save(job)
        return job

    def submit(self, job_id: str) -> None:
        self.update(job_id, status="queued", message="")
        self.q.put(job_id)

    def delete(self, job_id: str) -> bool:
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None or job["status"] in ("queued", "running", "uploading"):
                return False
            del self.jobs[job_id]
        shutil.rmtree(self.dir(job_id), ignore_errors=True)
        return True

    def stop(self) -> None:
        """Stop the running split; its job stays queued for the next start."""
        self.stopping = True
        proc = self.proc
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()

    # -- worker
    def _work(self) -> None:
        while True:
            job_id = self.q.get()
            try:
                self._run(job_id)
            except Exception as e:  # keep the worker alive whatever happens
                self.update(job_id, status="failed", message=f"internal error: {e}")

    def _run(self, job_id: str) -> None:
        with self.lock:
            job = dict(self.jobs.get(job_id) or {})
        if not job or job.get("status") != "queued":
            return
        d = self.dir(job_id)
        src = d / job["name"]
        outdir = d / (src.stem + "_chunks")
        cmd = [self.python, str(NOISESPLIT), str(src), "-o", str(outdir),
               *(["--copy"] if job.get("copy") else []), *self.split_args]
        self.update(job_id, status="running", started=time.time(), progress=0.0,
                    message="analyzing")
        with open(d / "log.txt", "wb") as log, subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as proc:
            self.proc = proc
            buf = b""
            last = 0.0
            while True:
                b = proc.stdout.read1(4096)
                if not b:
                    break
                log.write(b)
                buf += b
                *lines, buf = re.split(rb"[\r\n]", buf)
                for raw in lines:
                    line = raw.decode(errors="replace").strip()
                    m = ANALYZE_RE.search(line)
                    w = WRITE_RE.match(line)
                    # analysis is the slow part: 0-80 %, writing chunks 80-100 %
                    if m and float(m.group(2)) > 0:
                        p, msg = 0.8 * float(m.group(1)) / float(m.group(2)), "analyzing"
                    elif w:
                        p = 0.8 + 0.2 * (int(w.group(1)) - 1) / int(w.group(2))
                        msg = f"writing chunk {w.group(1)} of {w.group(2)}"
                    else:
                        continue
                    if time.time() - last > 0.5:
                        self.update(job_id, progress=round(p, 3), message=msg)
                        last = time.time()
        self.proc = None
        if self.stopping:
            self.update(job_id, status="queued", progress=0.0, message="interrupted, will restart")
            return
        if proc.returncode != 0:
            tail = (d / "log.txt").read_text(errors="replace").strip().splitlines()[-3:]
            self.update(job_id, status="failed", finished=time.time(),
                        message=" / ".join(tail) or f"noisesplit exited {proc.returncode}")
            return
        chunks = sorted(p.name for p in outdir.glob("*") if p.suffix.lower() in VIDEO_EXT)
        noise = []
        try:
            noise = json.loads((outdir / "segments.json").read_text()).get("noise", [])
        except (OSError, json.JSONDecodeError):
            pass
        if self.delete_input:
            src.unlink(missing_ok=True)
        self.update(job_id, status="done", finished=time.time(), progress=1.0,
                    message=f"{len(chunks)} chunk(s), {len(noise)} noise section(s)",
                    chunks=chunks, noise=noise)


class Handler(BaseHTTPRequestHandler):
    server_version = "splitserver/1"
    jobs: Jobs
    max_upload: int
    min_free: int

    def log_message(self, fmt: str, *args) -> None:
        if not getattr(self.server, "quiet", False):
            super().log_message(fmt, *args)

    # -- helpers
    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _error(self, code: int, msg: str) -> None:
        self._json({"error": msg}, code)

    # -- routes
    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/":
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        elif path == "/api/jobs":
            self._json(self.jobs.all())
        elif path.startswith("/api/jobs/"):
            job = self.jobs.get(path.split("/")[3])
            self._json(job) if job else self._error(404, "no such job")
        elif path.startswith("/files/"):
            self._file(path)
        else:
            self._error(404, "not found")

    do_HEAD = do_GET

    def do_DELETE(self) -> None:
        path = urlsplit(self.path).path
        if not path.startswith("/api/jobs/"):
            return self._error(404, "not found")
        job_id = path.split("/")[3]
        if self.jobs.get(job_id) is None:
            return self._error(404, "no such job")
        if not self.jobs.delete(job_id):
            return self._error(409, "job is still active")
        self._json({"deleted": job_id})

    def do_PUT(self) -> None:
        url = urlsplit(self.path)
        if not url.path.startswith("/upload/"):
            return self._error(404, "not found")
        name = safe_name(url.path[len("/upload/"):])
        if name is None:
            return self._error(400, "need a video file name ending in " + ", ".join(sorted(VIDEO_EXT)))
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return self._error(411, "Content-Length required")
        if length <= 0:
            return self._error(400, "empty upload")
        if length > self.max_upload:
            return self._error(413, f"larger than --max-upload ({self.max_upload >> 30} GiB)")
        # the upload plus re-encoded chunks need about twice the space
        free = shutil.disk_usage(self.jobs.data).free
        if free - 2 * length < self.min_free:
            return self._error(507, f"not enough disk space ({free >> 20} MiB free)")
        q = parse_qs(url.query)
        copy = q.get("copy", ["0"])[0] in ("1", "true", "yes", "on")
        job = self.jobs.new(name, copy)
        dest = self.jobs.dir(job["id"]) / name
        part = dest.with_name(dest.name + ".part")
        got = 0
        try:
            with open(part, "wb") as f:
                while got < length:
                    b = self.rfile.read(min(BLOCK, length - got))
                    if not b:
                        break
                    f.write(b)
                    got += len(b)
        except OSError:
            pass
        if got < length:
            part.unlink(missing_ok=True)
            self.jobs.update(job["id"], status="failed", size=got,
                             message=f"upload interrupted after {got >> 20} of {length >> 20} MiB")
            self.close_connection = True
            return self._error(400, "upload incomplete")
        part.replace(dest)
        self.jobs.update(job["id"], size=got)
        self.jobs.submit(job["id"])
        self._json(self.jobs.get(job["id"]), 201)

    def _file(self, path: str) -> None:
        parts = [unquote(p) for p in path.split("/")[2:]]
        if len(parts) < 2 or any(p in ("", ".", "..") for p in parts):
            return self._error(404, "not found")
        if self.jobs.get(parts[0]) is None:
            return self._error(404, "no such job")
        base = self.jobs.dir(parts[0]).resolve()
        f = base.joinpath(*parts[1:]).resolve()
        if base not in f.parents or not f.is_file() or f.name.startswith("job.json"):
            return self._error(404, "not found")
        size = f.stat().st_size
        start, end = 0, size - 1
        code = 200
        rng = self.headers.get("Range", "")
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", rng.strip())
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
            else:  # suffix range: last N bytes
                start = max(0, size - int(m.group(2)))
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            code = 206
        self.send_response(code)
        self.send_header("Content-Type", TYPES.get(f.suffix.lower(), "application/octet-stream"))
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if "download" in parse_qs(urlsplit(self.path).query):
            self.send_header("Content-Disposition",
                             f"attachment; filename*=UTF-8''{quote(f.name)}")
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(f, "rb") as fh:
            fh.seek(start)
            left = end - start + 1
            try:
                while left > 0:
                    b = fh.read(min(BLOCK, left))
                    if not b:
                        break
                    self.wfile.write(b)
                    left -= len(b)
            except (BrokenPipeError, ConnectionResetError):
                pass


def make_server(data: Path, host: str = "127.0.0.1", port: int = 8080,
                split_args: list[str] | None = None, delete_input: bool = False,
                max_upload: int = 64 << 30, min_free: int = 1 << 30,
                quiet: bool = False) -> tuple[ThreadingHTTPServer, Jobs]:
    jobs = Jobs(data, split_args or [], delete_input)
    handler = type("BoundHandler", (Handler,), {
        "jobs": jobs, "max_upload": max_upload, "min_free": min_free})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    httpd.quiet = quiet
    return httpd, jobs


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="HTTP upload front end for noisesplit (for use over Tailscale).")
    p.add_argument("--data", type=Path, default=Path("uploads"),
                   help="where uploads and chunks are stored (default ./uploads)")
    p.add_argument("--host", default="127.0.0.1",
                   help="address to listen on (default 127.0.0.1; use `tailscale serve` "
                        "or your Tailscale IP - there is no authentication)")
    p.add_argument("--port", type=int, default=8080, help="port (default 8080)")
    p.add_argument("--split-args", default="",
                   help='extra noisesplit options for every job, e.g. "--preset veryfast"')
    p.add_argument("--delete-input", action="store_true",
                   help="delete the uploaded video once it is split (keep only the chunks)")
    p.add_argument("--max-upload", type=float, default=64, metavar="GIB",
                   help="largest accepted upload in GiB (default 64)")
    p.add_argument("--min-free", type=float, default=1, metavar="GIB",
                   help="keep at least this much disk free in GiB (default 1)")
    p.add_argument("-q", "--quiet", action="store_true", help="no request log")
    a = p.parse_args(argv)

    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            p.error(f"{tool} not found on PATH")
    httpd, jobs = make_server(a.data.expanduser(), a.host, a.port, shlex.split(a.split_args),
                           a.delete_input, int(a.max_upload * (1 << 30)),
                           int(a.min_free * (1 << 30)), a.quiet)
    host, port = httpd.server_address[:2]
    print(f"splitserver: http://{host}:{port}/  (data: {a.data.expanduser().resolve()})",
          flush=True)
    if a.host not in ("127.0.0.1", "localhost", "::1") and not a.host.startswith("100."):
        print("warning: no authentication - make sure this address is only reachable "
              "from your tailnet", file=sys.stderr)
    def term(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, term)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        jobs.stop()
        httpd.server_close()
    return 0


PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>noisesplit – upload</title>
<style>
:root {
  color-scheme: light dark;
  --surface-0: #f4f3f0; --surface-1: #fcfcfb; --text: #0b0b0b; --text-2: #52514e;
  --muted: #7a7974; --line: #e1e0d9; --accent: #2a78d6; --good: #0ca30c; --bad: #d03b3b;
}
@media (prefers-color-scheme: dark) {
  :root { --surface-0: #111110; --surface-1: #1a1a19; --text: #f0efec; --text-2: #c3c2b7;
    --muted: #8f8e86; --line: #2c2c2a; --accent: #3987e5; }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--surface-0); color: var(--text);
  font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 900px; margin: 0 auto; padding: 24px 16px 48px; }
h1 { font-size: 20px; margin: 0 0 4px; }
.sub { color: var(--text-2); margin: 0 0 20px; }
.card { background: var(--surface-1); border-radius: 10px; padding: 16px; margin-bottom: 16px; }
#drop { border: 2px dashed var(--line); border-radius: 8px; padding: 28px 16px; text-align: center;
  color: var(--text-2); cursor: pointer; }
#drop.over { border-color: var(--accent); color: var(--text); }
#drop b { color: var(--text); }
.opts { margin-top: 12px; color: var(--text-2); display: flex; gap: 16px; flex-wrap: wrap; }
.job { border-top: 1px solid var(--line); padding: 12px 0; }
.job:first-child { border-top: 0; padding-top: 0; }
.row { display: flex; gap: 12px; align-items: baseline; flex-wrap: wrap; }
.name { font-weight: 600; word-break: break-all; }
.meta { color: var(--muted); font-size: 12px; font-variant-numeric: tabular-nums; }
.status { font-size: 12px; font-weight: 600; padding: 1px 8px; border-radius: 999px;
  border: 1px solid var(--line); color: var(--text-2); }
.status.done { color: var(--good); border-color: currentColor; }
.status.failed { color: var(--bad); border-color: currentColor; }
.status.running, .status.uploading { color: var(--accent); border-color: currentColor; }
.bar { height: 6px; background: var(--line); border-radius: 3px; overflow: hidden; margin: 8px 0 4px; }
.bar i { display: block; height: 100%; background: var(--accent); border-radius: 3px; width: 0; transition: width .3s; }
.msg { color: var(--text-2); font-size: 13px; }
.files { margin: 8px 0 0; padding: 0; list-style: none; display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 4px 16px; font-size: 13px; }
.files a { color: var(--accent); text-decoration: none; }
.files a:hover { text-decoration: underline; }
.files .meta { margin-left: 6px; }
button.link { background: none; border: 0; color: var(--muted); cursor: pointer; font: inherit; font-size: 12px; padding: 0; margin-left: auto; }
button.link:hover { color: var(--bad); }
.empty { color: var(--muted); }
code { background: var(--surface-0); padding: 1px 5px; border-radius: 4px; }
</style>
</head>
<body>
<main>
  <h1>Split FPV recordings</h1>
  <p class="sub">Upload a recording. The server cuts it into chunks at every white-noise section (battery swap); the chunks are listed below when it's done.</p>

  <div class="card">
    <div id="drop" tabindex="0" role="button" aria-label="Choose video files to upload"><b>Drop videos here</b> or click to choose<br><span class="meta">.mp4 .mov .mkv .avi .ts – several files are processed one after another</span></div>
    <input type="file" id="file" accept="video/*,.mp4,.mov,.mkv,.avi,.ts,.m4v" multiple hidden>
    <div class="opts">
      <label><input type="checkbox" id="copy"> fast lossless split (<code>--copy</code>, cuts snap to keyframes)</label>
    </div>
    <div id="uploads"></div>
  </div>

  <div class="card">
    <div class="row" style="margin-bottom:8px"><b>Jobs</b><span class="meta" id="updated"></span></div>
    <div id="jobs"><p class="empty">No jobs yet.</p></div>
  </div>
  <p class="meta">Or from a terminal: <code>curl -T recording.mp4 <span id="origin"></span>/upload/recording.mp4</code> (add <code>?copy=1</code> for a lossless split)</p>
</main>
<script>
(() => {
  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const mib = (n) => n >= 1 << 30 ? (n / (1 << 30)).toFixed(2) + " GiB" : n >= 1 << 20 ? (n / (1 << 20)).toFixed(1) + " MiB" : Math.max(1, Math.round(n / 1024)) + " KiB";
  // chunks first, then the original upload, segments.json, log
  const rank = (j, f) => f.name.includes("/") && /\.(mp4|mov|mkv|avi|ts|m4v|mpe?g)$/i.test(f.name) ? 0 : f.name === j.name ? 1 : f.name.endsWith(".json") ? 2 : 3;
  const ago = (t) => { const s = Date.now() / 1000 - t; return s < 90 ? "just now" : s < 5400 ? Math.round(s / 60) + " min ago" : new Date(t * 1000).toLocaleString(); };
  $("origin").textContent = location.origin;

  // uploads: one at a time, with progress
  const pending = [];
  let busy = false;
  function enqueue(files) {
    for (const f of files) pending.push(f);
    pump();
  }
  function pump() {
    if (busy || !pending.length) return;
    busy = true;
    const f = pending.shift();
    const box = el("div", "job"), r = el("div", "row");
    r.append(el("span", "name", f.name), el("span", "status uploading", "uploading"), el("span", "meta", mib(f.size)));
    const bar = el("div", "bar"), fill = el("i"); bar.append(fill);
    const msg = el("div", "msg", "starting…");
    box.append(r, bar, msg); $("uploads").append(box);
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", "/upload/" + encodeURIComponent(f.name) + ($("copy").checked ? "?copy=1" : ""));
    const t0 = performance.now();
    xhr.upload.onprogress = (e) => {
      if (!e.lengthComputable) return;
      fill.style.width = (100 * e.loaded / e.total).toFixed(1) + "%";
      const rate = e.loaded / ((performance.now() - t0) / 1000);
      msg.textContent = `${mib(e.loaded)} of ${mib(e.total)} · ${mib(rate)}/s`;
    };
    xhr.onload = () => {
      if (xhr.status === 201) { box.remove(); refresh(); }
      else { r.children[1].className = "status failed"; r.children[1].textContent = "failed"; msg.textContent = (JSON.parse(xhr.responseText || "{}").error) || xhr.statusText; }
      busy = false; pump();
    };
    xhr.onerror = () => { r.children[1].className = "status failed"; r.children[1].textContent = "failed"; msg.textContent = "connection lost"; busy = false; pump(); };
    xhr.send(f);
  }
  const drop = $("drop"), input = $("file");
  drop.addEventListener("click", () => input.click());
  drop.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.click(); } });
  input.addEventListener("change", () => { enqueue(input.files); input.value = ""; });
  drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
  drop.addEventListener("dragleave", () => drop.classList.remove("over"));
  drop.addEventListener("drop", (e) => { e.preventDefault(); drop.classList.remove("over"); enqueue(e.dataTransfer.files); });
  window.addEventListener("beforeunload", (e) => { if (busy || pending.length) { e.preventDefault(); e.returnValue = ""; } });

  // job list
  function render(jobs) {
    const box = $("jobs");
    if (!jobs.length) { box.replaceChildren(el("p", "empty", "No jobs yet.")); return; }
    box.replaceChildren(...jobs.map((j) => {
      const d = el("div", "job"), r = el("div", "row");
      r.append(el("span", "name", j.name), el("span", "status " + j.status, j.status),
               el("span", "meta", `${mib(j.size)} · ${ago(j.created)}${j.copy ? " · lossless" : ""}`));
      if (!["queued", "running", "uploading"].includes(j.status)) {
        const del = el("button", "link", "delete");
        del.addEventListener("click", async () => { await fetch("/api/jobs/" + j.id, { method: "DELETE" }); refresh(); });
        r.append(del);
      }
      d.append(r);
      if (j.status === "running" || j.status === "queued") {
        const bar = el("div", "bar"), fill = el("i"); fill.style.width = (100 * j.progress).toFixed(1) + "%"; bar.append(fill); d.append(bar);
      }
      if (j.message) d.append(el("div", "msg", j.message));
      const files = j.files.filter((f) => f.name !== j.name || j.status === "done")
        .sort((a, b) => rank(j, a) - rank(j, b) || a.name.localeCompare(b.name));
      if (files.length) {
        const ul = el("ul", "files");
        for (const f of files) {
          const li = el("li"), a = el("a", null, (f.name === j.name ? "original: " : "") + f.name.split("/").pop());
          a.href = "/files/" + j.id + "/" + f.name.split("/").map(encodeURIComponent).join("/");
          a.target = "_blank";
          li.append(a, el("span", "meta", mib(f.size))); ul.append(li);
        }
        d.append(ul);
      }
      return d;
    }));
  }
  async function refresh() {
    try {
      const r = await fetch("/api/jobs");
      render(await r.json());
      $("updated").textContent = "updated " + new Date().toLocaleTimeString();
    } catch { $("updated").textContent = "server not reachable"; }
  }
  refresh();
  setInterval(refresh, 2000);
})();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    sys.exit(main())
