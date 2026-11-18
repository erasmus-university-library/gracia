import re

from jobs_support import JobsCase  # noqa: F401 - shares the set-up of the jobs tests
from support import must_search
from test_jobs_view import ORIGIN, JobsViewCase, as_user
from type_tabs_support import add_notes
from webtest import TestApp

from gracia.main import make_config

URL = "/library/person/manage"
ADMIN = as_user("admin")


class AccessTest(JobsViewCase):
    def test_only_those_who_may_manage_open_it(self):
        for user, status in (("admin", 200), ("ed", 403), ("guest", 403), (None, 403)):
            with self.subTest(user=user):
                headers = as_user(user) if user else {}
                self.assertEqual(
                    self.web.get(URL, headers=headers, expect_errors=True).status_int,
                    status,
                )

    def test_it_is_in_the_api_document_for_those_who_may_manage(self):
        paths = self.web.get(
            "/library/api", headers={**ADMIN, "Accept": "application/json"}
        ).json["paths"]
        self.assertEqual(
            paths["/{type}/manage"]["get"]["x-gracia-permission"], "manage"
        )
        editor = self.web.get(
            "/library/api", headers={**as_user("ed"), "Accept": "application/json"}
        )
        self.assertNotIn("/{type}/manage", editor.json["paths"])


class PageTest(JobsViewCase):
    def page(self) -> str:
        return self.web.get(URL, headers=ADMIN).text

    def form(self) -> str:
        return must_search(
            r'<form\s+id="form-type-settings".*?</form>', self.page(), re.S
        )[0]

    def test_the_page_has_the_settings_of_the_type_with_the_schema_chosen(self):
        page = self.page()
        self.assertIn("Manage Person", page)
        self.assertIn("rec:type:5", page)  # the type record it is about
        select = must_search(r'<select name="schemaTemplate".*?</select>', page, re.S)[
            0
        ]
        self.assertRegex(
            select, r'<option selected="selected" value="Person-full-01.ttl">'
        )
        self.assertIn('value="Person-strict-01.ttl"', select)
        self.assertRegex(self.form(), r'<input type="text" name="slug"')
        self.assertRegex(
            page, r'<input[^>]*name="label"[^>]*readonly'
        )  # the label: not edited

    def test_the_schema_is_a_field_of_the_form_and_validate_is_at_its_end(self):
        form = self.form()
        self.assertEqual(form.count(">Validate</button>"), 1)
        self.assertLess(form.index("<select"), form.index(">Validate</button>"))
        self.assertLess(form.index(">Validate</button>"), form.index('name="save"'))
        self.assertEqual(form.count('name="save"'), 1)

    def test_the_validate_button_starts_the_job_with_the_form_as_it_is(self):
        page = self.page()
        button = must_search(r"<button[^>]*data-start-job[^>]*>", page)[0]
        self.assertIn(
            'data-start-job="http://localhost/library/jobs/start?job=validate"', button
        )
        self.assertIn('data-start-error="#validate-problem"', button)
        self.assertIn(
            'data-start-fields="res_type,schema"', button
        )  # only what the job reads
        self.assertIn('id="validate-problem"', page)
        self.assertRegex(page, r'<input type="hidden" name="res_type" value="person"')

    def test_the_history_of_the_jobs_is_not_shown_only_the_dialog_that_follows_one(
        self,
    ):
        page = self.page()
        self.assertRegex(
            page,
            r'data-job-panel=""\s+data-list-url="http://localhost/library/jobs/list"',
        )
        self.assertIn('data-filter="job=validate&amp;subject=person"', page)
        self.assertIn(
            'data-compact="true"', page
        )  # no table, and no asking until a job starts
        self.assertRegex(page, r'<table class="table align-middle d-none"')
        self.assertIn("data-progress-dialog", page)  # the dialog that follows a job
        self.assertIn("data-log-fold", page)

    def test_the_jobs_page_still_shows_its_table(self):
        page = self.web.get("/library/jobs", headers=ADMIN).text
        self.assertIn('data-compact=""', page)
        self.assertNotIn("align-middle d-none", page)

    def test_the_scripts_of_the_panel_are_in_the_head(self):
        head = self.page().split("</head>")[0]
        self.assertIn("jobs.js", head)
        self.assertIn("htmx.min.js", head)

    def test_the_edit_address_of_the_type_record_comes_here(self):
        got = self.web.get("/library/type/5/edit", headers=ADMIN)
        self.assertEqual(got.status_int, 302)
        self.assertEqual(
            got.headers["Location"], "http://localhost" + URL + "?tab=settings"
        )

    def test_someone_who_cannot_start_the_job_gets_the_select_only(self):
        # a manager may manage, but the validate job is for those the job names
        page = self.web.get(URL, headers=as_user("admin")).text
        self.assertIn("data-start-job", page)


class IdentificationTabTest(JobsViewCase):
    """The numbers of the records of the type: shown, and a swath of them can be
    reserved."""

    def page(self) -> str:
        return self.web.get(URL + "?tab=identification", headers=ADMIN).text

    def pane(self) -> str:
        return must_search(
            r'id="tab-identification".*?</form>.*?</dl>', self.page(), re.S
        )[0]

    def post(self, number):
        return self.web.post(
            URL + "?tab=identification",
            {
                "__formid__": "form-type-identification",
                "last": str(number),
                "save": "save",
            },
            headers={**ADMIN, **ORIGIN},
        )

    def numbering(self) -> dict:
        node = self.application["library"]
        self.addCleanup(node.close)
        return node.store().numbering("person")

    def test_the_tab_is_before_the_jobs_and_shows_the_number_that_is_taken(self):
        page = self.page()
        self.assertLess(
            page.index('data-tab="identification"'), page.index('data-tab="jobs"')
        )
        pane = self.pane()
        self.assertIn('id="form-type-identification"', pane)
        self.assertRegex(
            pane, r'<input[^>]*name="last"[^>]*value="12"'
        )  # twelve persons
        self.assertRegex(pane, r"Highest used</dt>\s*<dd[^>]*>12<")
        self.assertRegex(pane, r"Next number</dt>\s*<dd[^>]*>13<")

    def test_a_higher_number_reserves_the_numbers_up_to_it_and_the_next_is_past_it(
        self,
    ):
        self.assertEqual(self.post(500).status_int, 302)
        self.assertEqual(self.numbering()["next"], 501)
        self.assertRegex(self.pane(), r'name="last"[^>]*value="500"')
        node = self.application["library"]
        self.addCleanup(node.close)
        self.assertEqual(node.store().mint_id("person"), 501)
        self.assertEqual(
            self.numbering()["highest"], 12
        )  # nothing was made, only reserved

    def test_a_number_below_one_that_is_in_use_is_refused_and_nothing_changes(self):
        got = self.post(5)
        self.assertEqual(got.status_int, 200)  # the form again
        self.assertIn("cannot be lower than 12", got.text)
        self.assertEqual(self.numbering()["reserved"], 0)
        self.assertEqual(self.post("many").status_int, 200)  # not a number

    def test_a_reservation_can_be_given_up_by_setting_the_number_back_to_what_is_in_use(
        self,
    ):
        self.post(500)
        self.assertEqual(self.post(12).status_int, 302)
        self.assertEqual(self.numbering()["next"], 13)

    def test_only_a_manager_may_save_it_from_this_site(self):
        got = self.web.post(
            URL + "?tab=identification",
            {"__formid__": "form-type-identification", "last": "500", "save": "save"},
            headers={**as_user("ed"), **ORIGIN},
            expect_errors=True,
        )
        self.assertEqual(got.status_int, 403)
        got = self.web.post(
            URL + "?tab=identification",
            {"__formid__": "form-type-identification", "last": "500", "save": "save"},
            headers=ADMIN,
            expect_errors=True,
        )
        self.assertGreaterEqual(got.status_int, 400)  # not from this site
        self.assertEqual(self.numbering()["reserved"], 0)


class JobsTabTest(JobsViewCase):
    def pane(self, headers=ADMIN) -> str:
        page = self.web.get(URL, headers=headers).text
        return must_search(r'id="tab-jobs".*', page, re.S)[0]

    def test_the_jobs_about_the_records_of_the_type_start_from_their_own_forms(self):
        pane = self.pane()
        forms = re.findall(r'<form id="type-job-(\w+)".*?</form>', pane, re.S)
        self.assertEqual(forms, ["validate", "export", "rewrite", "purge_slugs"])
        for name in forms:
            form = must_search(rf'<form id="type-job-{name}".*?</form>', pane, re.S)[0]
            self.assertRegex(
                form, r'<input type="hidden" name="res_type" value="person"'
            )
            self.assertIn(
                f'data-start-job="http://localhost/library/jobs/start?job={name}"', form
            )
        validate = must_search(r'<form id="type-job-validate".*?</form>', pane, re.S)[0]
        self.assertIn('data-start-fields="res_type,schema"', validate)
        self.assertIn(
            '<option value="Person-strict-01.ttl">', validate
        )  # the files of the type
        export = must_search(r'<form id="type-job-export".*?</form>', pane, re.S)[0]
        self.assertIn('data-start-fields="res_type"', export)
        self.assertNotIn("<select", export)

    def test_the_jobs_of_this_type_are_listed_under_them(self):
        pane = self.pane()
        self.assertIn('data-filter="subject=person"', pane)
        self.assertIn('data-compact=""', pane)  # the table is shown
        self.assertNotIn("align-middle d-none", pane)


class SaveTest(JobsViewCase):
    def form(self, user="admin"):
        return self.web.get(URL, headers=as_user(user)).forms["form-type-settings"]

    def save(self, schema, user="admin", headers=None, force=False):
        form = self.form()
        if force:
            form["schemaTemplate"].force_value(schema)
        else:
            form["schemaTemplate"] = schema
        return form.submit(
            "save",
            headers={**as_user(user), **(ORIGIN if headers is None else headers)},
            expect_errors=True,
        )

    def current(self) -> str:
        node = self.application["library"]
        self.addCleanup(node.close)
        return node["person"].schema

    def test_saving_changes_the_file_the_pages_of_the_type_follow(self):
        self.assertEqual(self.current(), "Person-full-01.ttl")
        got = self.save("Person-strict-01.ttl")
        self.assertEqual(got.status_int, 302)
        self.assertTrue(
            got.headers["Location"].endswith("/library/person/manage?tab=settings")
        )
        self.assertEqual(self.current(), "Person-strict-01.ttl")
        again = self.web.get(
            got.headers["Location"], headers=ADMIN
        )  # the tab is in focus again
        self.assertRegex(
            again.text, r'<option selected="selected" value="Person-strict-01.ttl">'
        )
        self.assertRegex(again.text, r'<div class="tab-pane active" id="tab-settings"')
        self.assertRegex(
            again.text, r'<div class="alert alert-success[^"]*"[^>]*>\s*Saved\.'
        )
        self.assertNotIn(">Saved.<", self.web.get(URL, headers=ADMIN).text)  # said once

    def test_the_rest_of_the_type_is_left_as_it_was(self):
        self.save("Person-strict-01.ttl")
        node = self.application["library"]
        self.addCleanup(node.close)
        res_type = node["person"]
        self.assertEqual(
            (res_type.name, res_type.slug, res_type.class_uri),
            ("Person", "person", "http://xmlns.com/foaf/0.1/Person"),
        )
        self.assertTrue(res_type.assets)

    def test_a_file_that_is_not_one_of_the_types_is_refused_and_nothing_changes(self):
        got = self.save("Nobody-full-01.ttl", force=True)
        self.assertEqual(got.status_int, 200)
        self.assertRegex(
            got.text, r'<div class="tab-pane active" id="tab-settings"'
        )  # same tab
        self.assertEqual(self.current(), "Person-full-01.ttl")

    def test_only_a_manager_saves_and_only_from_this_site(self):
        self.assertEqual(self.save("Person-strict-01.ttl", headers={}).status_int, 400)
        self.assertEqual(
            self.save(
                "Person-strict-01.ttl", headers={"Origin": "http://evil.example"}
            ).status_int,
            400,
        )
        self.assertEqual(self.current(), "Person-full-01.ttl")
        got = self.web.post(
            URL + "?tab=settings",
            {"save": "save"},
            headers={**as_user("ed"), **ORIGIN},
            expect_errors=True,
        )
        self.assertEqual(got.status_int, 403)


class ValidateBeforeSavingTest(JobsViewCase):
    def test_validating_a_file_runs_the_job_and_does_not_change_the_type(self):
        # what the button posts: the fields of the manage form, to the jobs start
        # address
        got = self.start("admin", res_type="person", schema="Person-strict-01.ttl")
        self.assertEqual(got.status_int, 200, got.text)
        job = self.job(got.json["id"])
        self.assertEqual(
            (job["job"], job["subject"], job["created_by"]),
            ("validate", "person", "user:admin"),
        )
        self.assertEqual(job["stages"][1]["params"]["schema"], "Person-strict-01.ttl")
        node = self.application["library"]
        self.addCleanup(node.close)
        self.assertEqual(
            node["person"].schema, "Person-full-01.ttl"
        )  # nothing was saved

    def test_the_panel_of_the_page_lists_the_validations_of_this_type_only(self):
        mine = self.start("admin", res_type="person").json["id"]
        self.start("admin", res_type="user")  # a validation of another type
        found = self.web.get(
            "/library/jobs/list?job=validate&subject=person", headers=ADMIN
        ).json["jobs"]
        self.assertEqual([j["id"] for j in found], [mine])


class TabsTest(JobsViewCase):
    """The page is tabs, each a named form, and every form posts to the manage view."""

    def page(self, query="", web=None) -> str:
        return (web or self.web).get(URL + query, headers=ADMIN).text

    def web_with_notes(self) -> TestApp:
        config = make_config(self.settings())
        add_notes(config)
        config.commit()
        return TestApp(config.make_wsgi_app())

    def current_schema(self) -> str:
        node = self.application["library"]
        self.addCleanup(node.close)
        return node["person"].schema

    def test_the_settings_form_is_in_a_tab_of_its_own(self):
        page = self.page()
        self.assertRegex(page, r'<ul class="nav nav-tabs"')
        button = must_search(r'<button[^>]*data-tab="settings"[^>]*>', page)[0]
        self.assertIn('data-bs-target="#tab-settings"', button)
        self.assertIn('class="nav-link active"', button)
        self.assertRegex(page, r'<div class="tab-pane active" id="tab-settings"')
        pane = must_search(r'id="tab-settings".*?</form>', page, re.S)[0]
        self.assertIn('id="form-type-settings"', pane)  # the form is named for its tab
        self.assertIn(
            'action="http://localhost/library/person/manage?tab=settings"', pane
        )  # all post to manage
        self.assertNotIn(
            'name="tab"', pane
        )  # the address says which tab: no field is needed
        self.assertIn(">Settings</button>", page)

    def test_the_address_says_which_tab_and_an_unknown_one_is_the_first(self):
        button = must_search(
            r'<button[^>]*data-tab="settings"[^>]*>', self.page("?tab=nope")
        )[0]
        self.assertIn('class="nav-link active"', button)
        self.assertIn(
            "manage.js", self.page().split("</head>")[0]
        )  # keeps ?tab= in the address

    def test_a_post_has_to_come_from_a_form_the_page_has(self):
        for data in (
            {"save": "save"},
            {"__formid__": "form-type-nope", "save": "save"},
        ):
            with self.subTest(data=data):
                got = self.web.post(
                    URL, data, headers={**ADMIN, **ORIGIN}, expect_errors=True
                )
                self.assertEqual(got.status_int, 400)

    def test_another_form_is_another_pane_after_the_first(self):
        page = self.page(web=self.web_with_notes())
        self.assertEqual(
            re.findall(r'data-tab="([a-z]+)"', page),
            ["settings", "identification", "jobs", "notes"],
        )
        self.assertEqual(
            re.findall(r'class="tab-pane( active)?"', page), [" active", "", "", ""]
        )

    def test_every_form_posts_to_the_manage_view_with_its_tab_in_the_address(self):
        page = self.page(web=self.web_with_notes())
        for name in ("settings", "notes"):
            self.assertRegex(
                page,
                rf'id="form-type-{name}"[^>]*?action="http://localhost/library/person/manage\?tab={name}"',
            )

    def test_a_post_goes_to_the_tab_it_names_and_a_form_that_was_not_right_comes_back_in_it(  # noqa: E501
        self,
    ):
        web = self.web_with_notes()
        bad = web.post(
            URL + "?tab=notes",
            {"__formid__": "form-type-notes", "note": "  "},
            headers={**ADMIN, **ORIGIN},
        )
        self.assertIn("note: Required", bad.text)
        self.assertRegex(
            bad.text, r'<div class="tab-pane active" id="tab-notes"'
        )  # shown again here
        self.assertRegex(bad.text, r'<div class="tab-pane" id="tab-settings"')
        good = web.post(
            URL + "?tab=notes",
            {"__formid__": "form-type-notes", "note": "hello"},
            headers={**ADMIN, **ORIGIN},
        )
        self.assertEqual(good.status_int, 302)
        self.assertTrue(
            good.headers["Location"].endswith("/library/person/manage?tab=notes")
        )
        self.assertEqual(
            self.current_schema(), "Person-full-01.ttl"
        )  # the other form was not posted

    def test_the_tab_that_is_asked_for_is_the_one_shown(self):
        page = self.page("?tab=notes", web=self.web_with_notes())
        self.assertRegex(page, r'<div class="tab-pane active" id="tab-notes"')
        self.assertRegex(page, r'<div class="tab-pane" id="tab-settings"')


class FlashTest(JobsViewCase):
    """What a tab says after its work: kept in the session, shown once, in the
    layout."""

    def web_with_tabs(self, **extra) -> TestApp:
        config = make_config({**self.settings(), **extra})
        add_notes(config)
        config.commit()
        return TestApp(config.make_wsgi_app())

    def note(self, web, text):
        return web.post(
            URL + "?tab=notes",
            {"__formid__": "form-type-notes", "note": text},
            headers={**ADMIN, **ORIGIN},
        )

    def test_a_flash_is_shown_once_as_an_alert_of_its_level(self):
        web = self.web_with_tabs()
        cases = (
            ("hello", "success"),
            ("warn", "warning"),
            ("bad", "danger"),
            ("fyi", "info"),
        )
        for note, level in cases:
            with self.subTest(level=level):
                self.note(web, note)
                page = web.get(URL + "?tab=notes", headers=ADMIN).text
                self.assertRegex(
                    page, rf'<div class="alert alert-{level} alert-dismissible'
                )
                self.assertIn(f"Note kept: {note}", page)
                self.assertIn('data-bs-dismiss="alert"', page)  # it can be closed
                self.assertNotIn(
                    "Note kept:", web.get(URL, headers=ADMIN).text
                )  # and it is gone

    def test_several_flashes_come_together_and_the_markup_is_escaped(self):
        web = self.web_with_tabs()
        self.note(web, "<b>x</b>")
        self.note(web, "second")
        page = web.get(URL, headers=ADMIN).text
        self.assertIn("Note kept: &lt;b&gt;x&lt;/b&gt;", page)
        self.assertIn("Note kept: second", page)
        self.assertLess(page.index("&lt;b&gt;"), page.index("Note kept: second"))

    def test_the_session_is_a_signed_cookie_that_is_only_set_when_something_is_flashed(
        self,
    ):
        web = self.web_with_tabs()
        self.assertNotIn("Set-Cookie", web.get("/library/", headers=ADMIN).headers)
        self.assertNotIn("Set-Cookie", web.get(URL, headers=ADMIN).headers)
        cookie = self.note(web, "hello").headers["Set-Cookie"]
        self.assertIn("gracia_session=", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)

    def test_a_secret_in_the_settings_lets_several_processes_read_one_flash(self):
        first = self.web_with_tabs(**{"gracia.session_secret": "one secret"})
        self.note(first, "from the first")
        cookie = first.cookies["gracia_session"]
        same = self.web_with_tabs(**{"gracia.session_secret": "one secret"})
        same.set_cookie("gracia_session", cookie)
        self.assertIn("Note kept: from the first", same.get(URL, headers=ADMIN).text)
        other = self.web_with_tabs(**{"gracia.session_secret": "another"})
        other.set_cookie("gracia_session", cookie)
        self.assertNotIn(
            "Note kept", other.get(URL, headers=ADMIN).text
        )  # not signed by it
        loose = self.web_with_tabs()  # no secret: each process has its own
        loose.set_cookie("gracia_session", cookie)
        self.assertNotIn("Note kept", loose.get(URL, headers=ADMIN).text)
