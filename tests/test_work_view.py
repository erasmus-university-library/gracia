"""The page of a work: the view of the type `work` (types/work/view/), in the layout."""

from dataclasses import replace

import pyoxigraph as ox
from test_manage import ADMIN, ManageCase
from test_work_doc import (
    ARTICLE,
    BARE,
    BASE,
    PREFIXES,
    ark,
    work,
)

from gracia import types
from gracia.utils import assets

HTML = {"Accept": "text/html"}
FILE = (
    f"schema:author ( {ark('person', 2)} ) ; "
    'schema:encoding [ a schema:MediaObject ; schema:name "paper.pdf" ; '
    "schema:contentUrl <https://example.org/paper.pdf> ; "
    'schema:encodingFormat "application/pdf" ; schema:contentSize "575kb" ]'
)


def labelled(kind: str, number: int, name: str, schema_class: str = "Person") -> str:
    """A record as a repository has it: with the label that its hooks make."""
    return (
        f'<rec:{kind}:{number}> a g:Record ; g:label "{name}" ; '
        f"g:resource_uri {ark(kind, number)} ; "
        f'g:resource [ a schema:{schema_class} ; schema:name "{name}" ] .\n'
    )


class WorkViewCase(ManageCase):
    def setUp(self):
        super().setUp()
        found = types.available()
        self.application.create_repository(
            "lib",
            admin=("admin", "correct horse battery"),
            content_types=[
                replace(found["work"], view="work-schemaorg-01.xslt"),
                found["person"],
                found["organization"],
                found["series"],
            ],
        )
        self.put(
            BASE,
            labelled("person", 1, "Ada Lovelace"),
            labelled("person", 2, "Alan Turing"),
            labelled("series", 1, "Journal of Engines", "Periodical"),
            labelled("organization", 1, "Delft University", "Organization"),
            ARTICLE,
            BARE,
            work(6, "Report", "A Report", FILE),
        )

    def put(self, *turtle: str) -> None:
        text = (PREFIXES + "".join(turtle)).encode()
        data = ox.Dataset(ox.parse(text, format=ox.RdfFormat.TURTLE))
        with self.application.new_store("lib") as store:
            store.put_multi(data)

    def get(self, path: str) -> str:
        return self.client().get(path, headers={**HTML, **ADMIN}).text


class WorkViewTest(WorkViewCase):
    def test_the_type_names_the_view_and_the_view_is_a_file_of_it(self):
        repository = self.application["lib"]
        self.addCleanup(repository.close)
        res_type = repository.types["work"]
        self.assertEqual(res_type.view_template, "work-schemaorg-01.xslt")
        self.assertTrue(
            str(res_type.view_spec).endswith("work/view/work-schemaorg-01.xslt")
        )
        self.assertIsNone(repository.types["person"].view_spec)
        self.assertEqual(
            assets.views_of("gracia.types:work/"), ["work-schemaorg-01.xslt"]
        )

    def test_a_work_has_a_header_a_publication_and_its_metadata(self):
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

    def test_a_work_with_nothing_to_say_has_only_what_it_has(self):
        page = self.get("/lib/work/5")
        self.assertIn("<title>Measurements</title>", page)
        for section in ("publication", "metadata", "files"):
            self.assertNotIn(f'id="{section}"', page)

    def test_the_files_of_a_work_are_listed(self):
        page = self.get("/lib/work/6")
        files = page.split('<section id="files"')[1]
        self.assertIn('<a href="https://example.org/paper.pdf">paper.pdf</a>', files)
        self.assertIn("application/pdf, 575kb", files)

    def test_a_type_without_a_view_has_the_page_every_record_has(self):
        page = self.get("/lib/person/1")
        self.assertNotIn('id="metadata"', page)

    def test_the_page_is_in_the_layout_and_has_a_debug_view(self):
        page = self.get("/lib/work/1")
        self.assertIn("<!DOCTYPE html>", page)
        self.assertIn('<main class="container pb-4">', page)
        shown = self.get("/lib/work/1?debug=xslt")
        self.assertIn('<xsl:import href="work-base-01.xslt"/>', shown)
