from gracia import uri
from gracia.predicates import Predicate, predicate
from gracia.predicates.gracia_modified_at import local_time_node


@predicate(uri.CREATED_AT)
class CreatedAtPredicate(Predicate):
    """`gracia:created_at` of a record: when it was first saved, `xsd:dateTime` in UTC. Only shown,
    in the time zone of the repository: the value is made when the record is saved, by the
    predicate of `gracia:modified_at` (the two come from one statement, see there)."""

    def schema_node(self, record, request, schema_node=None):
        return local_time_node(schema_node, request, record)
