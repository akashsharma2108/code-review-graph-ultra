"""Shared limits and helpers for the Team Sync HTTP protocol."""

MAX_REQUEST_BYTES = 2 * 1024 * 1024

LIKE_ESCAPE = "\\"


def escape_like(value: str) -> str:
    """Escape LIKE metacharacters so user filters match literally.

    Use with ``LIKE ? ESCAPE '\\'`` so ``%`` and ``_`` in a filter value
    match themselves instead of acting as wildcards.
    """
    return (
        value.replace(LIKE_ESCAPE, LIKE_ESCAPE + LIKE_ESCAPE)
        .replace("%", LIKE_ESCAPE + "%")
        .replace("_", LIKE_ESCAPE + "_")
    )
