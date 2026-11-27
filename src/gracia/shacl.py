from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass

import pyoxigraph as ox

from gracia import uri
from gracia.graph import Graph
from gracia.uri import G_NS, PROPERTY_TEMPLATE, SH_NS, local_name, uri_label
from gracia.utils import dates

DASH_ICON_ROLE = ox.NamedNode("http://datashapes.org/dash#IconRole")
PROPERTY_ROLE = ox.NamedNode("http://datashapes.org/dash#propertyRole")
SH_OR, SH_PROPERTY, SH_PATH = (SH_NS["or"], SH_NS.property, SH_NS.path)
SH_HAS_VALUE = SH_NS.hasValue
RDF_NS = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"
RDF_TYPE = RDF_NS + "type"
LANG_STRING = ox.NamedNode(RDF_NS + "langString")
DASH = "http://datashapes.org/dash#"
DASH_TEXT_AREA = ox.NamedNode(DASH + "TextAreaEditor")  # a text area of four lines
DASH_URI = ox.NamedNode(
    DASH + "URIEditor"
)  # an IRI, as text (no widget of its own yet)
LIST_EDITOR = ox.NamedNode(
    G_NS.URIListEditor
)  # an rdf:list of IRIs, in the order the form puts them in
ROLE_NAMES = {
    DASH + "LabelRole": "label",
    DASH + "KeyInfoRole": "key_info",
    DASH + "DescriptionRole": "description",
}
UNIQUE_VALUE_FOR_CLASS = DASH + "uniqueValueForClass"


class Shapes(Graph):
    """A graph of SHACL shapes (and the dash terms around them), read for what a form,
    a page and a template need of a shape."""

    # What a shape says of the things in a choice of URIs, when it describes them (an
    # rdfs:label and an icon, as a shape has them): the name and the icon of each.
    def describe(self, uris) -> dict[str, dict]:
        found = {}
        for value in uris:
            node = ox.NamedNode(value)
            labels = [
                o.value
                for o in self.objects(node, ox.NamedNode(RDFS_LABEL))
                if isinstance(o, ox.Literal)
            ]
            found[value] = {
                "label": labels[0] if labels else uri_label(value),
                "icon": self.icon(node),
            }
        return found

    # the shapes that say what a reference points at: the `sh:node` of the property, or
    # of
    # the alternatives of its `sh:or`; of a list (an rdf:list of references) the shapes
    # of
    # its items, which the shape of the list says as the `sh:node` of `rdf:first`
    def reference_shapes(self, prop, shape=None) -> list[ox.NamedNode]:
        def named(subject) -> list[ox.NamedNode]:
            return [
                o for o in self._objects(subject, "node") if isinstance(o, ox.NamedNode)
            ]

        def alternatives(subject) -> list:
            found = [subject]
            for head in self._objects(subject, "or"):
                found += self._list_items(head)
            return found

        if not self.is_list(prop):
            found = [s for a in alternatives(prop) for s in named(a)]
            # and what the alternatives of its shape say it points at (a contributor's
            # role)
            paths = self._objects(prop, "path")
            for head in self._objects(shape, "or") if shape is not None else []:
                for alternative in self._list_items(head):
                    for given in self._alternative_parts(alternative)[1].get(
                        paths[0].value if len(paths) == 1 else "", []
                    ):
                        found.append(given)
            return list(dict.fromkeys(found))
        first = ox.NamedNode(RDF_NS + "first")
        found: list[ox.NamedNode] = []
        for list_shape in named(prop):
            for part in alternatives(list_shape):
                for item in self._objects(part, "property"):
                    if first in self._objects(item, "path"):
                        found += [s for a in alternatives(item) for s in named(a)]
        return found

    def _objects(self, subject, predicate):
        return self.objects(subject, ox.NamedNode(SH_NS[predicate]))

    def _value(self, subject, predicate):
        objects = self._objects(subject, predicate)
        return objects[0].value if objects else None

    def _editable(self, prop, many: bool = False) -> bool:
        if self.is_list(prop) and not many:
            return False  # the one value is a list: the rows of it are the fields
        if (self._objects(prop, "node") and not self.by_uri(prop)) or (
            not many and self._value(prop, "maxCount") != "1"
        ):
            return False
        kinds = self._objects(prop, "nodeKind")
        if ox.NamedNode(SH_NS.BlankNode) in kinds:
            return False
        # an IRI is typed as text, when the shape says it is meant to be
        # (dash:URIEditor)
        return (
            ox.NamedNode(SH_NS.IRI) not in kinds
            or self.has_editor(prop, DASH_URI)
            or bool(self._objects(prop, "in"))  # a choice of IRIs (the types)
        )

    def single_literal(self, prop) -> bool:
        return (
            self._value(prop, "maxCount") == "1"
            and not self._objects(prop, "node")
            and not self.is_iri(prop)
        )

    def property_title(self, prop, path: str) -> str:
        return self._value(prop, "name") or path

    def is_iri(self, prop) -> bool:
        return ox.NamedNode(SH_NS.IRI) in self._objects(
            prop, "nodeKind"
        ) or self.by_uri(prop)

    def shape_description(self, shape) -> str:
        return self._value(shape, "description") or ""

    # the name of the group a property is in (`sh:group` of a `sh:PropertyGroup`, which
    # has
    # an rdfs:label): the form shows the properties of a group next to each other in a
    # box
    def property_group(self, prop) -> str | None:
        for group in self._objects(prop, "group"):
            labels = [
                o.value
                for o in self.objects(group, ox.NamedNode(RDFS_LABEL))
                if isinstance(o, ox.Literal)
            ]
            return labels[0] if labels else local_name(group.value)
        return None

    def properties(self, shape) -> list[tuple[str, ox.NamedNode | ox.BlankNode]]:
        found = []
        for prop in self._objects(shape, "property"):
            path = self._objects(prop, "path")
            if len(path) != 1 or not isinstance(path[0], ox.NamedNode):
                continue  # property paths are not supported yet
            order = self._value(prop, "order")
            found.append((float(order) if order else 1e9, path[0].value, prop))
        return [(path, prop) for _, path, prop in sorted(found, key=lambda p: p[:2])]

    def cardinality(self, prop) -> tuple[int, int | None]:
        highest = self._value(prop, "maxCount")
        return int(self._value(prop, "minCount") or 0), int(
            highest
        ) if highest else None

    def nested_class(self, shape) -> str | None:
        for predicate in ("class", "targetClass"):
            found = [
                o
                for o in self._objects(shape, predicate)
                if isinstance(o, ox.NamedNode)
            ]
            if found:
                return found[0].value
        return None

    # the name of a Bootstrap icon (without "bi-") that a shape has with the icon role
    # (its
    # sh:defaultValue); a property shape with none has the one of the node it points at
    def icon(self, *candidates) -> str | None:
        for shape in candidates:
            if shape is not None and DASH_ICON_ROLE in self.objects(
                shape, PROPERTY_ROLE
            ):
                name = self._value(shape, "defaultValue")
                if name:
                    return name
        return None

    def nested_shape(self, prop):
        if self.by_uri(prop):  # a reference to records, not a form
            return None
        self = self._objects(prop, "node")
        return self[0] if self else None

    def _list_items(self, head) -> list:
        found = []
        while isinstance(head, ox.BlankNode):
            found += self.objects(head, ox.NamedNode(RDF_NS + "first"))[:1]
            rest = self.objects(head, ox.NamedNode(RDF_NS + "rest"))
            head = rest[0] if rest else None
        return found

    # a property whose shape allows language-tagged strings (sh:datatype rdf:langString,
    # on
    # the property or in one of the alternatives of its sh:or, or sh:languageIn)
    def allows_language(self, prop) -> bool:
        if self._objects(prop, "languageIn"):
            return True
        alternatives = [prop]
        for head in self._objects(prop, "or"):
            alternatives += self._list_items(head)
        return any(
            LANG_STRING in self._objects(alternative, "datatype")
            for alternative in alternatives
        )

    # the datatypes that a property allows (on it, or in the alternatives of its sh:or)
    def datatypes(self, prop) -> list[str]:
        alternatives = [prop]
        for head in self._objects(prop, "or"):
            alternatives += self._list_items(head)
        return [d.value for a in alternatives for d in self._objects(a, "datatype")]

    # the date datatypes of a property that has dates and nothing else (xsd:date,
    # gYearMonth, gYear): it gets a date widget
    def date_kinds(self, prop) -> tuple[str, ...]:
        found = self.datatypes(prop)
        if found and all(d in dates.KINDS for d in found):
            return tuple(d for d in dates.KINDS if d in found)
        return ()

    def has_editor(self, prop, editor) -> bool:
        return editor in self.objects(prop, ox.NamedNode(DASH + "editor"))

    # a property whose one value is an rdf:list of IRIs (its shape says what the list
    # is)
    def is_list(self, prop) -> bool:
        return self.has_editor(prop, LIST_EDITOR)

    # a reference to records, by URI: what its shape (sh:node) says is not a form to
    # fill in
    def by_uri(self, prop) -> bool:
        return self.has_editor(prop, DASH_URI) or self.is_list(prop)

    def wants_text_area(self, prop) -> bool:
        return self.has_editor(prop, DASH_TEXT_AREA)

    # A reference that follows a field next to it: what it points at is what the value
    # of
    # the field says (the role of a contributor: a funder is an organization, a
    # supervisor a
    # person). The shape says it with `sh:or`: each alternative has a property with a
    # choice
    # of values (`sh:in`, `sh:hasValue`) and the property of the reference with the
    # shape of
    # what it points at (`sh:node`), so that the values of the first are for the second.
    def _alternative_parts(self, alternative):
        """(the choices in an alternative: path -> values; the self: path -> self)"""
        choices: dict[str, list[str]] = {}
        pointing: dict[str, list] = {}
        for prop in self._objects(alternative, "property"):
            paths = self._objects(prop, "path")
            if len(paths) != 1 or not isinstance(paths[0], ox.NamedNode):
                continue
            path = paths[0].value
            values = [o.value for o in self._objects(prop, "hasValue")] + [
                o.value
                for head in self._objects(prop, "in")
                for o in self._list_items(head)
            ]
            if values:
                choices[path] = values
            named = [
                o for o in self._objects(prop, "node") if isinstance(o, ox.NamedNode)
            ]
            for head in self._objects(prop, "or"):  # one or the other of the self
                for either in self._list_items(head):
                    named += [
                        o
                        for o in self._objects(either, "node")
                        if isinstance(o, ox.NamedNode)
                    ]
            if named:
                pointing[path] = named
        return choices, pointing

    def class_follow(self, shape, prop) -> tuple[str, dict[str, list]] | None:
        paths = self._objects(prop, "path")
        if len(paths) != 1:
            return None
        mine = paths[0].value
        follows, found = None, {}
        for head in self._objects(shape, "or"):
            for alternative in self._list_items(head):
                choices, pointing = self._alternative_parts(alternative)
                guards = [p for p in choices if p != mine]
                if mine in pointing and len(guards) == 1:
                    follows = guards[0]
                    for value in choices[guards[0]]:
                        found[value] = pointing[mine]
        return (follows, found) if follows and found else None

    # Choices that are only for some resources: the thesis advisor is for a thesis. The
    # shape says it with an `sh:or` in the shape of the resource, of the alternatives
    # that
    # the resource has one of the classes (a property `rdf:type` that has the value), or
    # the
    # choice is not among the values of the path (`sh:not` of a `sh:hasValue` or
    # `sh:in`, on
    # a path of the property that has the nodes and the property of the choice in them).
    # The
    # choice is left out of the menu for the others (uri_select.js), and a form that has
    # it
    # for another resource is refused (only_for_validator).
    def _rule_shapes(self, shape) -> list:
        """The shape of a resource and the self of its rules: the self that are for
        the same works (they have one of the classes of its `sh:targetClass`). A rule is
        a
        shape of its own, so that it says what is wrong in its own words."""
        own = set(self._objects(shape, "targetClass"))
        rules = [
            q.subject
            for q in self.dataset.quads_for_object(ox.NamedNode(SH_NS.NodeShape))
            if q.predicate.value == RDF_TYPE
            and q.subject != shape
            and own & set(self._objects(q.subject, "targetClass"))
        ]
        return [shape, *rules]

    def _rule_alternatives(self, shape):
        """The `sh:or` of the shape and of its rules, each as a list of alternatives."""
        for rule in self._rule_shapes(shape):
            for head in self._objects(rule, "or"):
                yield self._list_items(head)

    def only_for_rules(self, shape) -> dict[tuple[str, str], dict[str, list[str]]]:
        found: dict[tuple[str, str], dict[str, list[str]]] = {}
        for alternatives_ in self._rule_alternatives(shape):
            classes: list[str] = []
            guard = None
            for alternative in alternatives_:
                props = self._objects(alternative, "property")
                paths = self._objects(props[0], "path") if len(props) == 1 else []
                if len(paths) != 1:
                    break
                if isinstance(paths[0], ox.NamedNode) and paths[0].value == RDF_TYPE:
                    classes += [o.value for o in self._objects(props[0], "hasValue")]
                    continue
                steps = (
                    self._list_items(paths[0])
                    if isinstance(paths[0], ox.BlankNode)
                    else []
                )
                nots = self._objects(props[0], "not")
                if len(steps) != 2 or len(nots) != 1 or guard:
                    break
                values = [o.value for o in self._objects(nots[0], "hasValue")] + [
                    o.value
                    for h in self._objects(nots[0], "in")
                    for o in self._list_items(h)
                ]
                guard = ((steps[0].value, steps[1].value), values)
            else:
                if classes and guard:
                    for value in guard[1]:
                        found.setdefault(guard[0], {})[value] = classes
        return found

    # Fields that are only for some resources: the degree a thesis is in support of. The
    # same rule, of a path that is a property of the resource itself, which it has none
    # of
    # unless it is of the class (`sh:maxCount 0`): the field is left out of the form of
    # the
    # others, and what it had is not kept (apply_form).
    def fields_only_for(self, shape) -> dict[str, list[str]]:
        found: dict[str, list[str]] = {}
        for alternatives_ in self._rule_alternatives(shape):
            classes: list[str] = []
            fields: list[str] = []
            for alternative in alternatives_:
                props = self._objects(alternative, "property")
                paths = self._objects(props[0], "path") if len(props) == 1 else []
                if len(paths) != 1 or not isinstance(paths[0], ox.NamedNode):
                    break
                if paths[0].value == RDF_TYPE:
                    classes += [o.value for o in self._objects(props[0], "hasValue")]
                elif self._value(props[0], "maxCount") == "0":
                    fields.append(paths[0].value)
                else:
                    break
            else:
                if classes and len(fields) == 1:
                    found[fields[0]] = classes
        return found

    # a property that can have several values that are plain: literals (it says their
    # datatype, or that they have a language) or IRIs that are typed as text
    def is_bag(self, prop) -> bool:
        if self._value(prop, "maxCount") == "1" or not self._editable(prop, True):
            return False
        return bool(
            self._objects(prop, "datatype")
            or self.allows_language(prop)
            or (self.is_iri(prop) and self.has_editor(prop, DASH_URI))
        )

    # a property that has to be there: its own shape says so, or an alternative of an
    # sh:or
    # of the node does (a name, or the two parts it is made of)
    def needed(self, shape, path: str) -> bool:
        if path in self.required_paths(shape):
            return True
        for head in self._objects(shape, "or"):
            for alternative in self._list_items(head):
                if path in self.required_paths(alternative):
                    return True
        return False

    # The alternatives of an sh:or of a node shape, when each of them is made only of
    # properties with sh:hasValue or sh:in (they say which alternative it is) and
    # sh:pattern
    # (what the others have to look like, with its sh:message): the form can tell what
    # is
    # wrong, and where, which a validator of the shapes alone (it says the node does not
    # conform) cannot. Anything else in them, and the form leaves it to the shape.
    def alternatives(self, shape) -> list[list[tuple]] | None:
        known = {
            SH_NS.path,
            SH_NS.hasValue,
            SH_NS["in"],
            SH_NS.pattern,
            SH_NS.message,
            PROPERTY_TEMPLATE,  # (what a form does with it: template.js)
        }
        found = []
        for head in self._objects(shape, "or"):
            for alternative in self._list_items(head):
                rules = []
                if {
                    q.predicate.value
                    for q in self.dataset.quads_for_subject(alternative)
                } - {SH_NS.property}:
                    return None
                for prop in self._objects(alternative, "property"):
                    if {
                        q.predicate.value for q in self.dataset.quads_for_subject(prop)
                    } - known:
                        return None
                    path = self._value(prop, "path")
                    for kind in ("hasValue", "in", "pattern"):
                        for o in self._objects(prop, kind):
                            values = (
                                [i.value for i in self._list_items(o)]
                                if kind == "in"
                                else [o.value]
                            )
                            rules.append(
                                (path, kind, values, self._value(prop, "message"))
                            )
                found.append(rules)
        return found or None

    def required_paths(self, shape) -> set[str]:
        required = set()
        for prop in self._objects(shape, "property"):
            path = self._objects(prop, "path")
            if (
                len(path) == 1
                and isinstance(path[0], ox.NamedNode)
                and int(self._value(prop, "minCount") or 0) > 0
            ):
                required.add(path[0].value)
        return required

    def property_paths(self, shape, above: tuple = ()) -> set[str]:
        found: set[str] = set()
        for path, prop in self.properties(shape):
            found.add(path)
            nested = self.nested_shape(prop)
            if nested is not None and nested not in (*above, shape):  # a shape that
                found |= self.property_paths(
                    nested, (*above, shape)
                )  # refers to itself
        return found

    def property_names(self, shape) -> dict[str, str]:
        found: dict[str, str] = {}

        def walk(node, above: tuple) -> None:
            for path, prop in self.properties(node):
                title = self.property_title(prop, path)
                found.setdefault(path, local_name(path) if title == path else title)
                nested = self.nested_shape(prop)
                if nested is not None and nested not in (*above, node):
                    walk(nested, (*above, node))

        walk(shape, ())
        return found

    def property_roles(
        self, shape, class_uri: str | None = None
    ) -> list[tuple[str, str, int]]:
        found: list[tuple[str, str, int]] = []

        def walk(node, above: tuple) -> None:
            for path, prop in self.properties(node):
                place = len(found)
                for role in self.objects(prop, PROPERTY_ROLE.value):
                    if isinstance(role, ox.NamedNode) and role.value in ROLE_NAMES:
                        found.append((ROLE_NAMES[role.value], path, place))
                if class_uri is not None:
                    for target in self.objects(prop, UNIQUE_VALUE_FOR_CLASS):
                        if (
                            isinstance(target, ox.NamedNode)
                            and target.value == class_uri
                        ):
                            found.append(("unique", path, place))
                nested = self.nested_shape(prop)
                if nested is not None and nested not in (*above, node):
                    walk(nested, (*above, node))

        walk(shape, ())
        return found

    # A template can also be in an alternative of an sh:or of the node shape, on a
    # property
    # of it: it is then for the nodes that have that alternative, which says which by
    # the
    # property that has an sh:hasValue in it (and the alternative can say what the
    # property
    # has to look like, with sh:pattern, for the shape to check):
    #   sh:or ( [ sh:property [ sh:path schema:propertyID ; sh:hasValue "ORCID" ] ;
    #     sh:property [ sh:path schema:url ; sh:pattern "^https://orcid\\.org/" ;
    #       gracia:propertyTemplate ( "https://orcid.org/{}" schema:value ) ] ] )
    def alternative_templates(self, shape) -> list[PropertyTemplate]:
        found = []
        for head in self.objects(shape, ox.NamedNode(SH_OR)):
            for alternative in self.walk_list(head):
                props = self.objects(alternative, ox.NamedNode(SH_PROPERTY))
                guards = [
                    (self.objects(p, ox.NamedNode(SH_PATH))[0].value, v.value)
                    for p in props
                    for v in self.objects(p, ox.NamedNode(SH_HAS_VALUE))
                ]
                for prop in props:
                    heads = self.objects(prop, ox.NamedNode(uri.PROPERTY_TEMPLATE))
                    if not heads:
                        continue
                    target = self.objects(prop, ox.NamedNode(SH_PATH))[0].value
                    if len(guards) != 1:
                        raise ValueError(
                            f"{target}: a template in an alternative needs the "
                            f"alternative to have one property with sh:hasValue"
                        )
                    template = PropertyTemplate.from_list(self, heads, target)
                    found.append(dataclasses.replace(template, when=guards[0]))
        return found

    def templates(self, shape) -> list[PropertyTemplate]:
        properties = dict(self.properties(shape))
        found = []
        for path, prop in properties.items():
            heads = self.objects(prop, ox.NamedNode(uri.PROPERTY_TEMPLATE))
            if heads:
                found.append(PropertyTemplate.from_list(self, heads, path))
        found += self.alternative_templates(shape)
        targets = {t.target for t in found}
        for template in found:
            if template.target not in properties:
                raise ValueError(f"{template.target}: not a property of the shape")
        found = [
            dataclasses.replace(t, iri=self.is_iri(properties[t.target])) for t in found
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
                if source not in properties or not self.single_literal(
                    properties[source]
                ):
                    raise ValueError(
                        f"{template.target}: {source} must be a property of the same "
                        f"shape with one plain value (sh:maxCount 1, no sh:node)"
                    )
        return found

    # the templates of the bar of a node (gracia:headerTemplate, and
    # gracia:headerAddOnTemplate for the badge before it) in the form: the same text,
    # but
    # only for people to see, so no SQL; the properties are those of the shape
    def header_templates(
        self, shape
    ) -> tuple[PropertyTemplate | None, PropertyTemplate | None]:
        properties = {path for path, _prop in self.properties(shape)}
        found = []
        for predicate in (uri.HEADER_TEMPLATE, uri.HEADER_ADDON_TEMPLATE):
            heads = self.objects(shape, ox.NamedNode(predicate))
            template = PropertyTemplate.from_list(self, heads, "") if heads else None
            if template:  # the separators that are left over are not shown
                template = dataclasses.replace(template, strip=HEADER_STRIP)
            for source in template.sources if template else ():
                if source not in properties:
                    raise ValueError(
                        f"{predicate}: {source} is not a property of the shape"
                    )
            found.append(template)
        return found[0], found[1]


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

    @staticmethod
    def pieces_of(text: str, count: int) -> tuple[Piece, ...]:
        """The pieces a template text is made of (`{}` a property, `[ ]` an optional
        part, doubled the characters themselves); a ValueError says what is wrong with
        the text, and `count` is the number of properties it is given."""
        stack: list[list[Piece]] = [
            []
        ]  # the pieces of the text, of the group that is open
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

    @staticmethod
    def sources_in(pieces) -> list[int]:
        """The numbers of the properties in the pieces, and in the optional parts."""
        found = []
        for piece in pieces:
            if piece[0] == "source":
                found.append(piece[1])
            elif piece[0] == "optional":
                found += PropertyTemplate.sources_in(piece[1])
        return found

    @classmethod
    def from_list(cls, shapes: Graph, heads, target: str) -> PropertyTemplate:
        """The template that a shape says in an rdf list of a text and the properties it
        uses."""
        items = shapes.walk_list(heads[0])
        if len(heads) != 1 or len(items) < 2 or not isinstance(items[0], ox.Literal):
            raise ValueError(
                f"{target or 'a shape'}: a template is a list of a text and the "
                f"properties it uses"
            )
        sources = items[1:]
        if not all(isinstance(s, ox.NamedNode) for s in sources):
            raise ValueError(
                f"{target}: the template names its properties by their uri"
            )
        names = tuple(s.value for s in sources)
        return cls(target, names, cls.pieces_of(items[0].value, len(names)))

    @property
    def required(self) -> list[str]:
        return [
            self.sources[i]
            for i in self.sources_in(p for p in self.pieces if p[0] == "source")
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

        return self.render({s: text(values.get(local_name(s))) for s in self.sources})

    def as_json(self) -> str:
        def piece(found):
            kind, value = found
            if kind == "source":
                return ["source", local_name(self.sources[value])]
            if kind == "optional":
                return ["optional", [piece(p) for p in value]]
            return [kind, value]

        return json.dumps(
            [piece(p) for p in self.pieces], ensure_ascii=False, separators=(",", ":")
        )

    # what a form field that is made of others is given for the browser: the templates,
    # each
    # with the property it is for (or null) and its value
    @staticmethod
    def variants_json(variants: list[PropertyTemplate]) -> str:
        return json.dumps(
            [
                {
                    "when": ([local_name(t.when[0]), t.when[1]] if t.when else None),
                    "pieces": json.loads(t.as_json()),
                }
                for t in variants
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )
