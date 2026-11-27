from gracia import init
from gracia.presets import Preset


class MinimalPreset(Preset):
    """The repository with the types a repository starts with (the repository, the
    types,
    the users and the groups) and its admin user, and the content types it is given."""

    name = "minimal"

    def validate(self) -> None:
        init.initial_dataset(
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
                init.initial_dataset(
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
