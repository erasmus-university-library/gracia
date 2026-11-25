from datetime import UTC, datetime
from urllib.parse import urlsplit

from chameleon import PageTemplateFile
from pyramid.httpexceptions import HTTPBadRequest
from pyramid.response import Response

from gracia import static
from gracia.utils import assets

_TEMPLATES: dict[str, PageTemplateFile] = {}


def last_modified(when, now=None) -> str:
    now = now or datetime.now(UTC).replace(tzinfo=None)
    seconds = max(0, int((now - when).total_seconds()))
    days = seconds // 86400
    if days > 30:
        return f"Last modified on {when.strftime('%A, %B')} {when.day} {when.year}"

    def plural(n: int, unit: str) -> str:
        return f"{n} {unit}{'' if n == 1 else 's'} ago"

    if days:
        return "Last modified " + plural(days, "day")
    if seconds >= 3600:
        return "Last modified " + plural(seconds // 3600, "hour")
    if seconds >= 60:
        return "Last modified " + plural(seconds // 60, "minute")
    return "Last modified just now"


def template(spec: str) -> PageTemplateFile:
    path = str(assets.resolve(spec))
    if path not in _TEMPLATES:
        _TEMPLATES[path] = PageTemplateFile(path)
    return _TEMPLATES[path]


LEVELS = (
    "success",
    "info",
    "warning",
    "danger",
)  # the queues of a flash: Bootstrap's alert colours


def flash(request, message: str, level: str = "success") -> None:
    request.session.flash(message, queue=level)


def _flashes(request) -> list[tuple[str, str]]:
    session = request.session
    return [
        (level, text)
        for level in LEVELS
        if session.peek_flash(level)
        for text in session.pop_flash(level)
    ]


def fragment(request, spec: str, **values) -> str:
    return template(spec)(
        request=request,
        panel=template("gracia:assets/templates/job_panel.pt"),
        **values,
    )


def home_url(request) -> str:
    repository = getattr(getattr(request, "context", None), "repository", None)
    if repository is not None:
        return request.resource_url(repository)
    return f"{request.application_url}/"


def render(request, spec: str, status: int = 200, **values) -> Response:
    values.setdefault("title", values.get("heading", "gracia"))
    values.setdefault("home", home_url(request))
    values.setdefault("flashes", _flashes(request))
    # a page in a dialog (a form in an iframe, see reference.js): no header, no footer
    values.setdefault("embed", request.params.get("embed") == "1")
    # the styles and scripts of every page: in the head, see gracia.static
    static.page.need()
    if values["embed"]:
        static.embed_js.need()  # tells the page around it that the record is saved
    body = template(spec)(
        request=request,
        layout=template(
            "gracia:assets/templates/layout.pt"
        ),  # the page around it: header, content, footer
        panel=template(
            "gracia:assets/templates/job_panel.pt"
        ),  # the job panel, for pages that have one
        favicon=static.url("img/gracia.svg"),
        **values,
    )
    return Response(body=body.encode("utf-8"), content_type="text/html", status=status)


def render_form(
    request,
    heading: str,
    subtitle: str,
    form: str,
    errors=(),
    saved=False,
    status=200,
    widgets=None,
    toggle: tuple[str, str] | None = None,
    modified: str | None = None,
    history: str | None = None,
    announce: dict | None = None,
) -> Response:
    if widgets is not None:
        static.need_form(widgets)
    return render(
        request,
        "gracia:assets/templates/form.pt",
        status,
        heading=heading,
        subtitle=subtitle,
        form=form,
        saved=saved,
        errors=list(errors),
        toggle=toggle,
        modified=modified,
        history=history,
        announce=announce,
    )


class View:
    template: str | None = None  # the template of the page, an asset spec, for `render`

    def __init__(self, request):
        self.request = request
        self.context = request.context

    @property
    def store(self):
        return self.request.store

    @property
    def repository(self):
        return self.context.repository

    def values(self) -> dict:
        return {}

    def render(self, spec: str | None = None, status: int = 200, **values) -> Response:
        return render(
            self.request,
            spec or self.template,  # ty: ignore[invalid-argument-type]
            status,
            **{**self.values(), **values},
        )

    def render_form(
        self, heading: str, subtitle: str, form: str, **options
    ) -> Response:
        return render_form(self.request, heading, subtitle, form, **options)


def same_origin(request) -> None:
    source = request.headers.get("Origin") or request.headers.get("Referer")
    if not source or urlsplit(source).netloc != request.host:
        raise HTTPBadRequest("this request does not come from this site")
