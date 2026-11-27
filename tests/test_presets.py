"""Presets: what a new repository is configured with, and the page of a work."""

import re
from datetime import UTC

from test_manage import ADMIN, ManageCase
from test_work_view import WorkViewCase

from gracia import presets
from gracia.main import make_config
from gracia.presets.schemaorg import SchemaOrgPreset

HTML = {"Accept": "text/html"}


class PresetTest(ManageCase):
    def setUp(self):
        super().setUp()
        self.config = make_config(self.settings)
        self.config.commit()  # the extensions register with the commit

    def repository(self, name):
        node = self.application[name]
        self.addCleanup(node.close)
        return node

    def test_a_preset_is_found_by_its_name_and_an_unknown_one_is_refused(self):
        self.assertEqual(presets.names(), ["minimal", "schemaorg", "unittest"])
        self.assertTrue(issubclass(presets.find("schemaorg"), presets.find("minimal")))
        for name in ("nothing", "", "../x"):
            with self.assertRaises(ValueError, msg=name):
                presets.find(name)
        with self.assertRaises(ValueError):
            self.application.create_repository("other", preset="nothing")
        self.assertFalse(self.application.has_repository("other"))

    def test_minimal_has_the_system_types_and_the_admin_only(self):
        self.application.create_repository("small", admin=("boss", "correct horse"))
        node = self.repository("small")
        self.assertEqual(list(node.types), ["repository", "type", "user", "group"])
        self.assertEqual(node.timezone(), UTC)
        self.assertEqual(node.login("boss", "correct horse"), {"admin"})

    def test_the_unittest_preset_has_an_admin_of_its_own(self):
        self.application.create_repository("tested", preset="unittest")
        node = self.repository("tested")
        self.assertEqual(list(node.types), ["repository", "type", "user", "group"])
        self.assertEqual(node.login(*presets.find("unittest").admin_account), {"admin"})

    def test_schemaorg_has_the_types_the_settings_and_the_citations(self):
        self.application.create_repository(
            "lib", admin=("boss", "correct horse"), preset="schemaorg"
        )
        node = self.repository("lib")
        self.assertEqual(
            list(node.types),
            [
                "repository",
                "type",
                "user",
                "group",
                "person",
                "organization",
                "series",
                "collection",
                "work",
            ],
        )
        self.assertEqual(node.locale(), "nl-NL")
        self.assertEqual(str(node.timezone()), "Europe/Amsterdam")
        self.assertEqual(node.types["work"].view_template, "work-schemaorg-01.xslt")
        self.assertEqual(node.types["work"].doc_template, "work-schemaorg-01.xsl")
        self.assertIsNone(node.types["person"].view_template)
        citations = node.extension("citations")
        self.assertTrue(citations.enabled)
        self.assertEqual(citations.settings["style"], "apa")

    def test_a_preset_can_be_run_again_and_changes_nothing(self):
        self.application.create_repository("lib", preset="schemaorg")
        node = self.repository("lib")
        before = len(node.store.get(node.uri))
        for _ in range(2):
            SchemaOrgPreset(node).apply()
        node.sync_roles()
        self.assertEqual(
            list(node.types)[4:], [c.content_type.slug for c in SchemaOrgPreset.types]
        )
        self.assertEqual(len(node.store.get(node.uri)), before)
        self.assertEqual(node.extension("citations").settings["style"], "apa")

    def test_creating_it_again_leaves_the_repository_as_it_was(self):
        self.assertTrue(self.application.create_repository("lib", preset="schemaorg"))
        self.assertFalse(self.application.create_repository("lib", preset="minimal"))
        self.assertIn("work", self.repository("lib").types)


class WorkCitationTest(WorkViewCase):
    def values(self) -> str:
        return (
            self.client().get("/lib/work/5?debug=input", headers={**ADMIN, **HTML}).text
        )

    def test_a_work_has_no_citation_while_the_extension_is_off(self):
        self.assertNotIn('name="citation"', self.values())

    def test_a_work_has_its_citation_when_the_extension_is_enabled(self):
        make_config(self.settings).commit()
        node = self.application["lib"]
        self.addCleanup(node.close)
        node.extension("citations").enable()
        found = re.search(r'<param name="citation">(.*?)</param>', self.values(), re.S)
        self.assertIsNotNone(found)
        self.assertTrue(found.group(1))  # ty: ignore[possibly-unbound-attribute]
