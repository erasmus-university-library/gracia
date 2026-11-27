from importlib.metadata import entry_points

import colander
import venusian
from zope.interface import classImplements

from gracia.interfaces import IJob

ENTRY_POINT_GROUP = "gracia.jobs"
BUILTIN_PACKAGES = ["gracia.jobs.validate", "gracia.jobs.type_jobs", "gracia.jobs.repository_jobs"]


class Job:
    name = ""
    start_permission = ""
    title = ""
    description = ""
    groups = ("editor", "manager")  # besides the administrators
    params_schema = colander.MappingSchema  # the form; bound with `repository=` (a Repository node)

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


def add_gracia_job(config, name: str, cls: type) -> None:

    def register():
        cls.name = name
        cls.start_permission = f"start_{name}_job"
        classImplements(cls, IJob)
        config.registry.registerUtility(cls(), IJob, name=name)

    config.action(("gracia job", name), register)


def all_jobs(registry) -> list:
    return sorted((found for _name, found in registry.getUtilitiesFor(IJob)), key=lambda j: j.title)


def scan_targets(settings: dict) -> list[str]:
    found = list(BUILTIN_PACKAGES)
    found += [entry.value.split(":")[0] for entry in entry_points(group=ENTRY_POINT_GROUP)]
    found += settings.get("gracia.jobs", "").split()
    unique = list(dict.fromkeys(found))
    return [
        name
        for name in unique
        if not any(name.startswith(other + ".") for other in unique if other != name)
    ]
