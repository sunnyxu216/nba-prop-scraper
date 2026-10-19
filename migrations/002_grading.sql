-- 002: grading. Box-score results and one grade per closing line.

-- One row per player per game (from box scores).
create table results (
    sport       text not null references sports(code),
    game_date   date not null,                 -- US Eastern date of the game
    source_game_id text not null,              -- the box-score source's game id
    player_key  text not null,                 -- normalized name, matches offers.player_key
    player_name text not null,
    team        text not null,
    opponent    text not null,
    minutes     numeric(5, 1) not null,
    pts integer not null, reb integer not null, ast integer not null, fg3m integer not null,
    stl integer not null, blk integer not null, tov integer not null,
    loaded_at   timestamptz not null default now(),
    primary key (sport, source_game_id, player_key)
);
create index results_date_idx on results (sport, game_date);

-- One grade per closing line: the last price observed before tip-off for each
-- (event, player, market, book). The primary key means a line is graded once;
-- re-running the grader skips it.
create table grades (
    event_id    bigint not null references events(id),
    player_key  text not null,
    market      text not null,
    book        text not null,
    sport       text not null references sports(code),
    source      text not null references sources(name),
    player_name text not null,
    line        numeric(6, 1) not null,
    over_odds   integer not null,
    under_odds  integer not null,
    closing_observed_at timestamptz not null,  -- when the closing price was seen
    actual      numeric(6, 1),                 -- null when void
    result      text not null check (result in ('over', 'under', 'push', 'void')),
    graded_at   timestamptz not null default now(),
    primary key (event_id, player_key, market, book)
);
create index grades_graded_idx on grades (graded_at);

create view grades_licensed as
    select g.* from grades g join sources s on s.name = g.source where s.licensed;
