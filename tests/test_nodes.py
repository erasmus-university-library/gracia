from test_edit import EditCase

from gracia.resources import Jobs, Node, Record, Type


class RepositoryOfNodeTest(EditCase):
    def setUp(self):
        super().setUp()
        config = self.make_config()
        config.commit()
        config.begin()
        self.addCleanup(config.end)
        self.repo = self.application["library"]
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

    def test_a_node_that_is_not_in_a_repository_says_so(self):
        node = Node()
        self.assertFalse(hasattr(node, "repository"))
        with self.assertRaises(AttributeError):
            _ = node.repository

    def test_it_is_found_once(self):
        record = self.repo["person"]["1"]
        self.assertIs(record.repository, record.repository)
        self.assertIn("repository", record.__dict__)  # kept on the node

    def test_the_request_gets_the_store_from_the_repository_of_its_context(self):
        web = self.client()
        self.assertEqual(
            web.get("/library/person/1", headers=self.admin).status_int, 200
        )
        self.assertEqual(web.get("/library/", headers=self.admin).json["id"], "library")
