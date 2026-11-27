"""The minimal preset: the types a repository needs, its groups and its admin."""

from collections.abc import Callable
from importlib.resources import files

import pyoxigraph as ox

from gracia import uri
from gracia.extensions import Extension
from gracia.graph import Graph
from gracia.presets import Preset
from gracia.types.group import UserGroupRecord
from gracia.types.repository import RepositoryRecord
from gracia.types.type import TypeRecord
from gracia.types.user import UserRecord
from gracia.utils import passwords
from gracia.utils.validate import validate_ark, validate_password, validate_userid

# the types a repository starts with, in this order (the records of their types are the
# first of the type `type`); and its groups
SYSTEM_TYPES = (RepositoryRecord, TypeRecord, UserRecord, UserGroupRecord)
GROUPS_AT_START = ("admin", "editor", "manager")


def declared(record_class):
    """What a class of records declares of its type."""
    assert record_class.content_type is not None
    return record_class.content_type


class MinimalPreset(Preset):
    """The repository with the types a repository starts with (the repository, the
    types, the users and the groups) and its admin user, and the content types it is
    given."""

    name = "minimal"

    def validate(self) -> None:
        self.initial_dataset(
            self.repository.__name__,
            lambda res_type: 1,
            self.admin,
            self.content_types,
            self.ark,
        )

    def apply(self) -> None:
        repository = self.repository
        store = repository.store
        if not len(store.get(repository.uri)):  # a new repository
            store.put_multi(
                self.initial_dataset(
                    repository.__name__,
                    store.mint_id,
                    self.admin,
                    self.content_types,
                    self.ark,
                )
            )
            repository.sync_roles()
            return
        for content_type in self.content_types:  # one that is there is left as it is
            repository.add_type(content_type)
        repository.sync_roles()

    @staticmethod
    def user_turtle(
        record: str, userid: str, password: str, groups=(), resource_uri: str = ""
    ) -> str:
        validate_userid(userid)
        validate_password(password)
        default = uri.default_resource_uri(record)
        hashed = passwords.hash_password(password)
        member = "".join(f" ;\n        gracia:memberOf <{group}>" for group in groups)
        return (
            f"\n<{record}> a gracia:Record ;\n"
            f"    gracia:resource_uri <{resource_uri or default}> "
            ";\n"
            f'    gracia:label "{userid}" ;\n'
            f'    gracia:resource [ a gracia:User ; gracia:userid "{userid}" ;\n'
            f'        gracia:credentials "{hashed}"{member} ] '
            ".\n"
        )

    @staticmethod
    def user_dataset(
        record: str, userid: str, password: str, groups=(), resource_uri: str = ""
    ) -> Graph:
        text = (
            "@prefix gracia: <https://gracia.pub/ns/> .\n"
            + MinimalPreset.user_turtle(record, userid, password, groups, resource_uri)
        )
        return Graph(
            ox.Dataset(ox.parse(text.encode("utf-8"), format=ox.RdfFormat.TURTLE))
        )

    @staticmethod
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
        system = [declared(cls) for cls in SYSTEM_TYPES]
        type_records = [uri.record_uri("type", mint("type")) for _ in system]
        group_records = [
            uri.record_uri("group", mint("group")) for _ in GROUPS_AT_START
        ]
        content_types = list(content_types)
        slugs = [each.slug for each in system] + [c.slug for c in content_types]
        if len(set(slugs)) != len(slugs):
            raise ValueError(f"two types with the same slug: {sorted(slugs)}")
        extra = [uri.record_uri("type", mint("type")) for _ in content_types]
        values = {
            "@EXTRA_TYPES@": "".join(
                f" <{content_type.class_uri}>" for content_type in content_types
            ),
            "@REPO@": repository_record,
            "@TITLE@": repo_id,  # an id is lowercase letters, digits and _ only
            "@A1@": system[0].assets,
            "@A2@": system[1].assets,
            "@A3@": system[2].assets,
            "@A4@": system[3].assets,
            "@K1@": type_records[0],
            "@K2@": type_records[1],
            "@K3@": type_records[2],
            "@K4@": type_records[3],
            "@C1@": system[
                0
            ].class_uri,  # the classes of the types: their resource uris
            "@C2@": system[1].class_uri,
            "@C3@": system[2].class_uri,
            "@C4@": system[3].class_uri,
            "@G1@": group_records[0],
            "@G2@": group_records[1],
            "@G3@": group_records[2],
        }
        everything = (repository_record, *type_records, *group_records)
        for name, record in zip(
            ("REPO", "K1", "K2", "K3", "K4", "G1", "G2", "G3"), everything, strict=False
        ):
            values[f"@{name}U@"] = uri.default_resource_uri(record, naan, root_prefix)
        for number, each in enumerate(system, 1):
            # a type record is published under the class of its type
            values[f"@K{number}U@"] = each.class_uri
        for placeholder, replacement in sorted(
            values.items(), key=lambda kv: -len(kv[0])
        ):
            text = text.replace(placeholder, replacement)
        for record, content_type in zip(extra, content_types, strict=True):
            text += content_type.turtle(record)
        if admin:
            user_record = uri.record_uri("user", mint("user"))
            text += MinimalPreset.user_turtle(
                user_record,
                admin[0],
                admin[1],
                [values["@G1U@"]],  # the admin group, by its resource uri
                uri.default_resource_uri(user_record, naan, root_prefix),
            )
        # the ARK settings are fixed when the repository is made
        # (see gracia.extensions.ark)
        text += Extension.turtle(
            "_:content", "ark", {"naan": naan, "root_prefix": root_prefix}
        )
        return Graph(
            ox.Dataset(ox.parse(text.encode("utf-8"), format=ox.RdfFormat.TURTLE))
        )
