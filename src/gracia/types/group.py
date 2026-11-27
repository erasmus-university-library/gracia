from pyramid.authorization import DENY_ALL

from gracia import uri
from gracia.security import ADMINS
from gracia.types.type import ContentType, Record, RecordView


class UserGroupRecord(Record):
    content_type = ContentType(
        name="UserGroup",
        slug="group",
        class_uri=uri.GROUP_CLASS,
        schema="group-minimal-01.ttl",
        system=True,
    )
    acl = [ADMINS, DENY_ALL]  # noqa: RUF012
    private = True
    edit_permission = add_permission = "manage"


class UserGroupView(RecordView):
    """The views of a group: nothing of its own yet."""
