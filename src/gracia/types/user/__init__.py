import colander
import deform
import deform.widget
import pyoxigraph as ox

from gracia import uri
from gracia.resources import Record
from gracia.utils import passwords


class UserRecord(Record):
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

    def apply(self, data) -> None:
        super().apply(data)
        if data["resource"]["password"]:
            hashed = passwords.hash_password(data["resource"]["password"])
            self.replace(self.resource, uri.CREDENTIALS, ox.Literal(hashed))


def includeme(config):
    config.add_record_class(uri.USER_CLASS, UserRecord)
