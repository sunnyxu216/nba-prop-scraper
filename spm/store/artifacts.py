"""Named binary files kept in Postgres (the `artifacts` table), versioned by content hash."""

import hashlib


def put(conn, name, blob):
    """Store (or replace) a file; returns its version. Unchanged content is not re-uploaded."""
    version = hashlib.sha256(blob).hexdigest()[:16]
    if version_of(conn, name) == version:
        return version
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("""insert into artifacts (name, version, size_bytes, data) values (%s, %s, %s, %s)
                       on conflict (name) do update set version = excluded.version,
                           size_bytes = excluded.size_bytes, data = excluded.data, uploaded_at = now()""",
                    (name, version, len(blob), blob))
    return version


def version_of(conn, name):
    """The stored version, without downloading the data (None if missing)."""
    with conn.cursor() as cur:
        cur.execute("select version from artifacts where name = %s", (name,))
        row = cur.fetchone()
    return row[0] if row else None


def get(conn, name):
    """(version, bytes), or (None, None) if missing."""
    with conn.cursor() as cur:
        cur.execute("select version, data from artifacts where name = %s", (name,))
        row = cur.fetchone()
    return (row[0], bytes(row[1])) if row else (None, None)
