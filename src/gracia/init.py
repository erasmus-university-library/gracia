import json
import re
from collections.abc import Callable
from importlib.resources import files

import pyoxigraph as ox

from gracia import uri
from gracia.graph import Graph
from gracia.utils import passwords
from gracia.utils.validate import validate_ark, validate_password, validate_userid

# the types a repository starts with: (name in the urn, slug in the url, class)
TYPES_AT_START = (
    ("Repository", "repository", uri.REPOSITORY_CLASS),
    ("Type", "type", uri.TYPE_CLASS),
    ("User", "user", uri.USER_CLASS),
    ("UserGroup", "group", uri.GROUP_CLASS),
)
# shacl/, xslt/doc/ and xslt/view/: the files of the types that come with gracia
TYPE_ASSETS = "gracia:assets/"
GROUPS_AT_START = ("admin", "editor", "manager")

_TYPE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,62}$")


_SCHEMA = re.compile(r"^[a-z][a-z0-9]*-[a-z][a-z0-9_]*-[0-9]{2,}\.ttl$")
_DOC = re.compile(r"^[a-z][a-z0-9]*-[a-z][a-z0-9_]*-[0-9]{2,}\.xsl$")
_VIEW = re.compile(r"^[a-z][a-z0-9]*-[a-z][a-z0-9_]*-[0-9]{2,}\.xslt$")
_ASSETS = re.compile(r"^[A-Za-z0-9_.:/-]+/?$")


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


def user_turtle(
    record: str, userid: str, password: str, groups=(), resource_uri: str = ""
) -> str:
    validate_userid(userid)
    validate_password(password)
    member = "".join(f" ;\n        gracia:memberOf <{group}>" for group in groups)
    return (
        f"\n<{record}> a gracia:Record ;\n"
        f"    gracia:resource_uri <{resource_uri or uri.default_resource_uri(record)}> "
        ";\n"
        f'    gracia:label "{userid}" ;\n'
        f'    gracia:resource [ a gracia:User ; gracia:userid "{userid}" ;\n'
        f'        gracia:credentials "{passwords.hash_password(password)}"{member} ] '
        ".\n"
    )


def user_dataset(
    record: str, userid: str, password: str, groups=(), resource_uri: str = ""
) -> Graph:
    text = "@prefix gracia: <https://gracia.pub/ns/> .\n" + user_turtle(
        record, userid, password, groups, resource_uri
    )
    return Graph(ox.Dataset(ox.parse(text.encode("utf-8"), format=ox.RdfFormat.TURTLE)))


_CLASS_URI = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:[^\s<>\"{}|\\^`]+$")


def type_turtle(record: str, content_type, resource_uri: str = "") -> str:
    for label, text, pattern in (
        ("name", content_type.name, _TYPE_NAME),
        ("slug", content_type.slug, uri.SLUG_PATTERN),
        ("class", content_type.class_uri, _CLASS_URI),
        ("schema", content_type.schema, _SCHEMA),
        ("assets", content_type.assets, _ASSETS),
        (
            "doc template",
            getattr(content_type, "doc", None) or "none-none-01.xsl",
            _DOC,
        ),
        ("view", getattr(content_type, "view", None) or "none-none-01.xslt", _VIEW),
    ):
        if not isinstance(text, str) or not pattern.match(text):
            raise ValueError(f"invalid {label} for a type: {text!r}")
    if content_type.slug in uri.RESERVED_SLUGS:
        raise ValueError(f"the slug {content_type.slug!r} is a view, not a type")
    return (
        f"\n<{record}> a gracia:Record ;\n"
        f"    gracia:resource_uri <{resource_uri or content_type.class_uri}> ;\n"
        f'    gracia:label "{content_type.name}" ;\n'
        f'    gracia:resource [ a gracia:Type ; gracia:name "{content_type.name}" ;\n'
        f'        gracia:slug "{content_type.slug}" ; gracia:class '
        f"<{content_type.class_uri}> ;\n"
        f'        gracia:assets "{content_type.assets}" ; gracia:schemaTemplate '
        f'"{content_type.schema}"'
        + (
            f' ; gracia:docTemplate "{content_type.doc}"'
            if getattr(content_type, "doc", None)
            else ""
        )
        + (
            f' ; gracia:viewTemplate "{content_type.view}"'
            if getattr(content_type, "view", None)
            else ""
        )
        + " ] .\n"
    )


def initial_dataset(
    repo_id: str,
    mint: Callable[[str], int],
    admin: tuple[str, str] | None = None,
    content_types=(),
    ark: tuple[str, str] = (uri.DEFAULT_NAAN, uri.DEFAULT_ROOT_PREFIX),
) -> Graph:
    if admin:
        validate_userid(admin[0])
        validate_password(admin[1])
    naan, root_prefix = validate_ark(*ark)
    text = (files("gracia") / "assets" / "model" / "repository.ttl").read_text(
        encoding="utf-8"
    )
    repository_record = uri.record_uri("repository", mint("repository"))
    type_records = [uri.record_uri("type", mint("type")) for _ in TYPES_AT_START]
    group_records = [uri.record_uri("group", mint("group")) for _ in GROUPS_AT_START]
    content_types = list(content_types)
    slugs = [slug for _, slug, _ in TYPES_AT_START] + [c.slug for c in content_types]
    if len(set(slugs)) != len(slugs):
        raise ValueError(f"two types with the same slug: {sorted(slugs)}")
    extra = [uri.record_uri("type", mint("type")) for _ in content_types]
    values = {
        "@EXTRA_TYPES@": "".join(
            f" <{content_type.class_uri}>" for content_type in content_types
        ),
        "@REPO@": repository_record,
        "@TITLE@": repo_id,  # an id is lowercase letters, digits and _ only
        "@A1@": TYPE_ASSETS,
        "@A2@": TYPE_ASSETS,
        "@A3@": TYPE_ASSETS,
        "@A4@": TYPE_ASSETS,
        "@K1@": type_records[0],
        "@K2@": type_records[1],
        "@K3@": type_records[2],
        "@K4@": type_records[3],
        "@C1@": TYPES_AT_START[0][
            2
            # the classes of the types: their resource uris, as the list of types has
            # them
        ],
        "@C2@": TYPES_AT_START[1][2],
        "@C3@": TYPES_AT_START[2][2],
        "@C4@": TYPES_AT_START[3][2],
        "@G1@": group_records[0],
        "@G2@": group_records[1],
        "@G3@": group_records[2],
    }
    everything = (repository_record, *type_records, *group_records)
    for name, record in zip(
        ("REPO", "K1", "K2", "K3", "K4", "G1", "G2", "G3"), everything, strict=False
    ):
        values[f"@{name}U@"] = uri.default_resource_uri(record, naan, root_prefix)
    for number, (_name, _slug, class_uri) in enumerate(TYPES_AT_START, 1):
        values[f"@K{number}U@"] = (
            class_uri  # a type record is published under the class of its type
        )
    for placeholder, replacement in sorted(values.items(), key=lambda kv: -len(kv[0])):
        text = text.replace(placeholder, replacement)
    for record, content_type in zip(extra, content_types, strict=True):
        text += type_turtle(record, content_type)
    if admin:
        user_record = uri.record_uri("user", mint("user"))
        text += user_turtle(
            user_record,
            admin[0],
            admin[1],
            [values["@G1U@"]],  # the admin group, by its resource uri
            uri.default_resource_uri(user_record, naan, root_prefix),
        )
    # the ARK settings are fixed when the repository is made (see gracia.extensions.ark)
    text += extension_turtle(
        "_:content", "ark", {"naan": naan, "root_prefix": root_prefix}
    )
    return Graph(ox.Dataset(ox.parse(text.encode("utf-8"), format=ox.RdfFormat.TURTLE)))
