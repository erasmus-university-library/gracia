from zope.interface import Attribute, Interface


class IGraciaType(Interface):
    """A type of record that this application knows, registered as a utility under its
    class URI and under its slug: `registry.queryUtility(IGraciaType, "user")`, or with
    the URI of the class, and `registry.getAllUtilitiesRegisteredFor(IGraciaType)` for
    all of them (the same object is there once for every name,
    `gracia.interfaces.known_types` gives each once).

    What is here is what the code knows about a type. Which types a repository has, and
    under which slug, is data in that repository: see `resources.Repository.types`."""

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
    """Every type the application knows (`IGraciaType`), once each, by slug (a type
    without a slug comes last). `registry.getUtilitiesFor(IGraciaType)` gives the (name,
    type) pairs: a type is there under the URI of its class and under its slug."""
    unique = {
        id(res_type): res_type
        for _name, res_type in registry.getUtilitiesFor(IGraciaType)
    }
    return sorted(
        unique.values(), key=lambda k: (k.slug is None, k.slug or k.class_uri)
    )


class IJob(Interface):
    """A job a person can start from the jobs view of a repository, registered as a
    utility under its name: `registry.queryUtility(IJob, "validate")`,
    `registry.getUtilitiesFor(IJob)` for all of them (see `gracia.jobs.job.Job`). It
    turns the answers of a form into the job of tasks that `gracia.jobs.spec` checks."""

    name = Attribute("the name it is started by")
    title = Attribute("what the jobs view calls it")
    description = Attribute("one sentence for the jobs view")
    groups = Attribute("the groups that may start it besides the administrators")
    start_permission = Attribute("the permission to start it: start_<name>_job")


class IDeformView(Interface):
    """A deform view (`gracia.views.deform`), registered as a utility under its name: a
    function `(context, request, values)` that answers `[(value, label), ...]` or HTML.
    It is also a named Pyramid view of the node it is declared for; `values` are the
    fields it depends on."""


class IRepository(Interface):
    """A repository node of the resource tree (`gracia.resources.Repository`): what an
    extension is an adapter of."""


class IExtension(Interface):
    """An extension of one repository (`gracia.extensions`): an optional part of it,
    such as the resolver of ARKs. It is registered as a named adapter from
    `IRepository`, so `registry.queryAdapter(repository, IExtension, "ark")` is the
    extension as it is in that repository, and `repository.extension("ark")` is the way
    to ask. The extension decides for itself whether it is on there. It does not touch
    the data: the base class stores a flag and the settings on the repository resource
    (as JSON), and the extension is given its settings as a plain dict."""

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
        """Turn it on (nothing happens when it is on)."""

    def disable():
        """Turn it off, keeping its settings; refused when `can_disable` is false."""

    def save_settings(data):
        """Store settings (a dict, as the form gives it) after the schema and its
        validators have been satisfied."""

    def export():
        """The settings as JSON text (the serialized form of the schema)."""

    def import_json(text):
        """Store settings from JSON text (what `export` gives), checked like the form
        is. Returns them as a dict."""


class IRepositoryChild(Interface):
    """Something that can be reached as `/<repo>/<key>`, registered under its key as a
    utility with `factory(repository, segment)` (a node of the traversal tree, or
    `KeyError`) and `prefix`: the key is the start of the segment, not all of it (`rec:`
    for `rec:person:34`)."""

    factory = Attribute("(repository, segment) -> a node, or raises KeyError")
    prefix = Attribute("whether the key only has to start the segment")


class IPredicate(Interface):
    """The code for one property (`gracia.predicates`), registered as a utility under
    the URI of the property. The form of a record is made from its SHACL shape (the
    record shape with the shape of its content embedded); a predicate is what a property
    needs that SHACL cannot say. Every part is optional."""

    uri = Attribute("the property it is the code for")

    def schema_node(record, request, schema_node=None):
        """The Colander node of its field. `schema_node` is what the shape made (None
        when the shape's property is not one text value); return it changed (another
        widget, a validator), another node, or None for no field."""

    def get(record):
        """The value of its field for what is stored now. Optional: without it the
        stored text of the property is shown."""

    def put(record, text):
        """Put the value of its field into the dataset of the record. Optional: without
        it the text is stored as the one value of the property."""

    def begin(record):
        """The record was just made: put what every record has of this predicate."""

    def derive(record):
        """After the form was applied: store what follows from the rest of the record
        (the label, copied from the content)."""
