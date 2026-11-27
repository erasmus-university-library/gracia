import re


class NS:
    def __init__(self, base: str):
        self.base = base

    def __getattr__(self, name: str) -> str:
        if name.startswith("__"):  # copy, pickle and friends ask for these
            raise AttributeError(name)
        return self.base + name

    def __getitem__(self, name: str) -> str:
        return self.base + name

    def __str__(self) -> str:
        return self.base

    def __repr__(self) -> str:
        return f"NS({self.base!r})"


GRACIA_NS = "https://gracia.pub/ns/"
G_NS = NS(GRACIA_NS)
RDF_NS = NS("http://www.w3.org/1999/02/22-rdf-syntax-ns#")
SH_NS = NS("http://www.w3.org/ns/shacl#")
XSD_NS = NS("http://www.w3.org/2001/XMLSchema#")
XSL_NS = NS("http://www.w3.org/1999/XSL/Transform")

# classes
REPOSITORY_CLASS = G_NS.Repository
RECORD_CLASS = G_NS.Record
RECORD_SHAPE = G_NS.RecordShape  # the shape every record has (model/record.shacl.ttl)
TYPE_CLASS = G_NS.Type
USER_CLASS = G_NS.User
GROUP_CLASS = G_NS.UserGroup
SYSTEM_CLASSES = frozenset(
    {REPOSITORY_CLASS, TYPE_CLASS, USER_CLASS, GROUP_CLASS}
)  # what every repository is made of

# properties
RDF_TYPE = RDF_NS.type
RDF_JSON = RDF_NS.JSON
RESOURCE = G_NS.resource  # record -> the blank node that holds the content
RESOURCE_URI = G_NS.resource_uri  # record -> the name the resource is published under
TITLE = (
    G_NS.title
)  # repository -> its name for people (its id is the name of its schema)
LABEL = (
    G_NS.label
)  # record -> a short text for it, copied from its content (not edited)
PROPERTY_TEMPLATE = G_NS.propertyTemplate  # property shape -> (template source...) list
HEADER_TEMPLATE = G_NS.headerTemplate  # node shape -> the text of the bar of its node
HEADER_ADDON_TEMPLATE = G_NS.headerAddOnTemplate  # node shape -> a badge before it
EXTENSION = G_NS.extension  # repository -> a blank node: what it says of one extension
EXTENSION_NAME = G_NS.extension_name  # that blank node -> the name of the extension
EXTENSION_IS_ENABLED = G_NS.extension_is_enabled  # -> true or false, when it was said
EXTENSION_SETTINGS = G_NS.extension_settings  # -> the settings, as JSON text
TYPES = G_NS.types  # repository -> the list of its type records
ASSETS = G_NS.assets  # repository, type -> asset spec of its directory of files
SCHEMA_TEMPLATE = (
    G_NS.schemaTemplate
)  # repository, type -> its file in shacl/: <Type>-<profile>-<revision>.ttl
DOC_TEMPLATE = (
    G_NS.docTemplate
)  # type -> its file in doc/: doc-<profile>-<revision>.xsl (makes the doc of a record)
VIEW_TEMPLATE = (
    G_NS.viewTemplate
)  # type -> its file in view/: <type>-<profile>-<revision>.xslt (the page of a record)
NAME = G_NS.name
SLUG = G_NS.slug
CREATED_AT = (
    G_NS.created_at
)  # record -> when it was first stored (xsd:dateTime, UTC; made, not edited)
MODIFIED_AT = (
    G_NS.modified_at
)  # record -> when it was last changed (xsd:dateTime, UTC; made, not edited)
IS_PUBLISHED = G_NS.is_published  # record -> whether it is published (xsd:boolean)
STATE = G_NS.state  # record -> its state, a word (draft, ...); none: no state
TIMEZONE = (
    G_NS.timezone
)  # repository -> the name of its time zone (Europe/Amsterdam); none: UTC
LOCALE = (
    G_NS.locale
)  # repository -> the locale dates are shown and typed in (nl-NL); none: ISO 8601
# repository -> the stylesheet of the page around every page (an asset spec or a path)
LAYOUT_TEMPLATE = G_NS.layoutTemplate
TYPE_TEMPLATE = (
    G_NS.typeTemplate
)  # repository -> the stylesheet of the page of the records of a type
RECORD_TEMPLATE = (
    G_NS.recordTemplate
)  # repository -> the stylesheet of the page of a record
CLASS = G_NS["class"]  # type -> the rdf class of its resources
USERID = G_NS.userid
CREDENTIALS = G_NS.credentials  # a salted hash, never exported
MEMBER_OF = G_NS.memberOf  # user -> the group records it belongs to
SH_TARGET_CLASS = SH_NS.targetClass


# ---- record keys and the names given out
# -----------------------------------------------------

# the first path segments that are views, not types
RESERVED_SLUGS = frozenset({"api", "manage", "jobs", "deform", "_static"})
SLUG_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
# a record uri; the database checks the same
RECORD_PATTERN = r"^rec:([a-z][a-z0-9-]*):([1-9][0-9]{0,17})$"
SLUG_NAME = re.compile(
    r"^[a-z0-9]+(-[a-z0-9]+)*$"
)  # a slug of a record: letters, digits and dashes
_RECORD = re.compile(RECORD_PATTERN)


def slug_like(text: str) -> bool:
    return bool(SLUG_PATTERN.match(text))


def repository_uri() -> str:
    return "rec:repository:1"


# the numbers of the types a repository starts with: the numbers of their type records,
# which are
# made
# in this order (`init.TYPES_AT_START`). The database knows them from the start
# (`record_id`); the
# types that are added later get the next numbers, and a type never has another number
BASE_TYPE_NUMBERS = {"repository": 1, "type": 2, "user": 3, "group": 4}
TYPE_BASE = 10_000_000  # record numbers have to be below it to have an id
MAX_TYPE_NUMBER = (
    99  # the highest type number that has ids: 99 * 10^7 + 9,999,999 fits an INTEGER
)


def record_id(type_number: int, number: int) -> int:
    if not 0 <= type_number <= MAX_TYPE_NUMBER:
        raise ValueError(
            f"a type number has to be between 0 and {MAX_TYPE_NUMBER} to have an id: "
            f"{type_number}"
        )
    if not 0 < number < TYPE_BASE:
        raise ValueError(
            f"a record number has to be between 1 and {TYPE_BASE - 1} to have an id: "
            f"{number}"
        )
    return type_number * TYPE_BASE + number


def record_uri(slug: str, number: int) -> str:
    return f"rec:{slug}:{number}"


DEFAULT_NAAN = (
    "99999"  # the authority number ARK reserves for tests: such ARKs are not persistent
)
DEFAULT_ROOT_PREFIX = "ir"


def default_resource_uri(
    record: str, naan: str = DEFAULT_NAAN, root_prefix: str = DEFAULT_ROOT_PREFIX
) -> str:
    parsed = parse_record_uri(record)
    if parsed is None:
        return f"{record}#resource"
    return f"ark:/{naan}/{root_prefix}/{parsed[0]}/{parsed[1]}"


def parse_record_uri(key: str) -> tuple[str, int] | None:
    found = _RECORD.match(key)
    return (found[1], int(found[2])) if found else None
