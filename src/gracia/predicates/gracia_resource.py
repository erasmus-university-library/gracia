import colander

from gracia import uri
from gracia.predicates import Predicate


class ResourcePredicate(Predicate):
    """`gracia:resource`: the content of a record, a blank node of its type's class."""

    uri = uri.RESOURCE

    def schema_node(self, record, request, schema_node=None):
        content = schema_node or colander.SchemaNode(
            colander.Mapping(), name="resource"
        )
        content.title = record.type.name
        for field in record.extra_fields():
            content.add(field)
        return content
