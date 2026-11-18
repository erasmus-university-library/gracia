import re
import unittest
from typing import ClassVar

import pyoxigraph as ox
from base import DATA, REC, DbTestCase

from gracia.graph import Graph, ParseError, serialize_bytes
from gracia.store import Store

EX = "http://example.org/"
XSD_INT = ox.NamedNode("http://www.w3.org/2001/XMLSchema#integer")


def node(name: str) -> ox.NamedNode:
    return ox.NamedNode(REC.get(name, EX + name))


def parse(path) -> Graph:
    fmt = ox.RdfFormat.TRIG if str(path).endswith(".trig") else ox.RdfFormat.TURTLE
    return Graph(ox.Dataset(ox.parse(path=str(path), format=fmt)))


def iso(a, b) -> bool:
    """Same graph up to blank node renaming (RDFC-1.0), graph labels included."""
    da, db = ox.Dataset(list(a)), ox.Dataset(list(b))
    for d in (da, db):
        d.canonicalize(ox.CanonicalizationAlgorithm.RDFC_1_0)
    return da == db


class ExtractTest(unittest.TestCase):
    def test_follows_blank_nodes_and_lists_but_stops_at_uris(self):
        ds = parse(DATA / "04_complex.ttl")
        doc1 = ds.extract(REC["doc1"])
        self.assertTrue(len(doc1))
        # only doc1 as a uri subject, everything else is blank nodes below it
        self.assertEqual(doc1.subjects(), [node("doc1")])
        self.assertEqual(len(doc1.orphaned_nodes()), 0)
        self.assertGreater(
            len(doc1), len(list(ds.quads_for_subject(node("doc1"))))
        )  # went below
        # ex:ref ex:x points at another uri: ex:x is not pulled in
        self.assertFalse(any(q.subject == node("x") for q in doc1))

    def test_a_uri_object_is_not_followed(self):
        ds = Graph(
            ox.Dataset(
                [
                    ox.Quad(node("a"), node("p"), node("b")),
                    ox.Quad(node("b"), node("q"), ox.Literal("x")),
                ]
            )
        )
        self.assertEqual(len(ds.extract(node("a"))), 1)

    def test_shared_blank_node_is_in_both_resources(self):
        ds = parse(DATA / "04_complex.ttl")
        for name in ("doc2", "doc3"):
            self.assertEqual(len(ds.extract(node(name))), 2)  # ex:see + _:shared label

    def test_blank_nodes_are_followed_within_the_same_graph(self):
        b, g1, g2 = ox.BlankNode(), node("g1"), node("g2")
        ds = Graph(
            ox.Dataset(
                [
                    ox.Quad(node("a"), node("p"), b, g1),
                    ox.Quad(b, node("q"), ox.Literal("in g1"), g1),
                    ox.Quad(b, node("q"), ox.Literal("in g2"), g2),
                ]
            )
        )
        found = ds.extract(node("a"))
        self.assertEqual(
            {q.object.value for q in found if isinstance(q.object, ox.Literal)},
            {"in g1"},
        )

    def test_blank_node_cycle_terminates(self):
        b = ox.BlankNode()
        ds = Graph(
            ox.Dataset([ox.Quad(node("a"), node("p"), b), ox.Quad(b, node("self"), b)])
        )
        self.assertEqual(len(ds.extract(node("a"))), 2)

    def test_unknown_uri_is_empty_and_orphans_are_found(self):
        b = ox.BlankNode()
        ds = Graph(
            ox.Dataset(
                [
                    ox.Quad(node("a"), node("p"), ox.Literal("x")),
                    ox.Quad(b, node("q"), ox.Literal("y")),
                ]
            )
        )
        self.assertEqual(len(ds.extract(node("nope"))), 0)
        self.assertEqual(len(ds.orphaned_nodes()), 1)
        self.assertEqual(ds.subjects(), [node("a")])


class GetTest(DbTestCase):
    def test_get_one_resource(self):
        self.load(DATA / "04_complex.ttl")
        with Store(self.db) as store:
            got = store.get(REC["doc1"])
        self.assertTrue(iso(got, parse(DATA / "04_complex.ttl").extract(REC["doc1"])))

    def test_get_several_and_unknown_and_node_arguments(self):
        self.load(DATA / "01_simple.ttl")
        with Store(self.db) as store:
            both = store.get([REC["book1"], node("alice"), EX + "nobody"])
            self.assertEqual(set(both.subjects()), {node("book1"), node("alice")})
            self.assertEqual(len(store.get(node("alice"))), 1)
            self.assertEqual(len(store.get("http://example.org/nobody")), 0)
            self.assertEqual(len(store.get([])), 0)
            self.assertEqual(
                len(store.get([REC["alice"], REC["alice"]])), 1
            )  # no duplicates

    def test_get_expands_lists_and_blank_nodes(self):
        self.load(DATA / "03_lists.ttl")
        with Store(self.db) as store:
            book = store.get(REC["book1"])
        predicates = {q.predicate.value.rsplit("#", 1)[-1] for q in book}
        self.assertIn("first", predicates)
        self.assertIn("rest", predicates)
        self.assertEqual(len(book.orphaned_nodes()), 0)


class PutTest(DbTestCase):
    def test_put_new_resources_from_a_parsed_file(self):
        ds = parse(DATA / "02_bnodes.ttl")
        with Store(self.db) as store:
            counts = store.put(ds)
            self.assertEqual(counts["delete"], 0)
            self.assertGreater(counts["insert"], 0)
            got = store.get(ds.subjects())
        for root in ds.subjects():
            self.assertTrue(iso(got.extract(root), ds.extract(root)), root)

    def test_a_blank_node_used_twice_is_stored_once_per_use(self):
        """ex:alice ex:home _:addr ; ex:work _:addr: the database keeps one copy per
        path."""
        ds = parse(DATA / "05_shared.ttl")
        original = ds.extract(REC["alice"])
        with Store(self.db) as store:
            store.put(ds)
            stored = store.get(REC["alice"])

        def bnodes(d):
            return {q.subject for q in d if isinstance(q.subject, ox.BlankNode)}

        self.assertEqual(len(bnodes(stored)), 2 * len(bnodes(original)))

    def test_get_put_roundtrip_changes_nothing(self):
        self.load(DATA / "04_complex.ttl")
        with Store(self.db) as store:
            before = len(self.rows(store))
            store.put(store.get([REC["doc1"], REC["doc4"]]))
            self.assertEqual(len(self.rows(store)), before)

    def test_put_replaces_the_resource_as_a_whole(self):
        self.load(DATA / "01_simple.ttl")
        with Store(self.db) as store:
            book = store.get(REC["book1"])
            title = next(
                q
                for q in book
                if q.predicate.value.endswith("/title") and q.object.language == "fr"
            )
            book.remove(title)
            book.add(
                ox.Quad(
                    node("book1"), node("records"), ox.Literal("321", datatype=XSD_INT)
                )
            )  # second value
            book.add(ox.Quad(node("book1"), node("isbn"), ox.Literal("123")))
            counts = store.put(book)
            self.assertGreater(counts["delete"], 0)
            self.assertTrue(iso(store.get(REC["book1"]), book))
            self.assertEqual(len(store.get(REC["alice"])), 1)  # untouched

    def test_put_several_resources_in_one_call_leaves_others_alone(self):
        self.load(DATA / "01_simple.ttl")
        ds = ox.Dataset(
            [
                ox.Quad(node("new1"), node("p"), ox.Literal("a")),
                ox.Quad(node("new2"), node("p"), ox.Literal("b")),
                ox.Quad(node("alice"), node("name"), ox.Literal("Alice II")),
            ]
        )
        with Store(self.db) as store:
            store.put(ds)
            self.assertTrue(
                iso(store.get([node("new1"), node("new2"), node("alice")]), ds)
            )
            self.assertEqual(len(store.get(REC["book1"])), 5)  # untouched

    def test_literal_details_survive(self):
        quads = [
            ox.Quad(node("a"), node("label"), ox.Literal("Doc", language="en")),
            ox.Quad(node("a"), node("n"), ox.Literal("5", datatype=XSD_INT)),
            ox.Quad(node("a"), node("plain"), ox.Literal("x")),
        ]
        with Store(self.db) as store:
            store.put(ox.Dataset(quads))
            self.assertTrue(iso(store.get(node("a")), quads))

    def test_blank_node_orphans_are_an_error_and_nothing_is_stored(self):
        ds = ox.Dataset(
            [
                ox.Quad(node("a"), node("p"), ox.Literal("x")),
                ox.Quad(ox.BlankNode(), node("q"), ox.Literal("y")),
            ]
        )
        with Store(self.db) as store:
            store.set_type_ids(
                [("thing", 30)]
            )  # a record of a type that is not there is an error to ask for
            with self.assertRaisesRegex(ValueError, "belong to no resource"):
                store.put(ds)
            self.assertEqual(len(store.get(node("a"))), 0)

    def test_empty_dataset_is_a_no_op(self):
        with Store(self.db) as store:
            self.assertEqual(dict(store.put(ox.Dataset())), {})

    def test_strict_refuses_warnings(self):
        b = ox.BlankNode()
        ds = ox.Dataset(
            [
                ox.Quad(node("a"), node("p"), b),
                ox.Quad(b, node("self"), b),
                ox.Quad(b, node("x"), ox.Literal("y")),
            ]
        )
        with Store(self.db) as store:
            store.set_type_ids(
                [("thing", 30)]
            )  # a record of a type that is not there is an error to ask for
            with self.assertRaisesRegex(ValueError, "self-loop"):
                store.put(ds, strict=True)
            self.assertEqual(len(store.get(node("a"))), 0)

    def test_put_then_cli_export(self):
        with Store(self.db) as store:
            store.put(parse(DATA / "01_simple.ttl"))
        code, out, err = self.gracia(
            "export", "-f", "ntriples", "-u", REC["alice"], "-u", REC["book1"]
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(len(out.strip().splitlines()), 6)  # alice (1) + book1 (5)
        code, out, _ = self.gracia("export", "-f", "ntriples", "-u", REC["alice"])
        self.assertEqual(len(out.strip().splitlines()), 1)


class WrapperTest(unittest.TestCase):
    def test_a_graph_holds_the_dataset_it_is_given_and_does_not_copy_it(self):
        dataset = ox.Dataset([ox.Quad(node("a"), node("p"), ox.Literal("x"))])
        graph = Graph(dataset)
        self.assertIs(graph.dataset, dataset)
        graph.add(
            ox.Quad(node("a"), node("q"), ox.Literal("y"))
        )  # the dataset's own add
        self.assertEqual(len(dataset), 2)  # one dataset, two ways to it

    def test_of_makes_a_new_dataset_and_copy_is_independent(self):
        quads = [ox.Quad(node("a"), node("p"), ox.Literal("x"))]
        graph = Graph(ox.Dataset(quads))
        copy = graph.copy()
        copy.add(ox.Quad(node("a"), node("q"), ox.Literal("y")))
        self.assertEqual((len(graph), len(copy)), (1, 2))
        self.assertEqual(len(Graph()), 0)

    def test_it_goes_where_quads_go(self):
        graph = Graph(ox.Dataset([ox.Quad(node("a"), node("p"), ox.Literal("x"))]))
        self.assertEqual(list(graph), list(graph.dataset))
        self.assertIn(next(iter(graph)), graph)
        text = serialize_bytes(graph, format=ox.RdfFormat.N_QUADS).decode()
        self.assertIn('"x"', text)
        store = ox.Store()
        store.extend(graph)
        self.assertEqual(len(list(store)), 1)
        self.assertEqual(
            len(list(graph.quads_for_subject(node("a")))), 1
        )  # the dataset's

    def test_a_missing_dataset_is_an_attribute_error_and_not_a_loop(self):
        import copy

        graph = Graph(ox.Dataset([ox.Quad(node("a"), node("p"), ox.Literal("x"))]))
        self.assertEqual(len(copy.copy(graph)), 1)
        with self.assertRaises(AttributeError):
            _ = graph.no_such_method


class ReadingTest(unittest.TestCase):
    graph = Graph(
        ox.Dataset(
            [
                ox.Quad(node("a"), node("p"), ox.Literal("1")),
                ox.Quad(node("a"), node("p"), ox.Literal("2")),
                ox.Quad(node("a"), node("q"), node("b")),
            ]
        )
    )

    def test_objects_and_value_take_a_string_for_a_uri(self):
        self.assertEqual(
            {o.value for o in self.graph.objects(REC["a"], EX + "p")}, {"1", "2"}
        )
        self.assertEqual(self.graph.objects(node("a"), node("q")), [node("b")])
        self.assertEqual(self.graph.objects(REC["a"], EX + "nothing"), [])
        self.assertIsNone(self.graph.value(REC["a"], EX + "nothing"))
        self.assertEqual(self.graph.value(REC["a"], EX + "q"), node("b"))

    def test_walk_list_follows_first_and_rest_and_ends_where_it_breaks(self):
        rdf = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
        first, rest, nil = (ox.NamedNode(rdf + n) for n in ("first", "rest", "nil"))
        c1, c2 = ox.BlankNode(), ox.BlankNode()
        graph = Graph(
            ox.Dataset(
                [
                    ox.Quad(c1, first, ox.Literal("a")),
                    ox.Quad(c1, rest, c2),
                    ox.Quad(c2, first, ox.Literal("b")),
                    ox.Quad(c2, rest, nil),
                ]
            )
        )
        self.assertEqual([i.value for i in graph.walk_list(c1)], ["a", "b"])
        self.assertEqual(graph.walk_list(nil), [])
        graph.add(ox.Quad(c2, rest, c1))  # a cycle: it ends where it comes back
        self.assertEqual(len(graph.walk_list(c1)), 2)


class ChangingTest(unittest.TestCase):
    def test_replace_sets_the_one_value_and_none_removes_it(self):
        graph = Graph(
            ox.Dataset(
                [
                    ox.Quad(node("a"), node("p"), ox.Literal("1")),
                    ox.Quad(node("a"), node("p"), ox.Literal("2")),
                    ox.Quad(node("a"), node("q"), ox.Literal("keep")),
                ]
            )
        )
        graph.replace(node("a"), EX + "p", ox.Literal("3"))
        self.assertEqual([o.value for o in graph.objects(node("a"), node("p"))], ["3"])
        self.assertEqual(graph.value(node("a"), node("q")).value, "keep")
        graph.replace(REC["a"], EX + "p", None)
        self.assertIsNone(graph.value(node("a"), node("p")))

    def test_what_is_written_goes_in_the_graph_the_subject_is_in_or_the_one_given(self):
        g1, g2 = node("g1"), node("g2")
        graph = Graph(ox.Dataset([ox.Quad(node("a"), node("p"), ox.Literal("1"), g1)]))
        graph.replace(node("a"), node("p"), ox.Literal("2"))
        graph.add_value(node("a"), node("q"), ox.Literal("x"))
        self.assertEqual({q.graph_name for q in graph}, {g1})  # where the subject is
        graph.add_value(node("a"), node("r"), ox.Literal("y"), graph=g2)
        self.assertEqual(graph.graph_name_of(node("a")) in (g1, g2), True)
        self.assertEqual(
            {q.graph_name for q in graph.dataset.quads_for_predicate(node("r"))}, {g2}
        )
        fresh = Graph()
        fresh.add_value(node("b"), node("p"), ox.Literal("z"))
        self.assertEqual(
            next(iter(fresh)).graph_name, ox.DefaultGraph()
        )  # nothing yet: default
        self.assertIsNone(fresh.graph_name_of(node("nobody")))

    def test_add_value_adds_a_value_and_add_is_still_the_datasets_add_of_a_quad(self):
        graph = Graph()
        graph.add_value(node("a"), node("p"), ox.Literal("1"))
        graph.add_value(node("a"), node("p"), ox.Literal("2"))
        graph.add(ox.Quad(node("a"), node("q"), ox.Literal("3")))
        self.assertEqual(len(graph), 3)


class WholeGraphTest(unittest.TestCase):
    def test_without_leaves_out_the_quads_of_some_predicates(self):
        graph = Graph(
            ox.Dataset(
                [
                    ox.Quad(node("a"), node("p"), ox.Literal("1")),
                    ox.Quad(node("a"), node("secret"), ox.Literal("2")),
                ]
            )
        )
        shown = graph.without([EX + "secret"])
        self.assertEqual([q.predicate for q in shown], [node("p")])
        self.assertEqual(len(graph), 2)  # the graph is left as it was

    def test_remap_changes_a_uri_as_subject_and_as_object_in_a_new_graph(self):
        graph = Graph(
            ox.Dataset(
                [
                    ox.Quad(node("old"), node("p"), node("b")),
                    ox.Quad(node("c"), node("q"), node("old")),
                    ox.Quad(node("c"), node("r"), node("other")),
                ]
            )
        )
        moved = graph.remap(EX + "old", EX + "new")
        self.assertEqual(
            {(q.subject.value, q.object.value) for q in moved},
            {(EX + "new", REC["b"]), (REC["c"], EX + "new"), (REC["c"], EX + "other")},
        )
        self.assertIn(
            node("old"), {q.subject for q in graph}
        )  # the original is left as it was

    def test_patch_does_to_this_what_was_done_to_a_copy(self):
        graph = Graph(
            ox.Dataset(
                [
                    ox.Quad(node("a"), node("p"), ox.Literal("1")),
                    ox.Quad(node("a"), node("q"), ox.Literal("2")),
                ]
            )
        )
        before = graph.copy()
        after = before.copy()
        after.discard(ox.Quad(node("a"), node("p"), ox.Literal("1")))
        after.add(ox.Quad(node("a"), node("r"), ox.Literal("3")))
        graph.add(
            ox.Quad(node("z"), node("p"), ox.Literal("untouched"))
        )  # not in the copy
        graph.patch(before, after)
        self.assertEqual(
            {(q.predicate.value, q.object.value) for q in graph},
            {(EX + "q", "2"), (EX + "r", "3"), (EX + "p", "untouched")},
        )


class TextTest(unittest.TestCase):
    PREFIXES: ClassVar[dict] = {
        "ex": EX,
        "skos": "http://www.w3.org/2004/02/skos/core#",
        "foaf": "http://xmlns.com/foaf/0.1/",
    }
    graph = Graph.parse(
        "@prefix ex: <http://example.org/> . @prefix foaf: "
        "<http://xmlns.com/foaf/0.1/> ."
        ' ex:a a ex:T ; foaf:name "Ada" ; ex:knows [ a foaf:Person ; foaf:name "Bob" ] '
        ".",
        "turtle",
    )

    def test_parse_reads_every_serialization_into_the_default_graph(self):
        for fmt in ("turtle", "ntriples", "rdfxml", "jsonld"):
            with self.subTest(fmt=fmt):
                text = self.graph.serialize(fmt, self.PREFIXES)
                again = Graph.parse(text, fmt)
                self.assertTrue(iso(self.graph, again), text)
                self.assertEqual({q.graph_name for q in again}, {ox.DefaultGraph()})

    def test_a_text_that_is_not_in_the_format_is_a_parse_error(self):
        with self.assertRaisesRegex(ParseError, "This is not valid turtle"):
            Graph.parse("this is not turtle {", "turtle")
        with self.assertRaises(ValueError):  # it is one
            Graph.parse("<a", "ntriples")

    def test_turtle_declares_only_the_prefixes_it_uses(self):
        text = self.graph.serialize("turtle", self.PREFIXES)
        self.assertEqual(
            sorted(line for line in text.splitlines() if line.startswith("@prefix")),
            [
                "@prefix ex: <http://example.org/> .",
                "@prefix foaf: <http://xmlns.com/foaf/0.1/> .",
            ],
        )  # not skos

    def test_a_prefix_that_is_only_in_a_string_is_not_used(self):
        graph = Graph.parse(
            '<rec:thing:1> <http://example.org/p> "see skos:Concept" .', "turtle"
        )
        self.assertNotIn("@prefix skos:", graph.serialize("turtle", self.PREFIXES))

    def test_rdfxml_is_nested_declares_what_it_uses_and_always_rdf(self):
        text = self.graph.serialize("rdfxml", self.PREFIXES)
        self.assertNotIn(
            "rdf:nodeID", text
        )  # the blank node is inside what points to it
        self.assertEqual(
            sorted(re.findall(r"xmlns:(\w+)=", text)), ["ex", "foaf", "rdf"]
        )

    def test_jsonld_is_indented(self):
        self.assertGreater(len(self.graph.serialize("jsonld").splitlines()), 10)

    def test_the_same_triples_are_the_same_text_with_the_type_first(self):
        shuffled = Graph(ox.Dataset(reversed(list(self.graph))))
        for fmt in ("turtle", "ntriples"):
            self.assertEqual(
                self.graph.serialize(fmt, self.PREFIXES),
                shuffled.serialize(fmt, self.PREFIXES),
            )
        turtle = self.graph.serialize("turtle", self.PREFIXES)
        self.assertRegex(turtle, r"ex:a a ex:T ;")  # the type first, then the rest


class SelectTest(unittest.TestCase):
    PREFIXES: ClassVar[dict] = {"foaf": "http://xmlns.com/foaf/0.1/"}
    graph = Graph.parse(
        '@prefix foaf: <http://xmlns.com/foaf/0.1/> . <urn:a> foaf:name "Ada" . '
        "<urn:b> foaf:name "
        '"Bob" .',
        "turtle",
    )

    def test_select_runs_on_the_graph_with_the_prefixes_counted_on(self):
        rows = list(
            self.graph.select("SELECT ?n WHERE { <urn:a> foaf:name ?n }", self.PREFIXES)
        )
        self.assertEqual([r[0].value for r in rows], ["Ada"])

    def test_it_sees_only_this_graph(self):
        other = Graph.parse('<urn:c> <http://xmlns.com/foaf/0.1/name> "Cy" .', "turtle")
        rows = list(other.select("SELECT ?n WHERE { ?s foaf:name ?n }", self.PREFIXES))
        self.assertEqual([r[0].value for r in rows], ["Cy"])

    def test_what_cannot_be_run_says_why_and_a_missing_prefix_is_named(self):
        for query, why in (
            ("ASK { ?s ?p ?o }", "A query is a SELECT"),
            ("SELECT * WHERE { SERVICE <http://x/> { ?s ?p ?o } }", "SERVICE"),
            ("SELECT ?x WHERE { ?s", "Not a SPARQL query"),
            (
                "SELECT ?n WHERE { ?s skos:prefLabel ?n }",
                "The prefix skos: is not defined",
            ),
        ):
            with self.subTest(query=query):
                with self.assertRaisesRegex(ValueError, why):
                    Graph.validate_select(query, self.PREFIXES)
                with self.assertRaisesRegex(ValueError, why):
                    self.graph.select(query, self.PREFIXES)
        Graph.validate_select(
            "PREFIX skos: <http://www.w3.org/2004/02/skos/core#> SELECT ?n WHERE "
            "{ ?s skos:prefLabel ?n }",
            self.PREFIXES,
        )  # declared in the query: fine

    def test_the_answer_is_remembered_for_the_text_and_the_prefixes(self):
        from gracia import graph as module

        module._select_problem.cache_clear()
        for _ in range(3):
            Graph.validate_select("SELECT ?s WHERE { ?s ?p ?o }", self.PREFIXES)
        self.assertEqual(module._select_problem.cache_info().misses, 1)
