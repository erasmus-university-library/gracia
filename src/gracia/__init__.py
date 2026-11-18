__version__ = "0.1.0"

import warnings

# Pyramid 2.1 still imports pkg_resources, which setuptools says it will remove. That is
# theirs to
# fix; keep our own output readable (this filter names that one message only).
warnings.filterwarnings(
    "ignore", message="pkg_resources is deprecated", category=UserWarning
)
