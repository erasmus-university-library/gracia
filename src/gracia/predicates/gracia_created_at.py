import colander

from gracia import uri
from gracia.predicates import Predicate
from gracia.predicates.gracia_modified_at import LocalTimeWidget


class CreatedAtPredicate(Predicate):
    uri = uri.CREATED_AT

    def schema_node(self, record, request, schema_node=None):
        if schema_node is None:
            return schema_node
        schema_node.widget = LocalTimeWidget(record.repository)
        schema_node.validator = None
        schema_node.missing = colander.drop  # not posted: the record keeps what it has
        return schema_node
