import colander
import deform
import deform.widget
from pyramid.response import Response

from gracia.templates import DeformViews, DependentSelectWidget

SEEN: list = []  # (form, field) the shades view was called with
SHADES = {"red": ["crimson", "scarlet"], "blue": ["navy", "azure"]}


class Colours(colander.MappingSchema):
    family = colander.SchemaNode(
        colander.String(),
        widget=deform.widget.SelectWidget(values=[("red", "Red"), ("blue", "Blue")]),
    )
    shade = colander.SchemaNode(
        colander.String(),
        widget=DependentSelectWidget.deferred("shades", depends_on="family"),
    )
    validator = DependentSelectWidget.validator("shade", "shades", depends_on="family")


def colours_form(context, request) -> deform.Form:
    return deform.Form(Colours().bind(request=request, context=context), buttons=())


@DeformViews.declare("shades", permission="view", form=colours_form)
def shades(context, request, values, form, field):
    SEEN.append((form, field))
    return [(s, s.title()) for s in SHADES.get(values.get("family"), [])]


@DeformViews.declare("palette", permission="view", form=colours_form)
def palette(context, request, values, form, field):
    return f"<p id='palette'>{values.get('family', 'none')}</p>"


@DeformViews.declare("hue", permission="edit", form=colours_form)
def hue(context, request, values, form, field):
    if values.get("family") == "blue":
        form["shade"].widget = deform.widget.TextInputWidget()
    return form


@DeformViews.declare("teapot", permission="view", form=colours_form)
def teapot(context, request, values, form, field):
    return Response("short and stout", status=418, content_type="text/plain")


@DeformViews.declare("row", permission="view", form=colours_form)
def row(context, request, values, form, field):
    return DeformViews.render_fields(form, request.params.items(), [field.name])
