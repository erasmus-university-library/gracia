"""The schemaorg preset: its types, its settings, its citations, and the citation of a
work in its page."""

import re

import pyoxigraph as ox
from schemaorg.cases import WorkViewCase
from test_manage import ADMIN, ManageCase

from gracia import presets
from gracia.graph import Graph
from gracia.main import make_config
from gracia.presets.schemaorg import SchemaOrgPreset

HTML = {"Accept": "text/html"}


class SchemaOrgPresetTest(ManageCase):
    def setUp(self):
        super().setUp()
        self.config = make_config(self.settings)
        self.config.commit()  # the extensions register with the commit

    def test_schemaorg_has_the_types_the_settings_and_the_citations(self):
        self.assertTrue(issubclass(presets.find("schemaorg"), presets.find("minimal")))
        self.application.create_repository(
            "lib", admin=("boss", "correct horse"), preset="schemaorg"
        )
        node = self.application["lib"]
        self.addCleanup(node.close)
        for slug in ("person", "organization", "series", "collection", "work"):
            self.assertIn(slug, node.types)
        self.assertEqual(node.locale(), "nl-NL")
        self.assertEqual(str(node.timezone()), "Europe/Amsterdam")
        self.assertEqual(node.types["work"].view_template, "work-schemaorg-01.xslt")
        self.assertEqual(node.types["work"].doc_template, "work-schemaorg-01.xsl")
        self.assertIsNone(node.types["person"].view_template)
        citations = node.extension("citations")
        self.assertTrue(citations.enabled)
        self.assertEqual(citations.settings["style"], "apa")

    def test_the_preset_can_be_run_again_and_changes_nothing(self):
        self.application.create_repository("lib", preset="schemaorg")
        node = self.application["lib"]
        self.addCleanup(node.close)
        types = list(node.types)
        before = len(node.store.get(node.uri))
        for _ in range(2):
            SchemaOrgPreset(node).apply()
        node.sync_roles()
        self.assertEqual(list(node.types), types)
        self.assertEqual(len(node.store.get(node.uri)), before)
        self.assertEqual(node.extension("citations").settings["style"], "apa")


class WorkCitationTest(WorkViewCase):
    def values(self) -> str:
        return (
            self.client().get("/lib/work/5?debug=input", headers={**ADMIN, **HTML}).text
        )

    def test_a_work_has_its_citation_only_when_the_extension_is_enabled(self):
        self.assertNotIn('name="citation"', self.values())
        make_config(self.settings).commit()
        node = self.application["lib"]
        self.addCleanup(node.close)
        node.extension("citations").enable()
        found = re.search(r'<param name="citation">(.*?)</param>', self.values(), re.S)
        assert found is not None
        self.assertTrue(found.group(1))


class PersonDocTest(ManageCase):
    def setUp(self):
        super().setUp()
        make_config(self.settings).commit()  # the extensions register with the commit

    def test_the_doc_of_a_person_has_the_parts_of_the_name_by_schema_org(self):
        self.application.create_repository(
            "lib", admin=("admin", "correct horse"), preset="schemaorg"
        )
        node = self.application["lib"]
        self.addCleanup(node.close)
        text = (
            "@prefix gracia: <https://gracia.pub/ns/> . "
            "@prefix schema: <https://schema.org/> . "
            "<rec:person:1> a gracia:Record ; "
            'gracia:resource [ a schema:Person ; schema:name "Ada Lovelace" ; '
            'schema:givenName "Ada" ; schema:familyName "Lovelace" ] .'
        )
        data = ox.Dataset(ox.parse(text.encode(), format=ox.RdfFormat.TURTLE))
        node.store.put_multi(Graph(data))
        doc = node.store.get_doc("rec:person:1")
        self.assertEqual((doc["given_name"], doc["family_name"]), ("Ada", "Lovelace"))
