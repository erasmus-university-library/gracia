import datetime
import decimal
import json
import re
import secrets
import uuid
from pathlib import Path, PurePath
from types import SimpleNamespace

import colander
import deform
import deform.widget
import peppercorn
import pyoxigraph as ox
from deform.template import ZPTRendererFactory, default_dir

from gracia import static
from gracia.graph import Graph
from gracia.uri import PROPERTY_TEMPLATE, SH_NS

TEMPLATES = Path(__file__).parent / "assets" / "templates" / "deform"
# Deform's own templates, with ours in front (so far the form with tabs; more are
# expected: Deform
# still writes some of its templates with Bootstrap 3 classes)
RENDERER = ZPTRendererFactory([str(TEMPLATES), default_dir])


class CodeWidget(deform.widget.TextAreaWidget):
    def __init__(self, mode: str, rows: int = 4, **kw):
        attributes = {
            "data-editor": mode,
            "spellcheck": "false",
            **kw.pop("attributes", {}),
        }
        super().__init__(rows=rows, attributes=attributes, **kw)
        self.mode = mode

    def serialize(self, field, cstruct, **kw):
        static.editor_js.need()
        return super().serialize(field, cstruct, **kw)


# A sequence of blank nodes (a property of a shape that points at nodes, `sh:node`):
# an accordion with an item for each node, one open at a time (`<details name=...>`:
# the browser does it, no script), a small close button in the bar of each and an
# Add button. The buttons ask the server (htmx) at `url`, with the form and the path
# of the sequence in it, and the server answers with this widget again
# (`change_sequence`). They are disabled where `min_len` and `max_len` (the
# cardinality of the property) say no.
# the attributes of a widget, with an aria-label added: for a field whose label is not
# for it (the row of a list of values)
def labelled(field, label: str) -> dict:
    return {**(getattr(field.widget, "attributes", None) or {}), "aria-label": label}


class ButtonsWidget(deform.widget.SequenceWidget):
    url: str | None = None  # where the buttons ask; none: there are no buttons
    open_index: int | None = None  # (what change_sequence sets: for the accordion)
    focus_index: int | None = None  # the item that has just been added: form.js

    def serialize(self, field, cstruct, **kw):
        names, node = [], field
        while node.parent is not None:
            names.append(node.name)
            node = node.parent
        kw.setdefault("path", "/".join(reversed(names)))  # from the form to here
        kw.setdefault("url", self.url)
        kw.setdefault("focus_index", self.focus_index)
        return super().serialize(field, cstruct, **kw)


# The values of a property that can have several (`alternateName`, a list of keywords),
# as a list of rows, at least one, like the field of a property that has one value: the
# last row has a + at its end, which adds a row, and the rows before it an x, which
# takes them away (they ask the server like the blank nodes do). The widget is only the
# container: each row is shown by the widget of the item node (a text input, a
# LanguageWidget, and whatever widget for URIs there will be), and what is posted is a
# list of what the items post.
class BagWidget(ButtonsWidget):
    template = "bag"
    item_template = "bag_item"

    def serialize(self, field, cstruct, **kw):
        kw.setdefault("labelled", labelled)
        static.bootstrap_icons_css.need()  # the + and the x
        if cstruct in (colander.null, None) or len(cstruct) == 0:
            cstruct = [colander.null]  # one row to start with, as for any field
        return super().serialize(field, cstruct, **kw)


class BlankNodeWidget(ButtonsWidget):
    template = "blank_nodes"
    item_template = "blank_node"
    # the item that is open; none given: the first with errors, else all are shut

    def serialize(self, field, cstruct, **kw):
        if getattr(field.schema, "icon", None):
            static.bootstrap_icons_css.need()
        # a group of its own for each rendering: htmx puts the new items in the page
        # before it takes the old ones out, and the browser lets only the first open
        # item of a group stay open
        kw.setdefault("group", f"{field.oid}-{secrets.token_hex(3)}")
        kw.setdefault("open_index", self.open_index)
        return super().serialize(field, cstruct, **kw)


# The sequence of blank nodes at `path` (the names from the form down, joined by
# `/`) of a form that was posted (`controls`, not validated: what was typed is what
# is shown), with one more empty item (`action` "add") or without the item at
# `index` ("remove"), as HTML. What the cardinality of the property forbids is not
# done.
def change_sequence(
    form, controls, path: str, action: str, index: int | None = None
) -> str:
    cstruct = form.widget.deserialize(form, peppercorn.parse(controls))
    names = path.split("/")
    field, below = form, cstruct
    for name in names[:-1]:
        field = field[name]
        below = below.get(name) or {}
    field = field[names[-1]]
    items = list(below.get(names[-1]) or [])
    lowest, highest = field.widget.min_len or 0, field.widget.max_len
    items += [colander.null] * (lowest - len(items))  # the blank ones that are shown
    field.widget.open_index = 0
    field.widget.focus_index = None
    if action == "add" and (highest is None or len(items) < highest):
        items.append(colander.null)
        field.widget.open_index = field.widget.focus_index = len(items) - 1
    elif (
        action == "remove"
        and len(items) > lowest
        and index is not None
        and 0 <= index < len(items)
    ):
        del items[index]
        field.widget.open_index = max(0, index - 1)
    field.sequence_fields = []  # what was posted is not what is shown now
    return field.widget.serialize(field, items)


class ReadOnlyWidget(deform.widget.TextInputWidget):
    template = "readonly_text"


# the languages that the menu of a LanguageWidget lists, by their codes
LANGUAGES = [
    ("en", "English"),
    ("nl", "Dutch"),
    ("fr", "French"),
    ("de", "German"),
    ("es", "Spanish"),
    ("it", "Italian"),
    ("pt", "Portuguese"),
    ("ru", "Russian"),
    ("zh", "Chinese"),
    ("ja", "Japanese"),
    ("ar", "Arabic"),
]


# a text input (or area) with the language of the text as an addon: a short input for
# the code and a caret that opens a menu of common languages (language.js puts the one
# chosen in the input); its node is a mapping of `value` and `lang`
class LanguageWidget(deform.widget.MappingWidget):
    template = "language_text"
    category = "default"  # a mapping is structural (no label), but this is one field
    languages = LANGUAGES
    takes_addon = True  # a BagWidget puts its + or x at the end of the group
    stacked = False  # the language under the text, at the right (for a text area)

    def serialize(self, field, cstruct, **kw):
        if cstruct in (colander.null, None):
            cstruct = {}
        kw.setdefault("languages", self.languages)
        kw.setdefault("addon", "")
        # what the two inputs are called, and where their errors are (in the one place
        # this widget shows them)
        name = kw.pop("label", None) or field.title
        said = {"describedby": f"error-{field.oid}"}

        def part(part_name: str, label: str) -> str:
            sub = field[part_name]
            attributes = {**(getattr(sub.widget, "attributes", None) or {})}
            attributes["aria-label"] = label
            return sub.serialize(
                cstruct.get(part_name, colander.null), attributes=attributes, **said
            )

        kw["text"] = part("value", name)
        kw["lang"] = part("lang", f"{name}, language")
        return super().serialize(field, cstruct, **kw)


class TabsWidget(deform.widget.FormWidget):
    template = "form_tabs"

    def __init__(self, loose_title: str, **kw):
        super().__init__(**kw)
        self.loose_title = loose_title

    def tabs(self, form) -> list[SimpleNamespace]:
        static.bootstrap_icons_css.need()  # the icon of the Save button
        found = []
        loose = [c for c in form.children if not isinstance(c.typ, colander.Mapping)]
        for child in form.children:
            if isinstance(child.typ, colander.Mapping):
                found.append((child.title, child, [child], child.error))
        if loose:
            found.append((self.loose_title, None, loose, any(c.error for c in loose)))
        first = next((i for i, tab in enumerate(found) if tab[3]), 0)
        if any(getattr(getattr(t[1], "schema", None), "icon", None) for t in found):
            static.bootstrap_icons_css.need()
        return [
            SimpleNamespace(
                id=f"{form.formid}-tab-{i}",
                icon=getattr(getattr(mapping, "schema", None), "icon", None),
                title=title,
                mapping=mapping,
                fields=fields,
                error=bool(error),
                active=i == first,
            )
            for i, (title, mapping, fields, error) in enumerate(found)
        ]


class Form(deform.Form):
    def __init__(self, schema, tabs: str | None = None, **options):
        options.setdefault("css_class", "deform form-horizontal")
        options.setdefault("renderer", RENDERER)
        # the browser puts back what was typed in a form when a page is reloaded: for
        # forms that
        # show what is stored, a value that was never saved would come back after a
        # reload
        options.setdefault("autocomplete", False)  # renders autocomplete="off"
        super().__init__(schema, **options)
        if tabs:  # after it: the children would get the widget of the form
            self.widget = TabsWidget(tabs)  # ty: ignore[invalid-assignment]
        else:  # the server says what is wrong, not the browser
            self.widget.attributes = {"novalidate": "novalidate"}  # ty: ignore[invalid-assignment]


def _objects(shapes, subject, predicate):
    return shapes.objects(subject, ox.NamedNode(SH_NS[predicate]))


def _value(shapes, subject, predicate):
    objects = _objects(shapes, subject, predicate)
    return objects[0].value if objects else None


def _editable(shapes, prop, many: bool = False) -> bool:
    if _objects(shapes, prop, "node") or (
        not many and _value(shapes, prop, "maxCount") != "1"
    ):
        return False
    kinds = _objects(shapes, prop, "nodeKind")
    if ox.NamedNode(SH_NS.BlankNode) in kinds:
        return False
    # an IRI is typed as text, when the shape says it is meant to be (dash:URIEditor)
    return ox.NamedNode(SH_NS.IRI) not in kinds or has_editor(shapes, prop, DASH_URI)


def single_literal(shapes, prop) -> bool:
    return (
        _value(shapes, prop, "maxCount") == "1"
        and not _objects(shapes, prop, "node")
        and not is_iri(shapes, prop)
    )


def property_title(shapes: Graph, prop, path: str) -> str:
    return _value(shapes, prop, "name") or path


def is_iri(shapes: Graph, prop) -> bool:
    return ox.NamedNode(SH_NS.IRI) in _objects(shapes, prop, "nodeKind")


def shape_description(shapes: Graph, shape) -> str:
    return _value(shapes, shape, "description") or ""


def local_name(path: str) -> str:
    return path.rsplit("/", 1)[-1].rsplit("#", 1)[-1]


def properties(shapes: Graph, shape) -> list[tuple[str, ox.NamedNode | ox.BlankNode]]:
    found = []
    for prop in _objects(shapes, shape, "property"):
        path = _objects(shapes, prop, "path")
        if len(path) != 1 or not isinstance(path[0], ox.NamedNode):
            continue  # property paths are not supported yet
        order = _value(shapes, prop, "order")
        found.append((float(order) if order else 1e9, path[0].value, prop))
    return [(path, prop) for _, path, prop in sorted(found, key=lambda p: p[:2])]


def cardinality(shapes: Graph, prop) -> tuple[int, int | None]:
    highest = _value(shapes, prop, "maxCount")
    return int(_value(shapes, prop, "minCount") or 0), int(highest) if highest else None


def nested_class(shapes: Graph, shape) -> str | None:
    for predicate in ("class", "targetClass"):
        found = [
            o for o in _objects(shapes, shape, predicate) if isinstance(o, ox.NamedNode)
        ]
        if found:
            return found[0].value
    return None


DASH_ICON_ROLE = ox.NamedNode("http://datashapes.org/dash#IconRole")
PROPERTY_ROLE = ox.NamedNode("http://datashapes.org/dash#propertyRole")


# the name of a Bootstrap icon (without "bi-") that a shape has with the icon role (its
# sh:defaultValue); a property shape with none has the one of the node it points at
def icon(shapes: Graph, *candidates) -> str | None:
    for shape in candidates:
        if shape is not None and DASH_ICON_ROLE in shapes.objects(shape, PROPERTY_ROLE):
            name = _value(shapes, shape, "defaultValue")
            if name:
                return name
    return None


def nested_shape(shapes: Graph, prop):
    shapes = _objects(shapes, prop, "node")
    return shapes[0] if shapes else None


SH_OR, SH_PROPERTY, SH_PATH = (SH_NS["or"], SH_NS.property, SH_NS.path)
SH_HAS_VALUE = SH_NS.hasValue
RDF_NS = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
IRI_TEXT = r"[A-Za-z][A-Za-z0-9+.-]*:[^\s<>\"{}|\\^`]+\Z"  # what can be an IRI here
LANG_STRING = ox.NamedNode(RDF_NS + "langString")


def _list_items(shapes: Graph, head) -> list:
    found = []
    while isinstance(head, ox.BlankNode):
        found += shapes.objects(head, ox.NamedNode(RDF_NS + "first"))[:1]
        rest = shapes.objects(head, ox.NamedNode(RDF_NS + "rest"))
        head = rest[0] if rest else None
    return found


# a property whose shape allows language-tagged strings (sh:datatype rdf:langString, on
# the property or in one of the alternatives of its sh:or, or sh:languageIn)
def allows_language(shapes: Graph, prop) -> bool:
    if _objects(shapes, prop, "languageIn"):
        return True
    alternatives = [prop]
    for head in _objects(shapes, prop, "or"):
        alternatives += _list_items(shapes, head)
    return any(
        LANG_STRING in _objects(shapes, alternative, "datatype")
        for alternative in alternatives
    )


DASH = "http://datashapes.org/dash#"
DASH_TEXT_AREA = ox.NamedNode(DASH + "TextAreaEditor")  # a text area of four lines
DASH_URI = ox.NamedNode(
    DASH + "URIEditor"
)  # an IRI, as text (no widget of its own yet)


def has_editor(shapes: Graph, prop, editor) -> bool:
    return editor in shapes.objects(prop, ox.NamedNode(DASH + "editor"))


def wants_text_area(shapes: Graph, prop) -> bool:
    return has_editor(shapes, prop, DASH_TEXT_AREA)


def leaf_node(
    shapes: Graph, prop, path: str, many: bool = False
) -> colander.SchemaNode | None:
    if not _editable(shapes, prop, many):
        return None
    minimum = _value(shapes, prop, "minLength")
    maximum = _value(shapes, prop, "maxLength")
    pattern = _value(shapes, prop, "pattern")
    required = not many and int(_value(shapes, prop, "minCount") or 0) > 0
    validators = [
        colander.Length(
            min=int(minimum) if minimum else None, max=int(maximum) if maximum else None
        )
    ]
    if pattern:
        validators.append(
            colander.Regex(re.compile(pattern), msg="Not in the expected form")
        )
    if is_iri(shapes, prop):  # a text input, until there is a widget for URIs
        validators.append(
            colander.Regex(
                re.compile(IRI_TEXT), msg="Not a URI, like https://example.org/"
            )
        )
    choices = [
        o.value
        for head in _objects(shapes, prop, "in")
        for o in _list_items(shapes, head)
        if isinstance(o, ox.Literal)
    ]
    if choices:
        validators.append(colander.OneOf(choices))
    title = property_title(shapes, prop, path)
    description = _value(shapes, prop, "description") or ""
    text = colander.SchemaNode(
        colander.String(),
        name="value",
        title=title,
        missing=colander.required if required else "",
        validator=colander.All(*validators),
    )
    area = wants_text_area(shapes, prop)
    if area:
        text.widget = deform.widget.TextAreaWidget(rows=4, cols=None)
    if choices:  # sh:in: a choice of the values, none to begin with
        text.widget = deform.widget.SelectWidget(
            values=[("", "- choose -"), *((c, c) for c in choices)]
        )
    if allows_language(shapes, prop):
        language = colander.SchemaNode(
            colander.String(),
            name="lang",
            title="Language",
            missing="",
            validator=colander.Regex(
                re.compile(r"[A-Za-z]{1,8}(-[A-Za-z0-9]{1,8})*\Z"),
                msg="Not a language code, like en or pt-BR",
            ),
            widget=deform.widget.TextInputWidget(
                css_class="language-code", attributes={"placeholder": "lang"}
            ),
        )
        node = colander.SchemaNode(
            colander.Mapping(),
            text,
            language,
            name=local_name(path),
            title=title,
            description=description,
            widget=LanguageWidget(stacked=area),
        )
        node.language_tagged = True
    else:
        node = text
        node.name, node.description = local_name(path), description
    node.predicate = path
    return node


# the property is made of others (gracia:propertyTemplate: one template, or one for each
# value of another property): the form need not have it when they are there, and the
# templates go to the browser, which keeps the field up to date as the others are typed
# (template.js)
def made_of(
    node: colander.SchemaNode, variants: list, as_json: str, required: bool
) -> None:
    node.template = variants[0] if len(variants) == 1 and not variants[0].when else None
    text = node.children[0] if getattr(node, "language_tagged", False) else node
    node.template_required = required
    text.missing = ""
    if getattr(text, "widget", None) is None:
        text.widget = deform.widget.TextInputWidget()
    widget = text.widget
    widget.attributes = {  # ty: ignore[invalid-assignment]
        **(getattr(widget, "attributes", None) or {}),
        "data-template": as_json,
    }


def _filled(value) -> bool:
    return bool(_text_of(value))


# a property that is required, but is made of others when they are there: it is given,
# or so are they
def template_validator(nodes):
    made = [
        n
        for n in nodes
        if getattr(n, "template", None) and getattr(n, "template_required", False)
    ]
    if not made:
        return None
    by_path = {getattr(n, "predicate", None): n for n in nodes}

    def validate(mapping, value):
        error = None
        for node in made:
            sources = [by_path.get(p) for p in node.template.required]
            if _filled(value.get(node.name)) or all(
                s is not None and _filled(value.get(s.name)) for s in sources
            ):
                continue
            error = error or colander.Invalid(mapping)
            titles = " and ".join(s.title.lower() for s in sources if s is not None)
            error[node.name] = f"Give a {node.title.lower()}" + (
                f", or fill in {titles}" if titles else ""
            )
        if error:
            raise error

    return validate


# a property that can have several values that are plain: literals (it says their
# datatype, or that they have a language) or IRIs that are typed as text
def is_bag(shapes: Graph, prop) -> bool:
    if _value(shapes, prop, "maxCount") == "1" or not _editable(shapes, prop, True):
        return False
    return bool(
        _objects(shapes, prop, "datatype")
        or allows_language(shapes, prop)
        or (is_iri(shapes, prop) and has_editor(shapes, prop, DASH_URI))
    )


def _enough(lowest: int, highest: int | None):
    def validate(node, value):
        filled = [v for v in value if _filled(v)]  # a row with nothing is no value
        if len(filled) < lowest:
            raise colander.Invalid(node, f"Shorter than minimum length {lowest}")
        if highest is not None and len(filled) > highest:
            raise colander.Invalid(node, f"Longer than maximum length {highest}")

    return validate


def bag_node(shapes: Graph, prop, path: str, url: str | None):
    item = leaf_node(shapes, prop, path, many=True)
    assert item is not None
    item.name = "item"
    lowest, highest = cardinality(shapes, prop)
    node = colander.SchemaNode(
        colander.Sequence(),
        item,
        name=local_name(path),
        title=item.title,
        description=item.description,
        missing=[],
        validator=_enough(lowest, highest),
        widget=BagWidget(url=url, min_len=lowest, max_len=highest),
    )
    node.oid = f"bag-{local_name(path)}"  # the same on every page, for the buttons
    node.bag = True
    return node


def fields_from_shape(
    shapes: Graph, shape, hidden=frozenset()
) -> list[colander.SchemaNode]:
    nodes = []
    for path, prop in properties(shapes, shape):
        if path not in hidden and (node := leaf_node(shapes, prop, path)) is not None:
            nodes.append(node)
    return nodes


def schema_from_shape(shapes: Graph, shape) -> colander.Schema:
    schema = colander.Schema()
    for node in fields_from_shape(shapes, shape):
        schema.add(node)
    return schema


# a property that has to be there: its own shape says so, or an alternative of an sh:or
# of the node does (a name, or the two parts it is made of)
def needed(shapes: Graph, shape, path: str) -> bool:
    if path in required_paths(shapes, shape):
        return True
    for head in _objects(shapes, shape, "or"):
        for alternative in _list_items(shapes, head):
            if path in required_paths(shapes, alternative):
                return True
    return False


# The alternatives of an sh:or of a node shape, when each of them is made only of
# properties with sh:hasValue or sh:in (they say which alternative it is) and sh:pattern
# (what the others have to look like, with its sh:message): the form can tell what is
# wrong, and where, which a validator of the shapes alone (it says the node does not
# conform) cannot. Anything else in them, and the form leaves it to the shape.
def alternatives(shapes: Graph, shape) -> list[list[tuple]] | None:
    known = {
        SH_NS.path,
        SH_NS.hasValue,
        SH_NS["in"],
        SH_NS.pattern,
        SH_NS.message,
        PROPERTY_TEMPLATE,  # (what a form does with it: template.js)
    }
    found = []
    for head in _objects(shapes, shape, "or"):
        for alternative in _list_items(shapes, head):
            rules = []
            if {
                q.predicate.value for q in shapes.dataset.quads_for_subject(alternative)
            } - {SH_NS.property}:
                return None
            for prop in _objects(shapes, alternative, "property"):
                if {
                    q.predicate.value for q in shapes.dataset.quads_for_subject(prop)
                } - known:
                    return None
                path = _value(shapes, prop, "path")
                for kind in ("hasValue", "in", "pattern"):
                    for o in _objects(shapes, prop, kind):
                        values = (
                            [i.value for i in _list_items(shapes, o)]
                            if kind == "in"
                            else [o.value]
                        )
                        rules.append(
                            (path, kind, values, _value(shapes, prop, "message"))
                        )
            found.append(rules)
    return found or None


def alternatives_validator(shapes: Graph, shape, nodes):
    options = alternatives(shapes, shape)
    names = {getattr(n, "predicate", None): n.name for n in nodes}
    if options is None or any(r[0] not in names for rules in options for r in rules):
        return None

    def holds(rule, value) -> bool:
        path, kind, expected, _message = rule
        text = _text_of(value.get(names[path]))
        if kind == "pattern":
            return not text or re.search(expected[0], text) is not None
        return text in expected  # hasValue, in

    def validate(mapping, value):
        guards = ("hasValue", "in")
        chosen = [
            rules
            for rules in options
            if all(holds(r, value) for r in rules if r[1] in guards)
        ]
        error = colander.Invalid(mapping)
        if not chosen:  # none of the kinds: the property that tells them apart
            path = next(r[0] for rules in options for r in rules if r[1] in guards)
            error[names[path]] = "Not one of the allowed values"
            raise error
        for rules in chosen:
            if all(holds(r, value) for r in rules):
                return
        path, _kind, _values, message = next(
            r for r in chosen[0] if r[1] == "pattern" and not holds(r, value)
        )
        error[names[path]] = message or "Not in the expected form"
        raise error

    return validate


def _text_of(value) -> str:
    if isinstance(value, dict):
        value = value.get("value")
    return (value or "").strip()


def required_paths(shapes: Graph, shape) -> set[str]:
    required = set()
    for prop in _objects(shapes, shape, "property"):
        path = _objects(shapes, prop, "path")
        if (
            len(path) == 1
            and isinstance(path[0], ox.NamedNode)
            and int(_value(shapes, prop, "minCount") or 0) > 0
        ):
            required.add(path[0].value)
    return required


# ---- cstructs and JSON
# ------------------------------------------------------------------------
# A Colander schema turns what it is given into a *cstruct* (`schema.serialize`:
# strings, lists and
# dicts, a date as its ISO text) and back into an *appstruct* (`schema.deserialize`:
# real dates,
# numbers, booleans). The cstruct is what an extension stores of its settings, but it
# can hold
# `colander.null` (a value that is not there), which JSON cannot say.


def json_default(value):
    if value is colander.null:
        return None
    if isinstance(value, datetime.datetime | datetime.date | datetime.time):
        return value.isoformat()
    if isinstance(value, decimal.Decimal | uuid.UUID | PurePath):
        return str(value)
    if isinstance(value, set | frozenset):
        return sorted(value, key=str)
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


def to_json(value, *, indent: int | None = None) -> str:
    return json.dumps(value, default=json_default, indent=indent, ensure_ascii=False)


def nulls_to_none(cstruct):
    if cstruct is colander.null:
        return None
    if isinstance(cstruct, dict):
        return {k: nulls_to_none(v) for k, v in cstruct.items()}
    if isinstance(cstruct, list | tuple):
        return [nulls_to_none(v) for v in cstruct]
    return cstruct


def none_to_nulls(data):
    if data is None:
        return colander.null
    if isinstance(data, dict):
        return {k: none_to_nulls(v) for k, v in data.items()}
    if isinstance(data, list):
        return [none_to_nulls(v) for v in data]
    return data
