import re

from base import DbTestCase

RECORDS = """
@prefix gracia: <https://gracia.pub/ns/> .
@prefix schema: <https://schema.org/> .
<rec:person:1> a gracia:Record ;
    gracia:resource [ a schema:Person ; schema:name "Ada Lovelace" ] .
<rec:person:2> a gracia:Record ;
    gracia:resource_uri <https://orcid.org/0000-0002> ;
    gracia:resource [ a schema:Person ; schema:name "Grace Hopper" ] .
"""


class ImportHooksTest(DbTestCase):
    def setUp(self):
        super().setUp()
        code, _, err = self.create_repositories("lib")
        self.assertEqual(code, 0, err)
        code, _, err = self.gracia("-r", "lib", "types", "add", "person")
        self.assertEqual(code, 0, err)
        self.records = self.tmp / "records.ttl"
        self.records.write_text(RECORDS)

    def export(self, uri):
        code, out, err = self.gracia("-r", "lib", "export", "-f", "ntriples", "-u", uri)
        self.assertEqual(code, 0, err)
        return out

    def test_an_imported_record_gets_its_label_and_its_ark_a_given_resource_uri_is_kept_and_importing_again_changes_nothing(
        self,
    ):
        code, _, err = self.gracia("-r", "lib", "import", str(self.records))
        self.assertEqual(code, 0, err)
        first = self.export("rec:person:1")
        self.assertIn('<https://gracia.pub/ns/label> "Ada Lovelace"', first)
        self.assertIn(
            "<https://gracia.pub/ns/resource_uri> <ark:/99999/ir/person/1>", first
        )

        self.gracia("-r", "lib", "import", str(self.records))
        second = self.export("rec:person:2")
        self.assertIn("<https://orcid.org/0000-0002>", second)
        self.assertNotIn("ark:/99999/ir/person/2", second)
        self.assertIn('<https://gracia.pub/ns/label> "Grace Hopper"', second)

        self.gracia("-r", "lib", "import", str(self.records))
        before = self.normalised(self.export("rec:person:1"))
        code, _, err = self.gracia("-r", "lib", "import", str(self.records))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.normalised(self.export("rec:person:1")), before)

    def test_what_is_not_a_record_is_refused(self):
        other = self.tmp / "other.ttl"
        other.write_text(
            '<http://example.org/other> <http://example.org/p> "not a record" .\n'
        )
        code, _, err = self.gracia("-r", "lib", "import", str(other))
        self.assertEqual(code, 1)
        self.assertIn("only records are stored", err)
        self.assertIn("http://example.org/other", err)

    @staticmethod
    def normalised(text: str) -> list[str]:
        return sorted(
            re.sub(r"_:b\d+_\d+(_c\d+)?", "_:b", line) for line in text.splitlines()
        )


class PlainStoreTest(DbTestCase):
    def test_a_database_without_a_repository_record_imports_as_before(self):
        path = self.tmp / "r.ttl"
        path.write_text(RECORDS)
        code, _, err = self.gracia("import", str(path))
        self.assertEqual(code, 0, err)
        _, out, _ = self.gracia("export", "-f", "ntriples", "-u", "rec:person:1")
        self.assertNotIn("label", out)  # no types known: nothing to run
