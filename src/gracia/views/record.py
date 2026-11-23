import deform
import pyoxigraph as ox
from pyramid.httpexceptions import HTTPFound
from pyramid.view import view_config

from gracia import static, uri
from gracia.forms import Form
from gracia.graph import Graph, ParseError
from gracia.resources import Record, Type
from gracia.store import DuplicateValue
from gracia.views import View, fragment, last_modified

FORMATS = (
    "turtle",
    "ntriples",
    "rdfxml",
    "jsonld",
)  # what the text of a record can be written in
DEFAULT_FORMAT = "turtle"
EDITORS = {
    "turtle": "turtle",
    "ntriples": "turtle",
    "rdfxml": "xml",
    "jsonld": "json",
}  # editor.js
_N = ox.NamedNode


class SourceError(ValueError):
    """What is wrong with a text of a record: `errors` says what, one text each."""

    def __init__(self, errors):
        self.errors = [errors] if isinstance(errors, str) else list(errors)
        super().__init__("; ".join(self.errors))


class RecordView(View):
    def __init__(self, request):
        super().__init__(request)
        self.new = isinstance(
            self.context, Type
        )  # an add page: the context is the type
        self.res_type = self.context if self.new else self.context.type
        self.record = self.res_type.blank_record() if self.new else self.context
        self.view = "add" if self.new else "edit"
        self.target = (
            self.res_type if self.new else self.record
        )  # what the page is the page of
        self.address = request.resource_url(self.target, self.view)
        if self.new:
            self.heading, self.subtitle = (
                f"Add {self.res_type.name}",
                self.res_type.record,
            )
        else:
            self.heading, self.subtitle = self.record.title, self.record.uri

    # ---- the two views
    # -------------------------------------------------------------------

    @view_config(context=Record, name="edit", permission="edit")
    def edit(self):
        record = self.record
        if isinstance(record.__parent__, Type) and record.__parent__.slug == "type":
            # a type is configured on its manage page, in the Settings tab
            for res_type in record.repository.res_types.values():
                if res_type.record == record.uri:
                    return HTTPFound(
                        self.request.resource_url(
                            res_type, "manage", query={"tab": "settings"}
                        )
                    )
        return self.respond()

    @view_config(context=Type, name="add", permission="add")
    def add(self):
        problems = self.record.cannot_be_added()
        if problems:  # a record made with this form would be incomplete
            return self.page("", errors=problems, status=501)
        return self.respond()

    # ---- the page
    # ------------------------------------------------------------------------

    def respond(self):
        if (
            "source_text" in self.request.POST or "source" in self.request.GET
        ) and self.record.source_editable:
            return self.source_page()
        return self.form_page()

    def saved(self) -> HTTPFound:
        return HTTPFound(
            self.request.resource_url(self.record, "edit", query={"saved": "1"})
        )

    def page(self, body: str, errors=(), **options):
        if (
            not self.new
        ):  # when it was last changed, and the way to its history (not of a user:
            when = self.store.record_modified(
                self.record.uri
            )  # the history has the credentials)
            options.setdefault("modified", last_modified(when) if when else None)
            if self.record.source_editable:
                options.setdefault(
                    "history", self.request.resource_url(self.record, "history")
                )
        return self.render_form(
            self.heading, self.subtitle, body, errors=errors, **options
        )

    # ---- the form
    # ------------------------------------------------------------------------

    def form_page(self):
        request, record = self.request, self.record
        form = Form(record.edit_schema(request), tabs="Record", buttons=("save",))
        errors: list[str] = []
        if "save" in request.POST:
            try:
                data = form.validate(list(request.POST.items()))
            except deform.ValidationFailure as failure:
                rendered = failure.render()
            else:
                errors = record.check(data)
                if not errors:
                    try:
                        self.commit_form(data)
                    except (
                        DuplicateValue
                    ) as err:  # the database refused a value that is taken
                        errors = [str(err)]
                    else:
                        return self.saved()
                rendered = form.render(data)
        else:
            rendered = form.render(record.edit_values())
        toggle = None
        if record.source_editable:
            query = {"source": DEFAULT_FORMAT}
            toggle = (
                "Source Edit",
                request.resource_url(self.target, self.view, query=query),
            )
        return self.page(
            rendered,
            errors=errors,
            saved="saved" in request.GET,
            widgets=form,
            toggle=toggle,
        )

    def commit_form(self, data: dict) -> None:
        """Store what the form said (the number of a new record is made here)."""
        if self.new:
            self.record.begin(self.store.mint_id(self.res_type.slug))
        self.record.apply(data)
        self.record.save()

    # ---- the source
    # ----------------------------------------------------------------------

    def source_page(self):
        """The text of the record in place of the form; saved when it is posted with
        Save."""
        request = self.request
        prefix_map = self.repository.prefix_map()
        errors: list[str] = []
        if "source_text" in request.POST:
            text = request.POST["source_text"]
            was = self.known_format(request.POST.get("source_format_was"))
            chosen = self.known_format(request.POST.get("source_format"))
            try:
                graph = Graph.parse(text, was)
                if "convert" in request.POST:
                    text, was = graph.serialize(chosen, prefix_map), chosen
                else:
                    self.check_source(graph)
                    self.commit_source(graph)
                    return self.saved()
            except (ParseError, SourceError) as err:
                errors = getattr(err, "errors", [str(err)])
            except DuplicateValue as err:
                errors = [str(err)]
            fmt = chosen if "convert" in request.POST and not errors else was
        else:
            fmt = was = self.known_format(request.GET.get("source"))
            start = self.res_type.skeleton() if self.new else self.record.shown_graph()
            text = start.serialize(fmt, prefix_map)
        static.source_js.need()
        static.editor_js.need()  # the text is highlighted
        body = fragment(
            request,
            "gracia:assets/templates/source_form.pt",
            action=self.address,
            formats=FORMATS,
            fmt=fmt,
            editor=EDITORS[fmt],
            was=was,
            text=text,
            new=self.new,
            placeholder=self.res_type.placeholder,
        )
        return self.page(body, errors=errors, toggle=("Form Edit", self.address))

    @staticmethod
    def known_format(name: str | None) -> str:
        return name if name in FORMATS else DEFAULT_FORMAT

    def subject(self) -> str:
        """What the record is called in its text: its address, or a placeholder for the
        number a new one has not got yet."""
        return self.res_type.placeholder if self.new else self.record.uri

    def check_source(self, graph) -> None:
        """`SourceError` unless the text is this record, of this type, and follows the
        shape of the record. The hooks run first, so that what they make (the label, the
        ARK) is there."""
        if not self.new:
            graph = self.record.with_hidden_kept(graph)  # what is never shown is kept
        problems = self.record.structure_problems(graph, self.subject())
        if not problems:
            trial = self.record
            if self.new:  # a record of its own, so that nothing is used up by the trial
                trial = self.res_type.blank_record()
                trial.uri, trial.__name__ = self.subject(), "0"
            trial.graph = self.store.apply_hooks(graph)
            problems = trial.shape_problems()
        if problems:
            raise SourceError(problems)

    def commit_source(self, graph) -> None:
        """Store what the text said (it has been checked)."""
        record, slug = self.record, self.res_type.slug
        if self.new:
            number = self.store.mint_id(slug)  # only now that the text is right
            record.__name__, record.uri = str(number), uri.record_uri(slug, number)
            record.graph = graph.remap(self.subject(), record.uri)
        else:
            record.graph = self.record.with_hidden_kept(graph).copy()
        record.save()
