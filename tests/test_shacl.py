import tempfile
import unittest
from pathlib import Path

import pyoxigraph as ox
from base import DATA, REC, DbTestCase
from harness import open_store
from support import validate

from gracia.graph import ValidationError

EX = "http://example.org/"
PERSON = EX + "PersonShape"

SHAPES = """@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix ex: <http://example.org/> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
ex:PersonShape a sh:NodeShape ; sh:targetClass ex:Person ;
  sh:property [ sh:path ex:name ; sh:minCount 1 ; sh:datatype xsd:string ] ;
  sh:property [ sh:path ex:age ; sh:maxCount 1 ; sh:datatype xsd:integer ] ;
  sh:property [ sh:path ex:address ; sh:node ex:AddressShape ] .
ex:AddressShape a sh:NodeShape ;
  sh:property [ sh:path ex:city ; sh:minCount 1 ] .
"""


def data(turtle: str) -> ox.Dataset:
    text = (
        "@prefix ex: <http://example.org/> .\n"
        "@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .\n"
    )
    return ox.Dataset(ox.parse((text + turtle).encode(), format=ox.RdfFormat.TURTLE))


class ValidateTest(unittest.TestCase):
    def test_conforming_data(self):
        ds = data(
            'ex:a a ex:Person ; ex:name "A" ; ex:age 30 ; ex:address [ ex:city "Ghent" '
            "] ."
        )
        report = validate(ds, SHAPES)
        self.assertTrue(report.conforms)
        self.assertEqual(report.violations, ())

    def test_violations_name_resource_property_and_constraint(self):
        ds = data(
            'ex:ok a ex:Person ; ex:name "A" .\nex:bad a ex:Person ; ex:age "old" .'
        )
        report = validate(ds, SHAPES)
        self.assertFalse(report.conforms)
        self.assertEqual(
            list(report.by_resource()), [EX + "bad"]
        )  # only the resource that fails
        by_path = {v.path: v for v in report.violations}
        self.assertIn(
            "MinCountConstraintComponent", by_path[EX + "name"].constraint_component
        )
        self.assertIn(
            "DatatypeConstraintComponent", by_path[EX + "age"].constraint_component
        )
        self.assertEqual(by_path[EX + "age"].value, '"old"')
        self.assertEqual(by_path[EX + "age"].severity, "Violation")
        self.assertNotIn(
            '"', by_path[EX + "name"].message
        )  # pyrudof wraps messages in quotes
        self.assertEqual(report.errors(), report.violations)

    def test_nested_blank_node_is_checked_through_sh_node(self):
        ds = data('ex:a a ex:Person ; ex:name "A" ; ex:address [ ex:zip "9000" ] .')
        report = validate(ds, SHAPES)
        self.assertFalse(report.conforms)
        self.assertEqual(report.violations[0].path, EX + "address")

    def test_untyped_resources_are_skipped_unless_a_shape_is_forced(self):
        ds = data('ex:untyped ex:age "old" .')
        self.assertTrue(
            validate(ds, SHAPES).conforms
        )  # no rdf:type: no shape targets it
        report = validate(ds, SHAPES, shape=PERSON)
        self.assertFalse(report.conforms)
        self.assertEqual(set(report.by_resource()), {EX + "untyped"})
        self.assertEqual({v.path for v in report.violations}, {EX + "name", EX + "age"})

    def test_forced_shape_accepts_a_node_or_a_string_and_must_exist(self):
        ds = data("ex:a ex:age 3 .")
        self.assertFalse(validate(ds, SHAPES, shape=ox.NamedNode(PERSON)).conforms)
        with self.assertRaisesRegex(ValueError, "no such shape"):
            validate(ds, SHAPES, shape=EX + "NoSuchShape")

    def test_forcing_a_shape_does_not_change_the_shapes_you_passed(self):
        shapes = ox.Dataset(list(ox.parse(SHAPES.encode(), format=ox.RdfFormat.TURTLE)))
        before = len(shapes)
        validate(data("ex:a ex:age 3 ."), shapes, shape=PERSON)
        self.assertEqual(len(shapes), before)

    def test_shapes_as_text_dataset_and_file_agree(self):
        ds = data('ex:bad a ex:Person ; ex:age "old" .')
        as_dataset = ox.Dataset(ox.parse(SHAPES.encode(), format=ox.RdfFormat.TURTLE))

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "shapes.ttl"
            path.write_text(SHAPES, encoding="utf-8")
            reports = [
                validate(ds, SHAPES),
                validate(ds, as_dataset),
                validate(ds, path),
                validate(ds, str(path)),
            ]
        keys = [sorted((v.focus_node, v.path) for v in r.violations) for r in reports]
        self.assertTrue(all(k == keys[0] for k in keys))
        self.assertEqual(len(keys[0]), 2)

    def test_graph_labels_are_ignored(self):
        quads = [
            ox.Quad(
                ox.NamedNode(REC["a"]),
                ox.NamedNode("http://www.w3.org/1999/02/22-rdf-syntax-ns#type"),
                ox.NamedNode(EX + "Person"),
                ox.NamedNode(EX + "g1"),
            ),
            ox.Quad(
                ox.NamedNode(REC["a"]),
                ox.NamedNode(EX + "name"),
                ox.Literal("A"),
                ox.NamedNode(EX + "g2"),
            ),
        ]
        self.assertTrue(
            validate(ox.Dataset(quads), SHAPES).conforms
        )  # one graph: name is there

    def test_empty_dataset_conforms(self):
        self.assertTrue(validate(ox.Dataset(), SHAPES).conforms)

    def test_validation_error_carries_the_report(self):
        report = validate(data("ex:a a ex:Person ."), SHAPES)
        error = ValidationError(report)
        self.assertIs(error.report, report)
        self.assertIn(EX + "a", str(error))


class WithTheStoreTest(DbTestCase):
    def test_validate_what_get_returns(self):
        """Resources read back from the database keep their blank nodes, so shapes still
        apply."""
        with open_store(self.db) as store:
            store.put(
                data(
                    '<rec:thing:1> a ex:Person ; ex:name "A" ; ex:address [ ex:city '
                    '"Ghent" ] .'
                )
            )
            store.put(data("<rec:thing:2> a ex:Person ; ex:address [ ex:zip 1 ] ."))
            report = validate(store.get([REC["a"], REC["b"]]), SHAPES)
        self.assertEqual(set(report.by_resource()), {REC["b"]})

    def test_validate_a_whole_parsed_file(self):
        ds = ox.Dataset(
            ox.parse(path=str(DATA / "01_simple.ttl"), format=ox.RdfFormat.TURTLE)
        )
        self.assertTrue(
            validate(ds, SHAPES).conforms
        )  # no Person in there: nothing targeted
