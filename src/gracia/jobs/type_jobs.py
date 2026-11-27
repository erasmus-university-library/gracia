import colander

from gracia.jobs.job import Job, job


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
            lambda node, kw: colander.OneOf([k.slug for k in kw["repository"].types.values()])
        ),
    )


class ExportForm(TypeForm):
    graphs = colander.SchemaNode(
        colander.Boolean(),
        title="Graphs",
        description=(
            "Write the graph of every record (the record and everything it is made of, as TriG) "
            "instead of the record itself (N-Triples)."
        ),
        missing=False,
    )


@job("export")
class ExportJob(Job):
    title = "Export"
    description = (
        "Write the records of a type to one file you can download: N-Triples, or the graph of "
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
        "Run the write hooks (label, resource URI, ...) again on the stored records of a type and "
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
        "Forget the old slugs of the records of a type that are there: their old addresses are "
        "then not found any more. The slug each record has now, and everything that belongs to "
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
