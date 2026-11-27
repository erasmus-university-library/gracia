import re
from importlib.metadata import entry_points
from typing import Any

import colander
import luigi
import venusian
from zope.interface import classImplements

from gracia import uri
from gracia.interfaces import IJob
from gracia.jobs.stage import JobTask, Stage
from gracia.templates import DeformViews, DependentSelectWidget
from gracia.utils import assets

ENTRY_POINT_GROUP = "gracia.jobs"
BUILTIN_PACKAGES = ["gracia.jobs.job"]
MAX_STAGES = 20
REPO_ID = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
RESERVED = frozenset(
    {"job_dir", "repo", "n", "chain"}
)  # Luigi parameters the job sets itself
# the graphs are exported in files of about this size: one is loaded at a time
CHUNK_BYTES = 32_000_000
VALIDATE_CHUNK_BYTES = (
    20_000_000  # the records of a validation are exported in files of about this size
)
NO_SCHEMA = ""  # the select's first choice: the schema the type is on


class Job:
    name = ""
    start_permission = ""
    title = ""
    description = ""
    groups = ("editor", "manager")  # besides the administrators
    params_schema = (
        colander.MappingSchema
    )  # the form; bound with `repository=` (a Repository node)

    @property
    def form_id(self) -> str:
        return f"form-job-{self.name}"

    def form(self, context, request):
        from gracia.forms import Form

        schema = self.params_schema().bind(
            repository=context.repository, request=request, context=context
        )
        return Form(schema, buttons=(), formid=self.form_id)

    def spec(self, repo: str, params: dict) -> dict:
        raise NotImplementedError


def job(name: str):

    def decorate(cls):
        def callback(scanner, _name, _obj):
            scanner.config.add_gracia_job(name, cls)

        venusian.attach(cls, callback, category="pyramid")
        return cls

    return decorate


def add_gracia_job(config, name: str, cls: Any) -> None:

    def register():
        cls.name = name
        cls.start_permission = f"start_{name}_job"
        classImplements(cls, IJob)
        config.registry.registerUtility(cls(), IJob, name=name)

    config.action(("gracia job", name), register)


def all_jobs(registry) -> list:
    return sorted(
        (found for _name, found in registry.getUtilitiesFor(IJob)),
        key=lambda j: j.title,
    )


def scan_targets(settings: dict) -> list[str]:
    found = list(BUILTIN_PACKAGES)
    found += [
        entry.value.split(":")[0] for entry in entry_points(group=ENTRY_POINT_GROUP)
    ]
    found += settings.get("gracia.jobs", "").split()
    unique = list(dict.fromkeys(found))
    return [
        name
        for name in unique
        if not any(name.startswith(other + ".") for other in unique if other != name)
    ]


class StageNode(colander.MappingSchema):
    stage = colander.SchemaNode(colander.String(), validator=colander.Length(min=1))
    params = colander.SchemaNode(
        colander.Mapping(unknown="preserve"), missing=colander.drop
    )


class Stages(colander.SequenceSchema):
    stage = StageNode()


class JobNode(colander.MappingSchema):
    repo = colander.SchemaNode(
        colander.String(), validator=colander.Regex(REPO_ID, msg="Not a repository id")
    )
    job = colander.SchemaNode(colander.String(), missing=None)
    subject = colander.SchemaNode(colander.String(), missing=None)
    stages = Stages(validator=colander.Length(min=1, max=MAX_STAGES))


class JobSpecError(ValueError):
    def __init__(self, errors: dict[str, str]):
        self.errors = errors
        super().__init__("; ".join(f"{where}: {why}" for where, why in errors.items()))


def _check_stage(
    repo: str, index: int, entry: dict, before: list, errors: dict
) -> None:
    name = entry["stage"]
    try:
        cls = Stage.named(name)
    except KeyError:
        known = ", ".join(Stage.known()) or "none"
        errors[f"stages.{index}.stage"] = f"No such stage: {name!r} (known: {known})"
        return
    if (index == 0) != bool(cls.first):
        errors[f"stages.{index}.stage"] = (
            f"{name} must come first"
            if cls.first
            else f"{name} cannot be the first stage: something has to make the items"
        )
        return
    declared = {k: v for k, v in cls.get_params() if k not in RESERVED}
    typed: dict = {}
    for key, value in entry.get("params", {}).items():
        param = declared.get(key)
        if param is None:
            errors[f"stages.{index}.params"] = f"unknown parameter {key}"
            return
        try:  # Luigi parses text; a value from JSON is taken through the same door
            typed[key] = param.parse(param.serialize(value))
        except (ValueError, TypeError):
            errors[f"stages.{index}.params.{key}"] = f"not a valid value: {value!r}"
            return
    try:  # what Luigi itself finds wrong: a parameter without a value
        cls(
            job_dir="",
            repo=repo,
            n=0,
            chain=JobTask.dumps([*before, {"stage": name, "params": typed}]),
            **typed,
        )
    except luigi.parameter.MissingParameterException as err:
        errors[f"stages.{index}.params"] = str(err).split(": ", 1)[-1]
        return
    entry["params"] = typed


def validate_spec(data) -> dict:
    try:
        spec = JobNode().deserialize(data)
    except colander.Invalid as invalid:
        raise JobSpecError(invalid.asdict()) from None
    errors: dict[str, str] = {}
    before: list = []
    for index, entry in enumerate(spec["stages"]):
        entry["params"] = dict(entry.get("params", {}))
        _check_stage(spec["repo"], index, entry, before, errors)
        before.append(entry)
    if errors:
        raise JobSpecError(errors)
    return spec


@job("rebuild_etags")
class RebuildEtagsJob(Job):
    title = "Rebuild etags"
    description = (
        "Make the etags of the pages of every record again from the data, and list the "
        "records "
        "whose etag was wrong. Writes keep them up to date by themselves: this is for "
        "a repository "
        "that has none yet, or whose data was changed outside gracia."
    )
    groups = ()  # it reads every record: the administrators only

    def spec(self, repo: str, params: dict) -> dict:
        return {
            "repo": repo,
            "job": self.name,
            "stages": [{"stage": "RebuildEtags", "params": {}}],
        }


@job("update_cache")
class UpdateCacheJob(Job):
    title = "Update cache"
    description = (
        "Make the RDF/XML of the graph of every record again (the record and "
        "everything it is made "
        "of) and keep it in the graph cache, so that the pages are made from it. Only "
        "records that "
        "have an etag are done: rebuild the etags first in a repository that has none."
    )
    groups = ()  # it reads every record: the administrators only

    def spec(self, repo: str, params: dict) -> dict:
        return {
            "repo": repo,
            "job": self.name,
            "stages": [
                {
                    "stage": "Export",
                    "params": {"graphs": True, "chunk_bytes": CHUNK_BYTES},
                },
                {"stage": "UpdateCache", "params": {}},
            ],
        }


@job("process_change_events")
class ProcessActionsJob(Job):
    title = "Process pending change events"
    description = (
        "Hand every change that is waiting in the queue to what follows the data (the "
        "search index, "
        "the graph cache), as the worker does by itself, from files DuckDB writes in "
        "one go: for a "
        "repository whose worker is not running, or to catch up after something was "
        "down."
    )
    groups = ()  # it reaches every record that changed: the administrators only

    def spec(self, repo: str, params: dict) -> dict:
        return {
            "repo": repo,
            "job": self.name,
            "stages": [
                {
                    "stage": "Export",
                    "params": {"queued": True, "chunk_bytes": CHUNK_BYTES},
                },
                {"stage": "ProcessChangeEvents", "params": {}},
            ],
        }


@job("rebuild_search")
class RebuildSearchJob(Job):
    title = "Rebuild search"
    description = (
        "Make the search indexes of every record again, next to the ones that answer, "
        "and let the new ones "
        "take their place when they are done: for an index that is not there yet, that "
        "is out of date (its "
        "definition changed), or that lacks records. What is written meanwhile is "
        "brought up "
        "to date by the worker afterwards."
    )
    groups = ()  # it reads every record: the administrators only

    def spec(self, repo: str, params: dict) -> dict:
        return {
            "repo": repo,
            "job": self.name,
            "stages": [
                {
                    "stage": "Export",
                    "params": {"graphs": True, "chunk_bytes": CHUNK_BYTES},
                },
                {"stage": "IndexRecords", "params": {}},
            ],
        }


def _all_types(node, kw):
    from deform.widget import SelectWidget

    types = kw["repository"].types.values()
    return SelectWidget(values=[(k.slug, f"{k.name} ({k.slug})") for k in types])


class TypeForm(colander.MappingSchema):
    res_type = colander.SchemaNode(
        colander.String(),
        title="Type",
        description="The records of this type.",
        widget=colander.deferred(_all_types),
        validator=colander.deferred(
            lambda node, kw: colander.OneOf(
                [k.slug for k in kw["repository"].types.values()]
            )
        ),
    )


class ExportForm(TypeForm):
    graphs = colander.SchemaNode(
        colander.Boolean(),
        title="Graphs",
        description=(
            "Write the graph of every record (the record and everything it is made of, "
            "as TriG) "
            "instead of the record itself (N-Triples)."
        ),
        missing=False,
    )


@job("export")
class ExportJob(Job):
    title = "Export"
    description = (
        "Write the records of a type to one file you can download: N-Triples, or the "
        "graph of "
        "every record as TriG."
    )
    params_schema = ExportForm

    def spec(self, repo: str, params: dict) -> dict:
        res_type = params["res_type"]
        export = {"res_type": res_type}
        if params.get("graphs"):
            export["graphs"] = True
        return {
            "repo": repo,
            "job": self.name,
            "subject": res_type,
            "stages": [{"stage": "Export", "params": export}],
        }


@job("rewrite")
class RewriteJob(Job):
    title = "Rewrite records"
    description = (
        "Run the write hooks (label, resource URI, ...) again on the stored records of "
        "a type and "
        "store what they change. Needed after the schema of a type was switched."
    )
    params_schema = TypeForm

    def spec(self, repo: str, params: dict) -> dict:
        res_type = params["res_type"]
        return {
            "repo": repo,
            "job": self.name,
            "subject": res_type,
            "stages": [{"stage": "Rewrite", "params": {"res_type": res_type}}],
        }


@job("purge_slugs")
class PurgeSlugsJob(Job):
    title = "Purge old slugs"
    description = (
        "Forget the old slugs of the records of a type that are there: their old "
        "addresses are "
        "then not found any more. The slug each record has now, and everything that "
        "belongs to "
        "deleted records (which stay gone), is kept."
    )
    params_schema = TypeForm

    def spec(self, repo: str, params: dict) -> dict:
        res_type = params["res_type"]
        return {
            "repo": repo,
            "job": self.name,
            "subject": res_type,
            "stages": [{"stage": "PurgeSlugs", "params": {"res_type": res_type}}],
        }


def _types(repository):
    own = [k for k in repository.types.values() if k.assets and k.schema]
    return sorted(own, key=lambda k: k.class_uri in uri.SYSTEM_CLASSES)


def schema_options(repository, type_slug: str | None) -> list[tuple[str, str]]:
    values = [(NO_SCHEMA, "the schema the type is on")]
    res_type = repository.types.get(type_slug or "")
    if res_type is not None and res_type.assets:
        values += [
            (f, f) for f in assets.schemas_of(res_type.assets, res_type.slug)
        ]  # as the gracia:schemaTemplate field
    return values


def _type_choices(node, kw):
    from deform.widget import SelectWidget

    types = _types(kw["repository"])
    return SelectWidget(values=[(k.slug, f"{k.name} ({k.slug})") for k in types])


def validate_form(context, request):
    return ValidateJob().form(context, request)


@DeformViews.declare(
    "schema-files", at="jobs", permission="start_validate_job", form=validate_form
)
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
        description=(
            "A schema file of the type: name a candidate revision to see what "
            "it would do."
        ),
        missing="",
        widget=DependentSelectWidget.deferred("schema-files", depends_on="res_type"),
    )
    validator = DependentSelectWidget.validator(
        "schema", "schema-files", depends_on="res_type"
    )


@job("validate")
class ValidateJob(Job):
    title = "Validate"
    description = (
        "Check the records of a type against a schema and list those that do not "
        "conform."
    )
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
                        "chunk_bytes": params.get("chunk_bytes", VALIDATE_CHUNK_BYTES),
                    },
                },
                {"stage": "Validate", "params": validate},
            ],
        }
