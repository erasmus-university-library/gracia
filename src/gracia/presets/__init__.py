"""Presets: what a new repository is configured with, as classes. `create_repository`
takes the name of one (`minimal`, `schemaorg`, ...) and runs it once, in the setup of the
repository: `Preset(repository, admin=..., ark=..., content_types=...).apply()`.

A preset is idempotent: run again on a repository that it has run on, it changes nothing
(a type it adds is there already, a setting it sets has the value). That makes a preset
fit to upgrade a repository, later: a newer one adds what was not there.

A preset builds on another by subclassing it (`SchemaOrgPreset(MinimalPreset)`), and
registers by its `name`: the module of that name in this package is imported when it is
asked for."""

import importlib
import re

from gracia import uri

DEFAULT_PRESET = "minimal"
_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
PRESETS: dict[str, type["Preset"]] = {}


class Preset:
    name = ""

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if cls.name:
            PRESETS[cls.name] = cls

    def __init__(
        self,
        repository,
        admin: tuple[str, str] | None = None,
        ark: tuple[str, str] = (uri.DEFAULT_NAAN, uri.DEFAULT_ROOT_PREFIX),
        content_types=(),
    ):
        self.repository = repository
        self.admin = admin
        self.ark = ark
        self.content_types = list(content_types)

    def validate(self) -> None:
        """Refuses what it cannot do (a ValueError), before anything is made."""

    def apply(self) -> None:
        """Does it; the repository has its store, and the types the repository has."""
        raise NotImplementedError


def find(name: str) -> type[Preset]:
    if name not in PRESETS and _NAME.match(name or ""):
        try:
            importlib.import_module(f"gracia.presets.{name}")
        except ModuleNotFoundError as err:
            if err.name != f"gracia.presets.{name}":
                raise
    if name not in PRESETS:
        raise ValueError(f"no preset {name!r} (there are: {', '.join(names())})")
    return PRESETS[name]


def names() -> list[str]:
    for module in ("minimal", "schemaorg", "unittest"):
        importlib.import_module(f"gracia.presets.{module}")
    return sorted(PRESETS)
