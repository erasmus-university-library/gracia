import colander
import deform
import deform.widget

from gracia import citations
from gracia.extensions import Extension, extension

STYLES = citations.style_names()


class CitationSettings(colander.MappingSchema):
    style = colander.SchemaNode(
        colander.String(),
        title="Style",
        description=(
            "The citation style (a CSL file in the assets dir) the citation of every "
            "work is rendered in."
        ),
        widget=deform.widget.SelectWidget(values=[(s, s.upper()) for s in STYLES]),
        validator=colander.OneOf(STYLES),
        default=citations.DEFAULT_STYLE,
        missing=citations.DEFAULT_STYLE,
    )


@extension("citations")
class CitationsExtension(Extension):
    title = "Citations"
    description = (
        "Renders the CSL JSON of a doc (`csl`) in a citation style when the doc is "
        "made, and keeps it in the doc as `citation`."
    )
    settings_schema = CitationSettings

    @property
    def style(self) -> str:
        style = self.settings.get("style")
        return style if style in STYLES else citations.DEFAULT_STYLE
