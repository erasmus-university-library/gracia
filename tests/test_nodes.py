from test_edit import EditCase

from gracia.resources import Jobs, Record, Type


class RepositoryOfNodeTest(EditCase):
    def setUp(self):
        super().setUp()
        config = self.make_config()
        config.commit()
        config.begin()
        self.addCleanup(config.end)
        self.repo = self.application["test_repo"]
        self.addCleanup(self.repo.close)

    def make_config(self):
        from gracia.main import make_config

        return make_config(self.settings)

    def test_every_node_reaches_the_same_repository(self):
        person = self.repo["person"]
        record = person["1"]
        jobs = self.repo["jobs"]
        deform = self.repo["deform"]
        resolved = self.repo["rec:person:1"]
        for node in (person, record, jobs, deform, resolved):
            self.assertIs(node.repository, self.repo, type(node).__name__)
        self.assertIsInstance(person, Type)
        self.assertIsInstance(record, Record)
        self.assertIsInstance(jobs, Jobs)

    def test_a_repository_is_its_own_repository(self):
        self.assertIs(self.repo.repository, self.repo)

    def test_the_application_has_none(self):
        self.assertFalse(hasattr(self.application, "repository"))

    def test_what_is_below_a_repository_is_what_says_so_with_traversable(self):
        from gracia import traversal
        from gracia.resources import DeformViews, Resolved

        self.assertIsInstance(traversal.child(self.repo, "jobs"), Jobs)
        self.assertIsInstance(traversal.child(self.repo, "deform"), DeformViews)
        resolved = traversal.child(self.repo, "rec:person:1")
        self.assertIsInstance(resolved, Resolved)
        self.assertIs(resolved.repository, self.repo)  # given in its `__init__`
        self.assertEqual(resolved.record.uri, "rec:person:1")
        self.assertIsNone(traversal.child(self.repo, "person"))  # a type is not one
        with self.assertRaises(KeyError):
            traversal.child(self.repo, "rec:person:999")

    def test_a_node_has_the_repository_the_store_the_registry_and_the_application(self):
        record = self.repo["person"]["1"]
        self.assertIs(record.repository, self.repo)
        self.assertIs(record.store, self.repo.store)
        self.assertIs(record.registry, self.repo.registry)
        self.assertIs(record.application, self.application)

    def test_what_is_kept_is_in_the_registry_of_the_application(self):
        from pyramid.registry import Registry

        from gracia.main import adopt

        registry = Registry()
        adopt(self.application, registry)
        calls = []
        self.application.cache.slug(
            "test_repo",
            "person",
            "ada",
            lambda: calls.append(1) or ("rec:person:1", False),
        )
        self.assertEqual(
            registry["slugs"]["test_repo"], {("person", "ada"): ("rec:person:1", False)}
        )
        self.application.cache.invalidate("test_repo")
        self.assertNotIn("test_repo", registry["slugs"])

    def test_a_slug_is_kept_until_the_repository_is_written_and_none_is_not_kept(self):
        cache = self.application.cache
        calls = []

        def load():
            calls.append(1)
            return ("rec:person:1", False)

        self.assertEqual(
            cache.slug("test_repo", "person", "ada", load)[0], "rec:person:1"
        )
        cache.slug("test_repo", "person", "ada", load)
        self.assertEqual(len(calls), 1)
        self.application.cache.invalidate(
            "test_repo"
        )  # what a change of the types does
        cache.slug("test_repo", "person", "ada", load)
        self.assertEqual(len(calls), 2)
        nothing = []
        for _ in range(2):
            cache.slug("test_repo", "person", "no", lambda: nothing.append(1))
        self.assertEqual(len(nothing), 2)

    def test_it_is_found_once(self):
        record = self.repo["person"]["1"]
        self.assertIs(record.repository, record.repository)
        self.assertIn("repository", record.__dict__)  # kept on the node

    def test_the_request_gets_the_store_from_the_repository_of_its_context(self):
        web = self.client()
        self.assertEqual(
            web.get("/test_repo/person/1", headers=self.admin).status_int, 200
        )
        self.assertEqual(
            web.get("/test_repo/", headers=self.admin).json["id"], "test_repo"
        )
