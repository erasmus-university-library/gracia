import colander

from gracia import uri
from gracia.predicates import Predicate, predicate


@predicate(uri.RESOURCE)
class ResourcePredicate(Predicate):
    """`gracia:resource` of a record: its content, a blank node of the rdf class of its
    type. The shape of the record embeds the shape of the type here, so the form has a
    tab of its own with the fields of the content; this adds the fields of the record's
    class that are not stored as they are (a password)."""

    def schema_node(self, record, request, schema_node=None):
        content = schema_node or colander.SchemaNode(
            colander.Mapping(), name="resource"
        )
        content.title = record.type.name
        for field in record.extra_fields():
            content.add(field)
        return content
