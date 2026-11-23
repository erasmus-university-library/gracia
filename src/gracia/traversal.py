import venusian
from pyramid.exceptions import ConfigurationError
from zope.interface import providedBy

from gracia import uri
from gracia.interfaces import IPrefixTraversable, IRepository, ITraversable


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


def child(parent, segment: str):
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
