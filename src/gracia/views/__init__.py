from datetime import UTC, datetime
from urllib.parse import urlsplit

from chameleon import PageTemplateFile
from pyramid.httpexceptions import HTTPBadRequest
from pyramid.response import Response

from gracia import static
from gracia.utils import assets

_TEMPLATES: dict[str, PageTemplateFile] = {}


def last_modified(when, now=None) -> str:
    """ "Last modified 5 minutes ago": how long ago a record was changed (`when` is a
    naive datetime in UTC), as minutes, hours or days; after 30 days the day itself,
    "Last modified on Monday, August 23 2026"."""
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
    """The template an asset spec names (loaded once per file: Chameleon reloads it when
    it changes on disk)."""
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
    """Say something on the next page that is shown (a redirect after a post): it is
    kept in the session, a signed cookie, and shown once, in the layout, as an alert of
    that `level`."""
    request.session.flash(message, queue=level)


def _flashes(request) -> list[tuple[str, str]]:
    """What was said with `flash`, taken from the session so that it is shown only once.
    Looking is not changing: only a queue that has something in it is popped, so a page
    with nothing to say sets no cookie."""
    session = request.session
    return [
        (level, text)
        for level in LEVELS
        if session.peek_flash(level)
        for text in session.pop_flash(level)
    ]


def fragment(request, spec: str, **values) -> str:
    """The HTML of a template without the page around it: for a part of a page that a
    registered object makes (a tab of the manage page)."""
    return template(spec)(
        request=request,
        panel=template("gracia:assets/templates/job_panel.pt"),
        **values,
    )


def home_url(request) -> str:
    """Where the logo of a page goes: the repository the page is in, else the
    application."""
    repository = getattr(getattr(request, "context", None), "repository", None)
    if repository is not None:
        return request.resource_url(repository)
    return f"{request.application_url}/"


def render(request, spec: str, status: int = 200, **values) -> Response:
    """An HTML response from the template `spec`; what the page needs is in
    `gracia.static`."""
    values.setdefault("title", values.get("heading", "gracia"))
    values.setdefault("home", home_url(request))
    values.setdefault("flashes", _flashes(request))
    # the styles and scripts of every page: in the head, see gracia.static
    static.page.need()
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
) -> Response:
    """The page around a rendered Deform form. `widgets` is the form itself, for the
    scripts and styles its widgets need (see `static.need_form`). `toggle` is a link,
    (text, address), shown above the form at the right (the source of a record instead
    of its form, and back); `history` is the address of the history of the record, a
    button next to it, and `modified` the words above the buttons that say when it was
    last changed."""
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
    )


class View:
    """The base of the class views: what every one of them wants at hand (the request,
    its context, the store, the repository) and the rendering of a page, so that a
    subclass says only what is its own, and the pages share their settings (`template`,
    and whatever a subclass adds to `values`). A subclass is registered like any view:
    `@view_config` on its methods, or `config.add_view(Cls, attr="edit", context=...)`
    for a record class of a type."""

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
        """What every page of this class is given besides what it passes to `render`."""
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
    """Refuse a POST that does not come from a page of this site: its `Origin` (or else
    its `Referer`) must name this host. A browser sends HTTP Basic credentials by
    itself, so without this any page could post for the user; the cross-site fetch or
    form always says where it is from. (Pyramid's `check_csrf_origin` only looks at
    HTTPS requests.)"""
    source = request.headers.get("Origin") or request.headers.get("Referer")
    if not source or urlsplit(source).netloc != request.host:
        raise HTTPBadRequest("this request does not come from this site")
