import json
import logging

import colander
import pyoxigraph as ox
from pyramid.threadlocal import get_current_request
from zope.interface import implementer

from gracia import uri
from gracia.forms import Form, none_to_nulls, nulls_to_none, to_json
from gracia.interfaces import IExtension, IRepository

log = logging.getLogger("gracia.extensions")


class ExtensionError(ValueError):
    def __init__(self, errors):
        self.errors = [errors] if isinstance(errors, str) else list(errors)
        super().__init__("; ".join(self.errors))


# ---- what the repository resource says
# --------------------------------------------------------


def _turtle_text(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def extension_turtle(
    subject: str, name: str, settings: dict | None = None, enabled: bool | None = None
) -> str:

    lines = [f"gracia:extension_name {_turtle_text(name)}"]
    if enabled is not None:
        flag = "true" if enabled else "false"
        lines.append(f'gracia:extension_is_enabled "{flag}"^^xsd:boolean')
    if settings is not None:
        lines.append(
            "gracia:extension_settings "
            f"{_turtle_text(json.dumps(settings, ensure_ascii=False))}"
            f"^^<{uri.RDF_JSON}>"
        )
    return (
        "@prefix gracia: <https://gracia.pub/ns/> .\n"
        "@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .\n"
        f"{subject} gracia:extension [ " + " ; ".join(lines) + " ] .\n"
    )


def _states(repository) -> dict[str, tuple[bool | None, dict]]:
    cached = repository.extension_states
    if cached is None:
        try:
            record = repository.record()
        except LookupError:
            cached = {}
        else:
            cached = read_extension_states(record.graph, record.uri)
        repository.extension_states = cached
    return cached


def _write(repository, name: str, enabled: bool | None, settings: dict) -> None:
    store = repository.store
    record = repository.record()
    subject, record_graph = record.resource, record.graph
    if subject is None:
        raise LookupError(
            f"the record of repository {repository.__name__} has no content"
        )
    graph = next(
        iter(record_graph.quads_for_subject(ox.NamedNode(repository.uri)))
    ).graph_name
    for node in record_graph.objects(subject, ox.NamedNode(uri.EXTENSION)):
        said = record_graph.value(node, ox.NamedNode(uri.EXTENSION_NAME))
        if said is not None and said.value == name:
            for quad in list(record_graph.quads_for_subject(node)):
                record_graph.discard(quad)
            record_graph.discard(
                ox.Quad(subject, ox.NamedNode(uri.EXTENSION), node, graph)
            )
    placeholder = ox.NamedNode("urn:gracia:content")
    parsed = ox.parse(
        extension_turtle(
            f"<{placeholder.value}>", name, settings or None, enabled
        ).encode(),
        format=ox.RdfFormat.TURTLE,
    )
    for (
        quad
    ) in parsed:  # what the Turtle says of the placeholder is said of the content
        owner = subject if quad.subject == placeholder else quad.subject
        record_graph.add(ox.Quad(owner, quad.predicate, quad.object, graph))
    store.put_multi(record_graph)
    repository.extension_states = None


@implementer(IExtension)
class Extension:
    name = ""
    title = ""
    description = ""
    settings_schema: type | None = None

    def __init__(self, repository):
        self.repository = repository

    # ---- on and off
    # -------------------------------------------------------------------------

    @property
    def _flag(self) -> bool | None:
        return _states(self.repository).get(self.name, (None, {}))[0]

    @property
    def enabled(self) -> bool:
        return bool(self._flag)

    @property
    def can_disable(self) -> bool:
        return True

    def enable(self) -> None:
        if not self.enabled:
            _write(self.repository, self.name, True, self._stored)

    def disable(self) -> None:
        if not self.enabled:
            return
        if not self.can_disable:
            raise ExtensionError(f"{self.title or self.name} cannot be disabled")
        _write(self.repository, self.name, False, self._stored)  # the settings are kept

    # ---- settings
    # ---------------------------------------------------------------------------

    @property
    def _stored(self) -> dict:
        return _states(self.repository).get(self.name, (None, {}))[1]

    def schema(self, request=None) -> colander.SchemaNode | None:
        if self.settings_schema is None:
            return None
        return self.settings_schema().bind(
            repository=self.repository, request=request or get_current_request()
        )

    @property
    def settings(self) -> dict:
        states = _states(self.repository)
        cache = self.repository.extension_settings
        cached = cache.get(self.name)
        if cached is None or cached[0] is not states:
            cached = cache[self.name] = (states, self._read_settings())
        return dict(cached[1])  # a copy of the top level: the nested values are shared

    def _read_settings(self) -> dict:
        schema = self.schema()
        if schema is None:
            return {}
        stored = none_to_nulls(self._stored)
        out = {node.name: node.default for node in schema.children}
        for node in schema.children:
            if node.name in stored:
                try:
                    # the type reads it (a date from its text); what the schema asks of
                    # what
                    # people type (validators) is not asked of what was stored
                    out[node.name] = node.typ.deserialize(node, stored[node.name])
                except colander.Invalid as err:
                    log.warning(
                        "the stored setting %s of the extension %s is not valid (%s): "
                        "the default "
                        "is used",
                        node.name,
                        self.name,
                        err.msg,
                    )
        return out

    def _require_schema(self) -> colander.SchemaNode:
        schema = self.schema()
        if schema is None:
            raise ExtensionError(f"{self.title or self.name} has no settings")
        return schema

    def save_settings(self, data: dict) -> None:
        schema = self._require_schema()
        try:  # what the form and the JSON are asked: the schema and its validators
            data = schema.deserialize(schema.serialize(data))
        except colander.Invalid as err:
            raise ExtensionError(
                f"{field}: {why}" for field, why in err.asdict().items()
            ) from err
        stored = nulls_to_none(schema.serialize(data))
        _write(self.repository, self.name, self._flag, stored)

    def export(self) -> str:
        schema = self._require_schema()
        return to_json(nulls_to_none(schema.serialize(self.settings)), indent=2)

    def import_json(self, text: str) -> dict:
        schema = self._require_schema()
        try:
            data = json.loads(text)
        except ValueError as err:
            raise ExtensionError(f"this is not JSON: {err}") from err
        if not isinstance(data, dict):
            raise ExtensionError("the settings are a JSON object")
        for (
            node
        ) in schema.children:  # a JSON number or true is what the field's text would be
            if isinstance(data.get(node.name), bool | int | float):
                data[node.name] = node.typ.serialize(node, data[node.name])
        try:
            appstruct = schema.deserialize(none_to_nulls(data))
        except colander.Invalid as err:
            raise ExtensionError(
                f"{field}: {why}" for field, why in err.asdict().items()
            ) from err
        self.save_settings(appstruct)
        return appstruct


def settings_form(extension: Extension, request, **options) -> Form:
    return Form(extension.schema(request), **options)


# ---- registration
# -------------------------------------------------------------------------------


def add_gracia_extension(config, cls: type[Extension]) -> None:
    """The directive of the scan for a class that implements `IExtension`: one that has
    no `name` is not an extension (the base class)."""
    name = cls.name
    if not name:
        return

    def register():
        config.registry.registerAdapter(cls, (IRepository,), IExtension, name=name)

    config.action(("gracia extension", name), register)


class ExtensionPredicate:
    def __init__(self, name: str, config):
        self.name = name

    def text(self) -> str:
        return f"enabled_extension = {self.name}"

    phash = text

    def __call__(self, context, request) -> bool:
        repository = getattr(context, "repository", None)
        if repository is None:
            return False
        try:
            return repository.extension(self.name).enabled
        except KeyError:
            return False


def read_extension_states(graph, repo_uri: str) -> dict[str, tuple[bool | None, dict]]:
    content = graph.value(ox.NamedNode(repo_uri), ox.NamedNode(uri.RESOURCE))
    found: dict[str, tuple[bool | None, dict]] = {}
    if content is None:
        return found
    for node in graph.objects(content, ox.NamedNode(uri.EXTENSION)):
        name = graph.value(node, ox.NamedNode(uri.EXTENSION_NAME))
        if name is None:
            continue
        flag = graph.value(node, ox.NamedNode(uri.EXTENSION_IS_ENABLED))
        text = graph.value(node, ox.NamedNode(uri.EXTENSION_SETTINGS))
        try:
            settings = json.loads(text.value) if text is not None else {}
        except ValueError:
            settings = {}
        found[name.value] = (
            None if flag is None else flag.value.lower() in ("true", "1"),
            settings if isinstance(settings, dict) else {},
        )
    return found
