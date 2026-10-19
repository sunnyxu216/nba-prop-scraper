-- 003: NFL. Register the sport, and allow one-sided markets (Anytime TD: books post a
-- "yes" price, often with no "no" price), stored as over 0.5 with under_odds null.
-- Existing NBA rows are untouched; change detection already compares with "is distinct from".

insert into sports (code, name) values ('nfl', 'National Football League') on conflict do nothing;

alter table offers alter column under_odds drop not null;
alter table offers_current alter column under_odds drop not null;
alter table grades alter column under_odds drop not null;
