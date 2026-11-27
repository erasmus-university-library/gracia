from base import DATA, RDF_FILES, DbTestCase
from support import canonical, diff, parse_json, prune, same, triples_from_file


class RoundTripTest(DbTestCase):
    def test_all_data_files_and_two_files_in_one_database(self):
        self.assertTrue(RDF_FILES, "no test data found")
        for path in RDF_FILES:
            with self.subTest(file=path.name):
                self.db.unlink(missing_ok=True)
                self.load(path)
                expected = triples_from_file(path)
                actual = parse_json(self.export_json())
                self.assertTrue(same(expected, actual), diff(expected, actual))

        self.db.unlink(missing_ok=True)
        a, b = DATA / "02_bnodes.ttl", DATA / "05_shared.ttl"  # no resource in common
        self.load(a, b)
        expected = []
        for tag, path in (("a", a), ("b", b)):
            for st, sv, p, ot, ov, lang, dt in prune(triples_from_file(path)):
                sv = (
                    f"{tag}:{sv}" if st == "bnode" else sv
                )  # keep the bnodes of the files apart
                ov = f"{tag}:{ov}" if ot == "bnode" else ov
                expected.append((st, sv, p, ot, ov, lang, dt))
        self.assertEqual(canonical(expected), canonical(parse_json(self.export_json())))

    def test_large_file_streams(self):
        big = self.tmp / "big.nt"
        with big.open("w", encoding="utf-8") as f:
            for i in range(150_000):
                f.write(
                    f'<rec:big:{i // 5 + 1}> <http://example.org/p{i % 5}> "v{i}" .\n'
                )
        code, out, err = self.gracia("import", str(big))
        self.assertEqual(code, 0, err)
        self.assertIn("150000 rows inserted", out)
