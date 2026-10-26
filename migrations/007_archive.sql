-- 007: archive bookkeeping. Finished games' rows are copied to MotherDuck (scripts/archive.py) and,
-- after a 7-day window, deleted from here so Supabase stays within the free tier. Game rows
-- (`events`) are never deleted.

-- One row per archived game: what was copied, when, and when the Supabase copy was removed.
create table archive_log (
    event_id    bigint primary key references events(id),
    archived_at timestamptz not null,
    offers      integer not null,
    grades      integer not null,
    predictions integer not null,
    deleted_at  timestamptz                     -- null: still also stored here
);

-- Paper-trading results computed nightly over the full archive (the dashboard and the weekly
-- report read this, so they don't need DuckDB). Also the archive job's heartbeat.
create table paper_summary (
    sport       text primary key references sports(code),
    computed_at timestamptz not null,
    summary     jsonb not null
);
