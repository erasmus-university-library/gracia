import os
import re
from unittest import mock

import colander
from jobs_support import JobsCase, echo
from support import must_search
from test_jobs_processes import ProcessCase
from test_web import HeaderPolicy
from webtest import TestApp

from gracia.interfaces import IJob
from gracia.jobs import procs
from gracia.jobs.db import JobsDb
from gracia.jobs.job import Job, all_jobs, job
from gracia.main import make_config

ORIGIN = {"Origin": "http://localhost:80"}  # what the browser says of the test host


class People(HeaderPolicy):
    """`admin` is an administrator, `ed` and `ed2` are editors, `guest` is in no
    group."""

    def permits(self, request, context, permission):
        from pyramid.authorization import ACLHelper, Authenticated, Everyone

        user = self.identity(request)
        principals = {Everyone}
        if user:
            principals |= {Authenticated, f"user:{user}"}
        if user == "admin":
            principals.add("group:admin")
        if user in ("ed", "ed2"):
            principals.add("group:editor")
        return ACLHelper().permits(context, principals, permission)


def as_user(name: str) -> dict:
    return {"X-User": name}


class Closed(Job):
    """A job that nobody but administrators may start (no groups)."""

    title = "Closed"
    description = "Only for administrators."
    groups = ()

    def spec(self, repo, params):
        return {"repo": repo, "job": "closed", "stages": [echo()]}


class JobsViewCase(JobsCase):
    def setUp(self):
        super().setUp()
        self.web = self.client()

    def settings(self) -> dict:  # ty: ignore[incompatible-attribute-override]
        return {
            "gracia.application": self.application,
            "gracia.jobs_dir": str(self.jobs_dir),
            "gracia.stages": "jobs_support",
            "gracia.security_policy": People(),
        }

    def client(self, extra=None) -> TestApp:
        config = make_config({**self.settings(), **(extra or {})})
        job("closed")(Closed)
        config.add_gracia_job("closed", Closed)
        self.config = config
        config.commit()
        return TestApp(config.make_wsgi_app())

    def start(self, user="ed", job="validate", **fields):
        data = {"res_type": "person", "schema": "", **fields}
        with mock.patch.object(procs, "spawn", return_value=os.getpid()) as spawn:
            response = self.web.post(
                f"/test_repo/jobs/start?job={job}",
                data,
                headers={**as_user(user), **ORIGIN},
                expect_errors=True,
            )
        self.spawn = spawn
        return response


class AccessTest(JobsViewCase):
    def test_editors_and_administrators_may_open_the_page_the_public_may_not(self):
        for user, status in (("admin", 200), ("ed", 200), ("guest", 403), (None, 403)):
            with self.subTest(user=user):
                headers = as_user(user) if user else {}
                got = self.web.get(
                    "/test_repo/jobs", headers=headers, expect_errors=True
                )
                self.assertEqual(got.status_int, status)
        for user, status in (("ed", 200), ("guest", 403)):
            got = self.web.get(
                "/test_repo/jobs/list", headers=as_user(user), expect_errors=True
            )
            self.assertEqual(got.status_int, status)

    def test_the_page_is_made_with_the_layout_and_the_scripts_come_from_the_libraries(
        self,
    ):
        page = self.web.get("/test_repo/jobs", headers=as_user("ed"))
        for part in (
            "<header",
            "<main",
            "<footer",
            "Jobs of test_repo",
            "Validate",
            "Start…",
        ):
            self.assertIn(part, page.text)
        self.assertIn("👋", page.text)  # the logo
        self.assertIn("jobs.js", page.text)
        self.assertNotIn(
            "<script src=", page.text
        )  # nothing is written into the page by hand

    def test_every_element_the_script_looks_for_is_in_the_page(self):
        from importlib.resources import files

        script = (
            files("gracia") / "assets" / "static" / "scripts" / "jobs.js"
        ).read_text()
        page = self.web.get("/test_repo/jobs", headers=as_user("ed")).text
        wanted = set(re.findall(r"\[data-([a-z-]+)\]", script))
        self.assertIn("jobs-body", wanted)
        for name in wanted - {
            "start-job",
            "start-error",
        }:  # those two belong to other pages
            self.assertRegex(page, rf"data-{name}\b", name)

    def test_the_page_includes_the_job_panel_that_other_pages_can_include_too(self):
        page = self.web.get("/test_repo/jobs", headers=as_user("ed")).text
        self.assertRegex(
            page,
            r'data-job-panel=""\s+data-list-url="http://localhost/test_repo/jobs/list"',
        )
        self.assertIn('data-start-url="http://localhost/test_repo/jobs/start"', page)

    def test_the_form_of_the_job_offers_the_types_and_the_schemas_of_the_first(self):
        page = self.web.get("/test_repo/jobs", headers=as_user("ed")).text
        self.assertIn('id="form-job-validate"', page)
        self.assertIn('value="person"', page)
        self.assertIn(
            "Person-full-01.ttl", page
        )  # the content types come first: person is on top
        self.assertNotIn(
            "User-full-01.ttl", page
        )  # the schemas of the other types wait for htmx

    def test_the_schema_select_declares_that_it_follows_the_type_select(self):
        page = self.web.get("/test_repo/jobs", headers=as_user("ed")).text
        res_type = must_search(r'<select name="res_type"[^>]*>', page)[0]
        schema = must_search(r'<select name="schema"[^>]*>', page)[0]
        self.assertNotIn("hx-", res_type)  # the first select is a plain select
        type_id = must_search(r'id="([^"]+)"', res_type)[1]
        self.assertIn(
            'hx-get="http://localhost/test_repo/deform/schema-files?field=schema"',
            schema,
        )
        self.assertIn(f'hx-trigger="change from:#{type_id}"', schema)
        self.assertIn(f'hx-include="#{type_id}"', schema)
        self.assertIn('hx-target="this"', schema)

    def test_the_options_are_just_option_elements(self):
        got = self.web.get(
            "/test_repo/deform/schema-files?res_type=person", headers=as_user("ed")
        )
        self.assertIn("text/html", got.content_type)
        self.assertNotIn("<select", got.text)
        self.assertNotIn("<html", got.text)
        options = re.findall(r'<option value="([^"]*)">([^<]*)</option>', got.text)
        self.assertEqual(
            options,
            [
                ("", "the schema the type is on"),
                ("Person-full-01.ttl", "Person-full-01.ttl"),
                ("Person-strict-01.ttl", "Person-strict-01.ttl"),
            ],
        )
        other = self.web.get(
            "/test_repo/deform/schema-files?res_type=user", headers=as_user("ed")
        )
        self.assertIn("User-full-01.ttl", other.text)
        self.assertNotIn("Person-", other.text)
        nothing = self.web.get(
            "/test_repo/deform/schema-files?res_type=nobody", headers=as_user("ed")
        )
        self.assertEqual(re.findall(r'value="([^"]*)"', nothing.text), [""])

    def test_the_deform_view_has_the_permission_it_was_declared_with(self):
        url = "/test_repo/deform/schema-files?res_type=person"
        for user, status in (("admin", 200), ("ed", 200), ("guest", 403), (None, 403)):
            headers = as_user(user) if user else {}
            self.assertEqual(
                self.web.get(url, headers=headers, expect_errors=True).status_int,
                status,
            )

    def test_a_form_with_errors_comes_back_with_the_schemas_of_the_type_that_was_chosen(
        self,
    ):
        got = self.start(
            "ed", res_type="user", schema="Person-full-01.ttl"
        )  # not a schema of user
        self.assertEqual(got.status_int, 422)
        form = got.json["form"]
        self.assertIn("Not one of the choices for this type", form)
        self.assertIn("User-full-01.ttl", form)
        self.assertNotIn("Person-strict-01.ttl", form)

    def test_a_job_is_offered_only_to_those_who_may_start_it(self):
        self.assertNotIn(
            "Closed", self.web.get("/test_repo/jobs", headers=as_user("ed")).text
        )
        self.assertIn(
            "Closed", self.web.get("/test_repo/jobs", headers=as_user("admin")).text
        )
        refused = self.start("ed", job="closed")
        self.assertEqual(refused.status_int, 403)
        # below a node the caller may not `view`, "not there" is answered like "not
        # yours"
        self.assertEqual(self.start("ed", job="nope").status_int, 403)
        self.assertEqual(self.start("admin", job="nope").status_int, 404)

    def test_the_permissions_are_named_for_the_job(self):
        found = {j.name: j.start_permission for j in all_jobs(self.config.registry)}
        self.assertEqual(found["validate"], "start_validate_job")
        self.assertIs(
            self.config.registry.queryUtility(IJob, "validate"),  # ty: ignore[missing-union-attribute]
            all_jobs(self.config.registry)[
                [j.name for j in all_jobs(self.config.registry)].index("validate")
            ],
        )

    def test_without_a_jobs_directory_the_view_says_so(self):
        web = TestApp(
            make_config({**self.settings(), "gracia.jobs_dir": ""}).make_wsgi_app()
        )
        got = web.get("/test_repo/jobs/list", headers=as_user("ed"), expect_errors=True)
        self.assertEqual(got.status_int, 503)
        self.assertIn("gracia.jobs_dir", got.text)


class ApiDocumentTest(JobsViewCase):
    def paths(self, user) -> dict:
        got = self.web.get(
            "/test_repo/api", headers={**as_user(user), "Accept": "application/json"}
        )
        return got.json["paths"]

    def test_the_jobs_are_in_the_document_for_those_who_may_list_them(self):
        for user in ("ed", "admin"):
            paths = self.paths(user)
            self.assertEqual(
                {p for p in paths if p.startswith("/jobs")},
                {"/jobs", "/jobs/list", "/jobs/start", "/jobs/{id}", "/jobs/{id}/stop"},
            )
        self.assertEqual(
            self.paths("ed")["/jobs/{id}"]["get"]["x-gracia-permission"], "view_job"
        )
        self.assertIn("post", self.paths("ed")["/jobs/start"])
        for user in ("guest", None):
            found = self.web.get(
                "/test_repo/api", headers={**(as_user(user) if user else {})}
            )
            self.assertFalse([p for p in found.json["paths"] if p.startswith("/jobs")])

    def test_the_page_for_a_browser_lists_them_too(self):
        page = self.web.get(
            "/test_repo/api", headers={**as_user("ed"), "Accept": "text/html"}
        )
        self.assertIn("/jobs/{id}/stop", page.text)


class StartTest(JobsViewCase):
    def test_starting_makes_a_job_for_the_caller_and_starts_its_process(self):
        got = self.start("ed", schema="Person-strict-01.ttl")
        self.assertEqual(got.status_int, 200, got.text)
        job_id = got.json["id"]
        self.spawn.assert_called_once()
        job = self.job(job_id)
        self.assertEqual(
            (job["created_by"], job["repo"], job["state"]),
            ("user:ed", "test_repo", "running"),
        )
        self.assertEqual((job["job"], job["subject"]), ("validate", "person"))
        stages = job["stages"]
        self.assertEqual([s["stage"] for s in stages], ["Export", "Validate"])
        self.assertEqual(stages[0]["params"]["res_type"], "person")
        self.assertEqual(
            stages[1]["params"],
            {"res_type": "person", "schema": "Person-strict-01.ttl"},
        )
        self.assertTrue((self.jobs_dir / job_id).is_dir())

    def test_the_default_schema_is_left_out_of_the_stage(self):
        job_id = self.start("ed").json["id"]
        self.assertEqual(
            self.job(job_id)["stages"][1]["params"], {"res_type": "person"}
        )

    def test_a_form_with_errors_comes_back_to_be_shown_again(self):
        got = self.start("ed", res_type="")
        self.assertEqual(got.status_int, 422)
        self.assertIn("Required", got.json["form"])
        got = self.start("ed", res_type="person", schema="Nobody-full-01.ttl")
        self.assertEqual(got.status_int, 422)
        self.assertIn("Not one of the choices for this type", got.json["form"])
        self.spawn.assert_not_called()
        with JobsDb(self.application) as db:
            self.assertEqual(db.list_jobs(), [])

    def test_a_post_that_does_not_come_from_this_site_is_refused(self):
        for headers in ({}, {"Origin": "http://evil.example"}):
            with self.subTest(headers=headers):
                got = self.web.post(
                    "/test_repo/jobs/start?job=validate",
                    {"res_type": "person", "schema": ""},
                    headers={**as_user("ed"), **headers},
                    expect_errors=True,
                )
                self.assertEqual(got.status_int, 400)
        with JobsDb(self.application) as db:
            self.assertEqual(db.list_jobs(), [])


class ListAndStopTest(JobsViewCase):
    def listed(self, user, query="") -> list[dict]:
        return self.web.get(f"/test_repo/jobs/list{query}", headers=as_user(user)).json[
            "jobs"
        ]

    def progress(self, job_id, export=(2, 1, 4), validate=(0, 0, 4)):
        """What the job process would have written: (done, running, total) batches per
        stage."""

        def stage(name, state, batches):
            done, running, total = batches
            return {
                "name": name,
                "state": state,
                "batches": {"done": done, "running": running, "total": total},
                "processed": done * 2,
                "passed": done * 2,
                "rejected": {},
                "samples": [],
                "total": None,
                "estimated": False,
            }

        with JobsDb(self.application) as db:
            db.update_job(
                job_id,
                {
                    "progress": {
                        "stages": [
                            stage("Export", "running", export),
                            stage("Validate", "pending", validate),
                        ]
                    }
                },
            )

    def test_the_list_shows_state_and_progress_to_the_creator_and_administrators_only(
        self,
    ):
        job_id = self.start("ed").json["id"]
        self.progress(job_id)
        for user, count in (("ed", 1), ("admin", 1), ("ed2", 0)):
            self.assertEqual(len(self.listed(user)), count, user)
        job = self.listed("ed")[0]
        self.assertEqual(
            (job["id"], job["state"], job["active"]), (job_id, "running", True)
        )
        self.assertEqual(
            (job["label"], job["job"], job["subject"]),
            ("Validate: person", "validate", "person"),
        )
        self.assertEqual([s["name"] for s in job["stages"]], ["Export", "Validate"])
        self.assertEqual(
            job["stages"][0]["batches"], {"done": 2, "running": 1, "total": 4}
        )
        # the stages overlap, so how far the job is is the mean of how far each stage is
        self.assertEqual((job["percent"], job["determinate"]), (25.0, True))
        self.assertTrue(job["can_stop"])

    def test_a_job_without_a_snapshot_shows_its_stages_waiting_and_a_finished_one_is_complete(  # noqa: E501
        self,
    ):
        job_id = self.start("ed").json["id"]
        job = self.listed("ed")[0]
        self.assertEqual(
            [(s["name"], s["state"]) for s in job["stages"]],
            [("Export", "pending"), ("Validate", "pending")],
        )
        self.assertFalse(
            job["determinate"]
        )  # nothing is known yet: the panel slides a piece
        with JobsDb(self.application) as db:
            db.update_job(job_id, {"state": "done"})
        job = self.listed("ed")[0]
        self.assertEqual(
            (job["percent"], job["determinate"], job["active"]), (100.0, True, False)
        )

    def test_the_list_can_be_filtered_by_job_and_subject(self):
        mine = self.start("ed").json["id"]
        with JobsDb(self.application) as db:
            other = db.create(
                {
                    "repo": "test_repo",
                    "job": "validate",
                    "subject": "user",
                    "stages": [echo()],
                }
            )
            elsewhere = db.create(
                {
                    "repo": "test_repo",
                    "job": "closed",
                    "subject": "person",
                    "stages": [echo()],
                }
            )

        def ids(query):
            return sorted(j["id"] for j in self.listed("admin", query))

        self.assertEqual(ids(""), sorted([mine, other, elsewhere]))
        self.assertEqual(ids("?job=validate"), sorted([mine, other]))
        self.assertEqual(ids("?subject=person"), sorted([mine, elsewhere]))
        self.assertEqual(ids("?job=validate&subject=person"), [mine])
        self.assertEqual(ids("?subject=nobody"), [])
        self.assertEqual(
            self.listed("ed", "?job=validate&subject=person")[0]["id"], mine
        )

    def test_one_job_by_its_address_for_those_who_may_see_it(self):
        job_id = self.start("ed").json["id"]
        self.assertEqual(
            self.web.get(f"/test_repo/jobs/{job_id}", headers=as_user("ed")).json["id"],
            job_id,
        )
        self.assertEqual(
            self.web.get(
                f"/test_repo/jobs/{job_id}", headers=as_user("ed2"), expect_errors=True
            ).status_int,
            403,
        )
        self.assertEqual(
            self.web.get(
                "/test_repo/jobs/not-a-job", headers=as_user("ed"), expect_errors=True
            ).status_int,
            403,  # not there, and not said
        )

    def test_the_creator_and_administrators_may_stop_a_job_others_may_not(self):
        job_id = self.start("ed").json["id"]
        url = f"/test_repo/jobs/{job_id}/stop"
        with mock.patch.object(procs, "spawn_stop", return_value=1) as stopper:
            refused = self.web.post(
                url, headers={**as_user("ed2"), **ORIGIN}, expect_errors=True
            )
            self.assertEqual(refused.status_int, 403)
            stopper.assert_not_called()
            forged = self.web.post(url, headers=as_user("ed"), expect_errors=True)
            self.assertEqual(forged.status_int, 400)
            for user in ("ed", "admin"):
                got = self.web.post(url, headers={**as_user(user), **ORIGIN})
                self.assertEqual(
                    (got.status_int, got.json), (202, {"state": "stopping"})
                )
            self.assertEqual(stopper.call_args.kwargs["by"], "admin")

    def test_stopping_a_job_that_has_ended_does_nothing(self):
        job_id = self.start("ed").json["id"]
        with JobsDb(self.application) as db:
            db.update_job(job_id, {"state": "done"})
        with mock.patch.object(procs, "spawn_stop") as stopper:
            got = self.web.post(
                f"/test_repo/jobs/{job_id}/stop", headers={**as_user("ed"), **ORIGIN}
            )
            self.assertEqual(got.json, {"state": "done"})
            stopper.assert_not_called()

    def test_a_job_from_the_command_line_has_no_creator_so_only_administrators_see_it(
        self,
    ):
        with JobsDb(self.application) as db:
            db.create({"repo": "test_repo", "stages": [echo()]})
        self.assertEqual(len(self.listed("admin")), 1)
        self.assertEqual(self.listed("ed"), [])


class DownloadAndLogTest(JobsViewCase):
    def finished(self):
        job_id = self.start("ed").json["id"]
        directory = self.jobs_dir / job_id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "rejected.jsonl").write_text('{"id": "a"}\n{"id": "b"}\n')
        (directory / "luigi.log").write_text("".join(f"line {n}\n" for n in range(5)))
        (directory / "job.log").write_text("what the process printed\n")
        with JobsDb(self.application) as db:
            db.update_job(
                job_id,
                {
                    "state": "done",
                    "download_file": "rejected.jsonl",
                    "download_content_type": "application/x-ndjson",
                },
            )
        return job_id

    def test_what_the_job_rejected_is_a_download_for_those_who_may_see_the_job(self):
        job_id = self.finished()
        got = self.web.get(f"/test_repo/jobs/{job_id}/download", headers=as_user("ed"))
        self.assertEqual(got.text, '{"id": "a"}\n{"id": "b"}\n')
        self.assertIn("application/x-ndjson", got.content_type)
        self.assertIn(
            'attachment; filename="rejected.jsonl"', got.headers["Content-Disposition"]
        )
        self.assertEqual(
            self.listed_job("ed")["download_url"],
            f"http://localhost/test_repo/jobs/{job_id}/download",
        )
        for user in ("ed2", "guest"):
            self.assertEqual(
                self.web.get(
                    f"/test_repo/jobs/{job_id}/download",
                    headers=as_user(user),
                    expect_errors=True,
                ).status_int,
                403,
            )

    def listed_job(self, user):
        return self.web.get("/test_repo/jobs/list", headers=as_user(user)).json["jobs"][
            0
        ]

    def test_a_job_with_nothing_to_download_says_so(self):
        job_id = self.start("ed").json["id"]
        self.assertIsNone(self.listed_job("ed")["download_url"])
        # an administrator is told it is not there; below a node a caller may not
        # `view`, "not
        # there" is answered like "not yours" (the creator may see and stop the job, not
        # `view` it)
        got = self.web.get(
            f"/test_repo/jobs/{job_id}/download",
            headers=as_user("admin"),
            expect_errors=True,
        )
        self.assertEqual(got.status_int, 404)

    def test_the_download_is_by_the_name_in_the_row_never_by_a_path(self):
        job_id = self.finished()
        outside = self.jobs_dir / "secret.txt"
        outside.write_text("no")
        with JobsDb(self.application) as db:
            db.update_job(job_id, {"download_file": "../secret.txt"})
        got = self.web.get(
            f"/test_repo/jobs/{job_id}/download",
            headers=as_user("admin"),
            expect_errors=True,
        )
        self.assertEqual(got.status_int, 404)  # only the last part of the name counts

    def test_the_log_is_read_from_an_offset_in_pieces(self):
        job_id = self.finished()
        url = f"/test_repo/jobs/{job_id}/log"
        whole = self.web.get(url, headers=as_user("ed"))
        self.assertEqual(whole.text, "".join(f"line {n}\n" for n in range(5)))
        self.assertEqual(whole.headers["X-Log-Offset"], str(len(whole.text)))
        more = self.web.get(f"{url}?offset=7", headers=as_user("ed"))
        self.assertEqual(more.text, "".join(f"line {n}\n" for n in range(5))[7:])
        past = self.web.get(f"{url}?offset=9999", headers=as_user("ed"))
        self.assertEqual(
            (past.text, past.headers["X-Log-Offset"]), ("", str(len(whole.text)))
        )
        raw = self.web.get(f"{url}?name=job", headers=as_user("ed"))
        self.assertEqual(raw.text, "what the process printed\n")
        self.assertIn("text/plain", raw.content_type)

    def test_the_log_page_follows_the_log_by_asking_for_what_is_new(self):
        job_id = self.finished()
        page = self.web.get(f"/test_repo/jobs/{job_id}/logs", headers=as_user("ed"))
        self.assertIn("text/html", page.content_type)
        self.assertIn(
            f'data-log-url="http://localhost/test_repo/jobs/{job_id}/log"', page.text
        )
        self.assertIn(
            f'data-status-url="http://localhost/test_repo/jobs/{job_id}/"', page.text
        )
        self.assertIn("data-log-follow", page.text)
        self.assertIn(
            "joblog.js", page.text.split("</head>")[0]
        )  # the script is in the head
        self.assertRegex(page.text, r'<option value="luigi" selected')
        raw = self.web.get(
            f"/test_repo/jobs/{job_id}/logs?name=job", headers=as_user("ed")
        )
        self.assertRegex(raw.text, r'<option value="job" selected')
        odd = self.web.get(
            f"/test_repo/jobs/{job_id}/logs?name=../exit", headers=as_user("ed")
        )
        self.assertRegex(
            odd.text, r'<option value="luigi" selected'
        )  # only one of the two
        for user in ("ed2", "guest"):
            self.assertEqual(
                self.web.get(
                    f"/test_repo/jobs/{job_id}/logs",
                    headers=as_user(user),
                    expect_errors=True,
                ).status_int,
                403,
            )

    def test_while_the_job_runs_only_whole_lines_of_the_log_are_given(self):
        """A record can be written in two pieces: the half that is there waits for the
        next read."""
        job_id = self.start("ed").json["id"]  # running
        directory = self.jobs_dir / job_id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "luigi.log").write_text("first\nsecond\nthi")
        url = f"/test_repo/jobs/{job_id}/log"
        got = self.web.get(url, headers=as_user("ed"))
        self.assertEqual(
            (got.text, got.headers["X-Log-Offset"]), ("first\nsecond\n", "13")
        )
        (directory / "luigi.log").write_text("first\nsecond\nthird\n")
        more = self.web.get(f"{url}?offset=13", headers=as_user("ed"))
        self.assertEqual((more.text, more.headers["X-Log-Offset"]), ("third\n", "19"))
        # a line with no end in sight is given as it is, or the log would never move on
        (directory / "luigi.log").write_text("x" * 10)
        self.assertEqual(self.web.get(url, headers=as_user("ed")).text, "x" * 10)

    def test_when_the_job_is_over_the_last_line_is_given_as_it_is(self):
        job_id = self.finished()
        (self.jobs_dir / job_id / "luigi.log").write_text(
            "first\nlast without a newline"
        )
        got = self.web.get(f"/test_repo/jobs/{job_id}/log", headers=as_user("ed"))
        self.assertEqual(got.text, "first\nlast without a newline")

    def test_the_panel_has_a_triangle_that_folds_open_the_last_lines_of_the_log_in_a_frame(  # noqa: E501
        self,
    ):
        page = self.web.get("/test_repo/jobs", headers=as_user("ed")).text
        self.assertRegex(page, r"<details[^>]*data-log-fold")  # a native triangle
        self.assertRegex(page, r"<iframe[^>]*data-log-frame")
        frame = must_search(r"<iframe[^>]*>", page)[0]
        self.assertNotIn("src=", frame)  # nothing is loaded until it is opened
        self.assertRegex(
            page, r"<summary[^>]*>Details</summary>"
        )  # the triangle says Details
        self.assertNotIn("data-log-open", page)
        self.assertNotIn("open in a page", page)

    def test_the_log_page_can_be_the_bare_log_for_the_frame(self):
        job_id = self.finished()
        page = self.web.get(
            f"/test_repo/jobs/{job_id}/logs?embed=1&name=luigi", headers=as_user("ed")
        )
        self.assertIn("data-job-log", page.text)
        self.assertIn(
            f'data-log-url="http://localhost/test_repo/jobs/{job_id}/log"', page.text
        )
        self.assertNotIn("<header", page.text)  # no layout around it
        self.assertIn("overflow: auto", page.text)  # it scrolls in the frame
        self.assertIn("joblog.js", page.text.split("</head>")[0])
        self.assertEqual(
            self.web.get(
                f"/test_repo/jobs/{job_id}/logs?embed=1",
                headers=as_user("ed2"),
                expect_errors=True,
            ).status_int,
            403,
        )

    def test_the_panel_links_to_the_page_that_follows_the_log(self):
        job_id = self.finished()
        job = self.listed_job("ed")
        self.assertEqual(
            job["logs_url"], f"http://localhost/test_repo/jobs/{job_id}/logs"
        )
        self.assertEqual(
            job["log_url"], f"http://localhost/test_repo/jobs/{job_id}/log"
        )

    def test_the_script_of_the_log_page_finds_what_it_needs_in_the_page(self):
        from importlib.resources import files

        script = (
            files("gracia") / "assets" / "static" / "scripts" / "joblog.js"
        ).read_text()
        page = self.web.get(
            f"/test_repo/jobs/{self.finished()}/logs", headers=as_user("ed")
        ).text
        for name in set(re.findall(r"\[data-([a-z-]+)\]", script)):
            self.assertRegex(page, rf"data-{name}\b", name)
        for name in ("log-url", "status-url"):  # what it reads from the element
            self.assertIn(f"data-{name}=", page)
            self.assertIn(
                re.sub("-(.)", lambda m: m[1].upper(), name), script
            )  # logUrl, statusUrl

    def test_the_log_is_one_of_two_and_for_those_who_may_see_the_job(self):
        job_id = self.finished()
        url = f"/test_repo/jobs/{job_id}/log"
        self.assertEqual(
            self.web.get(
                f"{url}?name=../exit", headers=as_user("admin"), expect_errors=True
            ).status_int,
            404,
        )
        self.assertEqual(
            self.web.get(url, headers=as_user("ed2"), expect_errors=True).status_int,
            403,
        )
        empty = self.start("ed").json["id"]  # a job with no log yet
        got = self.web.get(f"/test_repo/jobs/{empty}/log", headers=as_user("ed"))
        self.assertEqual((got.text, got.headers["X-Log-Offset"]), ("", "0"))


class FormTest(JobsViewCase):
    def test_the_validate_form_is_a_colander_schema_bound_to_the_repository(self):
        # traversal asks the registry of the application what is below a repository
        self.config.begin()
        self.addCleanup(self.config.end)
        job = self.config.registry.queryUtility(IJob, "validate")  # ty: ignore[missing-union-attribute]
        repository = self.config.registry["application"]["test_repo"]  # ty: ignore[not-subscriptable]
        schema = job.params_schema().bind(
            repository=repository,
            request=mock.Mock(registry=self.config.registry),
            context=repository["jobs"],
        )
        self.assertEqual([n.name for n in schema.children], ["res_type", "schema"])
        self.assertEqual(
            schema.deserialize({"res_type": "person"}),
            {"res_type": "person", "schema": ""},
        )
        with self.assertRaises(colander.Invalid):
            schema.deserialize({"res_type": "nobody"})

    def test_the_command_line_makes_the_same_job(self):
        from gracia.jobs.validate import ValidateJob

        spec = ValidateJob().spec(
            "test_repo", {"res_type": "person", "schema": "Person-full-01.ttl"}
        )
        self.assertEqual((spec["job"], spec["subject"]), ("validate", "person"))
        self.assertEqual(
            spec["stages"][1],
            {
                "stage": "Validate",
                "params": {"res_type": "person", "schema": "Person-full-01.ttl"},
            },
        )
        self.assertEqual(spec["stages"][0]["stage"], "Export")


class EndToEndTest(ProcessCase):
    """The page starts a validate job that runs in a process of its own and reports on
    it."""

    def test_the_export_and_rewrite_jobs_of_a_type_run_from_the_view_and_the_export_downloads(  # noqa: E501
        self,
    ):
        config = make_config(
            {
                "gracia.application": self.application,
                "gracia.jobs_dir": str(self.jobs_dir),
                "gracia.stages": "jobs_support",
                "gracia.security_policy": People(),
                "gracia.config_file": str(self.ini),
            }
        )
        web = TestApp(config.make_wsgi_app())
        headers = {**as_user("ed"), **ORIGIN}
        ids = {
            name: web.post(
                f"/test_repo/jobs/start?job={name}",
                {"res_type": "person"},
                headers=headers,
            ).json["id"]
            for name in ("rewrite", "export")
        }
        for job_id in ids.values():
            self.wait_for(job_id, ("done", "failed"))
        jobs = {
            j["id"]: j
            for j in web.get(
                "/test_repo/jobs/list?subject=person", headers=as_user("ed")
            ).json["jobs"]
        }
        self.assertEqual({jobs[i]["state"] for i in ids.values()}, {"done"})
        export = jobs[ids["export"]]
        got = web.get(export["download_url"], headers=as_user("ed"))
        self.assertEqual(got.content_type, "application/n-triples")
        subjects = {
            line.split(" ", 1)[0]
            for line in got.text.splitlines()
            if line.startswith("<rec:")
        }
        self.assertEqual(len(subjects), 12)  # every person, once

    def test_a_job_started_from_the_view_runs_to_its_end(self):
        config = make_config(
            {
                "gracia.application": self.application,
                "gracia.jobs_dir": str(self.jobs_dir),
                "gracia.stages": "jobs_support",
                "gracia.security_policy": People(),
                "gracia.config_file": str(self.ini),
            }
        )
        web = TestApp(config.make_wsgi_app())
        started = web.post(
            "/test_repo/jobs/start?job=validate",
            {"res_type": "person", "schema": ""},
            headers={**as_user("ed"), **ORIGIN},
        )
        job_id = started.json["id"]
        self.wait_for(job_id, ("done", "failed"))
        job = web.get("/test_repo/jobs/list", headers=as_user("ed")).json["jobs"][0]
        self.assertEqual((job["state"], job["percent"]), ("done", 100.0))
        export, validate = job["stages"]
        self.assertEqual(
            (export["processed"], validate["processed"], validate["passed"]),
            (12, 12, 9),
        )
        self.assertEqual(validate["rejected"], {"violation": 2, "no_content": 1})
        self.assertEqual(job["rejected"], 3)
        # what it rejected can be fetched, and its log read, through the page
        got = web.get(job["download_url"], headers=as_user("ed"))
        self.assertEqual(len(got.text.splitlines()), 3)
        log = web.get(job["log_url"], headers=as_user("ed"))
        self.assertIn("luigi", log.text)
