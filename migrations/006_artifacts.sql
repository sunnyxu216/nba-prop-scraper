-- Private files the cloud jobs need but that don't belong in the public repo: the trained NBA
-- model plus the stats.nba.com history it reads (stats.nba.com blocks cloud servers).
-- Uploaded from a home PC by scripts/publish_nba_model.py; downloaded by scripts/score_nba.py.

create table artifacts (
    name        text primary key,
    version     text not null,                 -- sha256 prefix of data: cloud jobs cache by it
    size_bytes  integer not null,
    data        bytea not null,
    uploaded_at timestamptz not null default now()
);

-- Not readable through Supabase's public Data API (no policies = no access for anon/authenticated);
-- the jobs connect as the database owner, which RLS doesn't apply to.
alter table artifacts enable row level security;
