import re
import tempfile

from test_web import HeaderPolicy, WebCase

from gracia import types

# the attributes that carry an address: the ones of HTML, htmx, and what our scripts
# read
URL_ATTRIBUTES = re.compile(
    r"\b(href|src|action|hx-get|hx-post|data-base|data-list-url|data-start-url|data-start-job|"
    r'data-log-url|data-status-url|data-url)="([^"]*)"'
)
ABSOLUTE = re.compile(r"^(https?://|#|mailto:|$)")
ADMIN = {"X-User": "admin", "Accept": "text/html"}

PAGES = (
    "/library/api",
    "/library/manage",
    "/library/manage?tab=settings",
    "/library/manage?tab=extensions",
    "/library/jobs",
    "/library/person/manage",
    "/library/person/add",
    "/library/user/1/edit",
    "/library/type/manage",
)


class AbsoluteUrlsTest(WebCase):
    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.settings["gracia.security_policy"] = HeaderPolicy()  # ty: ignore[invalid-assignment]
        self.settings["gracia.jobs_dir"] = directory.name  # ty: ignore[invalid-assignment]
        self.application.create_repository(
            "library",
            admin=("admin", "correct horse battery"),
            content_types=[types.available()["person"]],
        )
        self.web = self.client()

    def test_the_html_of_every_page_has_absolute_addresses_only(self):
        for path in PAGES:
            page = self.web.get(path, headers=ADMIN)
            self.assertIn("html", page.content_type, path)
            found = URL_ATTRIBUTES.findall(page.text)
            self.assertTrue(found, path)
            for attribute, address in found:
                self.assertRegex(
                    address, ABSOLUTE, f"{path}: {attribute}={address!r} is relative"
                )

    def test_the_static_files_are_absolute_too(self):
        page = self.web.get("/library/manage", headers=ADMIN).text
        self.assertRegex(page, r'<link rel="icon"[^>]*href="http://localhost/_static/')
        self.assertRegex(
            page, r'<script type="text/javascript" src="http://localhost/_static/'
        )

    def test_the_addresses_in_the_json_are_absolute(self):
        document = self.web.get("/library/api", headers={"X-User": "admin"}).json
        self.assertEqual(document["servers"], [{"url": "http://localhost/library"}])
