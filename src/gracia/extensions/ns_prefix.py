import colander
import deform

from gracia import uri
from gracia.extensions import Extension, extension

DEFAULT_PREFIXES = [
    {"prefix": "dc", "namespace": "http://purl.org/dc/elements/1.1/"},
    {"prefix": "dcterms", "namespace": "http://purl.org/dc/terms/"},
    {"prefix": "foaf", "namespace": "http://xmlns.com/foaf/0.1/"},
    {"prefix": "gracia", "namespace": uri.GRACIA_NS},
    {"prefix": "owl", "namespace": "http://www.w3.org/2002/07/owl#"},
    {"prefix": "rdf", "namespace": "http://www.w3.org/1999/02/22-rdf-syntax-ns#"},
    {"prefix": "rdfs", "namespace": "http://www.w3.org/2000/01/rdf-schema#"},
    {"prefix": "skos", "namespace": "http://www.w3.org/2004/02/skos/core#"},
    {"prefix": "xsd", "namespace": "http://www.w3.org/2001/XMLSchema#"},
]


class PrefixItem(colander.MappingSchema):
    widget = deform.widget.MappingWidget(
        template="mapping_inline", item_template="mapping_item_inline"
    )
    prefix = colander.SchemaNode(
        colander.String(),
        title="Prefix",
        widget=deform.widget.TextInputWidget(attributes={"placeholder": "prefix", "size": 10}),
        validator=colander.All(
            colander.Regex(r"^[A-Za-z_][A-Za-z0-9_.-]*$", msg="Letters, digits, _ . - only"),
            colander.Function(lambda p: not p.lower().startswith("xml"), "xml… is reserved"),
        ),
    )
    namespace = colander.SchemaNode(
        colander.String(),
        title="Namespace",
        widget=deform.widget.TextInputWidget(attributes={"placeholder": "namespace"}),
        validator=colander.Regex(r"^[A-Za-z][A-Za-z0-9+.-]*:\S*$", msg="Not a URI"),
    )


def _no_duplicates(node, items):
    seen = set()
    for item in items:
        if item["prefix"] in seen:
            raise colander.Invalid(node, f"The prefix {item['prefix']} is there twice")
        seen.add(item["prefix"])


class PrefixSettings(colander.MappingSchema):
    prefixes = colander.SchemaNode(
        colander.Sequence(),
        PrefixItem(name="prefix_item", title="Prefix"),
        title="Prefixes",
        description="The prefixes that exports, the XSLT and the label queries know.",
        widget=deform.widget.SequenceWidget(
            template="sequence_compact", item_template="sequence_item_compact"
        ),
        default=DEFAULT_PREFIXES,
        missing=DEFAULT_PREFIXES,
        validator=_no_duplicates,
    )


@extension("ns_prefix")
class NsPrefixExtension(Extension):
    title = "Namespace prefixes"
    description = "The prefixes for exports, the XSLT and the label queries. Always on."
    settings_schema = PrefixSettings
    enabled = True
    can_disable = False
