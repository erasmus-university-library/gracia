from test_edit import EditCase

from gracia.jobs.nodes import Jobs
from gracia.types.type import Record, Type


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

    def test_every_node_reaches_the_same_repository_the_application_has_none_and_the_request_gets_its_store_from_it(
        self,
    ):
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
        self.assertIs(self.repo.repository, self.repo)  # its own repository
        self.assertFalse(hasattr(self.application, "repository"))
        web = self.client()
        self.assertEqual(
            web.get("/test_repo/person/1", headers=self.admin).status_int, 200
        )
        self.assertEqual(
            web.get(
                "/test_repo/", headers={**self.admin, "Accept": "application/json"}
            ).json["id"],
            "test_repo",
        )

    def test_what_is_below_a_repository_is_what_says_so_with_traversable(self):
        from gracia import interfaces
        from gracia.templates import DeformViews
        from gracia.types.repository import Resolved

        self.assertIsInstance(interfaces.find_child(self.repo, "jobs"), Jobs)
        self.assertIsInstance(interfaces.find_child(self.repo, "deform"), DeformViews)
        resolved = interfaces.find_child(self.repo, "rec:person:1")
        self.assertIsInstance(resolved, Resolved)
        self.assertIs(resolved.repository, self.repo)  # given in its `__init__`
        self.assertEqual(resolved.record.uri, "rec:person:1")
        self.assertIsNone(
            interfaces.find_child(self.repo, "person")
        )  # a type is not one
        with self.assertRaises(KeyError):
            interfaces.find_child(self.repo, "rec:person:999")

    def test_a_node_has_the_repository_the_store_the_registry_and_the_application_and_the_types_are_in_types(
        self,
    ):
        record = self.repo["person"].find_record("1")
        self.assertIs(record.repository, self.repo)
        self.assertIs(record.store, self.repo.store)
        self.assertIs(record.registry, self.repo.registry)
        self.assertIs(record.application, self.application)
        self.assertIs(record.repository, record.repository)  # found once
        self.assertIn("repository", record.__dict__)  # kept on the node
        self.assertIn("person", self.repo.types)
        self.assertIs(self.repo["person"], self.repo.types["person"])
        self.assertIs(self.repo.types["person"].repository, self.repo)

    def test_the_record_of_the_repository_is_a_record_with_its_resource(self):
        record = self.repo.record()
        self.assertIsInstance(record, Record)
        self.assertEqual(record.uri, self.repo.uri)
        self.assertIs(record.repository, self.repo)
        self.assertIsNotNone(record.resource)  # the blank node of its content
        self.assertEqual(record.type.slug, "repository")  # found by its address

    def test_a_traversed_record_has_its_type_as_parent_and_a_loose_one_the_repository(
        self,
    ):
        traversed = self.repo["person"].find_record("1")
        self.assertIs(traversed.__parent__, self.repo.types["person"])
        self.assertIs(traversed.type, self.repo.types["person"])
        loose = self.repo.record()
        self.assertIs(loose.__parent__, self.repo)
        self.assertIs(loose.type, self.repo.types["repository"])
