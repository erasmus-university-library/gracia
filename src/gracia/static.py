from pathlib import Path

import deform
import fanstatic
from fanstatic import ConfigurationError, Library, Publisher, Resource
from fanstatic.injector import TopBottomInjector
from pyramid.path import AssetResolver
from pyramid.settings import asbool

from gracia.utils import assets

SIGNATURE = "_static"  # the first path segment of every URL here: never a repository id

library = Library("gracia", "assets/static")
bootstrap_css = Resource(library, "css/bootstrap.min.css")
gracia_css = Resource(library, "css/gracia.css", depends=[bootstrap_css])
bootstrap_js = Resource(library, "scripts/bootstrap.bundle.min.js")
htmx_js = Resource(
    library, "scripts/htmx.min.js"
)  # htmx 2: attributes in the HTML ask the server
jobs_js = Resource(library, "scripts/jobs.js", depends=[bootstrap_js, htmx_js])
api_browser_js = Resource(library, "scripts/apibrowser.js")
manage_js = Resource(
    library, "scripts/manage.js", depends=[bootstrap_js]
)  # ?tab= follows the tab
job_log_js = Resource(
    library, "scripts/joblog.js"
)  # follows a log by asking for what is new
deform_load_js = Resource(
    library, "scripts/deform-load.js"
)  # deform.load() once the page is there
source_js = Resource(
    library, "scripts/source.js"
)  # the format of a source is changed in place
# code editors with syntax highlighting (CodeMirror 5, vendored:
# static/vendor/codemirror/)
codemirror_css = Resource(
    library, "vendor/codemirror/codemirror.css", depends=[gracia_css]
)
codemirror_js = Resource(
    library, "vendor/codemirror/codemirror.min.js", depends=[codemirror_css]
)
codemirror_sparql_js = Resource(
    library, "vendor/codemirror/sparql.js", depends=[codemirror_js]
)
codemirror_json_js = Resource(
    library, "vendor/codemirror/javascript.js", depends=[codemirror_js]
)
codemirror_xml_js = Resource(
    library, "vendor/codemirror/xml.js", depends=[codemirror_js]
)
editor_js = (
    Resource(  # a textarea with data-editor="sparql", "json" or "xml" becomes an editor
        library,
        "scripts/editor.js",
        depends=[
            codemirror_sparql_js,
            codemirror_json_js,
            codemirror_xml_js,
            bootstrap_js,
        ],
    )
)

# the history of a record: a version is shown in a dialog with the editor, and what it
# changed
history_js = Resource(library, "scripts/history.js", depends=[editor_js])

# what every page has: the styles of the site and Bootstrap's scripts (modals,
# dropdowns)
page = fanstatic.Group([gracia_css, bootstrap_js])

_registry = fanstatic.get_library_registry()
_registry.add(
    library
)  # a library is known to fanstatic when it is added: nothing finds it by itself

deform_library = Library("deform", AssetResolver().resolve("deform:static").abspath())
_registry.add(deform_library)
deform_jquery = Resource(
    deform_library, "scripts/jquery-2.0.3.min.js"
)  # deform names no jQuery
_known: dict[str, Resource] = {}  # asset spec -> resource: what widgets can name


def _deform_specs() -> list[str]:
    found: list[str] = []
    for versions in deform.Form.default_resource_registry.registry.values():
        for res_types in versions.values():
            for names in res_types.values():
                found += [names] if isinstance(names, str) else list(names)
    return sorted(set(found))


for _spec in _deform_specs():
    _relative = _spec.removeprefix("deform:static/")
    if _spec == "deform:static/scripts/deform.js":
        continue  # needs jquery.form, made first
    _known[_spec] = Resource(
        deform_library,
        _relative,
        depends=[deform_jquery] if _relative.endswith(".js") else [],
    )
# the sequence widget (a list of items that can be reordered) names its script in a
# requirement
# that the resource registry does not list
_known["deform:static/scripts/jquery-sortable.js"] = Resource(
    deform_library, "scripts/jquery-sortable.js", depends=[deform_jquery]
)
_known["deform:static/scripts/deform.js"] = Resource(
    deform_library,
    "scripts/deform.js",
    depends=[_known["deform:static/scripts/jquery.form-3.09.js"]],
)


def _resource(spec: str) -> Resource:
    if spec in _known:
        return _known[spec]
    path = assets.resolve(spec).resolve()
    for found in _registry.values():
        root = Path(found.path).resolve()
        if path.is_relative_to(root):
            relative = path.relative_to(root).as_posix()
            if relative in found.known_resources:
                return found.known_resources[relative]
            raise LookupError(
                f"{spec} is in the library {found.name!r} but is not one of its "
                "resources: "
                f"declare it with fanstatic.Resource({found.name}_library, "
                f"{relative!r})"
            )
    raise LookupError(
        f"{spec} is in no library: declare a fanstatic Library and Resource for it and "
        "register "
        "the library with config.add_gracia_library (or the fanstatic.libraries entry "
        "point)"
    )


def add_library(config, new: Library) -> None:

    def register():
        if _registry.get(new.name) is new:
            return  # the same library, from another configuration of the same process
        if _registry.prepared:
            raise ConfigurationError(
                f"the library {new.name!r} was added after the first request: "
                "declare libraries when the application is configured"
            )
        _registry.add(new)

    config.action(("gracia library", new.name), register)


def need_form(form) -> None:
    found = form.get_widget_resources()
    for spec in (*found["js"], *found["css"]):
        _resource(spec).need()
    if found["js"]:
        deform_jquery.need()
        deform_load_js.need()


def url(name: str) -> str:
    return fanstatic.get_needed().library_url(library) + "/" + name


def tween_factory(handler, registry):
    settings = registry.settings
    options = {
        "versioning": True,
        "versioning_use_md5": True,
        "recompute_hashes": asbool(
            settings.get("gracia.fanstatic.recompute_hashes", False)
        ),
        "publisher_signature": SIGNATURE,
        "bundle": True,
    }
    injector = TopBottomInjector(options)  # takes its own options out
    publisher = Publisher(fanstatic.get_library_registry())
    prefix = f"/{SIGNATURE}/"

    def tween(request):
        if request.path_info.startswith(prefix):
            served = request.copy()
            served.path_info_pop()  # the signature: the publisher takes it from here
            response = served.get_response(publisher)
            if response.status_int != 404:
                return response
        # an absolute address, like every other one (the application URL has the host,
        # and the
        # script name)
        needed = fanstatic.init_needed(base_url=request.application_url, **options)
        try:
            response = handler(request)
            is_html = (response.content_type or "").lower() in (
                "text/html",
                "application/xhtml+xml",
            )
            if is_html and needed.has_resources():
                response.body = injector(response.body, needed, request, response)
            return response
        finally:
            fanstatic.del_needed()

    return tween


def includeme(config) -> None:
    config.add_tween("gracia.static.tween_factory")
