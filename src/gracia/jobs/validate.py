import colander

from gracia import uri
from gracia.jobs.job import job
from gracia.jobs.job import Job
from gracia.utils import assets
from gracia.templates import DependentSelectWidget, DeformViews

CHUNK_BYTES = 20_000_000  # the records of a validation are exported in files of about this size
NO_SCHEMA = ""  # the select's first choice: the schema the type is on


def _types(repository):
    own = [k for k in repository.types.values() if k.assets and k.schema]
    return sorted(own, key=lambda k: k.class_uri in uri.SYSTEM_CLASSES)


def schema_options(repository, type_slug: str | None) -> list[tuple[str, str]]:
    values = [(NO_SCHEMA, "the schema the type is on")]
    res_type = repository.types.get(type_slug or "")
    if res_type is not None and res_type.assets:
        values += [(f, f) for f in assets.schemas_of(res_type.assets, res_type.slug)]  # as the gracia:schemaTemplate field
    return values


def _type_choices(node, kw):
    from deform.widget import SelectWidget

    types = _types(kw["repository"])
    return SelectWidget(values=[(k.slug, f"{k.name} ({k.slug})") for k in types])


def validate_form(context, request):
    return ValidateJob().form(context, request)


@DeformViews.declare("schema-files", at="jobs", permission="start_validate_job", form=validate_form)
def schema_files(context, request, values, form, field):
    repository = context.repository
    types = _types(repository)
    return schema_options(
        repository, values.get("res_type") or (types[0].slug if types else None)
    )


class ValidateForm(colander.MappingSchema):
    res_type = colander.SchemaNode(
        colander.String(),
        title="Type",
        description="The records of this type are checked.",
        widget=colander.deferred(_type_choices),
        validator=colander.deferred(
            lambda node, kw: colander.OneOf([k.slug for k in _types(kw["repository"])])
        ),
    )
    schema = colander.SchemaNode(
        colander.String(),
        title="Schema",
        description="A schema file of the type: name a candidate revision to see what it would do.",
        missing="",
        widget=DependentSelectWidget.deferred("schema-files", depends_on="res_type"),
    )
    validator = DependentSelectWidget.validator("schema", "schema-files", depends_on="res_type")


@job("validate")
class ValidateJob(Job):
    title = "Validate"
    description = "Check the records of a type against a schema and list those that do not conform."
    params_schema = ValidateForm

    def spec(self, repo: str, params: dict) -> dict:
        res_type = params["res_type"]
        validate = {"res_type": res_type}
        if params.get("schema"):
            validate["schema"] = params["schema"]
        return {
            "repo": repo,
            "job": self.name,
            "subject": res_type,
            "stages": [
                {
                    "stage": "Export",
                    "params": {
                        "res_type": res_type,
                        "chunk_bytes": params.get("chunk_bytes", CHUNK_BYTES),
                    },
                },
                {"stage": "Validate", "params": validate},
            ],
        }
