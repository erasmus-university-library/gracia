import unittest
from urllib.parse import unquote, urlsplit

import pyoxigraph as ox
from harness import open_application
from pyramid.authorization import ACLHelper, Authenticated, Everyone
from webtest import TestApp

from gracia.main import make_app, make_config, shutdown
from gracia.resources import Repository

EX = "http://example.org/"


class HeaderPolicy:
    def identity(self, request):
        return request.headers.get("X-User")

    def authenticated_userid(self, request):
        return self.identity(request)

    def permits(self, request, context, permission):
        user = self.identity(request)
        principals = {Everyone}
        if user:
            principals |= {Authenticated, f"user:{user}"}
        if user == "admin":
            principals.add("group:admin")
        return ACLHelper().permits(context, principals, permission)

    def remember(self, request, userid, **kw):
        return []

    def forget(self, request, **kw):
        return []


class FollowingApp(TestApp):
    follow = True

    def do_request(self, req, status=None, expect_errors=False):
        res = super().do_request(req.copy(), None, True)
        if self.follow and res.status_int in (301, 308) and res.headers.get("Location"):
            again = req.copy()
            moved = urlsplit(res.headers["Location"])
            again.path_info, again.query_string = unquote(moved.path), moved.query
            res = super().do_request(again, None, True)
        if not expect_errors:
            self._check_status(status, res)
            self._check_errors(res)
        elif status is not None:
            self._check_status(status, res)
        return res


class WebCase(unittest.TestCase):
    def setUp(self):
        self.application = open_application(self, ":memory:")
        self.settings = {"gracia.application": self.application}

    def client(self, config=None) -> TestApp:
        if config is not None:
            return FollowingApp(config.make_wsgi_app())
        built = getattr(self, "_built_app", None)
        if built is None or built[0] != self.settings:
            built = (dict(self.settings), make_config(self.settings).make_wsgi_app())
            self._built_app = built
        return FollowingApp(built[1])


class RepositoryEndpointsTest(WebCase):
    def test_the_root_lists_the_repositories(self):
        web = self.client()
        self.assertEqual(web.get("/").json, {"repositories": []})
        self.application.create_repository("test_repo")
        self.application.create_repository("archive")
        self.assertEqual(web.get("/").json, {"repositories": ["archive", "test_repo"]})

    def test_an_existing_repository_answers_200(self):
        self.application.create_repository("test_repo")
        response = self.client().get("/test_repo/")
        self.assertEqual(response.status_int, 200)
        body = response.json
        self.assertEqual(body["id"], "test_repo")
        self.assertGreater(
            body["rows"], 0
        )  # a new repository holds its resource and shape

    def test_the_figures_follow_the_data_of_that_repository_only(self):
        self.application.create_repository("test_repo")
        self.application.create_repository("archive")
        with self.application.new_store("test_repo") as store:
            quad = ox.Quad(
                ox.NamedNode("rec:thing:2"), ox.NamedNode(EX + "p"), ox.Literal("x")
            )
            store.put(ox.Dataset([quad]))
        web = self.client()
        self.assertEqual(
            web.get("/test_repo/").json["rows"], web.get("/archive/").json["rows"] + 1
        )

    def test_an_unknown_repository_is_404_and_creates_nothing(self):
        response = self.client().get("/nope/", expect_errors=True)
        self.assertEqual(response.status_int, 404)
        self.assertEqual(response.json, {"error": "not_found", "path": "/nope/"})
        self.assertEqual(self.application.list_repositories(), [])

    def test_ids_that_are_not_valid_repository_ids_are_404(self):
        web = self.client()
        for path in ("/Bad-ID/", "/1abc/", "/a.b/", "/x%20y/", "/docs/", "/api/"):
            with self.subTest(path=path):
                self.assertEqual(web.get(path, expect_errors=True).status_int, 404)

    def test_the_trailing_slash_is_optional(self):
        self.application.create_repository("test_repo")
        web = self.client()
        self.assertEqual(web.get("/test_repo").json, web.get("/test_repo/").json)

    def test_repositories_created_later_are_served_at_once(self):
        web = self.client()
        self.assertEqual(web.get("/later/", expect_errors=True).status_int, 404)
        self.application.create_repository("later")
        self.assertEqual(web.get("/later/").status_int, 200)

    def test_only_get_is_served(self):
        self.application.create_repository("test_repo")
        self.assertEqual(
            self.client().post("/test_repo/", expect_errors=True).status_int, 404
        )

    def test_an_application_can_open_its_own_database(self):
        app = make_app({"gracia.db": ":memory:"})
        self.addCleanup(shutdown, app.registry)
        self.assertTrue(app.registry["application_owned"])
        self.assertEqual(TestApp(app).get("/").json, {"repositories": []})


class RepositoryApiTest(WebCase):
    def setUp(self):
        super().setUp()
        self.application.create_repository("test_repo")
        self.application.create_repository("archive")
        self.settings["gracia.security_policy"] = HeaderPolicy()  # ty: ignore[invalid-assignment]

    def test_every_repository_has_an_api_document(self):
        web = self.client()
        for repo in ("test_repo", "archive"):
            with self.subTest(repo=repo):
                response = web.get(f"/{repo}/api")
                self.assertEqual(response.status_int, 200)
                document = response.json
                self.assertTrue(document["openapi"].startswith("3."))
                self.assertIn(repo, document["info"]["title"])
                self.assertEqual(
                    document["servers"], [{"url": f"http://localhost/{repo}"}]
                )
                self.assertEqual(
                    sorted(document["paths"]),
                    ["/", "/api", "/{type}", "/{type}/{number}"],
                )

    def test_a_browser_gets_the_document_as_a_page_a_program_gets_json(self):
        web = self.client()
        page = web.get(
            "/test_repo/api",
            headers={"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"},
        )
        self.assertIn("text/html", page.content_type)
        for part in (
            "gracia repository test_repo",
            "/{type}/{number}",
            "Try it",
            "apibrowser.js",
        ):
            self.assertIn(part, page.text)
        self.assertEqual(page.headers["Cache-Control"], "private")
        asked = web.get("/test_repo/api", headers={"Accept": "application/json"})
        self.assertIn("application/json", asked.content_type)

    def test_an_unknown_repository_has_no_api(self):
        self.assertEqual(
            self.client().get("/nope/api", expect_errors=True).status_int, 404
        )

    def test_the_document_is_never_shared_through_a_public_cache(self):
        response = self.client().get("/test_repo/api")
        self.assertEqual(response.headers["Cache-Control"], "private")
        self.assertIn("Authorization", response.headers["Vary"])

    def test_what_the_caller_may_not_use_is_not_advertised_and_not_served(self):
        config = make_config(self.settings)

        def stats(request):
            return {"statistics": True}

        config.add_view(stats, context=Repository,
                        name="stats", renderer="json", permission="admin")  # fmt: skip
        config.add_api_operation("/stats", "Statistics", permission="admin")
        web = self.client(config)

        anonymous = web.get("/test_repo/api").json["paths"]
        self.assertNotIn("/stats", anonymous)
        self.assertEqual(
            web.get("/test_repo/stats", expect_errors=True).status_int, 403
        )

        alice = web.get("/test_repo/api", headers={"X-User": "alice"}).json["paths"]
        self.assertNotIn("/stats", alice)  # logged in is not enough

        admin = web.get("/test_repo/api", headers={"X-User": "admin"}).json["paths"]
        self.assertIn("/stats", admin)
        self.assertEqual(admin["/stats"]["get"]["x-gracia-permission"], "admin")
        self.assertEqual(
            web.get("/test_repo/stats", headers={"X-User": "admin"}).status_int, 200
        )

    def test_a_view_without_a_permission_still_needs_one(self):

        class NobodyViews(HeaderPolicy):
            def permits(self, request, context, permission):
                return permission != "view" and super().permits(
                    request, context, permission
                )

        config = make_config({**self.settings, "gracia.security_policy": NobodyViews()})
        config.add_view(
            lambda request: {"ok": True},
            context=Repository,
            name="open",
            renderer="json",
        )
        self.assertEqual(
            self.client(config).get("/test_repo/open", expect_errors=True).status_int,
            403,
        )

    def test_forbidden_is_json(self):
        config = make_config(self.settings)

        config.add_view(
            lambda request: {},
            context=Repository,
            name="secret",
            renderer="json",
            permission="admin",
        )
        response = self.client(config).get("/test_repo/secret", expect_errors=True)
        self.assertEqual(response.status_int, 403)
        self.assertEqual(response.json["error"], "forbidden")


class ExtensionTest(WebCase):
    def test_an_extension_package_is_one_more_include(self):

        def includeme(config):  # what `gracia_statistics.includeme` would be
            config.add_view(
                lambda request: {"n": 3},
                context=Repository,
                name="count",
                renderer="json",
            )
            config.add_api_operation("/count", "Number of things")

        self.application.create_repository("test_repo")
        config = make_config(self.settings)
        config.include(includeme)
        web = self.client(config)
        self.assertEqual(web.get("/test_repo/count").json, {"n": 3})
        self.assertIn("/count", web.get("/test_repo/api").json["paths"])

    def test_an_including_config_overrides_what_an_included_package_registered(self):
        self.application.create_repository("test_repo")
        config = make_config(self.settings)
        config.add_api_operation("/api", "Our own description of the document")
        web = self.client(config)
        self.assertEqual(
            web.get("/test_repo/api").json["paths"]["/api"]["get"]["summary"],
            "Our own description of the document",
        )

    def test_two_operations_on_the_same_path_at_one_level_conflict(self):
        from pyramid.exceptions import ConfigurationConflictError

        config = make_config(self.settings)
        config.add_api_operation("/thing", "one")
        config.add_api_operation("/thing", "two")
        with self.assertRaises(ConfigurationConflictError):
            config.commit()
