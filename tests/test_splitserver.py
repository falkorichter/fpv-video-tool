"""splitserver: upload a synthetic clip over HTTP and get the chunks back."""
import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import splitserver  # noqa: E402
from tests.test_noisesplit import make_clip  # noqa: E402


class SplitServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.clip = Path(cls.tmp.name) / "clip.mp4"
        make_clip(cls.clip)
        cls.data = Path(cls.tmp.name) / "data"
        cls.httpd, cls.jobs = splitserver.make_server(
            cls.data, port=0, split_args=["--preset", "ultrafast"], quiet=True)
        cls.base = "http://127.0.0.1:%d" % cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    def req(self, path, method="GET", data=None, headers=None):
        r = urllib.request.Request(self.base + path, data=data, method=method,
                                   headers=headers or {})
        try:
            with urllib.request.urlopen(r, timeout=30) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, dict(e.headers), e.read()

    def upload(self, name, body, query=""):
        code, _, out = self.req("/upload/" + name + query, "PUT", body)
        return code, json.loads(out)

    def wait(self, job_id, timeout=120):
        end = time.time() + timeout
        while time.time() < end:
            job = json.loads(self.req("/api/jobs/" + job_id)[2])
            if job["status"] in ("done", "failed"):
                return job
            time.sleep(0.3)
        self.fail("job did not finish")

    def test_page(self):
        code, headers, body = self.req("/")
        self.assertEqual(code, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn(b"Split FPV recordings", body)

    def test_upload_split_download_delete(self):
        code, job = self.upload("my%20flight.mp4", self.clip.read_bytes())
        self.assertEqual(code, 201)
        self.assertEqual(job["name"], "my flight.mp4")
        job = self.wait(job["id"])
        self.assertEqual(job["status"], "done", job.get("message"))
        self.assertEqual(len(job["chunks"]), 3)
        self.assertEqual(len(job["noise"]), 2)
        names = {f["name"] for f in job["files"]}
        self.assertIn("my flight_chunks/segments.json", names)
        self.assertIn("log.txt", names)
        self.assertNotIn("job.json", names)

        # range request on a chunk, as a <video> element would do
        chunk = "my%20flight_chunks/" + job["chunks"][0].replace(" ", "%20")
        code, headers, body = self.req(f"/files/{job['id']}/{chunk}",
                                       headers={"Range": "bytes=0-99"})
        self.assertEqual(code, 206)
        self.assertEqual(len(body), 100)
        self.assertTrue(headers["Content-Range"].startswith("bytes 0-99/"))
        self.assertEqual(headers["Content-Type"], "video/mp4")

        listed = json.loads(self.req("/api/jobs")[2])
        self.assertIn(job["id"], [j["id"] for j in listed])
        code, _, _ = self.req("/api/jobs/" + job["id"], "DELETE")
        self.assertEqual(code, 200)
        self.assertFalse((self.data / job["id"]).exists())
        self.assertEqual(self.req("/api/jobs/" + job["id"])[0], 404)

    def test_copy_option(self):
        code, job = self.upload("fast.mp4", self.clip.read_bytes(), "?copy=1")
        self.assertEqual(code, 201)
        self.assertTrue(job["copy"])
        self.assertEqual(self.wait(job["id"])["status"], "done")

    def test_rejects_bad_names_and_paths(self):
        self.assertEqual(self.upload("notes.txt", b"x")[0], 400)
        self.assertEqual(self.upload("..%2F..%2Fx.sh", b"x")[0], 400)
        code, job = self.upload("..%2F..%2Fevil.mp4", b"not really a video")
        self.assertEqual(code, 201)
        self.assertEqual(job["name"], "evil.mp4")      # reduced to a basename
        self.assertTrue((self.data / job["id"] / "evil.mp4").exists())
        self.assertEqual(self.wait(job["id"])["status"], "failed")
        self.assertEqual(self.req(f"/files/{job['id']}/../../etc/passwd")[0], 404)
        self.assertEqual(self.req(f"/files/{job['id']}/job.json")[0], 404)
        self.assertEqual(self.req("/files/nope/evil.mp4")[0], 404)

    def test_restart_keeps_finished_jobs(self):
        code, job = self.upload("again.mp4", self.clip.read_bytes(), "?copy=1")
        self.wait(job["id"])
        jobs = splitserver.Jobs(self.data, [], False)
        self.assertEqual(jobs.get(job["id"])["status"], "done")


if __name__ == "__main__":
    unittest.main()
