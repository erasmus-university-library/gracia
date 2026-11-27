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
from gracia.shacl import RDF_TYPE
from gracia.uri import G_NS, local_name, parse_record_uri, uri_label
from gracia.utils import assets, dates

GENERIC_ICON = "link-45deg"  # of a reference to a record, when it is not said what of
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
IRI_TEXT = r"[A-Za-z][A-Za-z0-9+.-]*:[^\s<>\"{}|\\^`]+\Z"  # what can be an IRI here
LANGUAGE_EDITOR = ox.NamedNode(
    G_NS.LanguageEditor
)  # a language code, with a menu of languages
LANGUAGE_CODE = re.compile(r"[A-Za-z]{1,8}(-[A-Za-z0-9]{1,8})*\Z")
# what a shape says of its properties (dash): the role a property has in a page of a
# record (`property_roles`) and the class it is the unique value for
_TEMPLATES: dict[str, PageTemplateFile] = {}


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


def leaf_node(
    shapes: Graph, prop, path: str, many: bool = False
) -> colander.SchemaNode | None:
    if not shapes._editable(prop, many):
        return None
    minimum = shapes._value(prop, "minLength")
    maximum = shapes._value(prop, "maxLength")
    pattern = shapes._value(prop, "pattern")
    required = not many and int(shapes._value(prop, "minCount") or 0) > 0
    validators = [
        colander.Length(
            min=int(minimum) if minimum else None, max=int(maximum) if maximum else None
        )
    ]
    if pattern:
        validators.append(
            colander.Regex(re.compile(pattern), msg="Not in the expected form")
        )
    if shapes.is_iri(prop):  # a text input, until there is a widget for URIs
        validators.append(
            colander.Regex(
                re.compile(IRI_TEXT), msg="Not a URI, like https://example.org/"
            )
        )
    choices = [
        o.value
        for head in shapes._objects(prop, "in")
        for o in shapes._list_items(head)
        if isinstance(o, ox.Literal | ox.NamedNode)
    ]
    if choices:
        validators.append(colander.OneOf(choices))
    title = shapes.property_title(prop, path)
    description = shapes._value(prop, "description") or ""
    text = colander.SchemaNode(
        colander.String(),
        name="value",
        title=title,
        missing=colander.required if required else "",
        validator=colander.All(*validators),
    )
    area = shapes.wants_text_area(prop)
    if area:
        text.widget = deform.widget.TextAreaWidget(rows=4, cols=None)
    if choices and shapes.is_iri(prop):  # a choice of URIs (the classes of a type)
        said = shapes.describe(choices)
        labels = {u: d["label"] for u, d in said.items()}
        starts = [  # sh:defaultValue: the choice that a new node has
            o.value
            for o in shapes._objects(prop, "defaultValue")
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
    if shapes.has_editor(prop, LANGUAGE_EDITOR) and not shapes.allows_language(prop):
        text.widget = LanguageCodeWidget()
        validators.append(
            colander.Regex(LANGUAGE_CODE, msg="Not a language code, like en or pt-BR")
        )
        text.validator = colander.All(*validators)
    kinds = shapes.date_kinds(prop)
    if kinds and not choices and not area and not shapes.allows_language(prop):
        text.widget = DateWidget(kinds=kinds)
        text.date_kinds = kinds
    if shapes.allows_language(prop):
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


# Said of the choices in the nodes of a property (`path`): what the shape of the
# resource says, as above, for the property of the choice in them, which gets it (and
# the names that the form says of it in a message)
def apply_only_for(shapes: Graph, shape, path: str, children) -> None:
    for (outer, inner), by_value in shapes.only_for_rules(shape).items():
        if outer != path:
            continue
        for child in children:
            widget = getattr(child, "widget", None)
            if getattr(child, "predicate", None) != inner or not isinstance(
                widget, URISelectWidget
            ):
                continue
            widget.only_for = by_value
            names = shapes.describe([c for cs in by_value.values() for c in cs])
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
    lowest, highest = shapes.cardinality(prop)
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
    lowest = shapes.cardinality(prop)[0]  # of the list: its rows, not its one value
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
    for path, prop in shapes.properties(shape):
        if path not in hidden and (node := leaf_node(shapes, prop, path)) is not None:
            nodes.append(node)
    return nodes


def schema_from_shape(shapes: Graph, shape) -> colander.Schema:
    schema = colander.Schema()
    for node in fields_from_shape(shapes, shape):
        schema.add(node)
    return schema


def alternatives_validator(shapes: Graph, shape, nodes):
    options = shapes.alternatives(shape)
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
