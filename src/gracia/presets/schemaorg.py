import dataclasses
import json
from typing import ClassVar

from gracia.presets.minimal import MinimalPreset
from gracia.types.collection import CollectionRecord
from gracia.types.organization import OrganizationRecord
from gracia.types.person import PersonRecord
from gracia.types.series import SeriesRecord
from gracia.types.work import WorkRecord
from gracia.utils import assets


class SchemaOrgPreset(MinimalPreset):
    """A repository of schema.org content: persons, organizations, series, collections
    and works, each on its schema.org shape (`assets/shacl/<type>-schemaorg-01.ttl`),
    the
    docs of the persons and the works (`assets/xslt/doc/`), the page of a work
    (`assets/xslt/view/work-schemaorg-01.xslt`, in the place of the page every record
    has), the settings of a repository in the Netherlands and the citations of the works
    (the extension, in the style APA)."""

    name = "schemaorg"
    types = (
        PersonRecord,
        OrganizationRecord,
        SeriesRecord,
        CollectionRecord,
        WorkRecord,
    )
    views: ClassVar[dict] = {"work": "work-schemaorg-01.xslt"}  # slug: its page
    settings: ClassVar[dict] = {"timezone": "Europe/Amsterdam", "locale": "nl-NL"}
    citation_style = "apa"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for record_class in self.types:
            self.content_types.append(self.with_view(record_class.content_type))

    def with_view(self, content_type):
        name = self.views.get(content_type.slug)
        if name is None:
            return content_type
        assets.resolve(assets.view_spec(content_type.assets, name))
        return dataclasses.replace(content_type, view=name)

    def apply(self) -> None:
        super().apply()
        repository = self.repository
        repository.set_settings(self.settings)
        citations = repository.extension("citations")
        citations.enable()
        citations.import_json(json.dumps({"style": self.citation_style}))
