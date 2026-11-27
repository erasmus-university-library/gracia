import dataclasses
import json
from dataclasses import dataclass

import pyoxigraph as ox

from gracia import forms, uri
from gracia.graph import Graph
from gracia.store import literal

IRI_TEXT = "^" + forms.IRI_TEXT.removesuffix("\\Z") + "$"  # as SQL has it
HEADER_STRIP = " \t\n,;:"  # (template.js does the same)


# A property shape with `gracia:propertyTemplate ("{} {}" schema:givenName
# schema:familyName)` says that the property is made of the others: the text, with each
# {} replaced by the value of the next property of the list, and trimmed. [ ... ] is
# optional: left out when one of the {} inside it has no value. Doubled, {{ }} [[ ]] are
# the braces and brackets themselves. All the properties are of the node that the
# property is in, and have one plain value each. The property is written (and replaces
# what it had) when every {} outside the brackets has a value (not empty); otherwise it
# is left alone.

Piece = tuple  # ("text", str) | ("source", index) | ("optional", (Piece, ...))


def parse(text: str, count: int) -> tuple[Piece, ...]:
    stack: list[list[Piece]] = [[]]  # the pieces of the text, of the group that is open
    found = 0
    buffer = ""

    def flush() -> None:
        nonlocal buffer
        if buffer:
            stack[-1].append(("text", buffer))
            buffer = ""

    i = 0
    while i < len(text):
        char, after = text[i], text[i + 1 : i + 2]
        if char in "{}[]" and after == char:
            buffer += char  # the doubled one is the character itself
            i += 2
        elif char == "{":
            if after != "}":
                raise ValueError(f"{text!r}: a {{ must be doubled or be {{}}")
            flush()
            stack[-1].append(("source", found))
            found += 1
            i += 2
        elif char == "[":
            if len(stack) > 1:
                raise ValueError(f"{text!r}: the optional parts cannot be nested")
            flush()
            stack.append([])
            i += 1
        elif char == "]":
            if len(stack) == 1:
                raise ValueError(f"{text!r}: a ] that closes nothing")
            flush()
            group = tuple(stack.pop())
            if not any(piece[0] == "source" for piece in group):
                raise ValueError(f"{text!r}: an optional part has no {{}} in it")
            stack[-1].append(("optional", group))
            i += 1
        elif char == "}":
            raise ValueError(f"{text!r}: a }} must be doubled")
        else:
            buffer += char
            i += 1
    if len(stack) > 1:
        raise ValueError(f"{text!r}: a [ that is not closed")
    flush()
    if found != count:
        raise ValueError(f"{text!r} has {found} {{}} for {count} properties")
    return tuple(stack[0])


def _sources_of(pieces) -> list[int]:
    found = []
    for piece in pieces:
        if piece[0] == "source":
            found.append(piece[1])
        elif piece[0] == "optional":
            found += _sources_of(piece[1])
    return found


@dataclass(frozen=True)
class PropertyTemplate:
    target: str
    sources: tuple[str, ...]
    pieces: tuple[Piece, ...]
    strip: str | None = None  # what the text is trimmed of (none: white space)
    # only where this property of the node has this value (the others are for other
    # values): (property, value)
    when: tuple[str, str] | None = None
    iri: bool = False  # the property is an IRI: the text is one

    @property
    def required(self) -> list[str]:
        return [
            self.sources[i]
            for i in _sources_of(p for p in self.pieces if p[0] == "source")
        ]

    def render(self, values: dict[str, str | None]) -> str | None:
        def fill(pieces) -> str | None:
            out = ""
            for kind, value in pieces:
                if kind == "text":
                    out += value
                elif kind == "source":
                    text = (values.get(self.sources[value]) or "").strip()
                    if not text:
                        return None
                    out += text
                else:
                    out += fill(value) or ""
            return out

        made = fill(self.pieces)
        return (made.strip(self.strip) or None) if made is not None else None

    def render_names(self, values: dict) -> str | None:
        def text(value) -> str | None:
            return value.get("value") if isinstance(value, dict) else value

        return self.render(
            {s: text(values.get(forms.local_name(s))) for s in self.sources}
        )

    def as_json(self) -> str:
        def piece(found):
            kind, value = found
            if kind == "source":
                return ["source", forms.local_name(self.sources[value])]
            if kind == "optional":
                return ["optional", [piece(p) for p in value]]
            return [kind, value]

        return json.dumps(
            [piece(p) for p in self.pieces], ensure_ascii=False, separators=(",", ":")
        )

    def made_sql(self, context) -> str:
        flat, of_type = context.flat, context.of_type
        names = [f"v{i}" for i in range(len(self.sources))]

        def expression(pieces) -> str:
            parts = []
            for kind, value in pieces:
                if kind == "text":
                    parts.append(literal(value))
                elif kind == "source":
                    parts.append(names[value])
                else:
                    present = " AND ".join(
                        f"{names[i]} IS NOT NULL" for i in _sources_of(value)
                    )
                    parts.append(
                        f"CASE WHEN {present} THEN {expression(value)} ELSE '' END"
                    )
            return f"concat({', '.join(parts)})"

        used = (*self.sources, *([self.when[0]] if self.when else []))
        columns = [
            f"max(NULLIF(trim(lit), '')) FILTER (WHERE p = {literal(p)}) AS {name}"
            for p, name in zip(self.sources, names, strict=True)
        ]
        conditions = [
            f"{names[i]} IS NOT NULL"
            for i in _sources_of(p for p in self.pieces if p[0] == "source")
        ]
        if self.when:
            columns.append(
                f"max(lit) FILTER (WHERE p = {literal(self.when[0])}) AS discriminator"
            )
            conditions.append(f"discriminator = {literal(self.when[1])}")
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        # an IRI has to be one: the text of a value that is not fit for it makes none
        usable = f" AND regexp_matches(text, {literal(IRI_TEXT)})" if self.iri else ""
        made = f"""SELECT s, loc, text FROM (
                SELECT s, loc, trim({expression(self.pieces)}) AS text FROM (
                    SELECT s, loc, {", ".join(columns)}
                    FROM {flat}
                    WHERE {of_type} AND p IN ({", ".join(literal(p) for p in used)})
                      AND lit IS NOT NULL
                    GROUP BY s, loc
                ){where}
            ) WHERE text <> ''{usable}"""
        return made

    def sql(self, context) -> list[str]:
        return sql_of([self], context)


# the statements for the templates of one property (one for each value of another
# property, perhaps): one delete and one insert of what they all make, since each
# statement is a round trip to the database
def sql_of(variants: list[PropertyTemplate], context) -> list[str]:
    flat, of_type = context.flat, context.of_type
    target = literal(variants[0].target)
    made = " UNION ALL ".join(f"({t.made_sql(context)})" for t in variants)
    values = "text, NULL" if variants[0].iri else "NULL, text"  # the columns o and lit
    return [
        f"""DELETE FROM {flat} WHERE p = {target} AND {of_type}
            AND (s, loc) IN (SELECT s, loc FROM ({made}))""",
        f"""INSERT INTO {flat} SELECT s, loc, {target}, {values}, NULL, NULL
            FROM ({made})""",
    ]


def _list(shapes: Graph, head) -> list:
    found = []
    while isinstance(head, ox.BlankNode):
        found += shapes.objects(head, ox.NamedNode(forms.RDF_NS + "first"))[:1]
        rest = shapes.objects(head, ox.NamedNode(forms.RDF_NS + "rest"))
        head = rest[0] if rest else None
    return found


def _from_list(shapes: Graph, heads, target: str) -> PropertyTemplate:
    items = _list(shapes, heads[0])
    if len(heads) != 1 or len(items) < 2 or not isinstance(items[0], ox.Literal):
        raise ValueError(
            f"{target or 'a shape'}: a template is a list of a text and the "
            f"properties it uses"
        )
    sources = items[1:]
    if not all(isinstance(s, ox.NamedNode) for s in sources):
        raise ValueError(f"{target}: the template names its properties by their uri")
    names = tuple(s.value for s in sources)
    return PropertyTemplate(target, names, parse(items[0].value, len(names)))


# A template can also be in an alternative of an sh:or of the node shape, on a property
# of it: it is then for the nodes that have that alternative, which says which by the
# property that has an sh:hasValue in it (and the alternative can say what the property
# has to look like, with sh:pattern, for the shape to check):
#   sh:or ( [ sh:property [ sh:path schema:propertyID ; sh:hasValue "ORCID" ] ;
#             sh:property [ sh:path schema:url ; sh:pattern "^https://orcid\\.org/" ;
#               gracia:propertyTemplate ( "https://orcid.org/{}" schema:value ) ] ] )
def _of_alternatives(shapes: Graph, shape) -> list[PropertyTemplate]:
    found = []
    for head in shapes.objects(shape, ox.NamedNode(forms.SH_OR)):
        for alternative in _list(shapes, head):
            props = shapes.objects(alternative, ox.NamedNode(forms.SH_PROPERTY))
            guards = [
                (shapes.objects(p, ox.NamedNode(forms.SH_PATH))[0].value, v.value)
                for p in props
                for v in shapes.objects(p, ox.NamedNode(forms.SH_HAS_VALUE))
            ]
            for prop in props:
                heads = shapes.objects(prop, ox.NamedNode(uri.PROPERTY_TEMPLATE))
                if not heads:
                    continue
                target = shapes.objects(prop, ox.NamedNode(forms.SH_PATH))[0].value
                if len(guards) != 1:
                    raise ValueError(
                        f"{target}: a template in an alternative needs the alternative "
                        f"to have one property with sh:hasValue"
                    )
                template = _from_list(shapes, heads, target)
                found.append(dataclasses.replace(template, when=guards[0]))
    return found


def of_shape(shapes: Graph, shape) -> list[PropertyTemplate]:
    properties = dict(forms.properties(shapes, shape))
    found = []
    for path, prop in properties.items():
        heads = shapes.objects(prop, ox.NamedNode(uri.PROPERTY_TEMPLATE))
        if heads:
            found.append(_from_list(shapes, heads, path))
    found += _of_alternatives(shapes, shape)
    targets = {t.target for t in found}
    for template in found:
        if template.target not in properties:
            raise ValueError(f"{template.target}: not a property of the shape")
    found = [
        dataclasses.replace(t, iri=forms.is_iri(shapes, properties[t.target]))
        for t in found
    ]
    for template in found:
        for source in (
            *template.sources,
            *([template.when[0]] if template.when else []),
        ):
            if source == template.target or source in targets:
                raise ValueError(
                    f"{template.target}: {source} cannot be made by a template too"
                )
            if source not in properties or not forms.single_literal(
                shapes, properties[source]
            ):
                raise ValueError(
                    f"{template.target}: {source} must be a property of the same "
                    f"shape with one plain value (sh:maxCount 1, no sh:node)"
                )
    return found


# what a form field that is made of others is given for the browser: the templates, each
# with the property it is for (or null) and its value
def variants_json(variants: list[PropertyTemplate]) -> str:
    return json.dumps(
        [
            {
                "when": ([forms.local_name(t.when[0]), t.when[1]] if t.when else None),
                "pieces": json.loads(t.as_json()),
            }
            for t in variants
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )


# the templates of the bar of a node (gracia:headerTemplate, and
# gracia:headerAddOnTemplate for the badge before it) in the form: the same text, but
# only for people to see, so no SQL; the properties are those of the shape
def header_of(
    shapes: Graph, shape
) -> tuple[PropertyTemplate | None, PropertyTemplate | None]:
    properties = {path for path, _prop in forms.properties(shapes, shape)}
    found = []
    for predicate in (uri.HEADER_TEMPLATE, uri.HEADER_ADDON_TEMPLATE):
        heads = shapes.objects(shape, ox.NamedNode(predicate))
        template = _from_list(shapes, heads, "") if heads else None
        if template:  # the separators that are left over are not shown
            template = dataclasses.replace(template, strip=HEADER_STRIP)
        for source in template.sources if template else ():
            if source not in properties:
                raise ValueError(
                    f"{predicate}: {source} is not a property of the shape"
                )
        found.append(template)
    return found[0], found[1]
