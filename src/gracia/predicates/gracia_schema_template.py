import colander
import deform

from gracia import uri
from gracia.predicates import Predicate, predicate
from gracia.utils import assets
from gracia.views import fragment


class SchemaWidget(deform.widget.SelectWidget):
    """The select of `gracia:schemaTemplate` of a type, with a Validate button at its end: the validate
    job checks every record of the type against the file chosen, before it is saved. The page
    includes the job panel (compact) for the dialog that follows the job."""

    def __init__(self, values, request, jobs, res_type: str, **kw):
        super().__init__(values=values, **kw)
        self.request, self.jobs, self.res_type = request, jobs, res_type

    def serialize(self, field, cstruct, **kw):
        select = super().serialize(field, cstruct, **kw)
        request = self.request
        jobs = self.jobs
        if not request.has_permission("start_validate_job", jobs):
            return select
        return fragment(
            request,
            "gracia:assets/templates/schema_validate.pt",
            select=select,
            res_type=self.res_type,
            validate_url=request.resource_url(jobs, "start", query={"job": "validate"}),
            list_url=request.resource_url(jobs, "list"),
            can_list=request.has_permission("list_jobs", jobs),
        )


@predicate(uri.SCHEMA_TEMPLATE)
class SchemaTemplatePredicate(Predicate):
    """`gracia:schemaTemplate` of a type (or of the repository): one of the files in the directory that
    `gracia:assets` of the same content names. The field is a select of those files, so a name
    that is not a file of it cannot be saved; where the directory is not known (a type that is
    being made) it stays the text field the shape made."""

    def schema_node(self, record, request, schema_node=None):
        prefix = record.content_value(uri.ASSETS)
        files = assets.schemas_of(prefix) if prefix else []
        if schema_node is None or not files:
            return schema_node
        current = record.content_value(uri.SCHEMA_TEMPLATE)
        if current and current not in files:
            files = [current, *files]  # what is there now stays a choice: a form must not lose it
        choices = [(f, f) for f in files]
        slug = record.content_value(uri.SLUG)
        if request is not None and record.type.class_uri == uri.TYPE_CLASS and slug:
            schema_node.widget = SchemaWidget(
                choices, request, record.repository["jobs"], slug
            )  # Validate
        else:
            schema_node.widget = deform.widget.SelectWidget(values=choices)
        schema_node.validator = colander.OneOf(files)
        return schema_node
