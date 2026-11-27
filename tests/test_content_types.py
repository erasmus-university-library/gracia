import base64
import contextlib
import dataclasses
import importlib
import io
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pyoxigraph as ox
from base import DbTestCase
from harness import opened
from pyramid.exceptions import ConfigurationConflictError
from test_web import WebCase

from gracia import cli, init, interfaces, scan, uri
from gracia.interfaces import IRecordType
from gracia.main import make_config
from gracia.types import type as type_module
from gracia.types.user import UserRecord
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
    doc=None,
    view=None,
    files=None,
):
    """A package with a class of records that declares a type, and the shape it names;
    `files` are more files of the package (their path in it and their text)."""
    root = Path(tempfile.mkdtemp())
    test.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
    schema = schema or f"{slug}-minimal-01.ttl"
    folder = root / package.replace(".", "/")
    (folder / "shacl").mkdir(parents=True)
    for parent in [folder, *folder.parents]:
        if parent == root.parent or parent == root:
            break
        (parent / "__init__.py").touch()
    (folder / "shacl" / schema).write_text(SCHEMA.replace("@CLASS@", class_uri))
    for relative, text in (files or {}).items():
        (folder / relative).parent.mkdir(parents=True, exist_ok=True)
        (folder / relative).write_text(text)
    (folder / "__init__.py").write_text(
        "from gracia.types.type import ContentType, Record\n\n\n"
        "class WidgetRecord(Record):\n"
        f"    content_type = ContentType(name={name!r}, slug={slug!r}, "
        f"class_uri={class_uri!r}, schema={schema!r}, doc={doc!r}, view={view!r}, "
        f"assets='{package}:')\n"
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


def declared_by(package: str):
    """The class of records that the package declares its type with."""
    return importlib.import_module(package).WidgetRecord


def content_label(form):
    return next(f for f in form.fields["label"] if "readonly" not in f.attrs)


def find_type(registry, name: str):
    """The content type of a slug, as `gracia types add` finds it."""
    return interfaces.content_types(registry)[name].content_type


class DeclarationTest(WebCase):
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
        cls = declared_by(package)
        with self.assertRaises(ValueError, msg=res_type) as caught:
            type_module.check_declaration(cls, cls.content_type)
        self.assertIn(message, str(caught.exception))

    def test_a_class_with_a_declaration_and_a_schema_is_a_content_type(self):
        cls = declared_by(make_content_type(self))
        found = cls.content_type
        type_module.check_declaration(cls, found)
        self.assertEqual(
            (found.name, found.slug, found.class_uri, found.schema, found.assets),
            ("Widget", "widget", EX + "Widget", "widget-minimal-01.ttl", "widgets_ct:"),
        )
        self.assertIsNotNone(
            assets.shape(found.assets, found.schema, found.class_uri, found.slug)
        )
        missing = dataclasses.replace(found, schema="widget-full-09.ttl")
        with self.assertRaises(ValueError) as caught:
            type_module.check_declaration(cls, missing)
        self.assertIn("not a file of it", str(caught.exception))
        # the types of gracia have their files in the assets of gracia
        self.assertEqual(UserRecord.content_type.assets, type_module.BUNDLED_ASSETS)

    def test_what_is_wrong_with_a_declaration_is_said_and_nothing_can_break_the_turtle(
        self,
    ):
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
            "not a file of it", schema="other-minimal-01.ttl"
        )  # another type's name
        found = declared_by(make_content_type(self)).content_type
        for attribute, value in (
            ("name", 'W"; <x> <y> "z'),
            ("schema", "x\\n"),
            ("assets", 'a"b'),
        ):
            broken = dataclasses.replace(found, **{attribute: value})
            with self.assertRaises(ValueError):
                init.type_turtle("rec:type:9", broken)

    def test_a_class_that_only_inherits_its_declaration_is_another_class_of_the_type(
        self,
    ):
        package = make_content_type(self)

        class Another(declared_by(package)):
            pass

        config = make_config({**self.settings, "gracia.types": package})
        config.add_gracia_type(Another)  # nothing: the type is declared by its parent
        config.commit()
        self.assertIsNot(config.registry.queryUtility(IRecordType, "widget"), Another)
        self.assertIsNotNone(config.registry.queryUtility(IRecordType, "widget"))


class ScanTest(WebCase):
    def registry(self, **settings):
        config = make_config({**self.settings, **settings})
        config.commit()
        return config.registry

    def test_a_module_in_gracia_types_is_found_without_being_listed_anywhere(self):
        found = interfaces.content_types(self.registry())
        self.assertTrue(found)  # nothing names them: they implement IRecord
        for slug, cls in found.items():
            self.assertFalse(cls.content_type.system, slug)
        self.assertNotIn("user", found)  # the system types are not content types

    def test_a_package_is_found_by_its_dotted_name_or_by_its_entry_point(self):
        package = make_content_type(self)
        self.assertNotIn("widget", interfaces.content_types(self.registry()))
        registry = self.registry(**{"gracia.types": package})
        found = registry.queryUtility(IRecordType, "widget")
        self.assertEqual(
            (found.content_type.name, found.__module__, found.content_type.system),
            ("Widget", package, False),
        )
        self.assertIs(interfaces.content_types(registry)["widget"], found)
        point = SimpleNamespace(value=f"{package}")
        with mock.patch.object(
            scan,
            "entry_points",
            lambda group: [point] if group == scan.ENTRY_POINT_GROUP else [],
        ):
            self.assertIn("widget", interfaces.content_types(self.registry()))

    def test_two_content_types_with_one_slug_or_one_class_are_refused(self):
        first = make_content_type(self, "one_ct")
        same_slug = make_content_type(
            self, "two_ct", name="Gadget", class_uri=EX + "Gadget"
        )
        with self.assertRaises(Exception) as caught:
            self.registry(**{"gracia.types": f"{first} {same_slug}"})
        self.assertIn("widget", str(caught.exception))
        same_class = make_content_type(
            self, "three_ct", name="Gadget", slug="gadget", class_uri=EX + "Widget"
        )
        with self.assertRaises((ConfigurationConflictError, Exception)):
            self.registry(**{"gracia.types": f"{first} {same_class}"})

    def test_a_package_that_declares_no_type_is_included_and_nothing_more(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        (root / "code_only").mkdir()
        (root / "code_only" / "__init__.py").write_text(
            "def includeme(config):\n    config.registry['code_only'] = True\n"
        )
        sys.path.insert(0, str(root))
        self.addCleanup(sys.path.remove, str(root))
        self.addCleanup(sys.modules.pop, "code_only", None)
        registry = self.registry(**{"gracia.types": "code_only"})
        self.assertTrue(registry["code_only"])  # it is included
        self.assertNotIn(
            "code_only", [c.__module__ for c in interfaces.known_types(registry)]
        )


class RegistryTest(WebCase):
    def registry(self, **settings):
        config = make_config({**self.settings, **settings})
        config.commit()
        return config.registry

    def test_a_type_is_found_by_its_slug_and_by_its_class_and_listed_once(self):
        registry = self.registry()
        by_slug = registry.queryUtility(IRecordType, "user")
        self.assertIs(by_slug, registry.queryUtility(IRecordType, uri.USER_CLASS))
        self.assertIs(by_slug, UserRecord)
        declared = by_slug.content_type
        self.assertEqual(
            (declared.name, declared.slug, declared.system), ("User", "user", True)
        )
        self.assertIsNone(registry.queryUtility(IRecordType, "nothing"))
        names = dict(registry.getUtilitiesFor(IRecordType))
        self.assertIn("user", names)
        self.assertIn(uri.USER_CLASS, names)
        known = interfaces.known_types(registry)
        slugs = [cls.content_type.slug for cls in known]
        self.assertEqual(len(slugs), len(set(slugs)))
        self.assertTrue({"repository", "type", "user", "group"} <= set(slugs))
        content = interfaces.content_types(registry)
        for cls in known:
            slug = cls.content_type.slug
            self.assertEqual(slug in content, not cls.content_type.system, slug)

    def test_every_type_there_is_has_a_schema_that_loads(self):
        registry = self.registry()
        for cls in interfaces.known_types(registry):
            res_type = cls.content_type
            self.assertIsNotNone(
                assets.shape(
                    res_type.assets, res_type.schema, res_type.class_uri, res_type.slug
                ),
                res_type.slug,
            )


class RepositoryTest(WebCase):
    def setUp(self):
        super().setUp()
        self.package = make_content_type(self)
        self.other = make_content_type(
            self, "gadgets_ct", name="Gadget", slug="gadget", class_uri=EX + "Gadget"
        )
        config = make_config(
            {**self.settings, "gracia.types": f"{self.package} {self.other}"}
        )
        config.commit()
        self.registry = config.registry
        self.widget = find_type(self.registry, "widget")
        self.gadget = find_type(self.registry, "gadget")

    def slugs(self, repo="test_repo") -> list[str]:
        node = self.application[repo]
        self.addCleanup(node.close)
        return list(node.types)

    def test_a_new_repository_starts_with_the_system_types_and_the_content_types_given(
        self,
    ):
        self.application.create_repository("bare", admin=ADMIN)
        system = self.slugs("bare")
        self.assertEqual(
            set(system) & {"widget", "gadget"}, set()
        )  # content types are not in a bare one
        self.application.create_repository(
            "test_repo", admin=ADMIN, content_types=[self.widget, self.gadget]
        )
        self.assertEqual(self.slugs(), [*system, "widget", "gadget"])
        node = self.application["test_repo"]
        self.addCleanup(node.close)
        self.assertEqual(node["widget"].record, f"rec:type:{len(system) + 1}")
        self.assertEqual(
            (node["widget"].schema, node["widget"].assets),
            (self.widget.schema, "widgets_ct:"),
        )

    def test_a_content_type_that_is_wrong_is_refused_before_anything_is_made(self):
        broken = SimpleNamespace(**{**self.widget.__dict__, "slug": "Bad Slug"})
        with self.assertRaises(ValueError):
            self.application.create_repository("test_repo", content_types=[broken])
        self.assertEqual(self.application.list_repositories(), [])
        with self.assertRaises(ValueError):  # the same type twice
            self.application.create_repository(
                "test_repo", content_types=[self.widget, self.widget]
            )
        self.assertEqual(self.application.list_repositories(), [])

    def test_a_content_type_is_added_to_a_repository_that_exists_and_the_rest_stays(
        self,
    ):
        self.application.create_repository("test_repo", admin=ADMIN)
        node = self.application["test_repo"]
        self.addCleanup(node.close)
        system = self.slugs()
        before = node.store.get("rec:repository:1")
        self.assertGreater(len(before), 0)
        self.assertEqual(node.add_type(self.widget), f"rec:type:{len(system) + 1}")
        self.assertIsNone(node.add_type(self.widget))  # it has it
        self.assertEqual(node.add_type(self.gadget), f"rec:type:{len(system) + 2}")
        self.assertEqual(self.slugs(), [*system, "widget", "gadget"])
        after = node.store.get("rec:repository:1")
        wanted = {
            q.predicate.value for q in before if not isinstance(q.object, ox.BlankNode)
        }
        kept = {
            q.predicate.value for q in after if not isinstance(q.object, ox.BlankNode)
        }
        self.assertTrue(wanted <= kept)  # title, assets, schema: still there
        web = self.client()
        self.assertEqual(
            web.get("/test_repo/user", headers=basic(*ADMIN)).status_int, 200
        )
        self.assertEqual(
            web.get("/test_repo/manage", headers=basic(*ADMIN)).status_int, 200
        )

    def test_the_content_type_works_in_the_web_application_without_a_line_of_code_for_it(  # noqa: E501
        self,
    ):
        self.application.create_repository(
            "test_repo", admin=ADMIN, content_types=[self.widget]
        )
        web = self.client(make_config({**self.settings, "gracia.types": self.package}))
        admin = basic(*ADMIN)
        self.assertEqual(web.get("/test_repo/widget").status_int, 200)  # public to read
        form = web.get("/test_repo/widget/add", headers=admin).forms[0]
        self.assertIn("label", form.fields)  # from its schema file
        content_label(form).value = "First"
        first = form.submit("save", headers=admin)
        self.assertTrue(first.location.endswith("/test_repo/widget/1/edit?saved=1"))
        again = web.get("/test_repo/widget/add", headers=admin).forms[0]
        content_label(again).value = "Second"
        self.assertTrue(
            again.submit("save", headers=admin).location.endswith(
                "/widget/2/edit?saved=1"
            )
        )
        body = web.get(
            "/test_repo/widget/2", headers={**admin, "Accept": "application/json"}
        ).json
        self.assertEqual(body["resource"][EX + "label"], ["Second"])
        self.assertEqual(
            web.get(
                "/test_repo/widget", headers={**admin, "Accept": "application/json"}
            ).json["records"],
            [1, 2],
        )


class CounterTest(WebCase):
    def setUp(self):
        super().setUp()
        self.application.create_repository("test_repo", admin=ADMIN)

    def put(self, store, *uris):
        text = "".join(f'<{i}> <urn:p> "x" .\n' for i in uris)
        store.put_multi(ox.Dataset(ox.parse(text.encode(), format=ox.RdfFormat.TURTLE)))

    def test_a_type_nobody_has_heard_of_counts_from_one_and_stored_records_move_it(
        self,
    ):
        with self.application.new_store("test_repo") as store:
            self.assertEqual([store.mint_id("gizmo") for _ in range(3)], [1, 2, 3])
            self.assertEqual(store.mint_id("gadget"), 1)  # each type for itself
            self.put(store, "rec:thing:40", "rec:thing:7", "rec:doohickey:3")
            self.assertEqual(store.mint_id("thing"), 41)
            self.assertEqual(store.mint_id("doohickey"), 4)

    def test_it_never_goes_back_only_records_are_stored_and_it_moves_with_the_write(
        self,
    ):
        with self.application.new_store("test_repo") as store:
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
            before = store.mint_id("gadget")
            with self.assertRaises(ValueError):  # an orphan blank node: put refuses it
                store.put_multi(
                    ox.Dataset(
                        [
                            ox.Quad(
                                ox.BlankNode(), ox.NamedNode("urn:p"), ox.Literal("x")
                            )
                        ]
                    )
                )
            self.assertEqual(store.mint_id("gadget"), before + 1)

    def test_a_new_content_type_goes_on_after_the_records_that_came_in(self):
        package = make_content_type(self)
        config = make_config({**self.settings, "gracia.types": package})
        config.commit()
        widget = find_type(config.registry, "widget")
        node = self.application["test_repo"]
        self.addCleanup(node.close)
        self.put(node.store, "rec:widget:30")  # before the type is even added
        node.add_type(widget)
        web = self.client(make_config({**self.settings, "gracia.types": package}))
        form = web.get("/test_repo/widget/add", headers=basic(*ADMIN)).forms[0]
        content_label(form).value = "After the import"
        self.assertTrue(
            form.submit("save", headers=basic(*ADMIN)).location.endswith(
                "/widget/31/edit?saved=1"
            )
        )


class CommandsTest(DbTestCase):
    def setUp(self):
        super().setUp()
        self.package = make_content_type(self)
        self.other = make_content_type(
            self, "gadgets_ct", name="Gadget", slug="gadget", class_uri=EX + "Gadget"
        )
        self.ini = self.tmp / "config.ini"

    def config(self, **settings):
        lines = "".join(f"{k} = {v}\n" for k, v in settings.items())
        self.ini.write_text("[app:main]\n" + lines)

    def run_cli(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["-c", str(self.ini), "-d", str(self.db), *args])
        return code, out.getvalue(), err.getvalue()

    def application_repository(self, repo: str):
        """The repository, as the commands open it (read it inside `with`)."""
        args = cli.build_parser().parse_args(
            ["-c", str(self.ini), "-d", str(self.db), "-r", repo, "types", "list"]
        )
        return cli._repository(args)

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

    def listed(self, repo: str = "test_repo") -> dict[str, str]:
        """The lines of `types list` by the slug."""
        out = self.run_cli("-r", repo, "types", "list")[1]
        return {line.split()[0]: line for line in out.splitlines()}

    def test_available_lists_what_there_is_and_the_listed_packages(self):
        self.config(**{"gracia.types": self.package})
        code, out, _ = self.run_cli("types", "available")
        self.assertEqual(code, 0)
        self.assertIn("widget", out)
        self.assertNotIn(
            "user", out.replace("gracia.types.user", "")
        )  # the system types are not offered

    def test_a_new_repository_gets_the_content_types_the_config_names(self):
        self.config(
            **{
                "gracia.types": f"{self.package} {self.other}",
                "gracia.content_types": "widget gadget",
            }
        )
        self.assertEqual(self.create("test_repo")[0], 0)
        slugs = list(self.listed())
        self.assertEqual(slugs[-2:], ["widget", "gadget"])
        self.config(**{"gracia.types": f"{self.package} {self.other}"})
        self.assertEqual(self.create("bare")[0], 0)
        self.assertEqual(
            set(self.listed("bare")) & {"widget", "gadget"}, set()
        )  # without the setting: the system types only

    def test_what_is_not_there_is_an_error(self):
        self.config(**{"gracia.content_types": "nothing"})
        code, _, err = self.create("test_repo")
        self.assertEqual(code, 1)  # a name in the config that is no content type
        self.assertIn("no content type 'nothing'", err)
        self.assertEqual(self.run_cli("repo", "list")[1], "")  # nothing was made
        self.config()
        self.create("test_repo")
        code, _, err = self.run_cli("-r", "test_repo", "types", "add", "nothing")
        self.assertEqual(code, 1)
        self.assertIn("no content type 'nothing'", err)
        self.assertIn("gracia.types", err)  # and how to make it known
        self.assertEqual(self.run_cli("-r", "nowhere", "types", "list")[0], 1)
        self.assertEqual(self.run_cli("-r", "nowhere", "types", "add", "widget")[0], 1)

    def test_add_puts_content_types_in_a_repository_that_exists_and_says_when_it_is_there(  # noqa: E501
        self,
    ):
        self.config(**{"gracia.types": f"{self.package} {self.other}"})
        self.create("test_repo")
        first = len(self.listed()) + 1
        code, out, err = self.run_cli(
            "-r", "test_repo", "types", "add", "widget", self.other
        )  # by the slug, and by the dotted name of the package
        self.assertEqual(code, 0, err)
        self.assertIn(f"widget: added as rec:type:{first}", out)
        self.assertIn(f"gadget: added as rec:type:{first + 1}", out)
        code, out, _ = self.run_cli("-r", "test_repo", "types", "add", "widget")
        self.assertIn("test_repo has it already", out)
        self.assertIn(f"rec:type:{first + 1}", self.listed()["gadget"])

    def test_add_takes_the_doc_template_of_a_type_and_one_that_is_not_there_is_an_error(
        self,
    ):
        package = make_content_type(
            self,
            "docs_ct",
            doc="widget-alt-01.xsl",
            files={
                "xslt/doc/widget-alt-01.xsl": "",
                "xslt/doc/widget-other-01.xsl": "",
            },
        )
        self.config(**{"gracia.types": f"{package} {self.other}"})
        self.create("test_repo")
        code, out, err = self.run_cli(
            "-r", "test_repo", "types", "add", "widget", "gadget"
        )
        self.assertEqual(code, 0, err)
        lines = self.listed()  # the one a type names itself, and none
        self.assertTrue(lines["widget"].endswith("widget-alt-01.xsl  -"), lines)
        self.assertTrue(lines["gadget"].endswith(" -  -"), lines)
        self.create("test_repo2")
        for given in (
            "widget=nothing.xsl",
            "widget=gadget-alt-01.xsl",
            "gadget=x-y-01.xsl",
            "widget",
            "=x",
        ):
            code, _, err = self.run_cli(
                "-r", "test_repo2", "types", "add", "widget", "--doc", given
            )
            self.assertEqual(code, 1, given)
            self.assertIn("--doc", err, given)
        self.assertNotIn("widget", self.listed("test_repo2"))
        code, out, err = self.run_cli(
            "-r", "test_repo2", "types", "add", "widget",
            "--doc", "widget=widget-other-01.xsl",
        )  # fmt: skip
        self.assertEqual(code, 0, err)
        self.assertTrue(
            self.listed("test_repo2")["widget"].endswith("widget-other-01.xsl  -")
        )

    def test_add_takes_the_view_of_a_type_and_a_view_that_is_not_there_is_an_error(
        self,
    ):
        package = make_content_type(
            self, "views_ct", files={"xslt/view/widget-alt-01.xslt": ""}
        )
        self.config(**{"gracia.types": package})
        self.create("test_repo")
        for given in (
            "widget=nothing.xslt",
            "widget=widget-base-01.xslt",
            "gadget=x-y-01.xslt",
            "widget",
        ):
            code, _, err = self.run_cli(
                "-r", "test_repo", "types", "add", "widget", "--view", given
            )
            self.assertEqual(code, 1, given)
            self.assertIn("--view", err, given)
        code, out, err = self.run_cli(
            "-r", "test_repo", "types", "add", "widget",
            "--view", "widget=widget-alt-01.xslt",
        )  # fmt: skip
        self.assertEqual(code, 0, err)
        self.assertTrue(self.listed()["widget"].endswith("widget-alt-01.xslt"))

    def test_repo_set_makes_the_timezone_and_the_locale_and_says_what_is_wrong(self):
        self.config()
        self.create("test_repo")
        code, out, err = self.run_cli(
            "-r",
            "test_repo",
            "repo",
            "set",
            "timezone=Europe/Amsterdam",
            "locale=nl-NL",
        )
        self.assertEqual(code, 0, err)
        self.assertIn("timezone=Europe/Amsterdam", out)
        with self.application_repository("test_repo") as repository:
            self.assertEqual(str(repository.timezone()), "Europe/Amsterdam")
            self.assertEqual(repository.locale(), "nl-NL")
        self.run_cli("-r", "test_repo", "repo", "set", "locale=")  # cleared
        with self.application_repository("test_repo") as repository:
            self.assertEqual(repository.locale(), "")
        for given in ("timezone=Mars/Base", "locale=xx-XX", "colour=blue", "nope"):
            code, _, err = self.run_cli("-r", "test_repo", "repo", "set", given)
            self.assertEqual(code, 1, given)
            self.assertTrue(err, given)

    def test_records_imported_into_a_repository_with_the_new_type_are_numbered_on(self):
        self.config(**{"gracia.types": self.package})
        self.create("test_repo")
        self.run_cli("-r", "test_repo", "types", "add", "widget")
        records = self.write(
            "records.ttl",
            "@prefix gracia: <https://gracia.pub/ns/> .\n"
            "@prefix ex: <http://example.org/> .\n"
            "<rec:widget:1> a gracia:Record ; "
            'gracia:resource [ a ex:Widget ; ex:label "Ada" ] .\n'
            "<rec:widget:250> a gracia:Record ; "
            'gracia:resource [ a ex:Widget ; ex:label "Grace" ] .',
        )
        self.assertEqual(self.run_cli("-r", "test_repo", "import", str(records))[0], 0)
        with opened(self.db) as app, app.new_store("test_repo") as store:
            self.assertEqual(store.mint_id("widget"), 251)
