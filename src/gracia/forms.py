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
from chameleon import PageTemplateFile
from deform.template import ZPTRendererFactory, default_dir

from gracia import static
from gracia.graph import Graph
from gracia.uri import G_NS, PROPERTY_TEMPLATE, SH_NS, parse_record_uri
from gracia.utils import assets, dates

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


# The values of a property that is an rdf:list of IRIs (the authors of a work), as a bag
# whose rows have a handle: a row is dragged to another place (list.js), or moved with
# the arrow keys at the handle. The order of the rows is the order of the list.
class ListWidget(BagWidget):
    template = "list"
    item_template = "list_item"


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


# The name of a URI as people read it: the namespace is taken off and the words are
# split (https://schema.org/CollegeOrUniversity: College or university). Shapes do not
# describe the classes and other things they list, so this is the label of a choice.
def uri_label(value: str) -> str:
    split = re.sub(
        r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", local_name(value)
    )
    words = split.split()
    text = " ".join(w if w.isupper() and len(w) > 1 else w.lower() for w in words)
    return text[:1].upper() + text[1:]


# A select of URIs, each named by what is left of it when the namespace is taken off,
# with its icon if the shape has one. It is the widget of every property whose shape
# lists IRIs (sh:in), such as the rdf:type that a resource has, of the classes its shape
# allows: there is no predicate for any. It is a Bootstrap dropdown, not the select of
# the browser: what is posted is a hidden input, which uri_select.js sets and tells the
# form of (`change`).
class URISelectWidget(deform.widget.SelectWidget):
    template = "uri_select"

    def __init__(
        self,
        uris,
        labels: dict[str, str] | None = None,
        icons: dict[str, str | None] | None = None,
        only_for: dict[str, list[str]] | None = None,
        default: str | None = None,
        **kw,
    ):
        labels = labels or {}
        kw.setdefault(
            "values",
            [("", "- choose -"), *((u, labels.get(u) or uri_label(u)) for u in uris)],
        )
        super().__init__(**kw)
        self.icons = {u: i for u, i in (icons or {}).items() if i}
        # the choices that only some resources may have: choice -> the classes of those
        # (uri_select.js hides the choice from the others, by the rdf:type field)
        self.only_for = only_for or {}
        self.only_for_names: dict[str, str] = {}  # choice -> what it is for, in words
        # what a field that has nothing yet (a new node) starts with; what is posted,
        # even nothing, is what it is
        self.default = default

    def only_for_named(self, value) -> str | None:
        """What a choice that is only for some resources is for, in words."""
        return self.only_for_names.get(value)

    def only_for_json(self, value) -> str | None:
        """The classes a choice is only for, as the page tells the script, or none."""
        return json.dumps(self.only_for[value]) if value in self.only_for else None

    def label_of(self, value) -> str | None:
        """The name of a choice (what the bar of a node shows of it)."""
        return (
            dict(self.values).get(value) if isinstance(value, str) and value else None
        )

    def serialize(self, field, cstruct, **kw):
        static.bootstrap_icons_css.need()
        # (a field that is wrong has what was posted, maybe nothing: not the default)
        if cstruct in (colander.null, None) and self.default and not field.error:
            cstruct = self.default
        return super().serialize(field, cstruct, **kw)


# What a shape says of the things in a choice of URIs, when it describes them (an
# rdfs:label and an icon, as a shape has them): the name and the icon of each.
def describe(shapes: Graph, uris) -> dict[str, dict]:
    found = {}
    for value in uris:
        node = ox.NamedNode(value)
        labels = [
            o.value
            for o in shapes.objects(node, ox.NamedNode(RDFS_LABEL))
            if isinstance(o, ox.Literal)
        ]
        found[value] = {
            "label": labels[0] if labels else uri_label(value),
            "icon": icon(shapes, node),
        }
    return found


class ReadOnlyWidget(deform.widget.TextInputWidget):
    template = "readonly_text"


# the language of a resource (`schema:inLanguage`): a text input for the code, with the
# menu of languages of a LanguageWidget as its addon (language.js puts the language that
# is clicked in the code)
class LanguageCodeWidget(deform.widget.TextInputWidget):
    takes_addon = True  # a BagWidget puts its + or x at the end of the group

    def __init__(self, **kw):
        kw.setdefault("css_class", "language-code")
        super().__init__(**kw)

    def serialize(self, field, cstruct, **kw):
        addon, label = kw.pop("addon", ""), kw.pop("label", None)
        if label:
            kw["attributes"] = {
                **(getattr(self, "attributes", None) or {}),
                "aria-label": label,
            }
        text = super().serialize(field, cstruct, **kw)
        return field.renderer(
            "language_code",
            field=field,
            text=text,
            addon=addon,
            languages=LANGUAGES,
            cstruct=cstruct,
        )


GENERIC_ICON = "link-45deg"  # of a reference to a record, when it is not said what of


# The records of the resources a form refers to, by their uris (what the form needs of
# them: the label to show, and the address to edit them at): asked of the store in
# one go for what the form has (`prefetch`: they are all in the repository already), and
# one at a time for what it has not (a choice that was posted and not saved, when the
# form is shown again with an error).
class ReferenceLabels:
    def __init__(self, ask):
        self.ask = ask  # uris -> {uri: (label, record uri, slug)}
        self.found: dict[str, tuple[str, str, str | None] | None] = {}

    def prefetch(self, uris) -> None:
        wanted = sorted({u for u in uris if u and u not in self.found})
        if wanted:
            records = self.ask(wanted)
            for found in wanted:
                self.found[found] = records.get(found)

    def record(self, uri: str) -> tuple[str, str, str | None] | None:
        self.prefetch([uri])
        return self.found.get(uri)

    def get(self, uri: str) -> str | None:
        found = self.record(uri)
        return (found[0] or None) if found else None


# A reference to another resource: the label of the resource, in a text input that
# looks for the records (typeahead: reference.js asks the lookup of the repository, with
# the rdf types the shape says the resource can have), and the uri of the one that is
# chosen, in a hidden input, which is what is posted and stored. Nothing is posted of
# what is typed. At the end of the group is a button: Edit, when a resource is chosen,
# which opens its form in a dialog, and New, when none is, which opens the form of a new
# one (`new`: the types of the records it can be, with the address of the form of each).
class ReferenceWidget(deform.widget.TextInputWidget):
    template = "reference"
    takes_addon = True  # a BagWidget or a ListWidget puts its + or x in the group

    def __init__(
        self,
        classes=(),
        url=None,
        labels=None,
        editor=None,
        new=(),
        icons=None,
        follow=None,
        **kw,
    ):
        super().__init__(**kw)
        # what a reference points at when it follows the field next to it (the role of a
        # contributor): {"name": the field, "map": {its value: {classes, new, icon}}}
        self.follow = follow
        self.icons = icons or {}  # type slug -> the icon of its records
        self.classes = list(classes)
        self.url = url
        self.labels = labels  # a ReferenceLabels
        self.editor = editor  # (record uri, slug) -> the address of its form, or ""
        self.new = list(new)  # [{"label": ..., "url": ...}]

    def label_of(self, uri: str) -> str:
        found = self.labels.get(uri) if self.labels is not None and uri else None
        return found or uri

    def record_of(self, uri: str) -> str:
        """The record the resource is in (what the text tells on hover), or the uri, if
        it is the resource of none."""
        found = self.labels.record(uri) if self.labels is not None and uri else None
        return found[1] if found else uri

    def edit_url(self, uri: str) -> str:
        found = self.labels.record(uri) if self.labels is not None and uri else None
        if not found or self.editor is None:
            return ""
        return self.editor(found[1], found[2]) or ""

    # the icon of what is pointed at: of the type of the record that is chosen; before,
    # of the type it can be, if there is only one
    def icon_of(self, uri: str) -> str:
        found = self.labels.record(uri) if self.labels is not None and uri else None
        parsed = parse_record_uri(found[1]) if found else None
        if parsed and self.icons.get(parsed[0]):
            return self.icons[parsed[0]]
        return self.default_icon()

    def default_icon(self) -> str:
        known = {i for i in self.icons.values() if i}
        return known.pop() if len(known) == 1 else GENERIC_ICON

    def serialize(self, field, cstruct, **kw):
        uri = cstruct if isinstance(cstruct, str) else ""
        static.bootstrap_icons_css.need()
        label = kw.pop("label", None) or field.title
        said = {
            **(getattr(self, "attributes", None) or {}),
            **(kw.get("attributes") or {}),
        }
        said.setdefault("aria-label", label)
        edit = self.edit_url(uri)
        return field.renderer(
            self.template,
            field=field,
            uri=uri,
            label=self.label_of(uri),
            record=self.record_of(uri),
            title=label,
            url=self.url,
            classes=json.dumps(self.classes),
            follow=json.dumps(self.follow) if self.follow else "",
            edit=edit,
            icon=self.icon_of(uri),
            default_icon=self.default_icon(),
            new=json.dumps(self.new),
            can_open=bool(edit) if uri else bool(self.new),
            attributes=said,
            addon=kw.get("addon", ""),
            describedby=kw.get("describedby", f"error-{field.oid}"),
        )


# the shapes that say what a reference points at: the `sh:node` of the property, or of
# the alternatives of its `sh:or`; of a list (an rdf:list of references) the shapes of
# its items, which the shape of the list says as the `sh:node` of `rdf:first`
def reference_shapes(shapes: Graph, prop, shape=None) -> list[ox.NamedNode]:
    def named(subject) -> list[ox.NamedNode]:
        return [
            o for o in _objects(shapes, subject, "node") if isinstance(o, ox.NamedNode)
        ]

    def alternatives(subject) -> list:
        found = [subject]
        for head in _objects(shapes, subject, "or"):
            found += _list_items(shapes, head)
        return found

    if not is_list(shapes, prop):
        found = [s for a in alternatives(prop) for s in named(a)]
        # and what the alternatives of its shape say it points at (a contributor's role)
        paths = _objects(shapes, prop, "path")
        for head in _objects(shapes, shape, "or") if shape is not None else []:
            for alternative in _list_items(shapes, head):
                for given in _alternative_parts(shapes, alternative)[1].get(
                    paths[0].value if len(paths) == 1 else "", []
                ):
                    found.append(given)
        return list(dict.fromkeys(found))
    first = ox.NamedNode(RDF_NS + "first")
    found: list[ox.NamedNode] = []
    for list_shape in named(prop):
        for part in alternatives(list_shape):
            for item in _objects(shapes, part, "property"):
                if first in _objects(shapes, item, "path"):
                    found += [s for a in alternatives(item) for s in named(a)]
    return found


# a node (a text, or the bag or list of them) that refers to resources of the classes:
# its text is looked for in the lookup of the repository
def wire_references(
    node: colander.SchemaNode,
    classes,
    url,
    labels,
    editor=None,
    new=(),
    icons=None,
    follow=None,
) -> None:
    target = node.children[0] if isinstance(node.typ, colander.Sequence) else node
    target.widget = ReferenceWidget(classes, url, labels, editor, new, icons, follow)
    node.reference = True


# a date, typed and shown as the locale of the repository writes it; the cstruct (what
# the schema validates and what is stored) is ISO text, whatever was typed
class DateWidget(deform.widget.TextInputWidget):
    takes_addon = True  # a BagWidget puts its + or x at the end of the group

    def __init__(self, repository=None, kinds=dates.KINDS, **kw):
        super().__init__(**kw)
        self.repository = repository
        self.kinds = tuple(kinds)

    def locale(self) -> str:
        return self.repository.locale() if self.repository is not None else ""

    def serialize(self, field, cstruct, **kw):
        locale = self.locale()
        addon, label = kw.pop("addon", ""), kw.pop("label", None)
        if isinstance(cstruct, str) and cstruct:
            cstruct = dates.display(cstruct, locale)
        said = {
            **(getattr(self, "attributes", None) or {}),
            **(kw.get("attributes") or {}),
        }
        said.setdefault("placeholder", dates.placeholder(locale, self.kinds))
        said.setdefault("inputmode", "numeric")
        if label:
            said.setdefault("aria-label", label)
        kw["attributes"] = said
        text = super().serialize(field, cstruct, **kw)
        static.bootstrap_icons_css.need()  # the calendar
        order, separator = dates.layout(locale)
        return field.renderer(
            "date_input",
            field=field,
            text=text,
            addon=addon,
            order=order,
            separator=separator,
            tag=dates.tag(locale),
            first_day=dates.first_day(locale),
            picker=dates.XSD_DATE in self.kinds,  # a calendar picks a whole date
            label=label or field.title,
        )

    def deserialize(self, field, pstruct):
        text = super().deserialize(field, pstruct)
        if text is colander.null:
            return text
        try:
            return dates.parse(text, self.locale(), self.kinds)
        except ValueError as problem:
            raise colander.Invalid(field.schema, str(problem), value=text) from None


# the date widgets of a node (a text, or the item of a bag) learn the repository, for
# its locale
def localize_dates(node: colander.SchemaNode, repository) -> None:
    for each in (node, *node.children[:1]):
        widget = getattr(each, "widget", None)
        if isinstance(widget, DateWidget):
            widget.repository = repository


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

    # The tab of a mapping that has a choice of classes (the rdf:type of a resource) is
    # named and iconed by the class that is chosen: what the shape says of it.
    def _choice(self, child, cstruct):
        for sub in child.children:
            said = getattr(sub.schema, "tab_choice", None)
            values = cstruct.get(child.name) if isinstance(cstruct, dict) else None
            chosen = values.get(sub.name) if isinstance(values, dict) else None
            if said and chosen in said:
                return said[chosen]
        return None

    # The fields of a tab in the runs they are shown in: the fields that are next to
    # each other and in the same group are one run, which has a box and the name of the
    # group; the others are runs of their own, with no name.
    @staticmethod
    def runs(children) -> list[SimpleNamespace]:
        found: list[SimpleNamespace] = []
        for child in children:
            group = getattr(child.schema, "group", None)
            if found and group and found[-1].group == group:
                found[-1].fields.append(child)
            else:
                found.append(SimpleNamespace(group=group, fields=[child]))
        return found

    def tabs(self, form, cstruct=None) -> list[SimpleNamespace]:
        static.bootstrap_icons_css.need()  # the icon of the Save button
        found = []
        loose = [c for c in form.children if not isinstance(c.typ, colander.Mapping)]
        for child in form.children:
            if isinstance(child.typ, colander.Mapping):
                chosen = self._choice(child, cstruct)
                title = chosen["label"] if chosen else child.title
                icon = (chosen or {}).get("icon") or getattr(child.schema, "icon", None)
                found.append((title, child, [child], child.error, icon))
        if loose:
            found.append(
                (self.loose_title, None, loose, any(c.error for c in loose), None)
            )
        first = next((i for i, tab in enumerate(found) if tab[3]), 0)
        return [
            SimpleNamespace(
                id=f"{form.formid}-tab-{i}",
                icon=icon,
                title=title,
                mapping=mapping,
                fields=fields,
                error=bool(error),
                active=i == first,
                # the fields that only some resources have (the script hides them)
                only_for=json.dumps(
                    getattr(getattr(mapping, "schema", None), "only_for_fields", None)
                    or {}
                ),
            )
            for i, (title, mapping, fields, error, icon) in enumerate(found)
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
    if is_list(shapes, prop) and not many:
        return False  # the one value is a list: the rows of it are the fields
    if (_objects(shapes, prop, "node") and not by_uri(shapes, prop)) or (
        not many and _value(shapes, prop, "maxCount") != "1"
    ):
        return False
    kinds = _objects(shapes, prop, "nodeKind")
    if ox.NamedNode(SH_NS.BlankNode) in kinds:
        return False
    # an IRI is typed as text, when the shape says it is meant to be (dash:URIEditor)
    return (
        ox.NamedNode(SH_NS.IRI) not in kinds
        or has_editor(shapes, prop, DASH_URI)
        or bool(_objects(shapes, prop, "in"))  # a choice of IRIs (the types)
    )


def single_literal(shapes, prop) -> bool:
    return (
        _value(shapes, prop, "maxCount") == "1"
        and not _objects(shapes, prop, "node")
        and not is_iri(shapes, prop)
    )


def property_title(shapes: Graph, prop, path: str) -> str:
    return _value(shapes, prop, "name") or path


def is_iri(shapes: Graph, prop) -> bool:
    return ox.NamedNode(SH_NS.IRI) in _objects(shapes, prop, "nodeKind") or by_uri(
        shapes, prop
    )


def shape_description(shapes: Graph, shape) -> str:
    return _value(shapes, shape, "description") or ""


def local_name(path: str) -> str:
    return path.rsplit("/", 1)[-1].rsplit("#", 1)[-1]


# the name of the group a property is in (`sh:group` of a `sh:PropertyGroup`, which has
# an rdfs:label): the form shows the properties of a group next to each other in a box
def property_group(shapes: Graph, prop) -> str | None:
    for group in _objects(shapes, prop, "group"):
        labels = [
            o.value
            for o in shapes.objects(group, ox.NamedNode(RDFS_LABEL))
            if isinstance(o, ox.Literal)
        ]
        return labels[0] if labels else local_name(group.value)
    return None


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
    if by_uri(shapes, prop):  # a reference to records, not a form
        return None
    shapes = _objects(shapes, prop, "node")
    return shapes[0] if shapes else None


SH_OR, SH_PROPERTY, SH_PATH = (SH_NS["or"], SH_NS.property, SH_NS.path)
SH_HAS_VALUE = SH_NS.hasValue
RDF_NS = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"
RDF_TYPE = RDF_NS + "type"
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


# the datatypes that a property allows (on it, or in the alternatives of its sh:or)
def datatypes(shapes: Graph, prop) -> list[str]:
    alternatives = [prop]
    for head in _objects(shapes, prop, "or"):
        alternatives += _list_items(shapes, head)
    return [d.value for a in alternatives for d in _objects(shapes, a, "datatype")]


# the date datatypes of a property that has dates and nothing else (xsd:date,
# gYearMonth, gYear): it gets a date widget
def date_kinds(shapes: Graph, prop) -> tuple[str, ...]:
    found = datatypes(shapes, prop)
    if found and all(d in dates.KINDS for d in found):
        return tuple(d for d in dates.KINDS if d in found)
    return ()


DASH = "http://datashapes.org/dash#"
DASH_TEXT_AREA = ox.NamedNode(DASH + "TextAreaEditor")  # a text area of four lines
DASH_URI = ox.NamedNode(
    DASH + "URIEditor"
)  # an IRI, as text (no widget of its own yet)


LIST_EDITOR = ox.NamedNode(
    G_NS.URIListEditor
)  # an rdf:list of IRIs, in the order the form puts them in
LANGUAGE_EDITOR = ox.NamedNode(
    G_NS.LanguageEditor
)  # a language code, with a menu of languages
LANGUAGE_CODE = re.compile(r"[A-Za-z]{1,8}(-[A-Za-z0-9]{1,8})*\Z")


def has_editor(shapes: Graph, prop, editor) -> bool:
    return editor in shapes.objects(prop, ox.NamedNode(DASH + "editor"))


# a property whose one value is an rdf:list of IRIs (its shape says what the list is)
def is_list(shapes: Graph, prop) -> bool:
    return has_editor(shapes, prop, LIST_EDITOR)


# a reference to records, by URI: what its shape (sh:node) says is not a form to fill in
def by_uri(shapes: Graph, prop) -> bool:
    return has_editor(shapes, prop, DASH_URI) or is_list(shapes, prop)


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
        if isinstance(o, ox.Literal | ox.NamedNode)
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
    if choices and is_iri(shapes, prop):  # a choice of URIs (the classes of a type)
        said = describe(shapes, choices)
        labels = {u: d["label"] for u, d in said.items()}
        starts = [  # sh:defaultValue: the choice that a new node has
            o.value
            for o in _objects(shapes, prop, "defaultValue")
            if isinstance(o, ox.NamedNode) and o.value in choices
        ]
        text.widget = URISelectWidget(
            choices,
            labels,
            {u: d["icon"] for u, d in said.items()},
            default=starts[0] if starts else None,
        )
        if path == RDF_TYPE:  # the tab of the form is named and iconed by the choice
            text.tab_choice = said
            text.widget.attributes = {  # ty: ignore[missing-attribute]
                "data-follow-tab": json.dumps(said)
            }
    elif choices:  # sh:in: a choice of the values, none to begin with
        text.widget = deform.widget.SelectWidget(
            values=[("", "- choose -"), *((c, c) for c in choices)]
        )
    if has_editor(shapes, prop, LANGUAGE_EDITOR) and not allows_language(shapes, prop):
        text.widget = LanguageCodeWidget()
        validators.append(
            colander.Regex(LANGUAGE_CODE, msg="Not a language code, like en or pt-BR")
        )
        text.validator = colander.All(*validators)
    kinds = date_kinds(shapes, prop)
    if kinds and not choices and not area and not allows_language(shapes, prop):
        text.widget = DateWidget(kinds=kinds)
        text.date_kinds = kinds
    if allows_language(shapes, prop):
        language = colander.SchemaNode(
            colander.String(),
            name="lang",
            title="Language",
            missing="",
            validator=colander.Regex(
                LANGUAGE_CODE,
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


# A reference that follows a field next to it: what it points at is what the value of
# the field says (the role of a contributor: a funder is an organization, a supervisor a
# person). The shape says it with `sh:or`: each alternative has a property with a choice
# of values (`sh:in`, `sh:hasValue`) and the property of the reference with the shape of
# what it points at (`sh:node`), so that the values of the first are for the second.
def _alternative_parts(shapes: Graph, alternative):
    """(the choices in an alternative: path -> values; the shapes: path -> shapes)"""
    choices: dict[str, list[str]] = {}
    pointing: dict[str, list] = {}
    for prop in _objects(shapes, alternative, "property"):
        paths = _objects(shapes, prop, "path")
        if len(paths) != 1 or not isinstance(paths[0], ox.NamedNode):
            continue
        path = paths[0].value
        values = [o.value for o in _objects(shapes, prop, "hasValue")] + [
            o.value
            for head in _objects(shapes, prop, "in")
            for o in _list_items(shapes, head)
        ]
        if values:
            choices[path] = values
        named = [
            o for o in _objects(shapes, prop, "node") if isinstance(o, ox.NamedNode)
        ]
        for head in _objects(shapes, prop, "or"):  # one or the other of the shapes
            for either in _list_items(shapes, head):
                named += [
                    o
                    for o in _objects(shapes, either, "node")
                    if isinstance(o, ox.NamedNode)
                ]
        if named:
            pointing[path] = named
    return choices, pointing


def class_follow(shapes: Graph, shape, prop) -> tuple[str, dict[str, list]] | None:
    paths = _objects(shapes, prop, "path")
    if len(paths) != 1:
        return None
    mine = paths[0].value
    follows, found = None, {}
    for head in _objects(shapes, shape, "or"):
        for alternative in _list_items(shapes, head):
            choices, pointing = _alternative_parts(shapes, alternative)
            guards = [p for p in choices if p != mine]
            if mine in pointing and len(guards) == 1:
                follows = guards[0]
                for value in choices[guards[0]]:
                    found[value] = pointing[mine]
    return (follows, found) if follows and found else None


# Choices that are only for some resources: the thesis advisor is for a thesis. The
# shape says it with an `sh:or` in the shape of the resource, of the alternatives that
# the resource has one of the classes (a property `rdf:type` that has the value), or the
# choice is not among the values of the path (`sh:not` of a `sh:hasValue` or `sh:in`, on
# a path of the property that has the nodes and the property of the choice in them). The
# choice is left out of the menu for the others (uri_select.js), and a form that has it
# for another resource is refused (only_for_validator).
def _rule_shapes(shapes: Graph, shape) -> list:
    """The shape of a resource and the shapes of its rules: the shapes that are for
    the same works (they have one of the classes of its `sh:targetClass`). A rule is a
    shape of its own, so that it says what is wrong in its own words."""
    own = set(_objects(shapes, shape, "targetClass"))
    rules = [
        q.subject
        for q in shapes.dataset.quads_for_object(ox.NamedNode(SH_NS.NodeShape))
        if q.predicate.value == RDF_TYPE
        and q.subject != shape
        and own & set(_objects(shapes, q.subject, "targetClass"))
    ]
    return [shape, *rules]


def _rule_alternatives(shapes: Graph, shape):
    """The `sh:or` of the shape and of its rules, each as a list of alternatives."""
    for rule in _rule_shapes(shapes, shape):
        for head in _objects(shapes, rule, "or"):
            yield _list_items(shapes, head)


def only_for_rules(shapes: Graph, shape) -> dict[tuple[str, str], dict[str, list[str]]]:
    found: dict[tuple[str, str], dict[str, list[str]]] = {}
    for alternatives_ in _rule_alternatives(shapes, shape):
        classes: list[str] = []
        guard = None
        for alternative in alternatives_:
            props = _objects(shapes, alternative, "property")
            paths = _objects(shapes, props[0], "path") if len(props) == 1 else []
            if len(paths) != 1:
                break
            if isinstance(paths[0], ox.NamedNode) and paths[0].value == RDF_TYPE:
                classes += [o.value for o in _objects(shapes, props[0], "hasValue")]
                continue
            steps = (
                _list_items(shapes, paths[0])
                if isinstance(paths[0], ox.BlankNode)
                else []
            )
            nots = _objects(shapes, props[0], "not")
            if len(steps) != 2 or len(nots) != 1 or guard:
                break
            values = [o.value for o in _objects(shapes, nots[0], "hasValue")] + [
                o.value
                for h in _objects(shapes, nots[0], "in")
                for o in _list_items(shapes, h)
            ]
            guard = ((steps[0].value, steps[1].value), values)
        else:
            if classes and guard:
                for value in guard[1]:
                    found.setdefault(guard[0], {})[value] = classes
    return found


# Said of the choices in the nodes of a property (`path`): what the shape of the
# resource says, as above, for the property of the choice in them, which gets it (and
# the names that the form says of it in a message)
def apply_only_for(shapes: Graph, shape, path: str, children) -> None:
    for (outer, inner), by_value in only_for_rules(shapes, shape).items():
        if outer != path:
            continue
        for child in children:
            widget = getattr(child, "widget", None)
            if getattr(child, "predicate", None) != inner or not isinstance(
                widget, URISelectWidget
            ):
                continue
            widget.only_for = by_value
            names = describe(shapes, [c for cs in by_value.values() for c in cs])
            widget.only_for_names = {
                value: " or ".join(names[c]["label"] for c in classes)
                for value, classes in by_value.items()
            }
            child.only_for = {
                value: {
                    "role": widget.label_of(value) or uri_label(value),
                    "classes": classes,
                    "named": " or ".join(names[c]["label"] for c in classes),
                }
                for value, classes in by_value.items()
            }


# Fields that are only for some resources: the degree a thesis is in support of. The
# same rule, of a path that is a property of the resource itself, which it has none of
# unless it is of the class (`sh:maxCount 0`): the field is left out of the form of the
# others, and what it had is not kept (apply_form).
def fields_only_for(shapes: Graph, shape) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for alternatives_ in _rule_alternatives(shapes, shape):
        classes: list[str] = []
        fields: list[str] = []
        for alternative in alternatives_:
            props = _objects(shapes, alternative, "property")
            paths = _objects(shapes, props[0], "path") if len(props) == 1 else []
            if len(paths) != 1 or not isinstance(paths[0], ox.NamedNode):
                break
            if paths[0].value == RDF_TYPE:
                classes += [o.value for o in _objects(shapes, props[0], "hasValue")]
            elif _value(shapes, props[0], "maxCount") == "0":
                fields.append(paths[0].value)
            else:
                break
        else:
            if classes and len(fields) == 1:
                found[fields[0]] = classes
    return found


def only_for_validator(nodes):
    typed = next((n for n in nodes if getattr(n, "predicate", None) == RDF_TYPE), None)
    guarded = []  # (the sequence of nodes, the field that has the choice, its rules)
    for node in nodes:
        item = node.children[0] if isinstance(node.typ, colander.Sequence) else None
        if item is None or not isinstance(item.typ, colander.Mapping):
            continue
        guarded += [
            (node, leaf.name, leaf.only_for)
            for leaf in item.children
            if getattr(leaf, "only_for", None)
        ]
    if typed is None or not guarded:
        return None

    def validate(mapping, value):
        kind = _text_of(value.get(typed.name))
        error = None
        for node, field, rules in guarded:
            listed = None
            for index, item in enumerate(value.get(node.name) or []):
                rule = rules.get(
                    _text_of(item.get(field)) if isinstance(item, dict) else ""
                )
                if not rule or kind in rule["classes"]:
                    continue
                # the error is in the node that has the choice, at the field of the
                # choice: the error of the list, of the node in it, of the field in
                # that (so the node is marked and opened, the message is at the choice)
                error = error or colander.Invalid(mapping)
                if listed is None:
                    listed = colander.Invalid(node)
                    error.add(listed, mapping.children.index(node))
                row = colander.Invalid(node.children[0])
                row[field] = f"{rule['role']} is only for {rule['named']}"
                listed.add(row, index)
        if error:
            raise error

    return validate


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


def _enough(lowest: int, highest: int | None, title: str = ""):
    noun = title.lower() or "value"

    def validate(node, value):
        filled = [v for v in value if _filled(v)]  # a row with nothing is no value
        if len(filled) < lowest:
            raise colander.Invalid(
                node,
                f"At least one {noun} is needed"
                if lowest == 1
                else f"At least {lowest} values are needed for {noun}",
            )
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
        validator=_enough(lowest, highest, item.title),
        widget=BagWidget(url=url, min_len=lowest, max_len=highest),
    )
    node.oid = f"bag-{local_name(path)}"  # the same on every page, for the buttons
    node.bag = True
    return node


def list_node(shapes: Graph, prop, path: str, url: str | None):
    item = leaf_node(shapes, prop, path, many=True)
    assert item is not None
    item.name = "item"
    lowest = cardinality(shapes, prop)[0]  # of the list: its rows, not its one value
    node = colander.SchemaNode(
        colander.Sequence(),
        item,
        name=local_name(path),
        title=item.title,
        description=item.description,
        missing=[],
        validator=_enough(lowest, None, item.title),
        widget=ListWidget(url=url, min_len=lowest, max_len=None),
    )
    node.oid = f"list-{local_name(path)}"  # the same on every page, for the buttons
    node.bag = True  # a bag of rows, for what reads and writes the rows
    node.list = True
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


def edit_address(request, repository, record: str, slug: str | None) -> str:
    """Where the form of a record is, for a dialog (`embed`): none, when the person may
    not edit records of its type."""
    parsed = parse_record_uri(record)
    if parsed is None:
        return ""
    try:
        node = repository[parsed[0]]
    except KeyError:
        return ""
    if not request.has_permission("edit", node):
        return ""
    return request.resource_url(
        node, slug or str(parsed[1]), "edit", query={"embed": "1"}
    )


# what a shape says of its properties (dash): the role a property has in a page of a
# record (`property_roles`) and the class it is the unique value for
ROLE_NAMES = {
    DASH + "LabelRole": "label",
    DASH + "KeyInfoRole": "key_info",
    DASH + "DescriptionRole": "description",
}
UNIQUE_VALUE_FOR_CLASS = DASH + "uniqueValueForClass"


def property_paths(shapes: Graph, shape, above: tuple = ()) -> set[str]:
    found: set[str] = set()
    for path, prop in properties(shapes, shape):
        found.add(path)
        nested = nested_shape(shapes, prop)
        if nested is not None and nested not in (*above, shape):  # a shape that
            found |= property_paths(shapes, nested, (*above, shape))  # refers to itself
    return found


def property_names(shapes: Graph, shape) -> dict[str, str]:
    found: dict[str, str] = {}

    def walk(node, above: tuple) -> None:
        for path, prop in properties(shapes, node):
            title = property_title(shapes, prop, path)
            found.setdefault(path, local_name(path) if title == path else title)
            nested = nested_shape(shapes, prop)
            if nested is not None and nested not in (*above, node):
                walk(nested, (*above, node))

    walk(shape, ())
    return found


def property_roles(
    shapes: Graph, shape, class_uri: str | None = None
) -> list[tuple[str, str, int]]:
    found: list[tuple[str, str, int]] = []

    def walk(node, above: tuple) -> None:
        for path, prop in properties(shapes, node):
            place = len(found)
            for role in shapes.objects(prop, PROPERTY_ROLE.value):
                if isinstance(role, ox.NamedNode) and role.value in ROLE_NAMES:
                    found.append((ROLE_NAMES[role.value], path, place))
            if class_uri is not None:
                for target in shapes.objects(prop, UNIQUE_VALUE_FOR_CLASS):
                    if isinstance(target, ox.NamedNode) and target.value == class_uri:
                        found.append(("unique", path, place))
            nested = nested_shape(shapes, prop)
            if nested is not None and nested not in (*above, node):
                walk(nested, (*above, node))

    walk(shape, ())
    return found


_TEMPLATES: dict[str, PageTemplateFile] = {}


def template(spec: str) -> PageTemplateFile:
    """A Chameleon template of gracia (an asset spec), read once."""
    path = str(assets.resolve(spec))
    if path not in _TEMPLATES:
        _TEMPLATES[path] = PageTemplateFile(path)
    return _TEMPLATES[path]


def fragment(request, spec: str, **values) -> str:
    """The HTML of a template, a piece of a page (a form, a pane) for a stylesheet to
    place."""
    return template(spec)(request=request, **values)
