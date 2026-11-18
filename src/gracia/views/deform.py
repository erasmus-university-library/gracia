import colander
import peppercorn
import venusian
from deform.field import Field
from deform.widget import SelectWidget
from pyramid.httpexceptions import HTTPForbidden, HTTPNotFound
from pyramid.location import lineage
from pyramid.security import NO_PERMISSION_REQUIRED
from pyramid.view import view_config
from zope.interface import directlyProvides

from gracia.interfaces import IDeformView
from gracia.resources import DeformView, Repository
from gracia.views import template

RENDERER = "gracia-deform"  # no dot: a dot makes Pyramid look for a file extension


def deform_view(name: str, *, form, at: str | None = None, permission: str = "view"):
    """Decorator: the function `(context, request, values, form, field)` is the deform
    view `name`, served at `/<repo>/deform/<name>` (`serve`, below), of the form that
    `form(context, request)` makes. `context` is the repository, or the node below it
    called `at` (`at="jobs"`), and `permission` is asked of that node. What it answers
    is above."""

    def decorate(function):
        function.deform_form = (
            form  # the view and a widget's direct call make the form this way
        )
        function.deform_at = at
        function.deform_permission = permission

        def callback(scanner, _name, _obj):
            scanner.config.add_gracia_deform_view(name, function)

        venusian.attach(function, callback, category="pyramid")
        return function

    return decorate


def call(function, context, request, values, form=None, field=None):
    """Call a deform view the way the view does: with the form it was declared for, made
    fresh unless the caller has the form already (a widget, while the form is rendered),
    and the target field (None for a view of the whole form)."""
    form = form or function.deform_form(context, request)
    return function(context, request, values, form, field)


def render_posted(form, controls) -> str:
    """The form filled with what the browser posted (`controls`: the name and value
    pairs), as it would be shown again, but without validating (so without errors)."""
    cstruct = form.widget.deserialize(form, peppercorn.parse(controls))
    form.cstruct = cstruct
    return form.widget.serialize(form, cstruct)


def render_fields(form, controls, names=None) -> str:
    """Some fields of the form (all by default) as rows with their labels and errors,
    filled with what the browser posted, without validating, and without the `<form>`,
    the fieldset and the hidden inputs: to swap one row of a page's own form
    (`names=[field.name]`)."""
    cstruct = form.widget.deserialize(form, peppercorn.parse(controls))
    form.cstruct = cstruct
    posted = cstruct if isinstance(cstruct, dict) else {}
    chosen = [form[name] for name in names] if names else list(form.children)
    return "".join(
        child.render_template(
            "mapping_item", field=child, cstruct=posted.get(child.name, colander.null)
        )
        for child in chosen
    )


def add_gracia_deform_view(config, name: str, function) -> None:
    """Config directive: `function` is the deform view called `name`."""

    def register():
        directlyProvides(function, IDeformView)
        config.registry.registerUtility(function, IDeformView, name=name)

    config.action(("gracia deform view", name), register)


class DeformViewRenderer:
    """The answer of a deform view: `<option>` elements for a list, a string as it is,
    the form rendered with what was posted."""

    def __init__(self, info):
        pass

    def __call__(self, value, system):
        response = system["request"].response
        response.content_type = "text/html"
        response.headers["Cache-Control"] = "private, no-store"
        if isinstance(value, str):
            return value
        if isinstance(value, Field):
            return render_posted(value, system["request"].params.items())
        return template("gracia:assets/templates/options.pt")(options=list(value))


def _source(request, name: str):
    found = request.registry.queryUtility(IDeformView, name)
    if found is None:
        raise LookupError(
            f"no deform view called {name!r}: declare it with @deform_view({name!r}, "
            "...)"
        )
    return found


class DependentSelectWidget(SelectWidget):
    """A select whose options come from a deform view and follow another field of the
    form."""

    def __init__(self, *, view: str, depends_on: str, context, request, **kw):
        super().__init__(values=(), **kw)
        self.source, self.depends_on = view, depends_on
        self.context, self.request = context, request
        self.provider = _source(
            request, view
        )  # unknown names fail when the form is made

    def serialize(self, field, cstruct, **kw):
        parent = field.parent
        other = parent[self.depends_on]
        posted = (
            parent.cstruct.get(self.depends_on)
            if isinstance(parent.cstruct, dict)
            else None
        )
        if not posted:  # the first time: what the other select shows, its first choice
            first = list(getattr(other.widget, "values", None) or [])
            posted = first[0][0] if first else ""
        root = field
        while root.parent is not None:
            root = root.parent
        self.values = list(  # ty: ignore[invalid-assignment]
            call(
                self.provider,
                self.context,
                self.request,
                {self.depends_on: posted},
                root,
                field,
            )
        )
        repository = next(n for n in lineage(self.context) if isinstance(n, Repository))
        url = self.request.resource_url(
            repository, "deform", self.source, query={"field": field.name}
        )
        self.attributes = {
            "hx-get": url,
            "hx-trigger": f"change from:#{other.oid}",
            "hx-include": f"#{other.oid}",
            "hx-target": "this",
            "hx-swap": "innerHTML",
        }
        return super().serialize(field, cstruct, **kw)


def dependent_select(view: str, *, depends_on: str):
    """The widget for a field, to give to `SchemaNode(widget=...)`: it needs `context`
    (the node the source is a view of) and `request` when the schema is bound."""

    @colander.deferred
    def widget(node, kw):
        return DependentSelectWidget(
            view=view,
            depends_on=depends_on,
            context=kw["context"],
            request=kw["request"],
        )

    return widget


def options_validator(field: str, view: str, *, depends_on: str):
    """A validator for the schema (the mapping) that holds both fields: the value of
    `field` must be one of what the view offers for the value of `depends_on`."""

    @colander.deferred
    def validator(node, kw):
        provider = _source(kw["request"], view)

        def check(form, value):
            offered = {
                v
                for v, _label in call(
                    provider,
                    kw["context"],
                    kw["request"],
                    {depends_on: value[depends_on]},
                    None,
                    None,
                )
            }
            if value[field] not in offered:
                error = colander.Invalid(form)
                error[field] = (
                    f"Not one of the choices for this {depends_on.removeprefix('res_')}"
                )
                raise error

        return check

    return validator


# ---- over HTTP: `GET /<repo>/deform/<name>`
# -----------------------------------------------------


@view_config(
    context=DeformView,
    request_method="GET",
    renderer=RENDERER,
    # asked below, of the node the view was declared for
    permission=NO_PERMISSION_REQUIRED,
)
def serve(node, request):
    function, target = node.function, node.target
    if not request.has_permission(function.deform_permission, target):
        raise HTTPForbidden(f"{node.__name__} needs {function.deform_permission}")
    form = function.deform_form(target, request)
    wanted = request.params.get("field")
    try:
        field = form[wanted] if wanted else None
    except KeyError:
        raise HTTPNotFound(f"the form has no field {wanted!r}") from None
    return function(target, request, request.params, form, field)
