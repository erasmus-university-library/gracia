import functools
import json
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pyoxigraph as ox

from gracia import uri
from gracia.uri import SH_NS
from gracia.utils import xslt

_RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"


class ParseError(ValueError):
    """A text that is not valid in the serialization it was said to be in."""


VIOLATION = "Violation"


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
        """The violations per focus node."""
        grouped: dict[str, list[Violation]] = {}
        for violation in self.violations:
            grouped.setdefault(violation.focus_node, []).append(violation)
        return grouped

    def errors(self) -> tuple[Violation, ...]:
        """Only the violations of severity "Violation" (not warnings or infos)."""
        return tuple(v for v in self.violations if v.severity == VIOLATION)


class ValidationError(ValueError):
    """Raised when data must conform and does not; carries the report."""

    def __init__(self, report: Report):
        self.report = report
        first = report.violations[0]
        super().__init__(
            f"{len(report.violations)} violation(s), e.g. {first.focus_node}: "
            f"{first.message}"
        )


JSON_TRIPLES = "json-triples"

XSD_STRING = "http://www.w3.org/2001/XMLSchema#string"
RDF_LANGSTRING = "http://www.w3.org/1999/02/22-rdf-syntax-ns#langString"

Row = tuple[str, str, str, str, str, str | None, str | None]

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


class RdfError(RuntimeError):
    """The input is not valid RDF in the given format, or the format is not
    supported."""


def guess_format(path: str | Path) -> str:
    ext = Path(path).suffix.lower()
    try:
        return INPUT_BY_EXTENSION[ext]
    except KeyError:
        known = ", ".join(sorted(INPUT_BY_EXTENSION))
        raise RdfError(
            f"cannot guess the format of {path} (known extensions: {known})"
        ) from None


def rdf_format(name: str) -> ox.RdfFormat:
    try:
        return _FORMATS[name]
    except KeyError:
        raise RdfError(
            f"unknown format {name!r}; known: {', '.join(_FORMATS)}"
        ) from None


def _term_text(term) -> tuple[str, str]:
    if isinstance(term, ox.NamedNode):
        return term.value, "uri"
    if isinstance(term, ox.BlankNode):
        return "_:" + term.value, "bnode"
    raise RdfError(
        f"unsupported term {term!r} (RDF-star triple terms are not supported)"
    )


def _row_of(quad: ox.Quad) -> Row:
    """One quad as a staging row (see the module docstring)."""
    s, s_type = _term_text(quad.subject)
    lang = dtype = None
    if isinstance(quad.object, ox.Literal):
        literal = quad.object
        o, o_type, lang = literal.value, "literal", literal.language
        dt = literal.datatype.value
        dtype = None if dt in (XSD_STRING, RDF_LANGSTRING) else dt
    else:
        o, o_type = _term_text(quad.object)
    return (s, s_type, quad.predicate.value, o, o_type, lang, dtype)


def _node(value: str, res_type: str):
    return (
        ox.NamedNode(value)
        if res_type == "uri"
        else ox.BlankNode(value.removeprefix("_:"))
    )


def _quad_of(row: Row, with_graph: bool = True):
    s, s_type, p, o, o_type, lang, dtype = row
    subject = _node(s, s_type)
    if o_type == "literal":
        if lang:
            obj = ox.Literal(o, language=lang)
        elif dtype:
            obj = ox.Literal(o, datatype=ox.NamedNode(dtype))
        else:
            obj = ox.Literal(o)
    else:
        obj = _node(o, o_type)
    if with_graph:
        return ox.Quad(subject, ox.NamedNode(p), obj, ox.DefaultGraph())
    return ox.Triple(subject, ox.NamedNode(p), obj)


def streaming_parse(
    source: str | Path | bytes,
    fmt: str | None = None,
    prefixes: dict[str, str] | None = None,
) -> Iterator[Row]:
    """Parse an RDF file (or bytes, with an explicit format) into staging rows, one at a
    time: the file is never held in memory (`Graph.parse` and `Graph.of_rows` are the
    in-memory ones). When a dict is passed as `prefixes` it is filled with the prefixes
    the file declares, once the iteration is complete."""
    if isinstance(source, bytes):
        if fmt is None:
            raise RdfError("a format is required to parse bytes")
        parsed = ox.parse(source, format=rdf_format(fmt))
    else:
        fmt = fmt or guess_format(source)
        parsed = ox.parse(path=str(source), format=rdf_format(fmt))
    try:
        for quad in parsed:
            yield _row_of(quad)
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
    """Serialize staging-shaped rows one at a time (`Graph.serialize` is the in-memory
    one). Returns the text, or None when `output` is a path. Rows carry no graph name:
    everything goes to the default graph."""
    rdf_fmt = rdf_format(fmt)
    with_graph = rdf_fmt.supports_datasets
    quads = (_quad_of(row, with_graph) for row in rows)
    try:
        if output is None:
            return serialize_bytes(quads, format=rdf_fmt, prefixes=prefixes).decode(
                "utf-8"
            )
        ox.serialize(quads, str(output), format=rdf_fmt, prefixes=prefixes)
    except (ValueError, OSError) as err:
        raise RdfError(f"cannot write {fmt}: {err}") from err
    return None


def _term(term):
    return ox.NamedNode(term) if isinstance(term, str) else term


def serialize_bytes(data, **kwargs) -> bytes:
    """`pyoxigraph.serialize` to memory: without an output it gives bytes."""
    out = ox.serialize(data, **kwargs)
    if out is None:
        raise ValueError("serialize_bytes takes no output")
    return out


class Graph:
    def __init__(self, dataset: ox.Dataset | None = None):
        self.dataset = ox.Dataset() if dataset is None else dataset

    @classmethod
    def parse(cls, text: str, fmt: str) -> "Graph":
        """The text as a graph (everything in the default graph); `ParseError` when it
        is not valid `fmt` (`OUTPUT_FORMATS` names them: turtle, ntriples, rdfxml,
        jsonld, ...)."""
        try:
            quads = list(ox.parse(text.encode("utf-8"), format=rdf_format(fmt)))
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
    def of_rows(cls, rows: Iterable["Row"]) -> "Graph":
        """A new graph from rows (what the store returns)."""
        return cls(ox.Dataset(_quad_of(row, True) for row in rows))

    def rows(self) -> Iterator["Row"]:
        """The quads as rows (for the store's staging table)."""
        return (_row_of(quad) for quad in self.dataset)

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

    def copy(self) -> "Graph":
        return Graph(ox.Dataset(self.dataset))

    # ---- reading
    # -------------------------------------------------------------------------

    def objects(self, subject, predicate) -> list:
        """The objects of `subject predicate ?o`, in any graph."""
        subject, predicate = _term(subject), _term(predicate)
        return [
            q.object
            for q in self.dataset.quads_for_subject(subject)
            if q.predicate == predicate
        ]

    def value(self, subject, predicate):
        """The first object of `subject predicate ?o`, or None."""
        found = self.objects(subject, predicate)
        return found[0] if found else None

    def subjects(self) -> list[ox.NamedNode]:
        """The resources: the distinct uri subjects, in the order first seen."""
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

    def extract(self, uri) -> "Graph":
        """The resource `uri`: its quads and those of the blank nodes below it. Empty
        when there is no such subject."""
        return Graph(ox.Dataset(self._resource(_term(uri))))

    def orphaned_nodes(self) -> "Graph":
        """The quads that belong to no resource: blank nodes that no uri subject
        reaches."""
        covered: set[ox.Quad] = set()
        for root in self.subjects():
            covered |= self._resource(root)
        return Graph(ox.Dataset(q for q in self.dataset if q not in covered))

    def walk_list(self, head) -> list:
        """The items of the rdf list that starts at `head` (rdf:first / rdf:rest cells,
        as the store returns them), in order. A broken or cyclic list ends where it
        breaks."""
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
        """The graph name of the first quad of `subject`, or None when it has none."""
        return next(
            (q.graph_name for q in self.dataset.quads_for_subject(_term(subject))), None
        )

    def replace(self, subject, predicate, new, graph=None) -> None:
        """Make `new` the one value of `subject predicate ?o` (a term; None: no value).
        It is written in `graph`, else in the graph the subject is in, else in the
        default graph."""
        subject, predicate = _term(subject), _term(predicate)
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

    def add_value(self, subject, predicate, term, graph=None) -> None:
        """One more value of `subject predicate ?o`, in `graph`, else in the graph the
        subject is in, else in the default graph. (`add` is the dataset's, and takes a
        quad.)"""
        subject = _term(subject)
        if graph is None:
            graph = self.graph_name_of(subject) or ox.DefaultGraph()
        self.dataset.add(ox.Quad(subject, _term(predicate), term, graph))

    # ---- whole graphs
    # --------------------------------------------------------------------

    def without(self, predicates) -> "Graph":
        """A new graph without the quads whose predicate is one of `predicates`."""
        hidden = {_term(p) for p in predicates}
        return Graph(ox.Dataset(q for q in self.dataset if q.predicate not in hidden))

    def remap(self, old, new) -> "Graph":
        """A new graph with the uri `old` replaced by `new`, as subject and as
        object."""
        old, new = _term(old), _term(new)

        def swap(term):
            return new if term == old else term

        return Graph(
            ox.Dataset(
                ox.Quad(swap(q.subject), q.predicate, swap(q.object), q.graph_name)
                for q in self.dataset
            )
        )

    def patch(self, before, after) -> None:
        """Take out the quads that are in `before` and not in `after`, and put in those
        that are in `after` and not in `before`: what was done to a copy, done to
        this."""
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
        """The graph as text (the graph names are left out). The same triples give the
        same text: the resources come before blank nodes, the type of each first (a
        serializer writes a typed node when the type comes first), then by property and
        value. Turtle and rdf/xml declare only the `prefixes` that the text uses (they
        would declare all of them); rdf/xml is then nested by the stylesheet (blank
        nodes inside what points to them; `xslt_params` are its parameters), and json-ld
        indented."""
        # the triple formats refuse a graph name, so a quad that has one is written
        # without it
        quads = sorted(map(_flat, self.dataset), key=_order)
        text = serialize_bytes(quads, format=rdf_format(fmt), prefixes=prefixes).decode(
            "utf-8"
        )
        if fmt == "turtle":
            return _only_used_turtle_prefixes(text)
        if fmt == "rdfxml":
            return xslt.nest(_only_used_xml_prefixes(text), xslt_params)
        if fmt == "jsonld":  # the serializer writes one line
            return json.dumps(json.loads(text), indent=2, ensure_ascii=False)
        return text

    # ---- SHACL
    # ---------------------------------------------------------------------------

    def validate(
        self,
        shapes: "Graph | ox.Dataset | str | Path",
        shape: "str | ox.NamedNode | None" = None,
    ) -> Report:
        """Validate this graph against a SHACL shapes graph (a file, Turtle text, a
        graph or a dataset), with pyrudof.

            report = graph.validate("shapes.ttl") report = graph.validate(shapes,
            shape=PERSON_SHAPE)   # force a shape on every resource report.conforms
            report.by_resource()            # {resource uri: [Violation, ...]}

        Only this graph is validated: a shape that checks something outside it (sh:class
        on a reference to a resource that is not in the graph) cannot see that resource.

        Graph labels are ignored: the quads are validated as one graph. Shapes find
        their resources through their own targets (sh:targetClass and so on), so a
        resource that no shape targets, for instance one without an rdf:type, is not
        checked. With `shape`, that shape is also targeted at every resource (uri
        subject) in the dataset, whatever it is typed as.

        The cost is mostly per call (about 20 ms), so validate resources in batches.
        """
        import pyrudof  # about 30 ms to load: only when something is validated

        shapes_graph = _shapes_graph(shapes)
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
                    message=_clean(entry.message),
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
        """Raise `ValueError` with the reason when `query` is not one that `select`
        runs: it has to be one SELECT that fetches nothing from outside (no `SERVICE`),
        and may use `prefixes` without declaring them. The answer is remembered for the
        text and the prefixes."""
        problem = _select_problem(query, tuple(sorted(prefixes.items())))
        if problem is not None:
            raise ValueError(problem)

    def select(self, query: str, prefixes: dict[str, str]) -> ox.QuerySolutions:
        """Run a SELECT on this graph, in any graph name, and nothing else (the quads go
        into a new in-memory store as they are). `ValueError` when it is not a query
        that can be run (see `validate_select`)."""
        self.validate_select(query, prefixes)
        store = ox.Store()
        store.extend(self.dataset)
        return store.query(query, prefixes=prefixes, use_default_graph_as_union=True)  # ty: ignore[invalid-explicit-return]


def _flat(quad: ox.Quad) -> ox.Quad:
    if isinstance(quad.graph_name, ox.DefaultGraph):
        return quad  # as it is
    return ox.Quad(quad.subject, quad.predicate, quad.object, ox.DefaultGraph())


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


_TURTLE_PREFIX = re.compile(r"^@prefix ([^\s:]*): <[^>]*> \.\n", re.M)
_LEFT_OUT = re.compile(r'"""(?:.|\n)*?"""|"(?:[^"\\\n]|\\.)*"|<[^<>\s]*>|#[^\n]*')
_XMLNS = re.compile(r'\s+xmlns:([\w.-]+)="[^"]*"')


def _only_used_turtle_prefixes(text: str) -> str:
    declared = list(_TURTLE_PREFIX.finditer(text))
    if not declared:
        return text
    body = text[declared[-1].end() :]
    code = _LEFT_OUT.sub(" ", body)  # not what is inside a string or a URI
    drop = [
        m for m in declared if not re.search(rf"(?<![\w.-]){re.escape(m[1])}:", code)
    ]
    for match in reversed(drop):
        text = text[: match.start()] + text[match.end() :]
    return text


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


def _missing_prefixes(text: str, known: tuple) -> list[str]:
    """The prefixes a query uses that neither the repository nor the query itself
    defines."""
    declared = {name for name, _ in known} | set(
        re.findall(r"(?i)\bPREFIX\s+([^\s:]*):", text)
    )
    code = re.sub(
        r"<[^<>\s]*>|\"[^\"]*\"|'[^']*'|#[^\n]*", " ", text
    )  # URIs, strings, comments
    used = re.findall(r"(?<![\w:])([A-Za-z][\w.-]*):", code)
    return sorted({name for name in used if name not in declared})


@functools.lru_cache(maxsize=512)
def _select_problem(text: str, prefixes: tuple) -> str | None:
    """Why a text cannot be run (None when it can), remembered for the text and the
    prefixes: a type has one query, and every record that is saved asks."""
    if re.search(r"\bSERVICE\b", text, re.IGNORECASE):
        return "A query cannot use SERVICE"
    try:
        result = ox.Store().query(text, prefixes=dict(prefixes))
    except SyntaxError as err:
        missing = _missing_prefixes(text, prefixes)
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


def _shapes_graph(shapes: "Graph | ox.Dataset | str | Path") -> Graph:
    """A shapes graph, a new one (it is added to). A Path (or a str naming an existing
    file) is read, the format guessed from the extension; any other str is taken as
    Turtle text."""
    if isinstance(shapes, Graph | ox.Dataset):
        return Graph(ox.Dataset(shapes))
    if isinstance(shapes, Path) or ("\n" not in shapes and Path(shapes).exists()):
        return Graph.of_rows(streaming_parse(shapes))
    return Graph(
        ox.Dataset(ox.parse(shapes.encode("utf-8"), format=ox.RdfFormat.TURTLE))
    )


def _clean(message: str) -> str:
    return message.strip().strip('"')
