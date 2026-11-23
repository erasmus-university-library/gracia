import sys
import tempfile
import unittest
from pathlib import Path

import pyoxigraph as ox
from pyramid.config import Configurator
from pyramid.threadlocal import manager
from test_web import WebCase

from gracia.utils import xslt

XSL = 'xmlns:xsl="http://www.w3.org/1999/XSL/Transform"'


def sheet(body: str, head: str = "") -> str:
    return f'<xsl:stylesheet version="3.0" {XSL}>{head}{body}</xsl:stylesheet>'


PERSON = sheet('<xsl:template name="who"><xsl:text>person</xsl:text></xsl:template>')
WORK = sheet(
    '<xsl:output method="text"/><xsl:template match="/"><xsl:text>work by </xsl:text>'
    '<xsl:call-template name="who"/></xsl:template>',
    head='<xsl:import href="type:person/view.xsl"/>',
)


class TypeImportsCase(WebCase):
    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.dirs = {}
        for slug in ("person", "work", "common"):
            self.dirs[slug] = self.root / slug
            self.dirs[slug].mkdir()
        (self.dirs["person"] / "view.xsl").write_text(PERSON)
        (self.dirs["work"] / "view.xsl").write_text(WORK)
        self.application.create_repository("test_repo")
        self.add_types("test_repo", self.dirs)

    def add_types(self, repo, dirs):
        records = "".join(
            f"<rec:type:{10 + n}> a gracia:Record ;"
            f" gracia:resource_uri <http://example.org/{slug.title()}> ;"
            f' gracia:resource [ a gracia:Type ; gracia:name "{slug.title()}" ;'
            f' gracia:slug "{slug}" ; gracia:class <http://example.org/{slug.title()}> '
            ";"
            f' gracia:assets "{path}/" ] .\n'
            for n, (slug, path) in enumerate(dirs.items())
        )
        listing = " ".join(
            f"<http://example.org/{slug.title()}>" for slug in dirs
        )  # the classes: the resource uris
        text = (
            "@prefix gracia: <https://gracia.pub/ns/> .\n"
            "@prefix dct: <http://purl.org/dc/terms/> .\n"
            "<rec:repository:1> a gracia:Record ;"
            f' gracia:resource [ a gracia:Repository ; gracia:title "{repo}" ;'
            f" gracia:types ( {listing} ) ] .\n" + records
        )
        with self.application.new_store(repo) as store:
            store.put(ox.Dataset(ox.parse(text.encode(), format=ox.RdfFormat.TURTLE)))

    def repository(self, repo="test_repo"):
        node = self.application[repo]
        self.addCleanup(node.close)
        return node


class ReferencesTest(unittest.TestCase):
    def test_only_type_addresses_of_imports_and_includes_are_found(self):
        text = sheet(
            "",
            head=(
                '<xsl:import href="type:person/view.xsl"/><xsl:include '
                'href="type:work/a.xsl"/>'
                '<xsl:import href="other.xsl"/><xsl:import '
                'href="http://example.org/x.xsl"/>'
            ),
        )
        self.assertEqual(
            xslt.type_references(text), ["type:person/view.xsl", "type:work/a.xsl"]
        )

    def test_text_that_is_not_xml_has_no_references(self):
        self.assertEqual(xslt.type_references("<xsl:import"), [])


class ImportTest(TypeImportsCase):
    def run_work(self, repository=None, stylesheet=None):
        repository = repository or self.repository()
        return xslt.transform(
            "<a/>",
            stylesheet or self.dirs["work"] / "view.xsl",
            res_types=repository.resolve_type,
        )

    def test_a_stylesheet_imports_the_stylesheet_of_another_type(self):
        self.assertEqual(self.run_work(), "work by person")

    def test_without_types_the_address_means_nothing(self):
        with self.assertRaises(xslt.XsltError):
            xslt.transform("<a/>", self.dirs["work"] / "view.xsl")

    def test_a_stylesheet_given_as_text_works_too(self):
        self.assertEqual(self.run_work(stylesheet=WORK), "work by person")

    def test_imports_of_imported_files_are_followed(self):
        (self.dirs["common"] / "base.xsl").write_text(
            sheet('<xsl:template name="who"><xsl:text>common</xsl:text></xsl:template>')
        )
        (self.dirs["person"] / "view.xsl").write_text(
            sheet("", head='<xsl:import href="type:common/base.xsl"/>')
        )
        self.assertEqual(self.run_work(), "work by common")

    def test_a_relative_import_inside_a_type_still_works(self):
        (self.dirs["person"] / "name.xsl").write_text(
            sheet(
                '<xsl:template name="who"><xsl:text>relative</xsl:text></xsl:template>'
            )
        )
        (self.dirs["person"] / "view.xsl").write_text(
            sheet("", head='<xsl:import href="name.xsl"/>')
        )
        self.assertEqual(self.run_work(), "work by relative")

    def test_a_repository_can_give_a_type_other_files(self):
        elsewhere = self.root / "person-archive"
        elsewhere.mkdir()
        (elsewhere / "view.xsl").write_text(
            sheet(
                '<xsl:template name="who"><xsl:text>archive '
                "person</xsl:text></xsl:template>"
            )
        )
        self.application.create_repository("archive")
        self.add_types("archive", {"person": elsewhere, "work": self.dirs["work"]})
        self.assertEqual(
            self.run_work(self.repository("archive")), "work by archive person"
        )
        self.assertEqual(self.run_work(self.repository("test_repo")), "work by person")

    def test_an_unknown_type_or_file_is_a_clear_error(self):
        for href, message in (
            ("type:nobody/view.xsl", "no type"),
            ("type:person/missing.xsl", "no such asset"),
            ("type:person/../work/view.xsl", "not a path inside a type"),
            ("type:person//view.xsl", "not a path inside a type"),
        ):
            text = sheet("", head=f'<xsl:import href="{href}"/>')
            with self.assertRaises(xslt.XsltError, msg=href) as caught:
                self.run_work(stylesheet=text)
            self.assertIn(message, str(caught.exception), href)

    def test_a_type_without_assets_cannot_be_imported(self):
        self.add_types("test_repo", {"person": self.dirs["person"]})
        with self.application.new_store("test_repo") as store:
            data = store.get("rec:type:10")
            for quad in [q for q in data if q.predicate.value.endswith("/assets")]:
                data.discard(quad)
            store.put(data)
        with self.assertRaises(xslt.XsltError):
            self.run_work()


class OverrideImportTest(TypeImportsCase):
    def test_an_overridden_file_is_the_one_imported_and_others_fall_through(self):
        # the type's files live in a package, so that override_asset can name them
        package = self.root / "typefiles"
        (package / "person").mkdir(parents=True)
        (package / "__init__.py").touch()
        (package / "person" / "view.xsl").write_text(PERSON)
        sys.path.insert(0, str(self.root))
        self.addCleanup(sys.path.remove, str(self.root))
        self.addCleanup(sys.modules.pop, "typefiles", None)
        self.add_types("test_repo", {"person": "", "work": self.dirs["work"]})
        with self.application.new_store("test_repo") as store:
            data = store.get("rec:type:10")
            for quad in [q for q in data if q.predicate.value.endswith("/assets")]:
                data.discard(quad)
                data.add(
                    ox.Quad(
                        quad.subject,
                        quad.predicate,
                        ox.Literal("typefiles:person/"),
                        quad.graph_name,
                    )
                )
            store.put(data)
        replacement = self.root / "replacement"
        replacement.mkdir()
        (replacement / "view.xsl").write_text(
            sheet(
                "<xsl:template "
                'name="who"><xsl:text>overridden</xsl:text></xsl:template>'
            )
        )
        config = Configurator()
        config.override_asset("typefiles:person/", f"{replacement}/")
        config.commit()
        repository = self.repository()
        self.assertEqual(
            xslt.transform(
                "<a/>",
                self.dirs["work"] / "view.xsl",
                res_types=repository.resolve_type,
            ),
            "work by person",  # outside the application's registry: no overrides
        )
        manager.push({"registry": config.registry, "request": None})
        try:
            self.assertEqual(
                xslt.transform(
                    "<a/>",
                    self.dirs["work"] / "view.xsl",
                    res_types=repository.resolve_type,
                ),
                "work by overridden",
            )
        finally:
            manager.pop()
