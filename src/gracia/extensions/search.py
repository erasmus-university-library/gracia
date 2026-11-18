import functools

import colander
import deform
import deform.widget

from gracia import search
from gracia.extensions import Extension, ExtensionError, extension

TOKENIZERS = ("default", "raw", "en_stem", "whitespace")

# what the repository ships with: the definitions as settings (JSON-able), the default
# of the form
DEFAULT_INDEXES = [
    {
        "name": definition.name,
        "fields": [field.as_dict() for field in definition.fields],
    }
    for definition in (search.RECORDS,)
]


class FieldSettings(colander.MappingSchema):
    widget = deform.widget.MappingWidget(
        template="mapping_inline", item_template="mapping_item_inline"
    )
    name = colander.SchemaNode(
        colander.String(),
        title="Field",
        widget=deform.widget.TextInputWidget(
            attributes={"placeholder": "name", "size": 14}
        ),
        validator=colander.Regex(
            r"^[a-z][a-z0-9_]*$", msg="Lower case letters, digits and _"
        ),
    )
    type = colander.SchemaNode(
        colander.String(),
        title="Type",
        widget=deform.widget.SelectWidget(values=[(t, t) for t in search.FIELD_TYPES]),
        validator=colander.OneOf(search.FIELD_TYPES),
    )
    tokenizer = colander.SchemaNode(
        colander.String(),
        title="Tokenizer",
        description="Of a text field: `raw` keeps the whole value as one word.",
        widget=deform.widget.SelectWidget(values=[(t, t) for t in TOKENIZERS]),
        validator=colander.OneOf(TOKENIZERS),
        default="default",
        missing="default",
    )
    stored = colander.SchemaNode(
        colander.Boolean(),
        title="Stored",
        label="stored",
        default=True,
        missing=False,
        widget=deform.widget.CheckboxWidget(
            attributes={
                "title": "Keep the value, so that a hit can give it back (not needed "
                "to search it)"
            }
        ),
    )
    indexed = colander.SchemaNode(
        colander.Boolean(),
        title="Indexed",
        label="indexed",
        default=True,
        missing=False,
        widget=deform.widget.CheckboxWidget(
            attributes={
                "title": "Make it searchable (a number, a boolean or a date; text is "
                "always "
                "searchable)"
            }
        ),
    )
    fast = colander.SchemaNode(
        colander.Boolean(),
        title="Fast",
        label="fast",
        default=False,
        missing=False,
        widget=deform.widget.CheckboxWidget(
            attributes={
                "title": "Keep a column of the values, to sort and filter by without "
                "reading "
                "documents"
            }
        ),
    )


class IndexSettings(colander.MappingSchema):
    name = colander.SchemaNode(
        colander.String(),
        title="Index",
        validator=colander.Regex(
            r"^[a-z][a-z0-9_]*$", msg="Lower case letters, digits and _"
        ),
    )
    fields = colander.SchemaNode(
        colander.Sequence(),
        FieldSettings(name="field", title="Field"),
        title="Fields",
        widget=deform.widget.SequenceWidget(
            template="sequence_compact", item_template="sequence_item_compact"
        ),
    )


class SearchSettings(colander.MappingSchema):
    schema_type = functools.partial(colander.Mapping, unknown="raise")

    indexes = colander.SchemaNode(
        colander.Sequence(),
        IndexSettings(name="index", title="Index"),
        title="Indexes",
        description=(
            "The indexes of this repository and the fields of each. The Search tab "
            "shows the "
            "fields the docs "
            "can have and adds or removes them here; a change of the fields makes the "
            "index stale "
            "until "
            "the job Rebuild search has made it again."
        ),
        widget=deform.widget.SequenceWidget(
            template="sequence_compact", item_template="sequence_item_compact"
        ),
        default=DEFAULT_INDEXES,
        missing=DEFAULT_INDEXES,
    )


def _added(fields: list[dict], field: dict) -> list[dict]:
    if any(f["name"] == field.get("name") for f in fields):
        raise ExtensionError([f"the index has a field {field.get('name')!r} already"])
    return [
        *fields,
        {
            k: field[k]
            for k in ("name", "type", "stored", "indexed", "fast", "tokenizer")
            if k in field
        },
    ]


def _removed(fields: list[dict], name: str) -> list[dict]:
    if not any(f["name"] == name for f in fields):
        raise ExtensionError([f"the index has no field {name!r}"])
    return [f for f in fields if f["name"] != name]


@extension("search")
class SearchExtension(Extension):
    title = "Search"
    description = (
        "Full-text search of the records: the indexes (Tantivy) and their fields; the "
        "docs they "
        "are made "
        "of come from the doc stylesheets of the types. On unless turned off."
    )
    settings_schema = SearchSettings

    @property
    def enabled(self) -> bool:
        flag = self._flag
        return (
            True if flag is None else bool(flag)
        )  # a repository that never said has it on

    def add_field(self, index: str, field: dict) -> None:
        """Add a field (as `search.Field.as_dict()` has it) to the index named `index`
        of the settings. `ExtensionError` for an index that is not there, a field that
        is, or one that is not right."""
        indexes = self._indexes_with(index, lambda fields: _added(fields, field))
        self.save_settings({"indexes": indexes})

    def remove_field(self, index: str, name: str) -> None:
        """Take the field `name` out of the index `index` of the settings. The fields
        the index and the search page cannot do without stay
        (`search.REQUIRED_FIELDS`)."""
        if name in search.REQUIRED_FIELDS:
            raise ExtensionError(
                [f"{name} is needed by the index and cannot be removed"]
            )
        indexes = self._indexes_with(index, lambda fields: _removed(fields, name))
        self.save_settings({"indexes": indexes})

    def _indexes_with(self, index: str, change) -> list[dict]:
        indexes = [dict(entry) for entry in self.settings["indexes"]]
        for entry in indexes:
            if entry["name"] == index:
                entry["fields"] = change([dict(f) for f in entry["fields"]])
                return indexes
        raise ExtensionError([f"there is no index {index!r}"])

    def definitions(self) -> list[search.IndexDefinition]:
        """The indexes of the repository: none when the extension is off."""
        if not self.enabled:
            return []
        return [
            search.IndexDefinition(
                entry["name"],
                tuple(search.Field(**field) for field in entry["fields"]),
            )
            for entry in self.settings["indexes"]
        ]
