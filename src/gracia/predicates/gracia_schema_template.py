import colander
import deform
import deform.widget

from gracia import uri
from gracia.forms import fragment
from gracia.predicates import Predicate
from gracia.utils import assets


class SchemaWidget(deform.widget.SelectWidget):
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


class SchemaTemplatePredicate(Predicate):
    uri = uri.SCHEMA_TEMPLATE

    def schema_node(self, record, request, schema_node=None):
        prefix = record.content_value(uri.ASSETS)
        files = (
            assets.schemas_of(prefix, record.content_value(uri.SLUG)) if prefix else []
        )
        if schema_node is None or not files:
            return schema_node
        current = record.content_value(uri.SCHEMA_TEMPLATE)
        if current and current not in files:
            files = [current, *files]
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
