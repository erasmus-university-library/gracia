"""An extension module for the tests: a class marked @extension, and an includeme for what a
decorator cannot say."""

from gracia.extensions import Extension, extension


@extension("incl")
class Incl(Extension):
    title = "Incl"
    description = "Registers a child of the repository in its includeme."


class Child:
    def __init__(self, repository, name):
        self.__parent__, self.__name__ = repository, name


def includeme(config):
    config.add_gracia_repository_child("incl:", Child, prefix=True)
