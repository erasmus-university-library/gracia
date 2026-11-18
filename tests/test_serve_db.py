import signal
import socket
import subprocess
import sys
import threading
import unittest

import duckdb
from base import DbTestCase

from gracia import store


def quack_available() -> bool:
    try:
        duckdb.connect().execute("INSTALL quack; LOAD quack")
        return True
    except duckdb.Error:
        return False


class TokenFileTest(DbTestCase):
    def test_a_new_token_file_is_created_with_mode_0600(self):
        path = self.tmp / "t.token"
        token = store.read_or_create_token(path)
        self.assertGreaterEqual(len(token), 16)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_an_existing_token_is_reused(self):
        path = self.tmp / "t.token"
        first = store.read_or_create_token(path)
        self.assertEqual(store.read_or_create_token(path), first)

    def test_a_token_file_others_can_read_is_refused(self):
        path = self.tmp / "t.token"
        path.write_text("x" * 32 + "\n")
        path.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "readable by group or others"):
            store.read_or_create_token(path)

    def test_a_short_token_is_refused(self):
        path = self.tmp / "t.token"
        path.write_text("short\n")
        path.chmod(0o600)
        with self.assertRaisesRegex(ValueError, "too short"):
            store.read_or_create_token(path)


class ServeRefusalsTest(DbTestCase):
    def test_only_local_addresses_and_real_files_are_served(self):
        with self.assertRaisesRegex(ValueError, "in-memory"):
            store.serve_db(":memory:", token_file=self.tmp / "t")
        with self.assertRaisesRegex(ValueError, "only"):
            store.serve_db(self.db, host="0.0.0.0", token_file=self.tmp / "t")
        self.assertFalse(self.db.exists())  # nothing was opened or created


@unittest.skipUnless(quack_available(), "the DuckDB quack extension is not available")
class ServeDbProcessTest(DbTestCase):
    def free_port(self) -> int:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def start(self):
        self.port = self.free_port()
        self.token_file = self.tmp / "db.token"
        proc = subprocess.Popen(
            [sys.executable, "-m", "gracia", "-d", str(self.db), "serve-db",
             "--port", str(self.port), "--token-file", str(self.token_file)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )  # fmt: skip
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        watchdog = threading.Timer(60, proc.kill)  # never hang the suite
        watchdog.start()
        try:
            self.first_line = proc.stdout.readline()
        finally:
            watchdog.cancel()
        return proc

    def stop(self, proc) -> str:
        proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=30)
        return out

    def attach(self, token: str):
        con = duckdb.connect()
        con.execute(f"ATTACH 'quack:localhost:{self.port}' AS qk (TOKEN '{token}')")
        con.execute("CONNECT qk")
        return con

    def test_it_serves_the_file_and_shuts_down_cleanly(self):
        proc = self.start()
        self.assertIn(f"quack:localhost:{self.port}", self.first_line)
        self.assertEqual(self.token_file.stat().st_mode & 0o777, 0o600)
        token = self.token_file.read_text().strip()
        self.assertNotIn(token, self.first_line)  # the token is never printed

        con = self.attach(token)
        self.assertEqual(con.execute("SELECT 42").fetchone()[0], 42)
        con.execute(
            "CREATE SCHEMA demo"
        )  # a write through Quack lands in the served file
        con.execute("DISCONNECT")

        out = self.stop(proc)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("stopped", out)
        # the file is free again at once, and holds what was written through Quack
        reopened = duckdb.connect(str(self.db))
        schemas = {
            r[0]
            for r in reopened.execute(
                "SELECT schema_name FROM duckdb_schemas()"
            ).fetchall()
        }
        self.assertIn("demo", schemas)
        reopened.close()

    def test_while_it_runs_nobody_else_can_open_the_file(self):
        proc = self.start()
        with self.assertRaises(duckdb.IOException):
            duckdb.connect(str(self.db))
        self.stop(proc)

    def test_a_wrong_token_is_refused(self):
        proc = self.start()
        try:
            with self.assertRaises(duckdb.Error):
                self.attach("not-the-token-0123456789")
        finally:
            self.stop(proc)

    def test_the_token_survives_a_restart(self):
        proc = self.start()
        token = self.token_file.read_text()
        self.stop(proc)
        proc = self.start()
        self.assertEqual(self.token_file.read_text(), token)
        self.assertEqual(
            self.attach(token.strip()).execute("SELECT 1").fetchone()[0], 1
        )
        self.stop(proc)
