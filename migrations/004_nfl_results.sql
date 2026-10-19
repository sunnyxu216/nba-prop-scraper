-- 004: NFL box-score results for grading. One row per player who played (ESPN's game
-- roster: didNotPlay = false), including players with no stats (e.g. a receiver with no
-- targets grades as 0 receptions, not void). Players who didn't play have no row: void.

create table results_nfl (
    sport          text not null default 'nfl' references sports(code),
    game_date      date not null,                 -- US Eastern date of the game
    source_game_id text not null,                 -- 'espn:<event id>'
    player_key     text not null,                 -- normalized name, matches offers.player_key
    player_name    text not null,
    source_player_id text not null default '',    -- ESPN athlete id
    team           text not null,                 -- spm/nfl/teams.py codes
    opponent       text not null,
    pass_att integer not null, pass_cmp integer not null, pass_yds integer not null,
    pass_td integer not null, pass_int integer not null,
    rush_att integer not null, rush_yds integer not null, rush_td integer not null,
    rec integer not null, rec_yds integer not null, rec_td integer not null, targets integer not null,
    other_td integer not null,                    -- return and defensive TDs (count for Anytime TD)
    loaded_at timestamptz not null default now(),
    primary key (source_game_id, player_key)
);
create index results_nfl_date_idx on results_nfl (game_date);
