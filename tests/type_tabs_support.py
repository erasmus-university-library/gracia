from pyramid.httpexceptions import HTTPFound

from gracia.resources import Type
from gracia.views import flash
from gracia.views.manage import TypeManage


class NotesManage(TypeManage):
    panes = (*TypeManage.panes, ("notes", "Notes"))

    def pane_notes(self, state):
        errors = "".join(
            f'<div class="alert alert-danger">{e}</div>'
            for e in state.get("errors", [])
        )
        return (
            f'{errors}<form id="{self.form_id("notes")}" method="post" '
            f'action="{self.action("notes")}">'
            f'<input type="hidden" name="__formid__" value="{self.form_id("notes")}">'
            f'<input name="note" value="{state.get("note", "")}"><button '
            'name="save">Save</button></form>'
        )

    def post_type_notes(self, _rest):
        note = self.request.POST.get("note", "").strip()
        if not note:
            return {"errors": ["note: Required"], "note": note}
        level = {"warn": "warning", "bad": "danger", "fyi": "info"}.get(note, "success")
        flash(self.request, f"Note kept: {note}", level)
        return HTTPFound(self.action("notes"))


def add_notes(config) -> None:
    config.add_view(
        NotesManage, attr="manage", context=Type, name="manage", permission="manage"
    )
