"""Shared response types: every timestamp leaves the API as ISO-8601 UTC with a `Z`."""

from datetime import UTC, datetime
from typing import Annotated

from pydantic import PlainSerializer, WithJsonSchema


def utc_z(v: datetime) -> str:
    """`v` as ISO-8601 UTC ending in `Z`, e.g. `2026-11-18T14:00:00Z`.

    A naive value is UTC: SQLite (the unit-test database) hands `timestamptz`
    columns back without a zone.
    """
    if v.tzinfo is None:
        v = v.replace(tzinfo=UTC)
    return v.astimezone(UTC).isoformat().replace("+00:00", "Z")


# The JSON schema keeps `format: date-time`, so the OpenAPI document still says
# what the string is.
UtcDateTime = Annotated[
    datetime,
    PlainSerializer(utc_z, return_type=str, when_used="json"),
    WithJsonSchema({"type": "string", "format": "date-time"}, mode="serialization"),
]
