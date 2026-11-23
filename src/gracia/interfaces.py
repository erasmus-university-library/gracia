from zope.interface import Attribute, Interface


class IGraciaType(Interface):
    class_uri = Attribute("the rdf class of its resources")
    name = Attribute(
        "the name of the type (None: not described); its records are rec:<slug>:<n>"
    )
    slug = Attribute("the first path segment it normally has (None: not described)")
    schema = Attribute("the file in its shacl/ it starts on")
    assets = Attribute("the asset spec of the directory of its files")
    package = Attribute("the dotted name of its package")
    system = Attribute("True for the types a repository cannot work without")
    record_class = Attribute(
        "the class of its records, or None for the generic `Record`"
    )


def known_types(registry) -> list:
    unique = {
        id(res_type): res_type
        for _name, res_type in registry.getUtilitiesFor(IGraciaType)
    }
    return sorted(
        unique.values(), key=lambda k: (k.slug is None, k.slug or k.class_uri)
    )


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


class IExtension(Interface):
    # what the author of an extension says (class attributes)
    name = Attribute("the name of the extension (set by `@extension(name)`)")
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
