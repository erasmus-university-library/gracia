import colander
import deform.widget

from gracia import uri
from gracia.predicates import Predicate
from gracia.utils import dates


class LocalePredicate(Predicate):
    """`gracia:locale`: the format of dates in the forms, or none for ISO 8601."""

    uri = uri.LOCALE

    def schema_node(self, record, request, schema_node=None):
        if schema_node is None:
            return schema_node
        schema_node.widget = deform.widget.SelectWidget(
            values=[
                (code, "(none: ISO 8601)" if not code else dates.label(code))
                for code in dates.FORMATS
            ]
        )
        schema_node.validator = colander.OneOf(list(dates.FORMATS))
        return schema_node
