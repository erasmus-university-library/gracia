import colander
import deform

from gracia import uri
from gracia.predicates import Predicate, predicate
from gracia.utils import assets


@predicate(uri.DOC_TEMPLATE)
class DocTemplatePredicate(Predicate):
    """`gracia:docTemplate` of a type: one of the doc stylesheets in `doc/` of the directory that
    `gracia:assets` of the same content names (`doc-<profile>-<revision>.xsl`). The field is a select of
    those files (and none: the doc every record has), so a name that is not a file of it cannot be saved;
    where the directory is not known (a type that is being made) it stays the text field the shape made.
    The docs of the type's records are made again with the new stylesheet when they are asked for or
    when the records change, and the search index has to be made again to have them."""

    def schema_node(self, record, request, schema_node=None):
        prefix = record.content_value(uri.ASSETS)
        files = assets.docs_of(prefix) if prefix else []
        if schema_node is None or not files:
            return schema_node
        current = record.content_value(uri.DOC_TEMPLATE)
        if current and current not in files:
            files = [current, *files]  # what is there now stays a choice: a form must not lose it
        schema_node.widget = deform.widget.SelectWidget(values=[("", "(the doc of every record)"), *[(f, f) for f in files]])
        schema_node.validator = colander.OneOf(["", *files])
        return schema_node
