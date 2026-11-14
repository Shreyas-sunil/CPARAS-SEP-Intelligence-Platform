"""UTC timestamps without the deprecated datetime.utcnow().

Returns a *naive* datetime in UTC — exactly what datetime.utcnow() returned
— so values stored in the existing SQLite DateTime columns are unchanged.
"""
from datetime import datetime, timezone


def utcnow() -> datetime:
    """Current UTC time as a naive datetime - the convention used for every DateTime column (SQLite has
    no time-zone type).
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)
