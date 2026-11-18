from gracia import uri
from gracia.resources import Record


class UserGroupRecord(Record):
    edit_permission = add_permission = "manage"


def includeme(config):
    config.add_record_class(uri.GROUP_CLASS, UserGroupRecord)
