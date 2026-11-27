"""Presets: what a new repository is configured with."""

from datetime import UTC

from test_manage import ManageCase

from gracia import presets
from gracia.main import make_config


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
        names = presets.names()
        for name in ("minimal", "unittest"):
            self.assertIn(name, names)
            self.assertEqual(presets.find(name).name, name)
        for name in ("nothing", "", "../x"):
            with self.assertRaises(ValueError, msg=name):
                presets.find(name)
        with self.assertRaises(ValueError):
            self.application.create_repository("other", preset="nothing")
        self.assertFalse(self.application.has_repository("other"))

    def test_minimal_has_the_system_types_and_the_admin_and_unittest_its_own(self):
        self.application.create_repository("small", admin=("boss", "correct horse"))
        node = self.repository("small")
        self.assertEqual(list(node.types), ["repository", "type", "user", "group"])
        self.assertEqual(node.timezone(), UTC)
        self.assertEqual(node.login("boss", "correct horse"), {"admin"})

        self.application.create_repository("tested", preset="unittest")
        node = self.repository("tested")
        self.assertEqual(list(node.types), ["repository", "type", "user", "group"])
        self.assertEqual(node.login(*presets.find("unittest").admin_account), {"admin"})

    def test_a_preset_can_be_run_again_and_creating_it_again_changes_nothing(self):
        self.assertTrue(self.application.create_repository("lib", preset="minimal"))
        node = self.repository("lib")
        before = len(node.store.get(node.uri))
        for _ in range(2):
            presets.find("minimal")(node).apply()
        node.sync_roles()
        self.assertEqual(list(node.types), ["repository", "type", "user", "group"])
        self.assertEqual(len(node.store.get(node.uri)), before)
        self.assertFalse(self.application.create_repository("lib", preset="unittest"))
