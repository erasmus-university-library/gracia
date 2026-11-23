from pathlib import Path

from base import DbTestCase

from gracia import config

INI = """[app:main]
gracia.db = quack:localhost:9494   # clients
gracia.db.file = gracia.db
gracia.db.token_file = secret.token
gracia.base_iri = http://repo.example/
pyramid.reload = true
"""


class ConfigTest(DbTestCase):
    def write(self, text=INI):  # ty: ignore[incompatible-method-override]
        path = self.tmp / "config.ini"
        path.write_text(text)
        return path

    def test_reads_only_gracia_settings_and_makes_paths_relative_to_the_file(self):
        settings = config.read(self.write())
        self.assertEqual(
            settings["gracia.db"], "quack:localhost:9494"
        )  # a target, not a path
        self.assertEqual(settings["gracia.db.file"], str(self.tmp / "gracia.db"))
        self.assertEqual(
            settings["gracia.db.token_file"], str(self.tmp / "secret.token")
        )
        self.assertNotIn("pyramid.reload", settings)
        # a setting that is gone (the history directory) in an old file is read and not
        # used
        old = config.read(self.write(INI + "gracia.history_dir = elsewhere\n"))
        self.assertEqual(old["gracia.db"], "quack:localhost:9494")

    def test_the_jobs_directory_is_a_path_from_the_config_file_unless_it_is_absolute(
        self,
    ):
        relative = config.read(self.write("[app:main]\ngracia.jobs_dir = work/jobs\n"))
        self.assertEqual(relative["gracia.jobs_dir"], str(self.tmp / "work/jobs"))
        absolute = config.read(
            self.write("[app:main]\ngracia.jobs_dir = /var/lib/gracia/jobs\n")
        )
        self.assertEqual(absolute["gracia.jobs_dir"], "/var/lib/gracia/jobs")
        self.assertNotIn("gracia.jobs_dir", config.read(self.write()))  # optional

    def test_a_missing_file_or_section_is_an_error(self):
        with self.assertRaises(ValueError):
            config.read(self.tmp / "nope.ini")
        with self.assertRaises(ValueError):
            config.read(self.write("[other]\na = 1\n"))

    def test_targets(self):
        self.assertEqual(
            config.parse_target("quack:localhost:9494"), ("localhost", 9494)
        )
        for bad in ("quack:localhost", "quack::1", "quack:h:x", "file.db"):
            with self.assertRaises(ValueError):
                config.parse_target(bad)

    def test_the_token_comes_from_a_private_file(self):
        token = self.tmp / "secret.token"
        token.write_text("0123456789abcdef0123\n")
        token.chmod(0o600)
        self.assertEqual(
            config.read_token({"gracia.db.token_file": str(token)}),
            "0123456789abcdef0123",
        )
        token.chmod(0o644)
        with self.assertRaises(ValueError):
            config.read_token({"gracia.db.token_file": str(token)})
        with self.assertRaises(ValueError):
            config.read_token({})
        self.assertEqual(
            config.read_token({"gracia.db.token": "inline-secret"}), "inline-secret"
        )

    def test_flag_beats_environment_beats_file_beats_default(self):
        path = self.write()
        env = {"GRACIA_DB": "env.db"}
        self.assertEqual(config.resolve(path, "flag.db", env)["gracia.db"], "flag.db")
        self.assertEqual(config.resolve(path, None, env)["gracia.db"], "env.db")
        self.assertEqual(
            config.resolve(path, None, {})["gracia.db"], "quack:localhost:9494"
        )
        empty = self.tmp / "empty"  # a directory with no config.ini in it
        empty.mkdir()
        self.assertEqual(
            config.resolve(None, None, {}, cwd=empty)["gracia.db"], config.DEFAULT_DB
        )
        by_env = config.resolve(None, None, {"GRACIA_CONFIG": str(path)})
        self.assertEqual(by_env["gracia.base_iri"], "http://repo.example/")


class DefaultFileTest(DbTestCase):
    def test_config_ini_in_the_directory_is_the_default(self):
        self.assertIsNone(config.config_file(None, {}, cwd=self.tmp))  # none there
        (self.tmp / "config.ini").write_text(INI)
        self.assertEqual(
            config.config_file(None, {}, cwd=self.tmp), self.tmp / "config.ini"
        )

    def test_the_flag_beats_the_environment_beats_the_default(self):
        (self.tmp / "config.ini").write_text(INI)
        flag, env = self.tmp / "flag.ini", {"GRACIA_CONFIG": str(self.tmp / "env.ini")}
        self.assertEqual(config.config_file(str(flag), env, cwd=self.tmp), flag)
        self.assertEqual(
            config.config_file(None, env, cwd=self.tmp), self.tmp / "env.ini"
        )
        self.assertEqual(
            config.config_file(None, {}, cwd=self.tmp), self.tmp / "config.ini"
        )

    def test_an_empty_environment_variable_means_no_file_at_all(self):
        (self.tmp / "config.ini").write_text(INI)
        self.assertIsNone(config.config_file(None, {"GRACIA_CONFIG": ""}, cwd=self.tmp))
        self.assertEqual(
            config.resolve(None, None, {"GRACIA_CONFIG": ""}, cwd=self.tmp)[
                "gracia.db"
            ],
            config.DEFAULT_DB,
        )

    def test_a_file_that_is_named_and_missing_is_an_error_the_default_one_is_not(self):
        with self.assertRaises(ValueError):
            config.resolve("nope.ini", None, {})
        self.assertEqual(
            config.resolve(None, None, {}, cwd=self.tmp)["gracia.db"], config.DEFAULT_DB
        )

    def test_the_default_file_supplies_the_settings(self):
        (self.tmp / "config.ini").write_text(INI)
        found = config.resolve(None, None, {}, cwd=self.tmp)
        self.assertEqual(found["gracia.db"], "quack:localhost:9494")
        self.assertEqual(
            found["gracia.jobs_dir"], "/var/lib/gracia/jobs"
        ) if "jobs_dir" in INI else None
        self.assertEqual(
            found["gracia.db.file"], str(self.tmp / "gracia.db")
        )  # from the file's directory
        self.assertEqual(
            config.resolve(None, "flag.db", {}, cwd=self.tmp)["gracia.db"], "flag.db"
        )

    def test_commands_run_in_a_directory_with_a_config_ini_use_it_without_dash_c(self):
        import contextlib
        import io
        import os
        from unittest import mock

        from gracia import cli

        (self.tmp / "config.ini").write_text("[app:main]\ngracia.db = site.duckdb\n")
        with contextlib.chdir(self.tmp), mock.patch.dict(os.environ):
            os.environ.pop(
                "GRACIA_CONFIG", None
            )  # the tests set it empty; here it is not set
            out = io.StringIO()
            with (
                contextlib.redirect_stdout(out),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(
                    cli.main(
                        [
                            "repo",
                            "create",
                            "test_repo",
                            "--admin",
                            "admin",
                            "--admin-password-file",
                            str(self.write_password()),
                        ]
                    ),
                    0,
                )
                self.assertEqual(cli.main(["repo", "list"]), 0)
        self.assertTrue(
            (self.tmp / "site.duckdb").exists()
        )  # the file the config names
        self.assertIn("test_repo", out.getvalue())

    def write_password(self) -> Path:
        path = self.tmp / "pw"
        path.write_text("a long password\n")
        path.chmod(0o600)
        return path

    def test_serve_db_takes_its_file_and_address_from_the_default_config_ini(self):
        import contextlib
        import os
        from unittest import mock

        from gracia import cli

        (self.tmp / "config.ini").write_text(
            "[app:main]\ngracia.db = quack:localhost:9777\ngracia.db.file = mine.db\n"
            "gracia.db.token_file = mine.token\n"
        )
        seen = {}
        with (
            contextlib.chdir(self.tmp),
            mock.patch.dict(os.environ),
            mock.patch.object(cli, "serve_db", lambda *a, **k: seen.update(args=a)),
        ):
            os.environ.pop("GRACIA_CONFIG", None)
            args = cli.build_parser().parse_args(["serve-db"])
            self.assertEqual(cli.cmd_serve_db(args), 0)
        db, host, port, token_file = seen["args"]
        self.assertEqual(
            (Path(db).name, host, port, Path(token_file).name),
            ("mine.db", "localhost", 9777, "mine.token"),
        )
