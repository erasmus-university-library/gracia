from pyramid.httpexceptions import HTTPFound

from gracia.types.type import MANAGE_TEMPLATE, Type, TypeView


class NotesManage(TypeView):
    panes = (*TypeView.panes, ("notes", "Notes"))

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
        self.request.session.flash(f"Note kept: {note}", queue=level)
        return HTTPFound(self.action("notes"))


def add_notes(config) -> None:
    config.add_view(
        NotesManage,
        attr="manage",
        context=Type,
        name="manage",
        permission="manage",
        renderer=MANAGE_TEMPLATE,
    )
