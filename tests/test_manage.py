import re
import shutil
import threading
import time
from importlib.resources import files

import pyoxigraph as ox
from harness import FOAF_PERSON
from pyramid.registry import Registry
from support import must_search
from test_web import HeaderPolicy, WebCase

from gracia import forms, uri
from gracia.utils import assets

ADMIN = {"X-User": "admin"}
POSTING = {
    **ADMIN,
    "Origin": "http://localhost:80",
}  # a post has to come from this site


class RepositoryDataTest(WebCase):
    def test_a_new_repository_gets_its_resource_types_and_groups(self):
        self.application.create_repository("test_repo")
        with self.application.new_store("test_repo") as store:
            # the repository is one record of the type `repository` (rec:type:1)
            self.assertEqual(store.records_of_type("repository"), ["rec:repository:1"])
            self.assertEqual(len(store.records_of_type("type")), 4)  # types
            self.assertEqual(len(store.records_of_type("group")), 3)  # groups

    def test_creating_again_does_not_reload(self):
        self.application.create_repository("test_repo")
        with self.application.new_store("test_repo") as store:
            before = store.counts()
        self.application.create_repository("test_repo")
        with self.application.new_store("test_repo") as store:
            self.assertEqual(store.counts(), before)


class FormFromShapeTest(WebCase):
    def test_title_field_from_the_shape_file(self):
        self.application.create_repository("test_repo")
        with self.application.new_store("test_repo") as store:
            data = store.get("rec:repository:1")
        content = data.value(
            ox.NamedNode("rec:repository:1"), ox.NamedNode(uri.RESOURCE)
        )
        prefix = data.objects(content, ox.NamedNode(uri.ASSETS))[0].value
        name = data.objects(content, ox.NamedNode(uri.SCHEMA_TEMPLATE))[0].value
        self.assertEqual(
            (prefix, name), ("gracia.types:repository/", "Repository-full-01.ttl")
        )
        found = assets.shape(
            prefix, name, uri.REPOSITORY_CLASS, "Repository"
        )  # a file, not data
        schema = forms.schema_from_shape(*found)
        self.assertEqual([n.name for n in schema.children], ["title", "timezone"])
        self.assertEqual(schema.children[0].title, "Title")
        self.assertEqual(schema.children[0].predicate, uri.TITLE)


class ManageCase(WebCase):
    def setUp(self):
        super().setUp()
        self.settings["gracia.security_policy"] = HeaderPolicy()  # ty: ignore[invalid-assignment]
        self.application.create_repository("test_repo")

    def tabs(self, path="/test_repo/manage"):
        page = self.client().get(path, headers=ADMIN).text
        return re.findall(r'data-tab="([a-z]+)"[^>]*>([^<]+)', page), page

    def person(self):

        return FOAF_PERSON


class ManageViewTest(ManageCase):
    def title(self):
        with self.application.new_store("test_repo") as store:
            data = store.get("rec:repository:1")
            content = data.value(
                ox.NamedNode("rec:repository:1"), ox.NamedNode(uri.RESOURCE)
            )
            return [o.value for o in data.objects(content, ox.NamedNode(uri.TITLE))]

    def test_the_tabs_are_types_first_and_then_the_settings(self):
        tabs, page = self.tabs()
        self.assertEqual(
            tabs,
            [
                ("types", "Types"),
                ("settings", "Settings"),
                ("extensions", "Extensions"),
                ("search", "Search"),
                ("predicates", "Predicates"),
                ("stats", "Stats"),
                ("jobs", "Jobs"),
            ],
        )
        self.assertIn('class="tab-pane active" id="tab-types"', page)
        self.assertIn('class="tab-pane" id="tab-settings"', page)
        _, page = self.tabs("/test_repo/manage?tab=settings")
        self.assertIn('class="tab-pane active" id="tab-settings"', page)

    def test_the_types_tab_lists_the_types_and_each_is_a_link_to_its_manage_page(self):
        self.application.create_repository("archive", content_types=[self.person()])
        _, page = self.tabs("/archive/manage")
        cards = type_cards(page)
        self.assertEqual(
            list(cards), ["repository", "type", "user", "group", "person"]
        )  # a card for each type
        self.assertEqual(
            re.findall(
                r'<a href="([^"]+)" class="btn[^>]*>Manage</a>', "".join(cards.values())
            ),
            [f"http://localhost/archive/{slug}/manage" for slug in cards],
        )
        links = [(f"http://localhost/archive/{slug}/manage", slug) for slug in cards]
        self.assertEqual(
            "".join(cards.values()).count("system</span>"), 4
        )  # the four the repository cannot work without
        self.assertNotIn(
            "Person-minimal-01.ttl", "".join(cards.values())
        )  # not the schema
        self.assertIn(
            "A person, by name.", cards["person"]
        )  # what its shape says of it
        self.assertIn("A group of users", cards["group"])
        for url, _ in links:  # and the links lead to a page
            self.assertEqual(
                self.client().get(url, headers=ADMIN, expect_errors=True).status_int
                in (200, 404),
                True,
            )

    def test_the_settings_tab_has_the_title(self):
        _, page = self.tabs()
        pane = must_search(r'id="tab-settings".*?</form>', page, re.S)[0]
        self.assertIn('name="title"', pane)
        self.assertNotIn("ark_", pane)  # the ARK settings are the ARK extension's
        self.assertIn('id="form-repository-settings"', pane)
        self.assertIn('action="http://localhost/test_repo/manage?tab=settings"', pane)

    def test_a_post_from_another_site_is_refused(self):
        form = self.client().get("/test_repo/manage", headers=ADMIN).forms[0]
        form["title"] = "Hijacked"
        response = form.submit(
            "save",
            headers={**ADMIN, "Origin": "http://evil.example"},
            expect_errors=True,
        )
        self.assertEqual(response.status_int, 400)
        self.assertEqual(self.title(), ["test_repo"])

    def test_a_form_that_is_not_right_comes_back_in_the_settings_tab(self):
        form = self.client().get("/test_repo/manage", headers=ADMIN).forms[0]
        form["title"] = ""
        response = form.submit("save", headers=POSTING)
        self.assertEqual(response.status_int, 200)
        self.assertIn('class="tab-pane active" id="tab-settings"', response.text)

    def test_needs_the_manage_permission(self):
        web = self.client()
        self.assertEqual(
            web.get("/test_repo/manage", expect_errors=True).status_int, 403
        )
        self.assertEqual(web.get("/test_repo/manage", headers=ADMIN).status_int, 200)

    def test_renders_the_form_with_the_current_title(self):
        response = self.client().get("/test_repo/manage", headers=ADMIN)
        self.assertIn('name="title"', response.text)
        self.assertIn('value="test_repo"', response.text)

    def test_saving_replaces_the_title(self):
        web = self.client()
        form = web.get("/test_repo/manage", headers=ADMIN).forms[0]
        form["title"] = "The Library"
        response = form.submit("save", headers=POSTING)
        self.assertEqual(response.status_int, 302)
        self.assertEqual(self.title(), ["The Library"])
        followed = response.follow(headers=ADMIN)
        self.assertEqual(
            response.location, "http://localhost/test_repo/manage?tab=settings"
        )
        self.assertIn("Saved", followed.text)
        self.assertIn('class="tab-pane active" id="tab-settings"', followed.text)

    def test_an_empty_title_is_refused(self):
        form = self.client().get("/test_repo/manage", headers=ADMIN).forms[0]
        form["title"] = ""
        response = form.submit("save", headers=POSTING)
        self.assertEqual(response.status_int, 200)
        self.assertEqual(self.title(), ["test_repo"])


class CountsTest(ManageCase):
    def test_the_types_tab_counts_the_records_of_each_type(self):
        self.application.create_repository("archive", content_types=[self.person()])
        with self.application.new_store("archive") as store:
            for n in (1, 2, 3):
                text = (
                    f"<rec:person:{n}> a <{uri.RECORD_CLASS}> ; "
                    f" <{uri.RESOURCE}> [ a <http://xmlns.com/foaf/0.1/Person> ] ."
                )
                store.put(
                    ox.Dataset(ox.parse(text.encode(), format=ox.RdfFormat.TURTLE))
                )
        cards = type_cards(self.tabs("/archive/manage")[1])
        self.assertIn("3 records", cards["person"])
        self.assertIn(
            "1 record<", cards["repository"]
        )  # the one record of the repository

    def test_the_predicates_tab_counts_the_values_of_each_property(self):
        cards = predicate_cards(self.tabs("/test_repo/manage?tab=predicates")[1])
        label = int(must_search(r"(\d+) stored", cards[uri.LABEL])[1])
        self.assertGreater(
            label, 0
        )  # the records of a new repository have their labels


def type_cards(page: str) -> dict[str, str]:
    pane = page[page.index('id="tab-types"') : page.index('id="tab-settings"')]
    return {
        must_search(r'id="type-([^"]+)"', c)[1]: c
        for c in re.split(r'(?=<div class="type )', pane)
        if c.startswith('<div class="type ')
    }


def predicate_cards(page: str) -> dict[str, str]:
    pane = page[page.index('id="tab-predicates"') :]
    chunks = pane.split('<div class="predicate ')[1:]
    return {
        must_search(r'<code class="text-break">([^<]+)</code>', c)[1]: c for c in chunks
    }


class PredicatesTabTest(ManageCase):
    def cards(self):
        return predicate_cards(self.tabs("/test_repo/manage?tab=predicates")[1])

    def test_every_registered_predicate_has_a_card_by_the_uri_of_its_property(self):
        cards = self.cards()
        for property_uri in (
            uri.RESOURCE,
            uri.RESOURCE_URI,
            uri.LABEL,
            uri.SCHEMA_TEMPLATE,
        ):
            self.assertIn(property_uri, cards)
        self.assertEqual(list(cards), sorted(cards))  # by uri

    def test_the_title_is_the_name_the_shapes_give_the_property(self):
        cards = self.cards()
        for property_uri, title in (
            (uri.LABEL, "Label"),
            (uri.RESOURCE_URI, "Resource URI"),
            (uri.SCHEMA_TEMPLATE, "Schema template"),
            (uri.RESOURCE, "Content"),
        ):
            self.assertRegex(cards[property_uri], rf"<h2[^>]*>\s*{title}\s")
        self.assertIn(
            "A short text for the record", cards[uri.LABEL]
        )  # its sh:description

    def test_the_class_the_docstring_and_the_parts_are_shown(self):
        label = self.cards()[uri.LABEL]
        self.assertIn("gracia.predicates.gracia_label.LabelPredicate", label)
        self.assertIn(
            "a short text for a record, made from its label values on save", label
        )  # docstring
        self.assertIn("schema_node", label)
        self.assertIn("on_write", label)
        content = self.cards()[uri.RESOURCE]
        self.assertNotIn("on_write", content)  # the content: only a node
        self.assertNotIn(
            "base class", content
        )  # its own docstring, not the base class's
        self.assertNotIn("`", label)  # no markup


class JobsTabTest(ManageCase):
    def pane(self) -> str:
        _, page = self.tabs("/test_repo/manage?tab=jobs")
        return must_search(r'id="tab-jobs".*', page, re.S)[0]

    def test_the_jobs_of_the_repository_start_from_their_own_forms_and_their_runs_are_listed_under_them(  # noqa: E501
        self,
    ):
        pane = self.pane()
        self.assertEqual(
            re.findall(r'<form id="repository-job-(\w+)"', pane),
            ["rebuild_etags", "update_cache", "rebuild_search", "process_actions"],
        )
        cache = must_search(
            r'<form id="repository-job-update_cache".*?</form>', pane, re.S
        )[0]
        self.assertIn("Update cache", cache)
        self.assertIn(
            'data-start-job="http://localhost/test_repo/jobs/start?job=update_cache"',
            cache,
        )
        form = must_search(
            r'<form id="repository-job-rebuild_etags".*?</form>', pane, re.S
        )[0]
        self.assertIn("Rebuild etags", form)
        self.assertIn(
            'data-start-job="http://localhost/test_repo/jobs/start?job=rebuild_etags"',
            form,
        )
        self.assertNotIn("res_type", form)  # it is about the repository, not a type
        self.assertIn(
            'data-filter="job=rebuild_etags,update_cache,rebuild_search,process_actions"',
            pane,
        )  # their runs are listed, no others

    def test_the_tab_is_after_the_others(self):
        tabs, _ = self.tabs()
        self.assertEqual(tabs[-1], ("jobs", "Jobs"))

    def test_the_job_is_for_the_administrators_only_and_has_one_stage(self):
        from gracia.jobs.job import all_jobs
        from gracia.main import make_config

        config = make_config(self.settings)
        config.commit()
        job = next(j for j in all_jobs(config.registry) if j.name == "rebuild_etags")
        self.assertEqual(job.groups, ())  # no group of users: the administrators
        self.assertEqual(
            job.spec("test_repo", {}),
            {
                "repo": "test_repo",
                "job": "rebuild_etags",
                "stages": [{"stage": "RebuildEtags", "params": {}}],
            },
        )


class StatsTabTest(ManageCase):
    def setUp(self):
        super().setUp()
        import tempfile

        self.cache_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.cache_dir, True)
        self.application.cache._graph_dir = (
            self.cache_dir
        )  # a graph cache for the application of the test
        self.store = self.application.new_store("test_repo")
        self.addCleanup(self.store.close)

    def pane(self) -> str:
        _, page = self.tabs("/test_repo/manage?tab=stats")
        return must_search(r'id="tab-stats".*?(?=<div class="tab-pane|\Z)', page, re.S)[
            0
        ]

    @staticmethod
    def text(pane: str, name: str) -> str:
        found = re.search(rf'id="{name}"[^>]*>(.*?)</(?:dd|tr|fieldset)>', pane, re.S)
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", found[1])).strip()  # ty: ignore[not-subscriptable]

    def add_people(self, n: int = 3) -> None:
        text = (
            "@prefix gracia: <https://gracia.pub/ns/> . @prefix foaf: "
            "<http://xmlns.com/foaf/0.1/> "
            ".\n"
            + "".join(
                f"<rec:person:{k}> a gracia:Record ; gracia:resource_uri "
                f"<ark:/1/person/{k}> ; "
                f'gracia:label "P{k}" ; '
                "gracia:is_published "
                f'"{"true" if k % 2 else "false"}"'
                "^^<http://www.w3.org/2001/XMLSchema#boolean> ; "
                f'gracia:resource [ a foaf:Person ; foaf:name "Person {k}" ] .\n'
                for k in range(1, n + 1)
            )
        )
        self.store.put(ox.Dataset(ox.parse(text.encode(), format=ox.RdfFormat.TURTLE)))

    def test_the_fieldsets_are_types_then_cache_then_pending_actions(self):
        pane = self.pane()
        positions = [
            pane.index(f'id="{name}"')
            for name in ("stats-types", "stats-cache", "stats-search", "stats-actions")
        ]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(
            re.findall(r"<legend[^>]*>([^<]+)", pane),
            ["Types", "Cache", "Search", "Pending actions"],
        )

    def test_the_types_have_their_records_the_published_ones_and_the_size_of_their_sources(  # noqa: E501
        self,
    ):
        self.add_people(4)
        pane = self.pane()
        row = self.text(pane, "stats-type-person")
        self.assertRegex(
            row, r"^person person 4 2 \d"
        )  # four records, two of them published, then the size
        self.assertRegex(
            row, r"\d+(\.\d)? (B|KB) \d+(\.\d)? (B|KB) \d{4}-\d\d-\d\d \d\d:\d\d UTC"
        )
        total = self.text(pane, "stats-types-total")
        records = sum(self.store.count_records_by_type().values())
        self.assertTrue(total.startswith(f"All {records:,}"))
        self.assertIn(
            "stats-type-repository", pane
        )  # the types a repository is made of are there too

    def test_a_type_with_no_records_is_there_with_none(self):
        self.application.create_repository("archive", content_types=[self.person()])
        _, page = self.tabs("/archive/manage?tab=stats")
        self.assertRegex(
            self.text(page, "stats-type-person"), r"^Person person 0 0 0 B$"
        )

    def test_the_pages_the_etags_in_memory_and_the_graphs_of_the_cache(self):
        self.add_people(3)
        pages = self.store.etag_stats()["pages"]
        self.assertEqual(self.text(self.pane(), "stats-pages").split()[0], f"{pages:,}")
        self.assertEqual(self.text(self.pane(), "stats-etags-held").split()[0], "0")
        record_id = self.store.record_id_of("rec:person:1")
        self.application.cache.etags("test_repo", [record_id])  # asked for, so held
        self.assertEqual(self.text(self.pane(), "stats-etags-held").split()[0], "1")
        self.assertTrue(
            self.text(self.pane(), "stats-graphs").startswith("0 in this repository")
        )
        self.application.cache.graph("test_repo", record_id)  # ty: ignore[invalid-argument-type]
        self.assertTrue(
            self.text(self.pane(), "stats-graphs").startswith(
                "1 in this repository, 1 in all"
            )
        )
        self.assertIn(self.cache_dir, self.pane())  # where it is kept

    def test_without_a_graph_directory_it_says_there_is_no_graph_cache(self):
        self.application.cache._graph_dir = None
        self.assertIn("No graph cache", self.text(self.pane(), "stats-graphs"))

    def test_the_pending_work_is_what_the_queue_holds(self):
        self.add_people(3)
        summary = self.store.pending_summary()
        text = self.text(self.pane(), "stats-pending")
        self.assertTrue(text.startswith(f"{summary['records']:,} records"), text)
        self.assertIn(f"{summary['rows']:,} rows", text)
        self.assertIn("insert", text)
        self.assertIn(
            f"transaction {summary['oldest']} of {summary['last']}", self.pane()
        )

    def test_nothing_waiting_is_said_so_and_has_no_oldest(self):
        from gracia.actions import process_pending

        process_pending(self.application, Registry())
        pane = self.pane()
        self.assertTrue(self.text(pane, "stats-pending").startswith("0 records"))
        self.assertNotIn("Oldest", pane)

    def test_the_worker_is_off_when_the_setting_is_off_and_running_when_it_is_on(self):
        self.assertIn('data-state="off"', self.pane())
        self.assertIn("gracia.worker", self.text(self.pane(), "stats-worker"))
        self.settings["gracia.worker"] = "true"  # ty: ignore[invalid-assignment]
        self.settings["gracia.db"] = ":memory:"  # ty: ignore[invalid-assignment]
        _, page = self.tabs(
            "/test_repo/manage?tab=stats"
        )  # a new app: its worker is started
        worker = self.application.cache.worker
        self.addCleanup(worker.stop)  # ty: ignore[missing-union-attribute]
        self.assertIn('data-state="running"', page)

    def test_a_worker_that_is_not_running_or_keeps_failing_says_so(self):
        from gracia.actions import Worker

        worker = Worker(self.application, Registry())
        self.application.cache.worker = worker
        self.assertIn('data-state="stopped"', self.pane())  # never started
        worker._retry["test_repo"] = (time.monotonic() + 30, 3)
        worker._thread = threading.Thread(target=time.sleep, args=(5,), daemon=True)
        worker._thread.start()
        pane = self.pane()
        self.assertIn('data-state="failing"', pane)
        self.assertIn("3 time(s) in a row", self.text(pane, "stats-worker"))
        self.assertRegex(self.text(pane, "stats-worker"), r"next try in (29|30) s")

    def test_the_buttons_start_the_jobs_and_are_for_who_may_start_them(self):
        pane = self.pane()
        for job, form in (
            ("update_cache", "stats-job-update_cache"),
            ("process_actions", "stats-job-process_actions"),
        ):
            found = must_search(rf'<form id="{form}".*?</form>', pane, re.S)[0]
            self.assertIn(
                f'data-start-job="http://localhost/test_repo/jobs/start?job={job}"',
                found,
            )
        page = self.client().get(
            "/test_repo/manage?tab=stats",
            headers={"X-User": "nobody"},
            expect_errors=True,
        )
        self.assertNotIn(
            "stats-job-", page.text
        )  # not for those who may not manage it, nor start a job

    def test_the_tab_has_a_dialog_of_its_own_for_the_jobs_it_starts_and_a_refresh_link(
        self,
    ):
        pane = self.pane()
        dialog = must_search(
            r'<div class="job-panel"[^>]*data-compact="true"[^>]*>', pane
        )[0]
        self.assertIn(
            'data-filter="job=update_cache,rebuild_search,process_actions"', dialog
        )
        self.assertIn("http://localhost/test_repo/jobs/list", dialog)
        self.assertIn('data-progress-dialog=""', pane)
        self.assertRegex(
            pane,
            r"<a "
            r'href="http://localhost/test_repo/manage\?tab=stats"[^>]*id="stats-refresh"',
        )

    def test_a_panel_that_is_not_on_show_does_not_open_a_dialog(self):
        script = (
            files("gracia") / "assets" / "static" / "scripts" / "jobs.js"
        ).read_text()
        self.assertIn("get visible()", script)
        self.assertIn("this.visible && this.find(id)", script)

    def test_it_is_a_tab_of_the_repository_and_not_of_a_type(self):
        self.application.create_repository("archive", content_types=[self.person()])
        _, page = self.tabs("/archive/person/manage")
        self.assertNotIn('data-tab="stats"', page)


def test_sizes_and_times_are_text():
    import datetime

    from gracia.views.manage import _size, _when

    assert [
        _size(n) for n in (0, 999, 1000, 12345, 123456, 5_000_000, 3_200_000_000)
    ] == [
        "0 B",
        "999 B",
        "1.0 KB",
        "12.3 KB",
        "123 KB",
        "5.0 MB",
        "3.2 GB",
    ]
    assert _when(None) == ""
    assert _when(datetime.datetime(2026, 11, 16, 14, 5, 59)) == "2026-11-16 14:05 UTC"


class LogoTest(ManageCase):
    @staticmethod
    def logo(page: str) -> str:
        return must_search(r'<a class="gracia-logo[^"]*" href="([^"]*)"', page)[1]

    def test_the_pages_of_a_repository_and_of_what_is_in_it_go_to_the_repository(self):
        self.application.create_repository("archive", content_types=[self.person()])
        with self.application.new_store("archive") as store:
            store.put(
                ox.Dataset(
                    ox.parse(
                        b'<rec:person:1> <https://gracia.pub/ns/label> "Ada" .',
                        format=ox.RdfFormat.N_TRIPLES,
                    )
                )
            )
        for path in (
            "/archive/manage",
            "/archive/person/manage",
            "/archive/jobs",
            "/archive/person/add",
            "/archive/person/1/edit",
            "/archive/person/",
        ):
            page = self.client().get(
                path, headers={**ADMIN, "Accept": "text/html"}, expect_errors=True
            )
            if page.status_int == 200 and "gracia-logo" in page.text:
                self.assertEqual(
                    self.logo(page.text), "http://localhost/archive/", path
                )

    def test_the_search_page_and_the_manage_page_of_each_repository_go_to_their_own(
        self,
    ):
        self.application.create_repository("archive")
        for repo in ("test_repo", "archive"):
            page = self.client().get(f"/{repo}/manage", headers=ADMIN).text
            self.assertEqual(self.logo(page), f"http://localhost/{repo}/")
            home = (
                self.client()
                .get(f"/{repo}/", headers={**ADMIN, "Accept": "text/html"})
                .text
            )
            self.assertEqual(self.logo(home), f"http://localhost/{repo}/")

    def test_a_page_that_is_in_no_repository_goes_to_the_application(self):
        from pyramid.request import Request

        from gracia.views import home_url

        request = Request.blank("/")
        self.assertEqual(home_url(request), "http://localhost/")
