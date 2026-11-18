from gracia import uri
from gracia.resources import Record


class RepositoryRecord(Record):
    edit_permission = add_permission = "manage"

    def cannot_be_added(self) -> list[str]:
        return ["A repository has one record, and it was made with the repository."]


def includeme(config):
    config.add_record_class(uri.REPOSITORY_CLASS, RepositoryRecord)
