"""A second pane on the manage page of a type, for the tests: a view class that adds to the one
that comes with gracia."""

from pyramid.httpexceptions import HTTPFound

from gracia.resources import Type
from gracia.views import flash
from gracia.views.manage import TypeManage


class NotesManage(TypeManage):
    """A second form on the manage page: it posts to the manage view, like every form."""

    panes = (*TypeManage.panes, ("notes", "Notes"))

    def pane_notes(self, state):
        errors = "".join(
            f'<div class="alert alert-danger">{e}</div>' for e in state.get("errors", [])
        )
        return (
            f'{errors}<form id="{self.form_id("notes")}" method="post" action="{self.action("notes")}">'
            f'<input type="hidden" name="__formid__" value="{self.form_id("notes")}">'
            f'<input name="note" value="{state.get("note", "")}"><button name="save">Save</button></form>'
        )

    def post_type_notes(self, _rest):
        note = self.request.POST.get("note", "").strip()
        if not note:
            return {"errors": ["note: Required"], "note": note}
        level = {"warn": "warning", "bad": "danger", "fyi": "info"}.get(note, "success")
        flash(self.request, f"Note kept: {note}", level)
        return HTTPFound(self.action("notes"))


def add_notes(config) -> None:
    """The manage page of a type with the notes pane, in place of the one of gracia (the include
    of gracia is below the application)."""
    config.add_view(NotesManage, attr="manage", context=Type, name="manage", permission="manage")
