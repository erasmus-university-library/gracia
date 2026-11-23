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
    template = "readonly_text"


class TabsWidget(deform.widget.FormWidget):
    template = "form_tabs"

    def __init__(self, loose_title: str, **kw):
        super().__init__(**kw)
        self.loose_title = loose_title

    def tabs(self, form) -> list[SimpleNamespace]:
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
    if _value(shapes, prop, "maxCount") != "1" or _objects(shapes, prop, "node"):
        return False
    kinds = _objects(shapes, prop, "nodeKind")
    return (
        ox.NamedNode(SH_NS.IRI) not in kinds
        and ox.NamedNode(SH_NS.BlankNode) not in kinds
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


def nested_shape(shapes: Graph, prop):
    shapes = _objects(shapes, prop, "node")
    return shapes[0] if shapes else None


def leaf_node(shapes: Graph, prop, path: str) -> colander.SchemaNode | None:
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
