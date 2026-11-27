from gracia.extensions import Extension


class Incl(Extension):
    name = "incl"
    title = "Incl"
    description = "Registers a child of the repository in its includeme."


class Child:
    def __init__(self, repository, name):
        self.__parent__, self.__name__ = repository, name


def includeme(config):
    config.add_gracia_traversable(Child, prefix="incl:")
