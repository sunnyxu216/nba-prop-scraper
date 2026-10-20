-- 005: paper trading. What the model said about each line, at fixed checkpoints before kickoff,
-- so ROI, closing-line value and calibration can be measured against what actually happened.

-- Permanent log: one row per (line, checkpoint).
--   open  = first time the line was scored
--   t24   = first run within 24h of kickoff
--   t2    = first run within 2.5h
--   close = last run before kickoff (overwritten each run in the final hour)
create table predictions (
    id               bigserial primary key,
    event_id         bigint not null references events(id),
    sport            text not null references sports(code),
    source           text not null references sources(name),     -- where the line came from
    player_key       text not null,
    player_name      text not null,
    market           text not null,
    book             text not null,
    checkpoint       text not null check (checkpoint in ('open', 't24', 't2', 'close')),
    scored_at        timestamptz not null,
    price_seen_at    timestamptz not null,                        -- when the collector last saw this price
    line             numeric(6, 1) not null,
    over_odds        integer not null,
    under_odds       integer,                                     -- null: yes-only market
    model_version    text not null,
    projection       real not null,
    p_over real not null, p_under real not null, p_push real not null,
    market_prob_over real not null,
    ev_over          real not null,
    ev_under         real,
    decision         text not null check (decision in ('OVER', 'UNDER', 'PASS')),
    model_call       text not null check (model_call in ('OVER', 'UNDER', 'PASS')),  -- before track-only rules
    tier             text,
    stake_pct        real not null,
    track_only       boolean not null,
    low_confidence   boolean not null,
    reasoning        jsonb,                                       -- kept only when the model would bet
    unique (event_id, player_key, market, book, checkpoint)
);
create index predictions_event_idx on predictions (event_id);

create view predictions_licensed as
    select p.* from predictions p join sources s on s.name = p.source where s.licensed;

-- What the dashboard shows: the latest scoring of every open line, with full reasoning.
-- Overwritten each run; rows for games long finished are deleted.
create table predictions_latest (
    event_id         bigint not null references events(id),
    sport            text not null references sports(code),
    source           text not null references sources(name),
    player_key       text not null,
    player_name      text not null,
    market           text not null,
    book             text not null,
    scored_at        timestamptz not null,
    start_time       timestamptz not null,
    home_team text not null, away_team text not null, team text not null, opp text not null,
    position         text not null,
    line             numeric(6, 1) not null,
    over_odds        integer not null,
    under_odds       integer,
    model_version    text not null,
    kind             text not null,                               -- count / yards / binary
    projection       real not null,
    p_over real not null, p_under real not null, p_push real not null,
    decision         text not null,
    model_call       text not null,
    track_only       boolean not null,
    outside_training boolean not null,
    low_confidence   boolean not null,
    one_sided        boolean not null,
    injury_status    text,
    reasoning        jsonb not null,
    primary key (event_id, player_key, market, book)
);
