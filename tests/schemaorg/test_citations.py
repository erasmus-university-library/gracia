"""The Citations extension: the citation of a doc, rendered once when it is made."""

import json
import unittest

from schemaorg.cases import WorkDocCase

from gracia.extensions import ExtensionError
from gracia.utils import citations


class CitationsTest(WorkDocCase):
    def setUp(self):
        super().setUp()
        self.config.commit()  # the extensions register with the commit

    def citations(self):
        repository = self.application["lib"]
        self.addCleanup(repository.close)
        return repository.extension("citations")

    def enable(self, style: str | None = None):
        extension = self.citations()
        extension.enable()
        if style:
            extension.save_settings({"style": style})
        return extension

    def test_it_is_off_until_enabled_and_apa_is_the_default_style(self):
        extension = self.citations()
        self.assertFalse(extension.enabled)
        self.assertNotIn("citation", self.doc(1))
        self.assertEqual(extension.settings["style"], "apa")
        self.assertEqual(extension.style, "apa")
        self.assertIn("apa", citations.style_names())

    def test_a_doc_gets_its_citation_which_is_kept_with_the_doc(self):
        self.enable()
        text = self.doc(1)["citation"]
        self.assertIn("Lovelace", text)
        self.assertIn("(1843)", text)
        self.assertIn("On the Analytical Engine", text)
        self.assertEqual(self.doc(1)["citation"], text)  # from the kept docs
        for number in (1, 2, 3, 4, 5):  # every work is cited on its own
            title = self.csl(number)["title"]
            self.assertIn(title, self.doc(number)["citation"], number)

    def test_a_style_other_than_apa_and_turning_it_off_drops_the_citation(self):
        extension = self.enable()
        apa = self.doc(1)["citation"]
        self.enable("chicago-author-date")
        self.assertNotEqual(self.doc(1)["citation"], apa)
        extension.disable()
        self.assertNotIn("citation", self.doc(1))

    def test_the_settings_import_and_only_take_a_style_of_the_assets(self):
        extension = self.citations()
        extension.enable()
        extension.import_json('{"style": "apa"}')  # the way populate gives them
        self.assertEqual(extension.settings["style"], "apa")
        with self.assertRaises(ExtensionError):
            extension.save_settings({"style": "no-such-style"})


class RenderTest(unittest.TestCase):
    def test_an_item_that_cannot_be_rendered_is_left_out_and_every_style_renders(self):
        good = json.dumps({"id": "a", "type": "book", "title": "A book"})
        out = citations.render("apa", {"a": good, "b": "not json"})
        self.assertEqual(list(out), ["a"])
        self.assertIn("A book", out["a"])
        for name in ("ieee", "nature", "american-medical-association"):
            text = citations.render(name, {"a": good})["a"]  # no number in front
            self.assertRegex(text, r"^(<i>)?A [Bb]ook", name)
        item = json.dumps(
            {
                "id": "a",
                "type": "article-journal",
                "title": "On the Engine",
                "author": [{"family": "Lovelace", "given": "Ada"}],
                "issued": {"date-parts": [[1843]]},
            }
        )
        for name in citations.style_names():
            self.assertIn("Engine", citations.render(name, {"a": item}).get("a", ""))
