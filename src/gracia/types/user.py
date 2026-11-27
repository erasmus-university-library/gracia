import colander
import deform
import deform.widget
import pyoxigraph as ox
from pyramid.authorization import DENY_ALL, Allow

from gracia import uri
from gracia.security import ADMINS
from gracia.types.type import ContentType, Record, RecordView
from gracia.utils import passwords


class UserRecord(Record):
    content_type = ContentType(
        name="User",
        slug="user",
        class_uri=uri.USER_CLASS,
        schema="user-minimal-01.ttl",
        system=True,
    )
    # managers may see and change users, nobody else but the administrators
    acl = [ADMINS, (Allow, "group:manager", ["view", "super_edit"]), DENY_ALL]  # noqa: RUF012
    private = True
    edit_permission = add_permission = "super_edit"
    supplies = frozenset({uri.CREDENTIALS})  # the hash is made from the password
    source_editable = False  # a user is made with a password and a check of its id

    def extra_fields(self):
        return [
            colander.SchemaNode(
                colander.String(),
                name="password",
                title="Password" if self.is_new else "New password",
                description=""
                if self.is_new
                else "Leave empty to keep the current password",
                missing=colander.required if self.is_new else "",
                widget=deform.widget.PasswordWidget(redisplay=False),
                validator=colander.Length(min=passwords.MINIMUM_LENGTH),
            )
        ]

    def apply_form(self, data) -> None:
        super().apply_form(data)
        if data["resource"]["password"]:
            hashed = passwords.hash_password(data["resource"]["password"])
            self.graph.replace(self.resource, uri.CREDENTIALS, ox.Literal(hashed))


class UserView(RecordView):
    """The views of a user: nothing of its own yet."""
