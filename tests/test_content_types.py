import base64
import contextlib
import importlib
import io
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pyoxigraph as ox
from base import DbTestCase
from pyramid.exceptions import ConfigurationConflictError
from test_web import WebCase

from gracia import cli, init, types, uri
from gracia.application import Application
from gracia.interfaces import IGraciaType, known_types
from gracia.main import make_config
from gracia.utils import assets

EX = "http://example.org/"
ADMIN = ("admin", "sekretsekret")
SCHEMA = """
@prefix sh: <http://www.w3.org/ns/shacl#> . @prefix xsd:
  <http://www.w3.org/2001/XMLSchema#> .
@prefix ex: <http://example.org/> .
<urn:t:S> a sh:NodeShape ; sh:targetClass <@CLASS@> ;
    sh:property [ sh:path ex:label ; sh:name "Label" ; sh:order 1 ;
        sh:datatype xsd:string ; sh:minCount 1 ; sh:maxCount 1 ] ,
    [ sh:path ex:note ; sh:name "Note" ; sh:order 2 ; sh:datatype xsd:string ;
      sh:maxCount 1 ] .
"""


def basic(user: str, password: str) -> dict:
    return {
        "Authorization": "Basic "
        + base64.b64encode(f"{user}:{password}".encode()).decode()
    }


def make_content_type(
    test,
    package="widgets_ct",
    name="Widget",
    slug="widget",
    class_uri=EX + "Widget",
    schema=None,
):
    """A content type as a package: a descriptor and one schema file. It is put on the
    path (and taken off again), so that it can be imported, and found by its dotted
    name."""
    root = Path(tempfile.mkdtemp())
    test.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
    schema = schema or f"{name}-minimal-01.ttl"
    folder = root / package.replace(".", "/")
    (folder / "shacl").mkdir(parents=True)
    for parent in [folder, *folder.parents]:
        if parent == root.parent or parent == root:
            break
        (parent / "__init__.py").touch()
    (folder / "shacl" / schema).write_text(SCHEMA.replace("@CLASS@", class_uri))
    (folder / "__init__.py").write_text(
        f"TYPE = {{'name': {name!r}, 'slug': {slug!r}, 'class': {class_uri!r}, "
        f"'schema': {schema!r}}}\n"
    )
    sys.path.insert(0, str(root))
    importlib.invalidate_caches()
    test.addCleanup(sys.path.remove, str(root))
    top = package.split(".")[0]
    test.addCleanup(
        lambda: [
            sys.modules.pop(m)
            for m in list(sys.modules)
            if m == top or m.startswith(top + ".")
        ]
    )
    return package


def content_label(form):
    """The `label` of the content: the record has a read-only `gracia:label` of its
    own."""
    return next(f for f in form.fields["label"] if "readonly" not in f.attrs)


class DescriptorTest(WebCase):
    count = 0

    def bad(self, message: str, **res_type):
        type(
            self
        ).count += 1  # a package is imported once, so every call has a name of its own
        package = make_content_type(
            self,
            f"bad_ct_{type(self).count}",
            **{"name": "Bad", "slug": "bad", **res_type},
        )
        with self.assertRaises(ValueError, msg=res_type) as caught:
            types.describe(package)
        self.assertIn(message, str(caught.exception))

    def test_a_package_with_a_descriptor_and_a_schema_is_a_content_type(self):
        found = types.describe(make_content_type(self))
        self.assertEqual(
            (found.name, found.slug, found.class_uri, found.schema, found.assets),
            ("Widget", "widget", EX + "Widget", "Widget-minimal-01.ttl", "widgets_ct:"),
        )
        self.assertIsNotNone(
            assets.shape(found.assets, found.schema, found.class_uri, found.name)
        )

    def test_a_nested_package_names_its_directory_the_way_assets_do(self):
        self.assertEqual(
            types.assets_spec("gracia.types.person"),
            "gracia.types:person/",
        )
        self.assertEqual(types.assets_spec("mylib.content.work"), "mylib.content:work/")
        self.assertEqual(types.assets_spec("work"), "work:")

    def test_what_is_wrong_with_a_descriptor_is_said(self):
        self.bad("slug", slug="Bad Slug")
        self.bad("name", name="bad name")
        self.bad("class", class_uri="not an uri")
        self.bad(
            "class", class_uri="http://example.org/a>b"
        )  # it would break the Turtle
        self.bad("a view", slug="manage")
        self.bad("system type", name="User", slug="usr")
        self.bad("system type", name="Thing", slug="user")
        self.bad(
            "not a file of it", schema="Other-minimal-01.ttl"
        )  # another type's name

    def test_a_missing_schema_file_is_said(self):
        package = make_content_type(self)
        module = importlib.import_module(package)
        module.TYPE = {**module.TYPE, "schema": "Widget-full-09.ttl"}  # ty: ignore[missing-attribute]
        with self.assertRaises(ValueError) as caught:
            types.describe(package)
        self.assertIn("not a file of it", str(caught.exception))

    def test_a_package_that_is_no_content_type_says_so(self):
        with self.assertRaises(ValueError) as caught:
            types.describe("gracia.types.user")  # a system type: no TYPE
        self.assertIn("not a content type", str(caught.exception))

    def test_nothing_that_goes_into_the_turtle_can_break_it(self):
        found = types.describe(make_content_type(self))
        for attribute, value in (
            ("name", 'W"; <x> <y> "z'),
            ("schema", "x\\n"),
            ("assets", 'a"b'),
        ):
            broken = SimpleNamespace(**{**found.__dict__, attribute: value})
            with self.assertRaises(ValueError):
                init.type_turtle("rec:type:9", broken)


class DiscoveryTest(WebCase):
    def test_a_folder_in_gracia_types_is_found_without_being_listed_anywhere(self):
        self.assertIn("person", types.available())  # nothing names it: it is a folder
        self.assertNotIn(
            "user", types.available()
        )  # the system types are not content types

    def test_a_package_can_be_added_by_its_dotted_name(self):
        package = make_content_type(self)
        self.assertNotIn("widget", types.available())
        self.assertEqual(types.available([package])["widget"].package, package)

    def test_an_installed_package_is_found_by_its_entry_point(self):
        package = make_content_type(self)
        point = SimpleNamespace(value=f"{package}")
        with mock.patch.object(
            types,
            "entry_points",
            lambda group: [point] if group == types.ENTRY_POINT_GROUP else [],
        ):
            self.assertIn("widget", types.available())

    def test_two_content_types_with_one_slug_are_refused(self):
        first = make_content_type(self, "one_ct")
        second = make_content_type(
            self, "two_ct", name="Gadget", class_uri=EX + "Gadget"
        )
        with self.assertRaises(ValueError) as caught:
            types.available([first, second])
        self.assertIn("the slug 'widget'", str(caught.exception))

    def test_a_package_without_a_descriptor_is_passed_over(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        (root / "code_only").mkdir()
        (root / "code_only" / "__init__.py").write_text("def includeme(config): pass\n")
        sys.path.insert(0, str(root))
        self.addCleanup(sys.path.remove, str(root))
        self.addCleanup(sys.modules.pop, "code_only", None)
        self.assertNotIn(
            "code_only", [k.package for k in types.available(["code_only"]).values()]
        )
        self.assertIn(
            "code_only", types.packages({"gracia.types": "code_only"})
        )  # but it is included


class RegistryTest(WebCase):
    """The types in the registry of the application: `IGraciaType` utilities."""

    def registry(self, **settings):
        config = make_config({**self.settings, **settings})
        config.commit()
        return config.registry

    def test_a_type_is_found_by_its_slug_and_by_its_class(self):
        registry = self.registry()
        by_slug = registry.queryUtility(IGraciaType, "user")
        self.assertIs(by_slug, registry.queryUtility(IGraciaType, uri.USER_CLASS))
        self.assertTrue(IGraciaType.providedBy(by_slug))
        self.assertEqual(
            (by_slug.name, by_slug.slug, by_slug.system), ("User", "user", True)
        )
        self.assertIsNone(registry.queryUtility(IGraciaType, "nothing"))

    def test_the_types_can_be_listed_once_each_with_their_names(self):
        registry = self.registry()
        names = dict(registry.getUtilitiesFor(IGraciaType))
        self.assertIn("user", names)
        self.assertIn(uri.USER_CLASS, names)
        slugs = [k.slug for k in known_types(registry)]
        self.assertEqual(len(slugs), len(set(slugs)))
        self.assertTrue({"repository", "type", "user", "group", "person"} <= set(slugs))

    def test_the_system_types_are_marked_and_the_content_types_are_not(self):
        registry = self.registry()
        self.assertEqual(
            {k.slug for k in known_types(registry) if k.system},
            {"repository", "type", "user", "group"},
        )
        self.assertEqual(
            set(types.content_types(registry))
            & {"repository", "type", "user", "group"},
            set(),
        )

    def test_the_code_of_a_type_hangs_on_the_same_utility(self):
        registry = self.registry()
        self.assertEqual(
            registry.queryUtility(IGraciaType, "user").record_class.__name__,
            "UserRecord",
        )
        self.assertIsNone(
            registry.queryUtility(IGraciaType, "person").record_class
        )  # generic records

    def test_every_type_there_is_has_a_schema_that_loads(self):
        """No type is named here: a content type that is added later is checked too."""
        registry = self.registry()
        for res_type in known_types(registry):
            if res_type.slug is None:
                continue
            self.assertIsNotNone(
                assets.shape(
                    res_type.assets, res_type.schema, res_type.class_uri, res_type.name
                ),
                res_type.slug,
            )

    def test_a_content_type_of_a_listed_package_is_there_too(self):
        package = make_content_type(self)
        registry = self.registry(**{"gracia.types": package})
        found = registry.queryUtility(IGraciaType, "widget")
        self.assertEqual(
            (found.name, found.package, found.system), ("Widget", package, False)
        )
        self.assertIn("widget", types.content_types(registry))

    def test_two_types_with_the_same_class_or_slug_are_a_conflict(self):
        one = make_content_type(self, "one_ct")
        other = make_content_type(
            self, "two_ct", name="Gadget", slug="gadget", class_uri=EX + "Widget"
        )
        with self.assertRaises((ConfigurationConflictError, Exception)):
            self.registry(**{"gracia.types": f"{one} {other}"})

    def test_the_name_of_a_type_that_has_no_content_type_is_looked_up_with_a_clear_error(  # noqa: E501
        self,
    ):
        registry = self.registry()
        with self.assertRaises(ValueError) as caught:
            types.find_content_type(registry, "nothing")
        self.assertIn("there are: person", str(caught.exception))
        self.assertEqual(
            types.find_content_type(registry, "gracia.types.person").slug,
            "person",
        )


class RepositoryTest(WebCase):
    """Repositories and content types."""

    def setUp(self):
        super().setUp()
        self.package = make_content_type(self)
        config = make_config({**self.settings, "gracia.types": self.package})
        config.commit()
        self.registry = config.registry
        self.widget = types.find_content_type(self.registry, "widget")
        self.person = types.find_content_type(self.registry, "person")

    def slugs(self, repo="library") -> list[str]:
        node = self.application[repo]
        self.addCleanup(node.close)
        return list(node.res_types)

    def test_a_new_repository_starts_with_the_system_types_only(self):
        self.application.create_repository("library", admin=ADMIN)
        self.assertEqual(self.slugs(), ["repository", "type", "user", "group"])

    def test_a_new_repository_can_start_with_content_types(self):
        self.application.create_repository(
            "library", admin=ADMIN, content_types=[self.widget, self.person]
        )
        self.assertEqual(
            self.slugs(), ["repository", "type", "user", "group", "widget", "person"]
        )
        node = self.application["library"]
        self.addCleanup(node.close)
        self.assertEqual(node["widget"].record, "rec:type:5")
        self.assertEqual(
            (node["widget"].schema, node["widget"].assets),
            (self.widget.schema, "widgets_ct:"),
        )

    def test_a_content_type_that_is_wrong_is_refused_before_anything_is_made(self):
        broken = SimpleNamespace(**{**self.widget.__dict__, "slug": "Bad Slug"})
        with self.assertRaises(ValueError):
            self.application.create_repository("library", content_types=[broken])
        self.assertEqual(self.application.list_repositories(), [])
        with self.assertRaises(ValueError):  # the same type twice
            self.application.create_repository(
                "library", content_types=[self.widget, self.widget]
            )
        self.assertEqual(self.application.list_repositories(), [])

    def test_a_content_type_is_added_to_a_repository_that_exists(self):
        self.application.create_repository("library", admin=ADMIN)
        with self.application.open_repository("library") as store:
            record = types.add_content_type(store, "library", self.widget)
            self.assertEqual(record, "rec:type:5")
            self.assertIsNone(
                types.add_content_type(store, "library", self.widget)
            )  # it has it
            self.assertEqual(
                types.add_content_type(store, "library", self.person), "rec:type:6"
            )
        self.assertEqual(
            self.slugs(), ["repository", "type", "user", "group", "widget", "person"]
        )

    def test_adding_one_leaves_the_rest_of_the_repository_as_it_was(self):
        self.application.create_repository("library", admin=ADMIN)
        with self.application.open_repository("library") as store:
            before = store.get("rec:repository:1")
            self.assertGreater(len(before), 0)
            types.add_content_type(store, "library", self.widget)
            after = store.get("rec:repository:1")
        wanted = {
            q.predicate.value for q in before if not isinstance(q.object, ox.BlankNode)
        }
        kept = {
            q.predicate.value for q in after if not isinstance(q.object, ox.BlankNode)
        }
        self.assertTrue(wanted <= kept)  # title, assets, schema: still there
        web = self.client()
        self.assertEqual(
            web.get("/library/user", headers=basic(*ADMIN)).status_int, 200
        )
        self.assertEqual(
            web.get("/library/manage", headers=basic(*ADMIN)).status_int, 200
        )

    def test_the_content_type_works_in_the_web_application_without_a_line_of_code_for_it(  # noqa: E501
        self,
    ):
        self.application.create_repository(
            "library", admin=ADMIN, content_types=[self.widget]
        )
        web = self.client(make_config({**self.settings, "gracia.types": self.package}))
        admin = basic(*ADMIN)
        self.assertEqual(web.get("/library/widget").status_int, 200)  # public to read
        form = web.get("/library/widget/add", headers=admin).forms[0]
        self.assertIn("label", form.fields)  # from its schema file
        content_label(form).value = "First"
        first = form.submit("save", headers=admin)
        self.assertTrue(first.location.endswith("/library/widget/1/edit?saved=1"))
        again = web.get("/library/widget/add", headers=admin).forms[0]
        content_label(again).value = "Second"
        self.assertTrue(
            again.submit("save", headers=admin).location.endswith(
                "/widget/2/edit?saved=1"
            )
        )
        body = web.get(
            "/library/widget/2", headers={**admin, "Accept": "application/json"}
        ).json
        self.assertEqual(body["resource"][EX + "label"], ["Second"])
        self.assertEqual(
            web.get("/library/widget", headers=admin).json["records"], [1, 2]
        )


class CounterTest(WebCase):
    """The counter of the numbers of a type needs nothing: any name works."""

    def setUp(self):
        super().setUp()
        self.application.create_repository("library", admin=ADMIN)

    def put(self, store, *uris):
        text = "".join(f'<{i}> <urn:p> "x" .\n' for i in uris)
        store.put(ox.Dataset(ox.parse(text.encode(), format=ox.RdfFormat.TURTLE)))

    def test_a_type_nobody_has_heard_of_counts_from_one(self):
        with self.application.open_repository("library") as store:
            self.assertEqual([store.mint_id("gizmo") for _ in range(3)], [1, 2, 3])
            self.assertEqual(store.mint_id("gadget"), 1)  # each type for itself

    def test_records_that_are_stored_move_the_counter_past_their_numbers(self):
        with self.application.open_repository("library") as store:
            self.put(store, "rec:gizmo:40", "rec:gizmo:7", "rec:doohickey:3")
            self.assertEqual(store.mint_id("gizmo"), 41)
            self.assertEqual(store.mint_id("doohickey"), 4)

    def test_it_never_goes_back_and_only_records_are_stored(self):
        with self.application.open_repository("library") as store:
            self.put(store, "rec:gizmo:50")
            self.put(store, "rec:gizmo:20")  # lower: the counter stays
            for subject in (
                "urn:x:1",
                "rec:gizmo:0",
                "rec:repository:other",
                "rec:gizmo:abc",
            ):
                with self.subTest(subject=subject), self.assertRaises(ValueError):
                    self.put(
                        store, subject
                    )  # not a record: refused, the counter is not moved
            self.assertEqual(store.mint_id("gizmo"), 51)

    def test_the_counter_moves_inside_the_transaction_of_the_write(self):
        """A write that fails leaves the counter where it was."""
        with self.application.open_repository("library") as store:
            before = store.mint_id("gizmo")
            with self.assertRaises(ValueError):  # an orphan blank node: put refuses it
                store.put(
                    ox.Dataset(
                        [
                            ox.Quad(
                                ox.BlankNode(), ox.NamedNode("urn:p"), ox.Literal("x")
                            )
                        ]
                    )
                )
            self.assertEqual(store.mint_id("gizmo"), before + 1)

    def test_a_new_content_type_goes_on_after_the_records_that_came_in(self):
        package = make_content_type(self)
        config = make_config({**self.settings, "gracia.types": package})
        config.commit()
        widget = types.find_content_type(config.registry, "widget")
        with self.application.open_repository("library") as store:
            self.put(store, "rec:widget:30")  # before the type is even added
            types.add_content_type(store, "library", widget)
        web = self.client(make_config({**self.settings, "gracia.types": package}))
        form = web.get("/library/widget/add", headers=basic(*ADMIN)).forms[0]
        content_label(form).value = "After the import"
        self.assertTrue(
            form.submit("save", headers=basic(*ADMIN)).location.endswith(
                "/widget/31/edit?saved=1"
            )
        )


class CommandsTest(DbTestCase):
    """The commands build their own copy of the registry, as the web application
    does."""

    def setUp(self):
        super().setUp()
        self.package = make_content_type(self)
        self.ini = self.tmp / "config.ini"

    def config(self, **settings):
        lines = "".join(f"{k} = {v}\n" for k, v in settings.items())
        self.ini.write_text("[app:main]\n" + lines)

    def run_cli(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["-c", str(self.ini), "-d", str(self.db), *args])
        return code, out.getvalue(), err.getvalue()

    def create(self, *ids: str):
        password = self.tmp / "pw"
        password.write_text("sekretsekret\n")
        password.chmod(0o600)
        return self.run_cli(
            "repo",
            "create",
            *ids,
            "--admin",
            "admin",
            "--admin-password-file",
            str(password),
        )

    def test_available_lists_what_there_is_and_the_listed_packages(self):
        self.config(**{"gracia.types": self.package})
        code, out, _ = self.run_cli("types", "available")
        self.assertEqual(code, 0)
        self.assertIn("person", out)
        self.assertIn("widget", out)
        self.assertNotIn(
            "user", out.replace("gracia.types.user", "")
        )  # the system types are not offered

    def test_a_new_repository_gets_the_content_types_the_config_names(self):
        self.config(
            **{"gracia.types": self.package, "gracia.content_types": "person widget"}
        )
        self.assertEqual(self.create("library")[0], 0)
        out = self.run_cli("-r", "library", "types", "list")[1]
        self.assertEqual(
            [line.split()[0] for line in out.splitlines()],
            ["repository", "type", "user", "group", "person", "widget"],
        )

    def test_without_the_setting_a_new_repository_has_the_system_types_only(self):
        self.config()
        self.create("library")
        out = self.run_cli("-r", "library", "types", "list")[1]
        self.assertEqual(
            [line.split()[0] for line in out.splitlines()],
            ["repository", "type", "user", "group"],
        )

    def test_a_name_in_the_config_that_is_no_content_type_is_a_clear_error(self):
        self.config(**{"gracia.content_types": "nothing"})
        code, _, err = self.create("library")
        self.assertEqual(code, 1)
        self.assertIn("no content type 'nothing'", err)
        self.assertEqual(self.run_cli("repo", "list")[1], "")  # nothing was made

    def test_add_puts_a_content_type_in_a_repository_that_exists_and_says_when_it_is_there(  # noqa: E501
        self,
    ):
        self.config(**{"gracia.types": self.package})
        self.create("library")
        code, out, err = self.run_cli(
            "-r", "library", "types", "add", "person", "widget"
        )
        self.assertEqual(code, 0, err)
        self.assertIn("person: added as rec:type:5", out)
        self.assertIn("widget: added as rec:type:6", out)
        code, out, _ = self.run_cli("-r", "library", "types", "add", "person")
        self.assertIn("library has it already", out)
        listing = self.run_cli("-r", "library", "types", "list")[1]
        self.assertIn("rec:type:6", listing)

    def test_add_by_the_dotted_name_of_the_package(self):
        self.config()
        self.create("library")
        code, out, _ = self.run_cli(
            "-r", "library", "types", "add", "gracia.types.person"
        )
        self.assertEqual(code, 0)
        self.assertIn("person: added", out)

    def test_what_is_not_there_is_an_error(self):
        self.config()
        self.create("library")
        code, _, err = self.run_cli("-r", "library", "types", "add", "nothing")
        self.assertEqual(code, 1)
        self.assertIn("no content type 'nothing'", err)
        self.assertIn("gracia.types", err)  # and how to make it known
        self.assertEqual(self.run_cli("-r", "nowhere", "types", "list")[0], 1)
        self.assertEqual(self.run_cli("-r", "nowhere", "types", "add", "person")[0], 1)

    def test_records_imported_into_a_repository_with_the_new_type_are_numbered_on(self):
        self.config()
        self.create("library")
        self.run_cli("-r", "library", "types", "add", "person")
        records = self.write(
            "records.ttl",
            "@prefix gracia: <https://gracia.pub/ns/> .\n"
            "@prefix foaf: <http://xmlns.com/foaf/0.1/> .\n"
            "<rec:person:1> a gracia:Record ; "
            'gracia:resource [ a foaf:Person ; foaf:name "Ada" ] .\n'
            "<rec:person:250> a gracia:Record ; "
            'gracia:resource [ a foaf:Person ; foaf:name "Grace" ] .',
        )
        self.assertEqual(self.run_cli("-r", "library", "import", str(records))[0], 0)
        with Application(self.db) as app, app.open_repository("library") as store:
            self.assertEqual(store.mint_id("person"), 251)
