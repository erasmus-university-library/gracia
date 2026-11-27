import re
import tempfile

from harness import FOAF_PERSON
from test_web import HeaderPolicy, WebCase

# the attributes that carry an address: the ones of HTML, htmx, and what our scripts
# read
URL_ATTRIBUTES = re.compile(
    r"\b(href|src|action|hx-get|hx-post|data-base|data-list-url|data-start-url|data-start-job|"
    r'data-log-url|data-status-url|data-url)="([^"]*)"'
)
ABSOLUTE = re.compile(r"^(https?://|#|mailto:|$)")
ADMIN = {"X-User": "admin", "Accept": "text/html"}
PAGES = (
    "/test_repo/api",
    "/test_repo/manage",
    "/test_repo/manage?tab=settings",
    "/test_repo/manage?tab=extensions",
    "/test_repo/jobs",
    "/test_repo/person/manage",
    "/test_repo/person/add",
    "/test_repo/user/1/edit",
    "/test_repo/type/manage",
)


class AbsoluteUrlsTest(WebCase):
    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.settings["gracia.security_policy"] = HeaderPolicy()  # ty: ignore[invalid-assignment]
        self.settings["gracia.jobs_dir"] = directory.name  # ty: ignore[invalid-assignment]
        self.application.create_repository(
            "test_repo",
            admin=("admin", "correct horse battery"),
            content_types=[FOAF_PERSON],
        )
        self.web = self.client()

    def test_the_addresses_in_the_html_the_static_files_and_the_json_are_absolute(self):
        # the html of every page has absolute addresses only
        for path in PAGES:
            page = self.web.get(path, headers=ADMIN)
            self.assertIn("html", page.content_type, path)
            found = URL_ATTRIBUTES.findall(page.text)
            self.assertTrue(found, path)
            for attribute, address in found:
                self.assertRegex(
                    address, ABSOLUTE, f"{path}: {attribute}={address!r} is relative"
                )

        # the static files are absolute too
        page = self.web.get("/test_repo/manage", headers=ADMIN).text
        self.assertRegex(page, r'<link rel="icon"[^>]*href="http://localhost/_static/')
        self.assertRegex(
            page, r'<script type="text/javascript" src="http://localhost/_static/'
        )

        # the addresses in the json are absolute
        document = self.web.get(
            "/test_repo/api", headers={"X-User": "admin", "Accept": "application/json"}
        ).json
        self.assertEqual(document["servers"], [{"url": "http://localhost/test_repo"}])
