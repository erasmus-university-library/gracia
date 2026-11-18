import hashlib
import json
import re
from collections import Counter
from pathlib import Path

from gracia.graph import Graph, streaming_parse, streaming_serialize


def must_search(pattern: str, text: str, flags: int = 0) -> re.Match:
    """`re.search`, but a page that lacks what the test looks for is a failure that says
    so."""
    found = re.search(pattern, text, flags)
    if found is None:
        raise AssertionError(f"{pattern!r} not found in: {text[:300]!r}")
    return found


RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"


# triple = (s_type, s, p, o_type, o, lang, datatype)
def parse_json(text):
    """json-triples as test triples."""
    out = []
    for t in json.loads(text)["triples"]:
        s, p, o = t["subject"], t["predicate"], t["object"]
        out.append(
            (
                s["type"],
                s["value"],
                p["value"],
                o["type"],
                o["value"],
                o.get("lang"),
                o.get("datatype"),
            )
        )
    return out


def prune(triples):
    """Apply the import's semantics to the expected graph: copy each bnode once
    per path from a uri resource (shared bnodes are duplicated) and drop what
    the table cannot hold (bnodes without a leaf, unreachable bnodes)."""
    by_s = {}
    for t in triples:
        if t[0] == "bnode":
            by_s.setdefault(t[1], []).append(t)
    # live bnodes: have a real leaf, or a live bnode child (rest->nil is no leaf)
    live, changed = set(), True
    while changed:
        changed = False
        for b, ts in by_s.items():
            if b in live:
                continue
            for t in ts:
                ok = (
                    t[4] in live
                    if t[3] == "bnode"
                    else not (t[2] == RDF + "rest" and t[4] == RDF + "nil")
                )
                if ok:
                    live.add(b)
                    changed = True
                    break
    out, counter = [], [0]

    def copy(b, stack):
        if b in stack:
            raise SystemExit("bnode cycle in input: not supported by this check")
        counter[0] += 1
        new = f"copy{counter[0]}"
        for t in by_s.get(b, []):
            if t[3] == "bnode":
                if t[4] in live:
                    out.append(
                        (
                            "bnode",
                            new,
                            t[2],
                            "bnode",
                            copy(t[4], stack | {b}),
                            None,
                            None,
                        )
                    )
            else:
                out.append(("bnode", new, *t[2:]))
        return new

    for t in triples:
        if t[0] == "uri":
            if t[3] != "bnode":
                out.append(t)
            elif t[4] in live:
                out.append((*t[:4], copy(t[4], frozenset()), *t[5:]))
    return out


def _digest(value) -> str:
    return hashlib.sha1(repr(value).encode()).hexdigest()[:16]


def _term(color: dict, res_type: str, value: str):
    return ("b", color[value]) if res_type == "bnode" else (res_type, value)


def canonical(triples):
    """Relabel bnodes by iterated neighbourhood hashing; return a Counter."""
    bn = {t[1] for t in triples if t[0] == "bnode"} | {
        t[4] for t in triples if t[3] == "bnode"
    }
    color = dict.fromkeys(bn, "0")
    for rnd in range(len(bn) + 2):
        sig = {b: [] for b in bn}
        for st, sv, p, ot, ov, lang, dt in triples:
            if st == "bnode":
                sig[sv].append(("out", p, _term(color, ot, ov), lang, dt))
            if ot == "bnode":
                sig[ov].append(("in", p, _term(color, st, sv)))
        new = {b: _digest((color[b], sorted(map(repr, sig[b])))) for b in bn}
        stable = len(set(new.values())) == len(set(color.values())) and rnd > 0
        color = new
        if stable:
            break

    def label(res_type: str, value: str) -> str:
        return "_:" + color[value] if res_type == "bnode" else value

    return Counter(
        (label(st, sv), p, ot, label(ot, ov), lang, dt)
        for st, sv, p, ot, ov, lang, dt in triples
    )


def triples_from_rows(rows):
    """Staging rows (`Graph.rows`) as test triples."""
    return [
        (s_type, s, p, o_type, o, lang, dt)
        for s, s_type, p, o, o_type, lang, dt in rows
    ]


def triples_from_file(path):
    """The graph of an RDF file (.json = json-triples), as test triples."""
    path = Path(path)
    if path.suffix == ".json":
        return parse_json(path.read_text(encoding="utf-8"))
    return triples_from_rows(streaming_parse(path))


def flat_rdfxml_from_file(path, prefixes: dict[str, str] | None = None) -> str:
    """The graph of an RDF file as flat RDF/XML, the way the Dataset serializer writes
    it (bnodes keep their sharing: no unsharing as in the database)."""
    return streaming_serialize(streaming_parse(path), "rdfxml", prefixes=prefixes)  # ty: ignore[invalid-explicit-return]


def triples_from_rdfxml(text: str):
    return triples_from_rows(streaming_parse(text.encode("utf-8"), "rdfxml"))


def same(expected, actual, unshare_expected: bool = True) -> bool:
    if unshare_expected:
        expected = prune(expected)
    return canonical(expected) == canonical(actual)


def diff(expected, actual, unshare_expected: bool = True) -> str:
    if unshare_expected:
        expected = prune(expected)
    a, b = canonical(expected), canonical(actual)
    return "\n".join(
        [f"missing: {t}" for t in list(a - b)[:10]]
        + [f"extra: {t}" for t in list(b - a)[:10]]
    )


def validate(dataset, shapes, shape=None):
    """`Graph.validate` for something that may still be a plain pyoxigraph Dataset."""
    graph = dataset if isinstance(dataset, Graph) else Graph(dataset)
    return graph.validate(shapes, shape=shape)
