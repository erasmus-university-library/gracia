import json
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

from base import DbTestCase


class ServeProcessTest(DbTestCase):
    def free_port(self) -> int:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def get(self, port: int, path: str):
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}{path}", timeout=5
            ) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read())

    def test_it_serves_the_repositories_over_http(self):
        self.create_repositories("library")
        port = self.free_port()
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "gracia",
                "-d",
                str(self.db),
                "serve",
                "--port",
                str(port),
            ],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )  # fmt: skip
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        watchdog = threading.Timer(60, proc.kill)
        watchdog.start()
        try:
            line = ""
            for _ in range(20):  # skip anything else printed first (warnings)
                line = proc.stdout.readline()
                if "serving" in line or not line:
                    break
            self.assertIn("serving", line)
            for _ in range(100):  # the line is printed just before the socket opens
                try:
                    status, body = self.get(port, "/")
                    break
                except OSError:
                    time.sleep(0.1)
            self.assertEqual((status, body), (200, {"repositories": ["library"]}))
            self.assertEqual(self.get(port, "/library/")[0], 200)
            self.assertEqual(self.get(port, "/library/api")[0], 200)
            self.assertEqual(self.get(port, "/nope/")[0], 404)
        finally:
            watchdog.cancel()
            proc.terminate()
            proc.wait(timeout=20)
