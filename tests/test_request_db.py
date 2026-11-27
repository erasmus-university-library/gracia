import duckdb
from harness import ConfiguredCase
from pyramid.httpexceptions import HTTPNotFound
from pyramid.response import Response
from pyramid.security import NO_PERMISSION_REQUIRED

from gracia.types.application import Application
from gracia.types.repository import Repository


class RequestDbTest(ConfiguredCase):
    def setUp(self):
        super().setUp()
        self.application.create_repository("test_repo")
        self.seen = {}

    def probe(self, view):
        self.config.add_view(
            view,
            context=Repository,
            name="probe",
            permission=NO_PERMISSION_REQUIRED,
        )
        return self.client()

    def test_the_root_of_a_request_is_an_application_and_the_store_is_scoped_to_the_cursor_of_the_request(  # noqa: E501
        self,
    ):
        def view(request):
            self.seen.update(
                root=request.root,
                db=request.db,
                store=request.context.store,
            )
            return Response("ok")

        self.probe(view).get("/test_repo/probe")
        root = self.seen["root"]
        self.assertIsInstance(root, Application)
        self.assertIsNot(root, self.application)  # one for the request
        self.assertIs(root.db, self.seen["db"])
        self.assertIs(root.database, self.application.database)  # what is shared
        self.assertIs(root.cache, self.application.cache)
        self.assertIs(self.seen["store"].con, self.seen["db"])

    def test_the_cursor_is_closed_when_the_request_ends_and_closing_the_node_leaves_it_open(  # noqa: E501
        self,
    ):
        def view(request):
            self.seen["db"] = request.db
            request.db.execute("SELECT 1")  # open while the request runs
            self.assertIs(request.context.store.con, request.db)  # opens the store
            request.context.close()
            request.db.execute("SELECT 1")  # still open: the store left it
            return Response("ok")

        self.probe(view).get("/test_repo/probe")
        with self.assertRaises(duckdb.ConnectionException):
            self.seen["db"].execute("SELECT 1")

    def test_it_is_closed_when_the_view_raises_too(self):
        def view(request):
            self.seen["db"] = request.db
            raise HTTPNotFound()

        self.probe(view).get("/test_repo/probe", expect_errors=True)
        with self.assertRaises(duckdb.ConnectionException):
            self.seen["db"].execute("SELECT 1")

    def test_a_node_made_without_a_request_has_a_cursor_of_its_own_and_so_has_the_dummy_request(
        self,
    ):
        with self.application["test_repo"] as repository:
            store = repository.store
            self.assertIsNot(store.con, self.application.db)
        with self.assertRaises(duckdb.ConnectionException):
            store.con.execute("SELECT 1")  # closed with the node

        request = self.request()
        self.assertIs(request.registry, self.application.registry)
        self.assertEqual(request.db.execute("SELECT 1").fetchone(), (1,))
