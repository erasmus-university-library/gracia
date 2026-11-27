from base import DATA, DbTestCase

from gracia.extensions.ns_prefix import DEFAULT_PREFIXES


class PrefixCase(DbTestCase):
    def setUp(self):
        super().setUp()
        code, _, err = self.create_repositories("lib")
        self.assertEqual(code, 0, err)

    def run_in(self, *args):
        return self.gracia("-r", "lib", *args)

    def prefixes(self) -> dict[str, str]:
        _, out, _ = self.run_in("prefixes")
        return dict(line.split("\t") for line in out.splitlines())


class PrefixSettingsTest(PrefixCase):
    def test_a_new_repository_has_the_default_prefixes_and_they_can_be_set_and_removed(
        self,
    ):
        self.assertEqual(
            self.prefixes(), {p["prefix"]: p["namespace"] for p in DEFAULT_PREFIXES}
        )
        for name in ("rdf", "rdfs", "dc", "foaf", "gracia"):
            self.assertIn(name, self.prefixes())

        self.run_in("prefixes", "my=http://my.org/")
        self.assertEqual(self.prefixes()["my"], "http://my.org/")
        code, _, _ = self.run_in("prefixes", "--remove", "my")
        self.assertEqual(code, 0)
        self.assertNotIn("my", self.prefixes())
        code, _, err = self.run_in("prefixes", "--remove", "my")
        self.assertEqual(code, 1)
        self.assertIn("no such prefix", err)
        code, _, err = self.run_in("prefixes", "1bad=http://x/")
        self.assertEqual(code, 1)
        self.assertIn("invalid prefix", err)

        self.run_in("prefixes", "--remove", "skos")
        self.assertNotIn("skos", self.prefixes())

    def test_a_plain_triple_store_has_the_defaults_and_cannot_keep_others(self):
        code, out, _ = self.gracia(
            "prefixes"
        )  # the database of `lib`, repository `main`
        self.assertEqual(code, 0)
        self.assertIn("foaf\thttp://xmlns.com/foaf/0.1/", out)


class ImportTest(PrefixCase):
    def test_import_adds_the_declared_prefixes_once_never_rebinds_one_and_does_not_store_the_default(
        self,
    ):
        self.run_in("import", str(DATA / "01_simple.ttl"))
        self.assertEqual(self.prefixes()["ex"], "http://example.org/")

        self.run_in("prefixes", "ex=http://elsewhere.org/")
        code, _, err = self.run_in("import", str(DATA / "01_simple.ttl"))
        self.assertEqual(code, 0, err)
        self.assertIn("prefix 'ex'", err)
        self.assertEqual(self.prefixes()["ex"], "http://elsewhere.org/")

        path = self.write(
            "d.ttl", '@prefix : <http://default.org/> .\n<rec:thing:1> :p "x" .\n'
        )
        self.run_in("import", str(path))
        self.assertNotIn("", self.prefixes())


class OutputTest(PrefixCase):
    def test_turtle_declares_only_used_prefixes_and_the_cli_option_overrides(self):
        self.run_in("import", str(DATA / "01_simple.ttl"))
        _, out, _ = self.run_in("export", "-f", "turtle")
        self.assertIn("@prefix ex:", out)
        self.assertNotIn("@prefix foaf:", out)
        self.assertIn("rec:book:1", out)

        self.run_in("import", str(DATA / "01_simple.ttl"))
        _, out, _ = self.run_in(
            "export", "-f", "turtle", "-p", "ex=http://nowhere.org/"
        )
        self.assertIn("@prefix ex: <http://nowhere.org/>", out)


class PlainStoreTest(DbTestCase):
    def test_import_and_export_work_but_a_declared_prefix_is_not_kept(self):
        self.load(DATA / "01_simple.ttl")
        _, out, _ = self.gracia("export", "-f", "turtle")
        self.assertIn("<rec:book:1>", out)  # no `ex` to shorten it with
        self.assertIn("@prefix dcterms:", out)  # the defaults are there
