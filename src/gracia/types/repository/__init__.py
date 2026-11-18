"""The repository: one record of its own type, `rec:repository:1`. Its content holds the title, the
list of the types, the extensions and the schema its own form follows. It is private (administrators
only) and there is only one: it is made with the repository and cannot be added."""

from gracia import uri
from gracia.resources import Record


class RepositoryRecord(Record):
    edit_permission = add_permission = "manage"

    def cannot_be_added(self) -> list[str]:
        return ["A repository has one record, and it was made with the repository."]


def includeme(config):
    config.add_record_class(uri.REPOSITORY_CLASS, RepositoryRecord)
