import re
import tempfile
from pathlib import Path
from unittest import mock

import colander
import deform
import deform.widget
import fanstatic
from fanstatic import Library, Resource, checksum
from fanstatic.inclusion import sort_resources
from jobs_support import JobsCase
from support import must_search
from test_jobs_view import JobsViewCase, as_user

from gracia import static

# a package that brings a widget declares its library when it is imported, before any
# request
_EXT = tempfile.TemporaryDirectory()
EXT = Path(_EXT.name)
(EXT / "widget.js").write_text("window.widget = 1;")
(EXT / "secret.txt").write_text("not a resource")
EXT_LIBRARY = Library("gracia-test-ext", str(EXT))
Resource(EXT_LIBRARY, "widget.js")
fanstatic.get_library_registry().add(EXT_LIBRARY)


def head(page: str) -> str:
    return page[page.index("<head>") : page.index("</head>")]


class HeadTest(JobsViewCase):
    def page(self) -> str:
        return self.web.get("/test_repo/jobs", headers=as_user("ed")).text

    def test_the_site_has_one_stylesheet_and_its_scripts_in_the_head(self):
        found = head(self.page())
        self.assertEqual(len(re.findall(r"<link rel=\"stylesheet\"", found)), 1)
        self.assertIn(
            "bootstrap.min.css;gracia.css", found
        )  # Bootstrap and ours: one file
        for name in (
            "bootstrap.bundle.min.js",
            "htmx.min.js",
            "jobs.js",
        ):  # one file of ours
            self.assertRegex(found, rf"scripts/:bundle:[^\"]*{re.escape(name)}")
        self.assertNotIn(
            "<script", self.page().split("</head>")[1]
        )  # none at the end of the body

    def test_every_address_has_the_checksum_of_the_library_in_it(self):
        version = checksum.md5(static.library.path)
        for url in re.findall(
            r'(?:href|src)="([^"]*/_static/gracia/[^"]*)"', self.page()
        ):
            self.assertIn(f"/:version:{version}/", url)

    def test_a_file_is_served_for_a_year_so_a_browser_keeps_it(self):
        css = must_search(r'href="([^"]*bootstrap\.min\.css[^"]*)"', self.page())[1]
        response = self.web.get(css)
        self.assertEqual(response.status_int, 200)
        self.assertIn("max-age=31536000", response.headers["Cache-Control"])
        self.assertIn("text/css", response.content_type)
        # the bundle is the files one after the other
        self.assertIn(".gracia-logo", response.text)
        self.assertIn("Bootstrap", response.text[:300])

    def test_the_script_bundle_has_what_the_views_need_in_one_request(self):
        script = must_search(r'src="([^"]*jobs\.js[^"]*)"', self.page())[1]
        response = self.web.get(script)
        self.assertEqual(response.status_int, 200)
        for part in ("bootstrap.Modal", "DOMContentLoaded", "data-progress-dialog"):
            self.assertIn(part, response.text)

    def test_what_is_not_versioned_is_not_cached_for_good(self):
        response = self.web.get("/_static/gracia/css/gracia.css")
        self.assertEqual(response.status_int, 200)
        self.assertNotIn("max-age=31536000", response.headers.get("Cache-Control", ""))

    def test_a_file_that_is_not_there_is_not_found(self):
        for path in ("/_static/gracia/nope.js", "/_static/nolibrary/x.js", "/_static/"):
            self.assertEqual(
                self.web.get(path, expect_errors=True).status_int, 404, path
            )

    def test_data_is_left_alone(self):
        response = self.web.get("/test_repo/jobs/list", headers=as_user("ed"))
        self.assertNotIn("_static", response.text)

    def test_the_icon_is_versioned_too(self):
        icon = must_search(r'rel="icon"[^>]*href="([^"]+)"', self.page())[1]
        self.assertIn("/:version:", icon)
        self.assertEqual(self.web.get(icon).status_int, 200)


class WidgetsTest(JobsCase):
    """What a widget of a form needs is added to the page: once, with what it depends
    on."""

    def needed(self, form) -> list[str]:
        needed = fanstatic.init_needed()
        try:
            static.need_form(form)
            return [
                r.relpath for r in sort_resources(needed.resources())
            ]  # as it is written
        finally:
            fanstatic.del_needed()

    def form(self, widget=None) -> deform.Form:
        class Schema(colander.MappingSchema):
            day = colander.SchemaNode(
                colander.Date(), widget=widget or deform.widget.DateInputWidget()
            )

        return deform.Form(Schema())

    def test_a_widget_brings_its_scripts_after_jquery(self):
        found = [
            n for n in self.needed(self.form()) if n.endswith(".js")
        ]  # the styles come first
        self.assertEqual(found[0], "scripts/jquery-2.0.3.min.js")
        for name in (
            "scripts/deform.js",
            "pickadate/picker.date.js",
            "scripts/deform-load.js",
        ):
            self.assertIn(name, found)
        self.assertLess(
            found.index("scripts/jquery.form-3.09.js"), found.index("scripts/deform.js")
        )
        self.assertEqual(len(found), len(set(found)))  # each once

    def test_a_form_of_selects_needs_only_what_every_form_needs(self):
        found = self.needed(self.form(deform.widget.SelectWidget(values=[("a", "a")])))
        self.assertNotIn("pickadate/picker.js", found)

    def widgets_of(self, *paths):
        class Form:
            def get_widget_resources(self):
                return {"js": [str(p) for p in paths], "css": []}

        return Form()

    def test_a_file_of_a_declared_library_is_needed_and_served(self):
        found = self.needed(self.widgets_of(EXT / "widget.js"))
        self.assertIn("widget.js", found)
        self.assertLess(
            found.index("scripts/jquery-2.0.3.min.js"), found.index("widget.js")
        )
        from webtest import TestApp

        from gracia.main import make_config

        web = TestApp(
            make_config({"gracia.application": self.application}).make_wsgi_app()
        )
        got = web.get("/_static/gracia-test-ext/widget.js")
        self.assertEqual(got.text, "window.widget = 1;")
        # a library publishes its own directory (that is why it is declared on purpose,
        # and why
        # a directory that holds more than assets is never made into one on the fly)
        self.assertEqual(
            web.get(
                "/_static/gracia-test-ext/secret.txt", expect_errors=True
            ).status_int,
            200,
        )

    def test_a_file_in_no_library_is_an_error_that_says_what_to_do(self):
        (self.tmp / "loose.js").write_text("1")
        with self.assertRaises(LookupError) as caught:
            self.needed(self.widgets_of(self.tmp / "loose.js"))
        self.assertIn("declare a fanstatic Library", str(caught.exception))
        self.assertFalse(
            [n for n in fanstatic.get_library_registry() if n.startswith("dir-")]
        )

    def test_a_file_in_a_library_but_not_one_of_its_resources_is_refused(self):
        with self.assertRaises(LookupError) as caught:
            self.needed(self.widgets_of(EXT / "secret.txt"))
        self.assertIn("not one of its resources", str(caught.exception))

    def test_the_directive_takes_a_library_before_the_first_request_only(self):
        from pyramid.exceptions import ConfigurationExecutionError

        from gracia.main import make_config

        config = make_config({"gracia.application": self.application})
        config.add_gracia_library(EXT_LIBRARY)  # already there: the same library again
        config.commit()
        late = Library("gracia-too-late", str(EXT))
        config.add_gracia_library(late)
        fanstatic.get_library_registry().prepare()
        with self.assertRaises(ConfigurationExecutionError) as caught:
            config.commit()
        self.assertIn("after the first request", str(caught.exception))

    def test_a_page_with_a_form_that_has_widgets_gets_their_scripts(self):
        from test_jobs_view import People

        from gracia.views import render_form

        seen = {}

        def view(request):
            seen["response"] = render_form(
                request, "T", "t", "<p>x</p>", widgets=self.form()
            )
            return seen["response"]

        from pyramid.config import Configurator
        from webtest import TestApp

        config = Configurator(settings={"gracia.application": self.application})
        config.include("gracia.main")
        config.set_security_policy(People())
        config.add_route("t", "/t")
        config.add_view(view, route_name="t", permission=None)
        page = TestApp(config.make_wsgi_app()).get("/t").text
        self.assertIn("pickadate", head(page))
        self.assertIn("jquery-2.0.3.min.js", head(page))


class SettingsTest(JobsViewCase):
    def test_hashes_can_be_recomputed_for_development(self):
        config = None
        from gracia.main import make_config

        with mock.patch.object(
            static.library, "signature", wraps=static.library.signature
        ) as sig:
            from webtest import TestApp

            config = make_config(
                {**self.settings(), "gracia.fanstatic.recompute_hashes": "true"}
            )
            web = TestApp(config.make_wsgi_app())
            web.get("/test_repo/jobs", headers=as_user("ed"))
            self.assertTrue(sig.called)
            self.assertTrue(sig.call_args.kwargs["recompute_hashes"])
