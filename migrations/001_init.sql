-- 001: line history storage.
-- Applied by scripts/migrate.py, which records it in schema_migrations.
-- No schema name is hard-coded, so tests can apply this inside a throwaway schema.

-- Lookup tables: every row elsewhere must name a known sport and source.
create table sports (
    code text primary key,                -- 'nba', later 'nfl', 'nhl'
    name text not null
);

create table sources (
    name        text primary key,         -- adapter name, e.g. 'action_network'
    licensed    boolean not null,         -- true only if its terms allow commercial use
    description text not null default ''
);

-- One row per game per source (each source has its own game ids).
create table events (
    id              bigserial primary key,
    sport           text not null references sports(code),
    source          text not null references sources(name),
    source_event_id text not null,
    start_time      timestamptz,
    home_team       text not null default '',
    away_team       text not null default '',
    unique (source, source_event_id)
);
create index events_start_idx on events (start_time);

-- Append-only price history: a row is written only when an offer is new or its
-- line/price moved. Never updated in place.
create table offers (
    id               bigserial primary key,
    event_id         bigint not null references events(id),
    sport            text not null references sports(code),
    source           text not null references sources(name),
    player_key       text not null,           -- normalized name: the cross-source player identity
    player_name      text not null,           -- as the source spelled it
    source_player_id text not null default '',
    team             text not null default '',
    market           text not null,           -- 'PTS', 'REB', ...
    book             text not null,
    line             numeric(6, 1) not null,
    over_odds        integer not null,
    under_odds       integer not null,
    observed_at      timestamptz not null,
    -- the same observation can't be stored twice, so re-running a snapshot is harmless
    unique (event_id, player_key, market, book, observed_at)
);
create index offers_event_idx on offers (event_id);
create index offers_observed_idx on offers (observed_at);

-- Latest known price per open offer: what change detection compares against.
create table offers_current (
    event_id         bigint not null references events(id),
    sport            text not null references sports(code),
    source           text not null references sources(name),
    player_key       text not null,
    player_name      text not null,
    source_player_id text not null default '',
    team             text not null default '',
    market           text not null,
    book             text not null,
    line             numeric(6, 1) not null,
    over_odds        integer not null,
    under_odds       integer not null,
    first_seen       timestamptz not null,
    changed_at       timestamptz not null,    -- when line/price last moved
    last_seen        timestamptz not null,    -- when last observed at all
    primary key (event_id, player_key, market, book)
);

-- Anything commercial reads from this: scraped sources are excluded by construction.
create view offers_licensed as
    select o.* from offers o join sources s on s.name = o.source where s.licensed;

insert into sports (code, name) values ('nba', 'National Basketball Association');
