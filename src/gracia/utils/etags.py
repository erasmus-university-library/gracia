"""The etag of a page: a short hash of what it is made of."""

import hashlib


def digest(lines: list[str]) -> str:
    return hashlib.blake2b("\n".join(lines).encode("utf-8"), digest_size=8).hexdigest()


def etag_of(versions: dict[str, object], shapes: dict[str, str] | None = None) -> str:
    lines = [
        f"{rec_uri} {modified.isoformat()}"  # ty: ignore[missing-attribute]
        for rec_uri, modified in versions.items()
    ]
    lines += [f"shape:{slug} {version}" for slug, version in (shapes or {}).items()]
    return digest(sorted(lines))


def page_etag(stored: str, shapes: dict[str, str]) -> str:
    return digest(
        [
            f"etag:{stored}",
            *sorted(f"shape:{slug} {version}" for slug, version in shapes.items()),
        ]
    )
