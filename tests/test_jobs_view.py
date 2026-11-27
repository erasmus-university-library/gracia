import os
import re
from importlib.resources import files
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
from gracia.jobs.job import Job, ValidateJob, all_jobs, job
from gracia.main import make_config

ORIGIN = {"Origin": "http://localhost:80"}  # what the browser says of the test host


class People(HeaderPolicy):
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
    def test_editors_and_administrators_may_open_the_page_and_the_deform_view_the_public_may_not(
        self,
    ):
        for user, status in (("admin", 200), ("ed", 200), ("guest", 403), (None, 403)):
            with self.subTest(user=user):
                headers = as_user(user) if user else {}
                got = self.web.get(
                    "/test_repo/jobs", headers=headers, expect_errors=True
                )
                self.assertEqual(got.status_int, status)
                got = self.web.get(
                    "/test_repo/deform/schema-files?res_type=person",
                    headers=headers,
                    expect_errors=True,
                )
                self.assertEqual(got.status_int, status)
        for user, status in (("ed", 200), ("guest", 403)):
            got = self.web.get(
                "/test_repo/jobs/list", headers=as_user(user), expect_errors=True
            )
            self.assertEqual(got.status_int, status)

    def test_the_page_is_made_with_the_layout_the_job_panel_and_the_scripts_from_the_libraries(
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
        # every element the script looks for is in the page
        script = (
            files("gracia") / "assets" / "static" / "scripts" / "jobs.js"
        ).read_text()
        wanted = set(re.findall(r"\[data-([a-z-]+)\]", script))
        self.assertIn("jobs-body", wanted)
        for name in wanted - {
            "start-job",
            "start-error",
        }:  # those two belong to other pages
            self.assertRegex(page.text, rf"data-{name}\b", name)
        # the page includes the job panel that other pages can include too
        self.assertRegex(
            page.text,
            r'data-job-panel=""\s+data-list-url="http://localhost/test_repo/jobs/list"',
        )
        self.assertIn(
            'data-start-url="http://localhost/test_repo/jobs/start"', page.text
        )
        # the panel has a triangle that folds open the last lines of the log in a frame
        self.assertRegex(page.text, r"<details[^>]*data-log-fold")  # a native triangle
        self.assertRegex(page.text, r"<iframe[^>]*data-log-frame")
        frame = must_search(r"<iframe[^>]*>", page.text)[0]
        self.assertNotIn("src=", frame)  # nothing is loaded until it is opened
        self.assertRegex(page.text, r"<summary[^>]*>Details</summary>")
        self.assertNotIn("data-log-open", page.text)
        self.assertNotIn("open in a page", page.text)

    def test_the_form_of_the_job_offers_the_types_and_the_schemas_that_follow_the_type_chosen(
        self,
    ):
        page = self.web.get("/test_repo/jobs", headers=as_user("ed")).text
        self.assertIn('id="form-job-validate"', page)
        self.assertIn('value="person"', page)
        self.assertIn(
            "person-unittest-01.ttl", page
        )  # the content types come first: person is on top
        self.assertNotIn(
            "user-minimal-01.ttl", page
        )  # the schemas of the other types wait for htmx
        # the schema select declares that it follows the type select
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
        # the options are just option elements
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
                ("person-strict-01.ttl", "person-strict-01.ttl"),
                ("person-unittest-01.ttl", "person-unittest-01.ttl"),
            ],
        )
        other = self.web.get(
            "/test_repo/deform/schema-files?res_type=user", headers=as_user("ed")
        )
        self.assertIn("user-minimal-01.ttl", other.text)
        self.assertNotIn("Person-", other.text)
        nothing = self.web.get(
            "/test_repo/deform/schema-files?res_type=nobody", headers=as_user("ed")
        )
        self.assertEqual(re.findall(r'value="([^"]*)"', nothing.text), [""])
        # a form with errors comes back with the schemas of the type that was chosen
        got = self.start(
            "ed", res_type="user", schema="person-unittest-01.ttl"
        )  # not a schema of user
        self.assertEqual(got.status_int, 422)
        form = got.json["form"]
        self.assertIn("Not one of the choices for this type", form)
        self.assertIn("user-minimal-01.ttl", form)
        self.assertNotIn("person-strict-01.ttl", form)

    def test_a_job_is_offered_only_to_those_who_may_start_it_and_needs_a_jobs_directory(
        self,
    ):
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
        # the permissions are named for the job
        found = {j.name: j.start_permission for j in all_jobs(self.config.registry)}
        self.assertEqual(found["validate"], "start_validate_job")
        self.assertIs(
            self.config.registry.queryUtility(IJob, "validate"),  # ty: ignore[missing-union-attribute]
            all_jobs(self.config.registry)[
                [j.name for j in all_jobs(self.config.registry)].index("validate")
            ],
        )
        # without a jobs directory the view says so
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

    def test_the_jobs_are_in_the_document_for_those_who_may_list_them_and_in_the_page_too(
        self,
    ):

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
                "/test_repo/api",
                headers={
                    **(as_user(user) if user else {}),
                    "Accept": "application/json",
                },
            )
            self.assertFalse([p for p in found.json["paths"] if p.startswith("/jobs")])

        page = self.web.get(
            "/test_repo/api", headers={**as_user("ed"), "Accept": "text/html"}
        )
        self.assertIn("/jobs/{id}/stop", page.text)


class StartTest(JobsViewCase):
    def test_starting_makes_a_job_for_the_caller_and_starts_its_process(self):

        got = self.start("ed", schema="person-strict-01.ttl")
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
            {"res_type": "person", "schema": "person-strict-01.ttl"},
        )
        self.assertTrue((self.jobs_dir / job_id).is_dir())
        # the default schema is left out of the stage
        job_id = self.start("ed").json["id"]
        self.assertEqual(
            self.job(job_id)["stages"][1]["params"], {"res_type": "person"}
        )

    def test_a_form_with_errors_comes_back_to_be_shown_again_and_a_foreign_post_is_refused(
        self,
    ):
        got = self.start("ed", res_type="")
        self.assertEqual(got.status_int, 422)
        self.assertIn("Required", got.json["form"])
        got = self.start("ed", res_type="person", schema="nobody-full-01.ttl")
        self.assertEqual(got.status_int, 422)
        self.assertIn("Not one of the choices for this type", got.json["form"])
        self.spawn.assert_not_called()
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
        # a job without a snapshot shows its stages waiting
        job = self.listed("ed")[0]
        self.assertEqual(
            [(s["name"], s["state"]) for s in job["stages"]],
            [("Export", "pending"), ("Validate", "pending")],
        )
        self.assertFalse(
            job["determinate"]
        )  # nothing is known yet: the panel slides a piece
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
        # a finished one is complete
        with JobsDb(self.application) as db:
            db.update_job(job_id, {"state": "done"})
        job = self.listed("ed")[0]
        self.assertEqual(
            (job["percent"], job["determinate"], job["active"]), (100.0, True, False)
        )

    def test_the_list_can_be_filtered_by_job_and_subject_and_a_job_without_creator_is_for_administrators(
        self,
    ):
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
        # a job from the command line has no creator: only administrators see it
        self.assertEqual(self.listed("ed", "?job=closed"), [])
        self.assertEqual(len(self.listed("admin", "?job=closed")), 1)

    def test_one_job_by_its_address_and_the_creator_and_administrators_may_stop_it(
        self,
    ):
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
        # stopping a job that has ended does nothing
        with JobsDb(self.application) as db:
            db.update_job(job_id, {"state": "done"})
        with mock.patch.object(procs, "spawn_stop") as stopper:
            got = self.web.post(url, headers={**as_user("ed"), **ORIGIN})
            self.assertEqual(got.json, {"state": "done"})
            stopper.assert_not_called()


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

    def listed_job(self, user):
        return self.web.get("/test_repo/jobs/list", headers=as_user(user)).json["jobs"][
            0
        ]

    def test_what_the_job_rejected_is_a_download_by_the_name_in_the_row_for_those_who_may_see_the_job(
        self,
    ):
        empty = self.start("ed").json["id"]
        self.assertIsNone(self.listed_job("ed")["download_url"])
        # an administrator is told it is not there; below a node a caller may not
        # `view`, "not there" is answered like "not yours" (the creator may see and
        # stop the job, not `view` it)
        got = self.web.get(
            f"/test_repo/jobs/{empty}/download",
            headers=as_user("admin"),
            expect_errors=True,
        )
        self.assertEqual(got.status_int, 404)
        job_id = self.finished()
        got = self.web.get(f"/test_repo/jobs/{job_id}/download", headers=as_user("ed"))
        self.assertEqual(got.text, '{"id": "a"}\n{"id": "b"}\n')
        self.assertIn("application/x-ndjson", got.content_type)
        self.assertIn(
            'attachment; filename="rejected.jsonl"', got.headers["Content-Disposition"]
        )
        listed = self.web.get("/test_repo/jobs/list", headers=as_user("ed")).json
        urls = {j["id"]: j["download_url"] for j in listed["jobs"]}
        self.assertEqual(
            urls[job_id], f"http://localhost/test_repo/jobs/{job_id}/download"
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

    def test_the_log_is_read_from_an_offset_in_whole_lines_and_is_one_of_two(self):
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
        # when the job is over the last line is given as it is
        (self.jobs_dir / job_id / "luigi.log").write_text(
            "first\nlast without a newline"
        )
        got = self.web.get(url, headers=as_user("ed"))
        self.assertEqual(got.text, "first\nlast without a newline")
        # a job with no log yet
        empty = self.start("ed").json["id"]
        got = self.web.get(f"/test_repo/jobs/{empty}/log", headers=as_user("ed"))
        self.assertEqual((got.text, got.headers["X-Log-Offset"]), ("", "0"))
        # while the job runs only whole lines of the log are given
        directory = self.jobs_dir / empty
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "luigi.log").write_text("first\nsecond\nthi")
        url = f"/test_repo/jobs/{empty}/log"
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

    def test_the_log_page_follows_the_log_by_asking_for_what_is_new_and_can_be_the_bare_log(
        self,
    ):
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
        # the bare log for the frame
        bare = self.web.get(
            f"/test_repo/jobs/{job_id}/logs?embed=1&name=luigi", headers=as_user("ed")
        )
        self.assertIn("data-job-log", bare.text)
        self.assertNotIn("<header", bare.text)  # no layout around it
        self.assertIn("overflow: auto", bare.text)  # it scrolls in the frame
        self.assertIn("joblog.js", bare.text.split("</head>")[0])
        self.assertEqual(
            self.web.get(
                f"/test_repo/jobs/{job_id}/logs?embed=1",
                headers=as_user("ed2"),
                expect_errors=True,
            ).status_int,
            403,
        )
        # the panel links to the page that follows the log
        job = self.listed_job("ed")
        self.assertEqual(
            job["logs_url"], f"http://localhost/test_repo/jobs/{job_id}/logs"
        )
        self.assertEqual(
            job["log_url"], f"http://localhost/test_repo/jobs/{job_id}/log"
        )
        # the script of the log page finds what it needs in the page
        script = (
            files("gracia") / "assets" / "static" / "scripts" / "joblog.js"
        ).read_text()
        for name in set(re.findall(r"\[data-([a-z-]+)\]", script)):
            self.assertRegex(page.text, rf"data-{name}\b", name)
        for name in ("log-url", "status-url"):  # what it reads from the element
            self.assertIn(f"data-{name}=", page.text)
            self.assertIn(
                re.sub("-(.)", lambda m: m[1].upper(), name), script
            )  # logUrl, statusUrl


class FormTest(JobsViewCase):
    def test_the_validate_form_is_a_colander_schema_bound_to_the_repository_and_the_command_line_makes_the_same_job(
        self,
    ):
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
        spec = ValidateJob().spec(
            "test_repo", {"res_type": "person", "schema": "person-unittest-01.ttl"}
        )
        self.assertEqual((spec["job"], spec["subject"]), ("validate", "person"))
        self.assertEqual(
            spec["stages"][1],
            {
                "stage": "Validate",
                "params": {"res_type": "person", "schema": "person-unittest-01.ttl"},
            },
        )
        self.assertEqual(spec["stages"][0]["stage"], "Export")


class EndToEndTest(ProcessCase):
    def test_jobs_started_from_the_view_run_to_their_end_and_the_export_downloads(self):
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
            for name in ("validate", "rewrite", "export")
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
        job = jobs[ids["validate"]]
        self.assertEqual(job["percent"], 100.0)
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
        got = web.get(jobs[ids["export"]]["download_url"], headers=as_user("ed"))
        self.assertEqual(got.content_type, "application/n-triples")
        subjects = {
            line.split(" ", 1)[0]
            for line in got.text.splitlines()
            if line.startswith("<rec:")
        }
        self.assertEqual(len(subjects), 12)  # every person, once
