"""The Citations extension: the citation of a doc, rendered once when it is made."""

import json

from test_work_doc import WorkDocCase

from gracia import citations


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

    def test_it_is_off_until_enabled(self):
        self.assertFalse(self.citations().enabled)
        self.assertNotIn("citation", self.doc(1))

    def test_apa_is_the_default_style(self):
        extension = self.citations()
        self.assertEqual(extension.settings["style"], "apa")
        self.assertEqual(extension.style, "apa")
        self.assertIn("apa", citations.style_names())

    def test_a_doc_gets_its_citation(self):
        self.enable()
        text = self.doc(1)["citation"]
        self.assertIn("Lovelace", text)
        self.assertIn("(1843)", text)
        self.assertIn("On the Analytical Engine", text)

    def test_every_work_is_cited_on_its_own(self):
        self.enable()
        for number in (1, 2, 3, 4, 5):
            title = self.csl(number)["title"]
            self.assertIn(title, self.doc(number)["citation"], number)

    def test_the_citation_is_kept_with_the_doc(self):
        self.enable()
        first = self.doc(1)["citation"]
        again = self.doc(1)  # from the kept docs
        self.assertEqual(again["citation"], first)

    def test_a_style_other_than_apa(self):
        self.enable()
        apa = self.doc(1)["citation"]
        self.enable("chicago-author-date")
        self.assertNotEqual(self.doc(1)["citation"], apa)

    def test_turning_it_off_drops_the_citation_from_kept_docs(self):
        extension = self.enable()
        self.assertIn("citation", self.doc(1))
        extension.disable()
        self.assertNotIn("citation", self.doc(1))

    def test_the_settings_import_the_way_populate_gives_them(self):
        extension = self.citations()
        extension.enable()
        extension.import_json('{"style": "apa"}')
        self.assertEqual(extension.settings["style"], "apa")

    def test_the_setting_only_takes_a_style_of_the_assets(self):
        from gracia.extensions import ExtensionError

        with self.assertRaises(ExtensionError):
            self.citations().save_settings({"style": "no-such-style"})


class RenderTest(WorkDocCase):
    def test_an_item_that_cannot_be_rendered_is_left_out(self):
        good = json.dumps({"id": "a", "type": "book", "title": "A book"})
        out = citations.render("apa", {"a": good, "b": "not json"})
        self.assertEqual(list(out), ["a"])
        self.assertIn("A book", out["a"])

    def test_a_numeric_style_leaves_out_the_number(self):
        item = json.dumps({"id": "a", "type": "book", "title": "A book"})
        for name in ("ieee", "nature", "american-medical-association"):
            text = citations.render(name, {"a": item})["a"]
            self.assertRegex(text, r"^(<i>)?A [Bb]ook", name)

    def test_every_style_of_the_assets_renders(self):
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
