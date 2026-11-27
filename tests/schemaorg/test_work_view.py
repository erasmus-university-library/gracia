"""The page of a work: the view of the type `work` (assets/xslt/view/), in the layout."""

from schemaorg.cases import WorkViewCase

from gracia.utils import assets


class WorkViewTest(WorkViewCase):
    def test_the_type_names_the_view_and_the_view_is_a_file_of_it(self):
        repository = self.application["lib"]
        self.addCleanup(repository.close)
        res_type = repository.types["work"]
        self.assertEqual(res_type.view_template, "work-schemaorg-01.xslt")
        self.assertTrue(
            str(res_type.view_spec).endswith("xslt/view/work-schemaorg-01.xslt")
        )
        self.assertIsNone(repository.types["person"].view_spec)
        self.assertEqual(
            assets.views_of("gracia:assets/", "work"), ["work-schemaorg-01.xslt"]
        )

    def test_a_work_has_a_header_a_publication_its_metadata_and_files_in_the_layout(
        self,
    ):
        page = self.get("/lib/work/1")
        self.assertIn("<title>On the Analytical Engine</title>", page)
        self.assertIn('<h1 class="h3 mb-0">On the Analytical Engine</h1>', page)
        self.assertRegex(
            page, r'<li class="breadcrumb-item"><a href="http://localhost/lib/">'
        )
        self.assertRegex(  # the authors, in the order of the list, and the date
            page,
            r'(?s)work-byline.*<a href="http://localhost/lib/person/1">Ada Lovelace</a>'
            r".*<a href=\"http://localhost/lib/person/2\">Alan Turing</a>"
            r'.*<time datetime="1843-07-01">1843-07-01</time>',
        )
        self.assertRegex(
            page, r'(?s)<section id="publication".*<p>Notes on a machine\.</p>'
        )
        metadata = page.split('<section id="metadata"')[1].split("</section>")[0]
        self.assertRegex(
            metadata, r"<dt[^>]*>Keywords</dt>\s*<dd[^>]*>\s*<div>engines, computation"
        )
        self.assertRegex(
            metadata, r'<a href="http://localhost/lib/series/1">Journal of Engines</a>'
        )
        self.assertRegex(
            metadata,
            r'<a href="http://localhost/lib/organization/1">Delft University</a>',
        )
        self.assertRegex(metadata, r"<dt[^>]*>Pages</dt>\s*<dd[^>]*>\s*<div>666–731")
        self.assertNotIn('id="files"', page)

        page = self.get("/lib/work/6")
        files = page.split('<section id="files"')[1]
        self.assertIn('<a href="https://example.org/paper.pdf">paper.pdf</a>', files)
        self.assertIn("application/pdf, 575kb", files)

        page = self.get("/lib/work/1")
        self.assertIn("<!DOCTYPE html>", page)
        self.assertIn('<main class="container pb-4">', page)
        shown = self.get("/lib/work/1?debug=xslt")
        self.assertIn('<xsl:import href="work-base-01.xslt"/>', shown)

    def test_a_work_with_nothing_to_say_and_a_type_without_a_view_have_a_plain_page(
        self,
    ):
        page = self.get("/lib/work/5")
        self.assertIn("<title>Measurements</title>", page)
        for section in ("publication", "metadata", "files"):
            self.assertNotIn(f'id="{section}"', page)
        page = self.get("/lib/person/1")
        self.assertNotIn('id="metadata"', page)
