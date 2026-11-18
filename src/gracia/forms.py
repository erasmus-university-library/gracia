import datetime
import decimal
import json
import re
import uuid
from pathlib import Path, PurePath
from types import SimpleNamespace

import colander
import deform
import deform.widget
import pyoxigraph as ox
from deform.template import ZPTRendererFactory, default_dir

from gracia import static
from gracia.graph import Graph
from gracia.uri import SH_NS

TEMPLATES = Path(__file__).parent / "assets" / "templates" / "deform"
# Deform's own templates, with ours in front (so far the form with tabs; more are
# expected: Deform
# still writes some of its templates with Bootstrap 3 classes)
RENDERER = ZPTRendererFactory([str(TEMPLATES), default_dir])


class CodeWidget(deform.widget.TextAreaWidget):
    """A text area that is a syntax highlighting editor (CodeMirror): `mode` is
    `sparql`, `json` or `xml` (XSLT too). Showing it makes the page load the editor
    (`static.editor_js`), so only the pages with such a field do."""

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


class ReadOnlyWidget(deform.widget.TextInputWidget):
    """The value of a field that is not edited (`templates/deform/readonly_text.pt`)."""

    template = "readonly_text"


class TabsWidget(deform.widget.FormWidget):
    """The widget of a form with tabs (`templates/deform/form_tabs.pt`)."""

    template = "form_tabs"

    def __init__(self, loose_title: str, **kw):
        super().__init__(**kw)
        self.loose_title = loose_title

    def tabs(self, form) -> list[SimpleNamespace]:
        """What the template shows: a tab for every mapping field, then one with the
        other fields (when there are any). The first tab with an error is open, else the
        first one."""
        found = []
        loose = [c for c in form.children if not isinstance(c.typ, colander.Mapping)]
        for child in form.children:
            if isinstance(child.typ, colander.Mapping):
                found.append((child.title, child, [child], child.error))
        if loose:
            found.append((self.loose_title, None, loose, any(c.error for c in loose)))
        first = next((i for i, tab in enumerate(found) if tab[3]), 0)
        return [
            SimpleNamespace(
                id=f"{form.formid}-tab-{i}",
                title=title,
                mapping=mapping,
                fields=fields,
                error=bool(error),
                active=i == first,
            )
            for i, (title, mapping, fields, error) in enumerate(found)
        ]


class Form(deform.Form):
    """The Deform form of gracia: the label in front of the input (`.form-horizontal`,
    gracia.css; it stacks by itself where the form is narrow). Every form is made with
    this one.

    `tabs="Record"` puts the fields on tabs: a tab for every field that is a mapping
    (titled as the field is), and the fields that are not on one more tab with the title
    given."""

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


def _objects(shapes, subject, predicate):
    return shapes.objects(subject, ox.NamedNode(SH_NS[predicate]))


def _value(shapes, subject, predicate):
    objects = _objects(shapes, subject, predicate)
    return objects[0].value if objects else None


def _editable(shapes, prop) -> bool:
    """Whether a property is one text value: at most one, and not a URI, a blank node or
    a shape."""
    if _value(shapes, prop, "maxCount") != "1" or _objects(shapes, prop, "node"):
        return False
    kinds = _objects(shapes, prop, "nodeKind")
    return (
        ox.NamedNode(SH_NS.IRI) not in kinds
        and ox.NamedNode(SH_NS.BlankNode) not in kinds
    )


def property_title(shapes: Graph, prop, path: str) -> str:
    """The `sh:name` of a property shape, else its path."""
    return _value(shapes, prop, "name") or path


def is_iri(shapes: Graph, prop) -> bool:
    """Whether a property shape says its values are URIs (`sh:nodeKind sh:IRI`), not
    literals."""
    return ox.NamedNode(SH_NS.IRI) in _objects(shapes, prop, "nodeKind")


def shape_description(shapes: Graph, shape) -> str:
    """The `sh:description` of a node shape or of a property shape, or an empty text."""
    return _value(shapes, shape, "description") or ""


def local_name(path: str) -> str:
    return path.rsplit("/", 1)[-1].rsplit("#", 1)[-1]


def properties(shapes: Graph, shape) -> list[tuple[str, ox.NamedNode | ox.BlankNode]]:
    """The properties of a node shape as (path, property shape), in `sh:order` and then
    by path. A property whose path is not one plain URI (a property path) is left
    out."""
    found = []
    for prop in _objects(shapes, shape, "property"):
        path = _objects(shapes, prop, "path")
        if len(path) != 1 or not isinstance(path[0], ox.NamedNode):
            continue  # property paths are not supported yet
        order = _value(shapes, prop, "order")
        found.append((float(order) if order else 1e9, path[0].value, prop))
    return [(path, prop) for _, path, prop in sorted(found, key=lambda p: p[:2])]


def nested_shape(shapes: Graph, prop):
    """The shape a property embeds (`sh:node`), or None."""
    shapes = _objects(shapes, prop, "node")
    return shapes[0] if shapes else None


def leaf_node(shapes: Graph, prop, path: str) -> colander.SchemaNode | None:
    """The text field a property shape makes, or None when it is not one text value."""
    if not _editable(shapes, prop):
        return None
    minimum = _value(shapes, prop, "minLength")
    maximum = _value(shapes, prop, "maxLength")
    pattern = _value(shapes, prop, "pattern")
    required = int(_value(shapes, prop, "minCount") or 0) > 0
    validators = [
        colander.Length(
            min=int(minimum) if minimum else None, max=int(maximum) if maximum else None
        )
    ]
    if pattern:
        validators.append(
            colander.Regex(re.compile(pattern), msg="Not in the expected form")
        )
    node = colander.SchemaNode(
        colander.String(),
        name=local_name(path),
        title=property_title(shapes, prop, path),
        description=_value(shapes, prop, "description") or "",
        missing=colander.required if required else "",
        validator=colander.All(*validators),
    )
    node.predicate = path
    return node


def fields_from_shape(
    shapes: Graph, shape, hidden=frozenset()
) -> list[colander.SchemaNode]:
    """The text fields of a node shape, in `sh:order`. Properties in `hidden` get no
    field."""
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


def required_paths(shapes: Graph, shape) -> set[str]:
    """The predicates a shape requires (`sh:minCount` of 1 or more) at the top level."""
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
    """What `json.dumps` cannot write itself: dates and times as ISO text, decimals and
    UUIDs and paths as text, sets as sorted lists, and `colander.null` as null."""
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
    """JSON text of a cstruct or an appstruct (keys in the order they have)."""
    return json.dumps(value, default=json_default, indent=indent, ensure_ascii=False)


def nulls_to_none(cstruct):
    """A cstruct with `colander.null` as `None`, which is what JSON has (so it can be
    stored)."""
    if cstruct is colander.null:
        return None
    if isinstance(cstruct, dict):
        return {k: nulls_to_none(v) for k, v in cstruct.items()}
    if isinstance(cstruct, list | tuple):
        return [nulls_to_none(v) for v in cstruct]
    return cstruct


def none_to_nulls(data):
    """The other way, for what was read from JSON, before it goes to
    `schema.deserialize`."""
    if data is None:
        return colander.null
    if isinstance(data, dict):
        return {k: none_to_nulls(v) for k, v in data.items()}
    if isinstance(data, list):
        return [none_to_nulls(v) for v in data]
    return data
