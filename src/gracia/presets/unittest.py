from gracia.presets.minimal import MinimalPreset


class UnitTestPreset(MinimalPreset):
    """The minimal repository, with an admin that a test knows when it gives none."""

    name = "unittest"
    admin_account = ("admin", "correct horse battery staple")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.admin = self.admin or self.admin_account
