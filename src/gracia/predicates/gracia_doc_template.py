import colander
import deform
import deform.widget

from gracia import uri
from gracia.predicates import Predicate, predicate
from gracia.utils import assets


@predicate(uri.DOC_TEMPLATE)
class DocTemplatePredicate(Predicate):
    def schema_node(self, record, request, schema_node=None):
        prefix = record.content_value(uri.ASSETS)
        files = assets.docs_of(prefix) if prefix else []
        if schema_node is None or not files:
            return schema_node
        current = record.content_value(uri.DOC_TEMPLATE)
        if current and current not in files:
            files = [current, *files]
        schema_node.widget = deform.widget.SelectWidget(
            values=[("", "(the doc of every record)"), *[(f, f) for f in files]]
        )
        schema_node.validator = colander.OneOf(["", *files])
        return schema_node
