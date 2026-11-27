"""Finding what the application is made of. The modules of the places it is looked in (the
packages of gracia, the entry points of the groups `gracia.content_types` and
`gracia.extensions` and the settings `gracia.types` and `gracia.extensions`) are imported, and what a module defines that implements an interface that
is in `HANDLERS` is given to the directive of that interface: stating that a class
implements `IRecord` is all a type has to do to be found. A module that has an
`includeme(config)` is included as well."""

import importlib
import inspect
import pkgutil
from importlib.metadata import entry_points

from gracia.interfaces import IExtension, IPredicate, IRecord, IType

ENTRY_POINT_GROUP = "gracia.content_types"  # packages with types
ENTRY_POINT_GROUPS = (ENTRY_POINT_GROUP, "gracia.extensions")
SETTINGS = ("gracia.types", "gracia.extensions")  # the packages that are listed
BUILTIN_TARGETS = ("gracia.types", "gracia.predicates", "gracia.extensions")
HANDLERS = (  # the interface, the directive for a class that implements it
    (IRecord, "add_gracia_type"),
    (IType, "add_gracia_type_node"),
    (IExtension, "add_gracia_extension"),
    (IPredicate, "add_gracia_predicate"),
)


def targets(settings: dict) -> list[str]:
    found = list(BUILTIN_TARGETS)
    for group in ENTRY_POINT_GROUPS:
        for entry in entry_points(group=group):
            found.append(entry.value.split(":")[0])
    for key in SETTINGS:
        found += settings.get(key, "").split()
    unique = list(dict.fromkeys(found))
    return [
        name
        for name in unique
        if not any(name.startswith(other + ".") for other in unique if other != name)
    ]


def modules(target: str) -> list:
    package = importlib.import_module(target)
    found = [package]
    if hasattr(package, "__path__"):
        for info in pkgutil.walk_packages(package.__path__, target + "."):
            found.append(importlib.import_module(info.name))
    return found


def scan(config, places: list[str]) -> None:
    done: set[str] = (
        set()
    )  # a module is scanned once by a scan, as often as it is asked
    for place in places:
        for module in modules(place):
            if module.__name__ in done:
                continue
            done.add(module.__name__)
            if "includeme" in vars(module):
                config.include(module)
            for found in vars(module).values():
                if not inspect.isclass(found) or found.__module__ != module.__name__:
                    continue
                for interface, directive in HANDLERS:
                    if interface.implementedBy(found):
                        getattr(config, directive)(found)
