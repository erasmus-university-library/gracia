from __future__ import annotations

import functools
import json
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Self

import pyoxigraph as ox

from gracia import uri
from gracia.uri import SH_NS
from gracia.utils import xslt

_RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
VIOLATION = "Violation"
JSON_TRIPLES = "json-triples"
XSD_STRING = "http://www.w3.org/2001/XMLSchema#string"
RDF_LANGSTRING = "http://www.w3.org/1999/02/22-rdf-syntax-ns#langString"
# file extension -> format name (json-triples is gracia's own JSON, read by DuckDB
# itself)
INPUT_BY_EXTENSION = {
    ".ttl": "turtle",
    ".turtle": "turtle",
    ".nt": "ntriples",
    ".nq": "nquads",
    ".trig": "trig",
    ".rdf": "rdfxml",
    ".xml": "rdfxml",
    ".n3": "n3",
    ".jsonld": "jsonld",
    ".json": JSON_TRIPLES,
}
_FORMATS = {
    "turtle": ox.RdfFormat.TURTLE,
    "trig": ox.RdfFormat.TRIG,
    "ntriples": ox.RdfFormat.N_TRIPLES,
    "nquads": ox.RdfFormat.N_QUADS,
    "rdfxml": ox.RdfFormat.RDF_XML,
    "n3": ox.RdfFormat.N3,
    "jsonld": ox.RdfFormat.JSON_LD,
}
OUTPUT_FORMATS = (*_FORMATS, JSON_TRIPLES)
_TURTLE_PREFIX = re.compile(r"^@prefix ([^\s:]*): <[^>]*> \.\n", re.M)
_LEFT_OUT = re.compile(r'"""(?:.|\n)*?"""|"(?:[^"\\\n]|\\.)*"|<[^<>\s]*>|#[^\n]*')
_XMLNS = re.compile(r'\s+xmlns:([\w.-]+)="[^"]*"')


class ParseError(ValueError):
    pass


@dataclass(frozen=True)
class Violation:
    focus_node: str  # the resource (or blank node) that does not conform
    path: str | None  # the property the constraint is about
    message: str
    severity: str  # "Violation", "Warning" or "Info"
    source_shape: str | None
    constraint_component: str | None
    value: str | None  # the offending value, as written in N-Triples


@dataclass(frozen=True)
class Report:
    conforms: bool
    violations: tuple[Violation, ...] = ()

    def by_resource(self) -> dict[str, list[Violation]]:
        grouped: dict[str, list[Violation]] = {}
        for violation in self.violations:
            grouped.setdefault(violation.focus_node, []).append(violation)
        return grouped

    def errors(self) -> tuple[Violation, ...]:
        return tuple(v for v in self.violations if v.severity == VIOLATION)


class ValidationError(ValueError):
    def __init__(self, report: Report):
        self.report = report
        first = report.violations[0]
        super().__init__(
            f"{len(report.violations)} violation(s), e.g. {first.focus_node}: "
            f"{first.message}"
        )


Row = tuple[str, str, str, str, str, str | None, str | None]


class RdfError(RuntimeError):
    pass


def streaming_parse(
    source: str | Path | bytes,
    fmt: str | None = None,
    prefixes: dict[str, str] | None = None,
) -> Iterator[Row]:
    if isinstance(source, bytes):
        if fmt is None:
            raise RdfError("a format is required to parse bytes")
        parsed = ox.parse(source, format=Graph.rdf_format(fmt))
    else:
        fmt = fmt or Graph.guess_format(source)
        parsed = ox.parse(path=str(source), format=Graph.rdf_format(fmt))
    try:
        for quad in parsed:
            yield Graph._row_of(quad)
        if prefixes is not None:
            prefixes.update(parsed.prefixes)
    except (SyntaxError, ValueError, OSError) as err:
        raise RdfError(
            f"cannot parse {source if not isinstance(source, bytes) else 'input'}: "
            f"{err}"
        ) from err


def streaming_serialize(
    rows: Iterable[Row],
    fmt: str,
    output: str | Path | None = None,
    prefixes: dict[str, str] | None = None,
) -> str | None:
    rdf_fmt = Graph.rdf_format(fmt)
    with_graph = rdf_fmt.supports_datasets
    quads = (Graph._quad_of(row, with_graph) for row in rows)
    try:
        if output is None:
            return serialize_bytes(quads, format=rdf_fmt, prefixes=prefixes).decode(
                "utf-8"
            )
        ox.serialize(quads, str(output), format=rdf_fmt, prefixes=prefixes)
    except (ValueError, OSError) as err:
        raise RdfError(f"cannot write {fmt}: {err}") from err
    return None


def serialize_bytes(data, **kwargs) -> bytes:
    out = ox.serialize(data, **kwargs)
    if out is None:
        raise ValueError("serialize_bytes takes no output")
    return out


def dataset_rdfxml(dataset) -> bytes:
    """The flat RDF/XML of a dataset, as the graph cache keeps it."""
    return serialize_bytes(dataset, format=ox.RdfFormat.RDF_XML)


def rdfxml(ntriples: str) -> bytes:
    return dataset_rdfxml(
        ox.Dataset(ox.parse(ntriples.encode(), format=ox.RdfFormat.N_TRIPLES))
    )


class Graph:
    def __init__(self, dataset: ox.Dataset | None = None):
        self.dataset = ox.Dataset() if dataset is None else dataset

    @classmethod
    def parse(cls, text: str, fmt: str) -> Self:
        try:
            quads = list(ox.parse(text.encode("utf-8"), format=cls.rdf_format(fmt)))
        except (SyntaxError, ValueError) as err:
            raise ParseError(
                f"This is not valid {fmt}: {str(err).splitlines()[0][:200]}"
            ) from err
        return cls(
            ox.Dataset(
                ox.Quad(q.subject, q.predicate, q.object, ox.DefaultGraph())
                for q in quads
            )
        )

    @classmethod
    def of_rows(cls, rows: Iterable[Row]) -> Self:
        return cls(ox.Dataset(cls._quad_of(row, True) for row in rows))

    def rows(self) -> Iterator[Row]:
        return (self._row_of(quad) for quad in self.dataset)

    # ---- it is a dataset
    # -----------------------------------------------------------------

    def __iter__(self):
        return iter(self.dataset)

    def __len__(self) -> int:
        return len(self.dataset)

    def __contains__(self, quad) -> bool:
        return quad in self.dataset

    def __str__(self) -> str:
        return str(self.dataset)

    def __repr__(self) -> str:
        return f"Graph({len(self.dataset)} quads)"

    def __getattr__(
        self, name
    ):  # add, discard, remove, quads_for_subject, canonicalize, ...
        if name == "dataset":  # not there yet (a copy, a pickle): no loop
            raise AttributeError(name)
        return getattr(self.dataset, name)

    def copy(self) -> Graph:
        return Graph(ox.Dataset(self.dataset))

    # ---- reading
    # -------------------------------------------------------------------------

    def objects(self, subject, predicate) -> list:
        subject, predicate = self._term(subject), self._term(predicate)
        return [
            q.object
            for q in self.dataset.quads_for_subject(subject)
            if q.predicate == predicate
        ]

    def value(self, subject, predicate):
        found = self.objects(subject, predicate)
        return found[0] if found else None

    def subjects(self) -> list[ox.NamedNode]:
        seen: dict[ox.NamedNode, None] = {}
        for quad in self.dataset:
            if isinstance(quad.subject, ox.NamedNode):
                seen.setdefault(quad.subject)
        return list(seen)

    def _resource(self, root: ox.NamedNode) -> set[ox.Quad]:
        found: set[ox.Quad] = set()
        visited: set[tuple] = set()
        pending = list(self.dataset.quads_for_subject(root))
        while pending:
            quad = pending.pop()
            if quad in found:
                continue
            found.add(quad)
            node = quad.object
            # a blank node is followed inside the graph it was reached in, like the
            # database does
            if (
                isinstance(node, ox.BlankNode)
                and (node, quad.graph_name) not in visited
            ):
                visited.add((node, quad.graph_name))
                pending.extend(
                    q
                    for q in self.dataset.quads_for_subject(node)
                    if q.graph_name == quad.graph_name
                )
        return found

    def extract(self, uri) -> Graph:
        return Graph(ox.Dataset(self._resource(self._term(uri))))

    def orphaned_nodes(self) -> Graph:
        covered: set[ox.Quad] = set()
        for root in self.subjects():
            covered |= self._resource(root)
        return Graph(ox.Dataset(q for q in self.dataset if q not in covered))

    def walk_list(self, head) -> list:
        first, rest, nil = (ox.NamedNode(_RDF + n) for n in ("first", "rest", "nil"))
        items, seen = [], set()
        while head != nil and head not in seen:
            seen.add(head)
            cells = self.objects(head, first)
            if not cells:
                break
            items.append(cells[0])
            following = self.objects(head, rest)
            head = following[0] if following else nil
        return items

    # ---- changing
    # ------------------------------------------------------------------------

    def graph_name_of(self, subject):
        return next(
            (q.graph_name for q in self.dataset.quads_for_subject(self._term(subject))),
            None,
        )

    def replace(self, subject, predicate, new, graph=None) -> None:
        subject, predicate = self._term(subject), self._term(predicate)
        if graph is None:
            graph = self.graph_name_of(subject) or ox.DefaultGraph()
        for quad in [
            q
            for q in self.dataset.quads_for_subject(subject)
            if q.predicate == predicate
        ]:
            self.dataset.discard(quad)
        if new is not None:
            self.dataset.add(ox.Quad(subject, predicate, new, graph))

    def replace_list(self, subject, predicate, items: list) -> None:
        """Make the value of a property the rdf:list of the items, in their order (no
        value for none, and what the old list was made of is gone)."""
        subject, predicate = self._term(subject), self._term(predicate)
        for old in self.objects(subject, predicate):
            if isinstance(old, ox.BlankNode):
                self.discard_node(subject, predicate, old)
        self.replace(subject, predicate, None)
        first, rest = (ox.NamedNode(_RDF + n) for n in ("first", "rest"))
        graph = self.graph_name_of(subject)
        head = ox.NamedNode(_RDF + "nil")
        for item in reversed(items):
            cell = ox.BlankNode()
            self.add_value(cell, first, item, graph)
            self.add_value(cell, rest, head, graph)
            head = cell
        if items:
            self.add_value(subject, predicate, head, graph)

    def discard_plain(self, subject, predicate) -> None:
        """Take away the values of a property that are not blank nodes (a form of
        values leaves the nodes alone, which another form has)."""
        subject, predicate = self._term(subject), self._term(predicate)
        for quad in list(self.dataset.quads_for_subject(subject)):
            if quad.predicate == predicate and not isinstance(
                quad.object, ox.BlankNode
            ):
                self.dataset.discard(quad)

    def discard_node(self, subject, predicate, node) -> None:
        subject, predicate = self._term(subject), self._term(predicate)
        for quad in list(self.dataset.quads_for_subject(subject)):
            if quad.predicate == predicate and quad.object == node:
                self.dataset.discard(quad)
        pending = [node]  # and what is said of it, and of the blank nodes below it
        while pending:
            for quad in list(self.dataset.quads_for_subject(pending.pop())):
                self.dataset.discard(quad)
                if isinstance(quad.object, ox.BlankNode):
                    pending.append(quad.object)

    def add_value(self, subject, predicate, term, graph=None) -> None:
        subject = self._term(subject)
        if graph is None:
            graph = self.graph_name_of(subject) or ox.DefaultGraph()
        self.dataset.add(ox.Quad(subject, self._term(predicate), term, graph))

    # ---- whole graphs
    # --------------------------------------------------------------------

    def rdfxml(self, without=()) -> str:
        """The RDF/XML of the graph, without the values of the predicates in `without`
        (what is never shown); "" when nothing is left."""
        shown = self.without(without) if without else self
        if not len(shown):
            return ""
        return dataset_rdfxml(shown.dataset).decode()

    def without(self, predicates) -> Graph:
        hidden = {self._term(p) for p in predicates}
        return Graph(ox.Dataset(q for q in self.dataset if q.predicate not in hidden))

    def remap(self, old, new) -> Graph:
        old, new = self._term(old), self._term(new)

        def swap(term):
            return new if term == old else term

        return Graph(
            ox.Dataset(
                ox.Quad(swap(q.subject), q.predicate, swap(q.object), q.graph_name)
                for q in self.dataset
            )
        )

    def patch(self, before, after) -> None:
        before, after = set(before), set(after)
        for quad in before - after:
            self.dataset.discard(quad)
        for quad in after - before:
            self.dataset.add(quad)

    # ---- text
    # ----------------------------------------------------------------------------

    def serialize(
        self,
        fmt: str,
        prefixes: dict[str, str] | None = None,
        xslt_params: dict[str, str] | None = None,
    ) -> str:
        # the triple formats refuse a graph name, so a quad that has one is written
        # without it
        quads = sorted(map(self._flat, self.dataset), key=self._order)
        text = serialize_bytes(
            quads, format=self.rdf_format(fmt), prefixes=prefixes
        ).decode("utf-8")
        if fmt == "turtle":
            return self._only_used_turtle_prefixes(text)
        if fmt == "rdfxml":
            return xslt.nest(self._only_used_xml_prefixes(text), xslt_params)
        if fmt == "jsonld":  # the serializer writes one line
            return json.dumps(json.loads(text), indent=2, ensure_ascii=False)
        return text

    # ---- SHACL
    # ---------------------------------------------------------------------------

    def validate(
        self,
        shapes: Graph | ox.Dataset | str | Path,
        shape: str | ox.NamedNode | None = None,
    ) -> Report:
        import pyrudof  # about 30 ms to load: only when something is validated

        shapes_graph = self._shapes_graph(shapes)
        if shape is not None:
            shape_node = (
                shape if isinstance(shape, ox.NamedNode) else ox.NamedNode(shape)
            )
            if not any(True for _ in shapes_graph.quads_for_subject(shape_node)):
                raise ValueError(
                    f"no such shape in the shapes graph: {shape_node.value}"
                )
            for root in self.subjects():
                shapes_graph.add(
                    ox.Quad(shape_node, ox.NamedNode(SH_NS.targetNode), root)
                )
        if len(self) == 0:
            return Report(True)

        triples = (ox.Triple(q.subject, q.predicate, q.object) for q in self.dataset)
        data = serialize_bytes(triples, format=ox.RdfFormat.N_TRIPLES).decode("utf-8")
        rudof = pyrudof.Rudof(pyrudof.RudofConfig())
        rudof.read_shacl(
            serialize_bytes(shapes_graph, format=ox.RdfFormat.TURTLE).decode("utf-8")
        )
        rudof.read_data(data, pyrudof.RDFFormat.NTriples)
        result = rudof.validate_shacl()
        return Report(
            conforms=result.conforms,
            violations=tuple(
                Violation(
                    focus_node=entry.focus_node,
                    path=entry.path,
                    message=self._clean(entry.message),
                    severity=entry.severity,
                    source_shape=entry.source_shape,
                    constraint_component=entry.constraint_component,
                    value=entry.value,
                )
                for entry in result.violations
            ),
        )

    # ---- SPARQL
    # --------------------------------------------------------------------------

    @staticmethod
    def validate_select(query: str, prefixes: dict[str, str]) -> None:
        problem = _select_problem(query, tuple(sorted(prefixes.items())))
        if problem is not None:
            raise ValueError(problem)

    def select(self, query: str, prefixes: dict[str, str]) -> ox.QuerySolutions:
        self.validate_select(query, prefixes)
        store = ox.Store()
        store.extend(self.dataset)
        return store.query(query, prefixes=prefixes, use_default_graph_as_union=True)  # ty: ignore[invalid-explicit-return]

    @staticmethod
    def guess_format(path: str | Path) -> str:
        ext = Path(path).suffix.lower()
        try:
            return INPUT_BY_EXTENSION[ext]
        except KeyError:
            known = ", ".join(sorted(INPUT_BY_EXTENSION))
            raise RdfError(
                f"cannot guess the format of {path} (known extensions: {known})"
            ) from None

    @staticmethod
    def rdf_format(name: str) -> ox.RdfFormat:
        try:
            return _FORMATS[name]
        except KeyError:
            raise RdfError(
                f"unknown format {name!r}; known: {', '.join(_FORMATS)}"
            ) from None

    @staticmethod
    def _term_text(term) -> tuple[str, str]:
        if isinstance(term, ox.NamedNode):
            return term.value, "uri"
        if isinstance(term, ox.BlankNode):
            return "_:" + term.value, "bnode"
        raise RdfError(
            f"unsupported term {term!r} (RDF-star triple terms are not supported)"
        )

    @staticmethod
    def _row_of(quad: ox.Quad) -> Row:
        s, s_type = Graph._term_text(quad.subject)
        lang = dtype = None
        if isinstance(quad.object, ox.Literal):
            literal = quad.object
            o, o_type, lang = literal.value, "literal", literal.language
            dt = literal.datatype.value
            dtype = None if dt in (XSD_STRING, RDF_LANGSTRING) else dt
        else:
            o, o_type = Graph._term_text(quad.object)
        return (s, s_type, quad.predicate.value, o, o_type, lang, dtype)

    @staticmethod
    def _node(value: str, res_type: str):
        return (
            ox.NamedNode(value)
            if res_type == "uri"
            else ox.BlankNode(value.removeprefix("_:"))
        )

    @staticmethod
    def _quad_of(row: Row, with_graph: bool = True):
        s, s_type, p, o, o_type, lang, dtype = row
        subject = Graph._node(s, s_type)
        if o_type == "literal":
            if lang:
                obj = ox.Literal(o, language=lang)
            elif dtype:
                obj = ox.Literal(o, datatype=ox.NamedNode(dtype))
            else:
                obj = ox.Literal(o)
        else:
            obj = Graph._node(o, o_type)
        if with_graph:
            return ox.Quad(subject, ox.NamedNode(p), obj, ox.DefaultGraph())
        return ox.Triple(subject, ox.NamedNode(p), obj)

    @staticmethod
    def _term(term):
        return ox.NamedNode(term) if isinstance(term, str) else term

    @staticmethod
    def _flat(quad: ox.Quad) -> ox.Quad:
        if isinstance(quad.graph_name, ox.DefaultGraph):
            return quad  # as it is
        return ox.Quad(quad.subject, quad.predicate, quad.object, ox.DefaultGraph())

    @staticmethod
    def _order(quad: ox.Quad) -> tuple:
        subject, predicate, term = quad.subject, quad.predicate.value, quad.object
        if isinstance(term, ox.Literal):
            value = (1, term.value, term.language or "", term.datatype.value)
        else:  # a uri before a literal before a blank node
            value = (0 if isinstance(term, ox.NamedNode) else 2, str(term), "", "")
        return (
            isinstance(subject, ox.BlankNode),
            subject.value,  # ty: ignore[missing-union-attribute]
            predicate != uri.RDF_TYPE,
            predicate,
            value,
        )

    @staticmethod
    def _only_used_turtle_prefixes(text: str) -> str:
        declared = list(_TURTLE_PREFIX.finditer(text))
        if not declared:
            return text
        body = text[declared[-1].end() :]
        code = _LEFT_OUT.sub(" ", body)  # not what is inside a string or a URI
        drop = [
            m
            for m in declared
            if not re.search(rf"(?<![\w.-]){re.escape(m[1])}:", code)
        ]
        for match in reversed(drop):
            text = text[: match.start()] + text[match.end() :]
        return text

    @staticmethod
    def _only_used_xml_prefixes(text: str) -> str:
        root_end = text.index(">", text.index("<rdf:RDF")) + 1
        root, body = text[:root_end], text[root_end:]
        tags = " ".join(
            re.findall(r"<[^>]*>", body)
        )  # names and attributes, not the text between
        for match in _XMLNS.finditer(root):
            name = match[1]
            if name != "rdf" and not re.search(rf"(?<![\w.-]){re.escape(name)}:", tags):
                root = root.replace(match[0], "", 1)
        return root + body

    @staticmethod
    def _missing_prefixes(text: str, known: tuple) -> list[str]:
        declared = {name for name, _ in known} | set(
            re.findall(r"(?i)\bPREFIX\s+([^\s:]*):", text)
        )
        code = re.sub(
            r"<[^<>\s]*>|\"[^\"]*\"|'[^']*'|#[^\n]*", " ", text
        )  # URIs, strings, comments
        used = re.findall(r"(?<![\w:])([A-Za-z][\w.-]*):", code)
        return sorted({name for name in used if name not in declared})

    @staticmethod
    def _shapes_graph(shapes: Graph | ox.Dataset | str | Path) -> Graph:
        if isinstance(shapes, Graph | ox.Dataset):
            return Graph(ox.Dataset(shapes))
        if isinstance(shapes, Path) or ("\n" not in shapes and Path(shapes).exists()):
            return Graph.of_rows(streaming_parse(shapes))
        return Graph(
            ox.Dataset(ox.parse(shapes.encode("utf-8"), format=ox.RdfFormat.TURTLE))
        )

    @staticmethod
    def _clean(message: str) -> str:
        return message.strip().strip('"')


@functools.lru_cache(maxsize=512)
def _select_problem(text: str, prefixes: tuple) -> str | None:
    if re.search(r"\bSERVICE\b", text, re.IGNORECASE):
        return "A query cannot use SERVICE"
    try:
        result = ox.Store().query(text, prefixes=dict(prefixes))
    except SyntaxError as err:
        missing = Graph._missing_prefixes(text, prefixes)
        if missing:
            names = ", ".join(f"{name}:" for name in missing)
            return (
                f"The prefix {names} is not defined: add it to the namespace prefixes "
                "of the "
                "repository, or declare it in the query with PREFIX"
            )
        return f"Not a SPARQL query: {str(err).splitlines()[0][:120]}"
    if not isinstance(result, ox.QuerySolutions):
        return "A query is a SELECT"
    return None
