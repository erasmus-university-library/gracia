"""What can be traversed into below a node, registered the way views are.

A class (or a function) says it is reachable below a node that provides `context` (the
repository, by default), by one name or by a prefix, and `config.scan` finds it:

    @traversable("jobs")                  # /<repo>/jobs
    class Jobs(Node): ...

    @traversable(prefix="rec:")           # /<repo>/rec:person:34
    class Resolved(Node): ...

A node of a name is made by `factory(parent)` (a class takes the parent in its
`__init__`: the repository, for a node of a repository); one of a prefix by
`factory(parent, segment)`, which raises `KeyError` when the segment is not there. Core
and extensions register alike; `child` is what a node asks for its `__getitem__`.
"""

import venusian
from pyramid.exceptions import ConfigurationError
from zope.interface import providedBy

from gracia import uri
from gracia.interfaces import IPrefixTraversable, IRepository, ITraversable


def traversable(
    name: str | None = None, *, prefix: str | None = None, context=IRepository
):
    """Decorator: the class or function is reachable below nodes that provide `context`
    as the segment `name`, or as any segment that starts with `prefix` (found by
    `config.scan`)."""

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
    """Config directive: below a node that provides `context`, the segment `name` (or
    any that starts with `prefix`) is the node `factory` makes. Below a repository a
    key that a type could have as its slug is refused (the repository would not know
    which is meant): keep it among the reserved slugs, or give it a character a slug
    cannot have (`ark:`)."""
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


def child(parent, segment: str):
    """What is below `parent` as `segment`: the node registered for the name, else the
    one of the longest prefix that starts it. None when there is none; `KeyError` when
    a prefix takes the segment and does not find it."""
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
