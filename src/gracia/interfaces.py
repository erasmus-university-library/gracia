from __future__ import annotations

from typing import TYPE_CHECKING

import venusian
from pyramid.exceptions import ConfigurationError
from zope.interface import Attribute, Interface, providedBy

from gracia import uri

if TYPE_CHECKING:
    from gracia.types.repository import Repository


class IRecordType(Interface):
    """What is registered for the class of the resources of a type (`gracia:class`): the
    class of its records, a subclass of `Record` (the utility is that class, by the
    class
    uri and by the slug of its type). A class that declares a type has a `content_type`
    (`ContentType`: the name, the slug, the schema, ...)."""

    content_type = Attribute("what the class declares of its type, or None")


def known_types(registry) -> list:
    """The record classes that declare a type, by slug."""
    unique = {
        id(cls): cls
        for _name, cls in registry.getUtilitiesFor(IRecordType)
        if cls.content_type is not None
    }
    return sorted(unique.values(), key=lambda cls: cls.content_type.slug)


def content_types(registry) -> dict:
    """The types a repository can be given, by slug: all but the system types."""
    return {
        cls.content_type.slug: cls
        for cls in known_types(registry)
        if not cls.content_type.system
    }


class IJob(Interface):
    name = Attribute("the name it is started by")
    title = Attribute("what the jobs view calls it")
    description = Attribute("one sentence for the jobs view")
    groups = Attribute("the groups that may start it besides the administrators")
    start_permission = Attribute("the permission to start it: start_<name>_job")


class IDeformView(Interface):
    pass


class IRepository(Interface):
    pass


class IType(Interface):
    """A type of a repository: the node that its records are below
    (`rec:<slug>:<n>`)."""

    slug = Attribute("the first path segment of its records")
    name = Attribute("the name of the type")
    class_uri = Attribute("the rdf class of the resources of its records")


class IRecord(Interface):
    """A record of a type: its subject (`rec:<slug>:<n>`) and what is stored of it."""

    uri = Attribute("the subject of the record (None: it is not stored yet)")
    graph = Attribute("what is stored of the record")


class IExtension(Interface):
    # what the author of an extension says (class attributes)
    name = Attribute("the name of the extension (a class that has none is not one)")
    title = Attribute("what the extensions tab calls it")
    description = Attribute("one or two sentences for the extensions tab")
    settings_schema = Attribute(
        "a Colander schema class (a mapping) of the settings, or None for an extension "
        "without "
        "any. Every field needs a `default` (what the extension gets until someone "
        "says) and a "
        "`missing` (so that JSON may leave it out). The user interface builds the "
        "Deform form from "
        "it, after `bind(repository=..., request=...)`, so `colander.deferred` works."
    )
    repository = Attribute("the repository this extension is the extension of")

    # reading: cheap, no side effects (properties, which a subclass may override)
    enabled = Attribute(
        "whether the extension is on in this repository: the flag that was stored, "
        "else off. An "
        "extension may ignore the flag (always on) or look at the repository"
    )
    can_disable = Attribute(
        "whether it may be turned off (an extension that is always on says no)"
    )
    settings = Attribute(
        "the settings as a plain dict: what was stored, read leniently (a missing "
        "setting is its "
        "default, an unknown key is ignored, a value the schema no longer takes is its "
        "default). "
        "A new dict every time: to change settings use `save_settings`"
    )

    # changing: these write to the repository and may raise
    # `gracia.extensions.ExtensionError`
    def enable():
        pass

    def disable():
        pass

    def save_settings(data):
        pass

    def export():
        pass

    def import_json(text):
        pass


class ITraversable(Interface):
    pass


class IPrefixTraversable(Interface):
    pass


class IPredicate(Interface):
    uri = Attribute("the property it is the code for")

    def schema_node(record, request, schema_node=None):
        pass

    def get(record):
        pass

    def put(record, text):
        pass

    def begin(record):
        pass

    def derive(record):
        pass


class Node:
    """A node of the traversal tree below a repository: what its children, jobs and
    other things are made of."""

    def __init__(self, repository, parent=None):
        self.repository: Repository = repository
        self.__parent__ = repository if parent is None else parent

    @property
    def store(self):
        return self.repository.store

    @property
    def registry(self):
        return self.repository.registry

    @property
    def application(self):
        return self.repository.application


def traversable(
    name: str | None = None, *, prefix: str | None = None, context=IRepository
):

    def decorate(factory):
        def callback(scanner, _name, _obj):
            scanner.config.add_gracia_traversable(
                factory, name=name, prefix=prefix, context=context
            )

        venusian.attach(factory, callback, category="pyramid")
        return factory

    return decorate


def add_gracia_traversable(
    config,
    factory,
    name: str | None = None,
    prefix: str | None = None,
    context=IRepository,
) -> None:
    key = name if name is not None else prefix
    if key is None or (name is not None and prefix is not None):
        raise ConfigurationError("give a name or a prefix, not both and not neither")
    if context is IRepository and uri.slug_like(key) and key not in uri.RESERVED_SLUGS:
        raise ConfigurationError(
            f"{key!r} could be the slug of a type: add it to uri.RESERVED_SLUGS or use "
            "another key"
        )
    provided = ITraversable if name is not None else IPrefixTraversable

    def register():
        config.registry.registerAdapter(factory, (context,), provided, name=key)

    config.action(("gracia traversable", context, provided, key), register)


def find_child(parent, segment: str):
    """The node below `parent` at the path segment: by its name, else by the longest
    prefix that is registered for it (`rec:`). None: nothing is there."""
    registry = parent.registry
    found = registry.queryAdapter(parent, ITraversable, name=segment)
    if found is not None:
        return found
    starts = [
        (prefix, factory)
        for prefix, factory in registry.adapters.lookupAll(
            (providedBy(parent),), IPrefixTraversable
        )
        if segment.startswith(prefix)
    ]
    if not starts:
        return None
    return max(starts, key=lambda pair: len(pair[0]))[1](parent, segment)
