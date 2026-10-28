-- Better Recently Played With: shared Twitch list.
-- Run once in a new Supabase project: Dashboard -> SQL Editor -> paste all of this -> Run. Safe to run again.
--
-- What the PUBLIC key (shipped with the app) may do:  read master_links, add rows to submissions. Nothing else.
-- What only the SECRET key (owner's PC) may do:        read/approve/reject submissions, change master_links, block.

-- The master list: one Twitch channel per Xbox account (XUID). Only the owner writes it.
create table if not exists public.master_links (
  xuid          text primary key check (xuid ~ '^[0-9]{10,20}$'),
  gamertag      text not null check (length(gamertag) between 1 and 32),
  twitch_id     text not null check (twitch_id ~ '^[0-9]{1,20}$'),
  twitch_login  text not null check (twitch_login ~ '^[a-z0-9_]{1,25}$'),
  twitch_name   text not null default '' check (length(twitch_name) <= 40),
  updated_at    timestamptz not null default now()
);

-- Suggestions sent from the app. "submitter" is a random ID per installed copy (no account, no IP stored here).
create table if not exists public.submissions (
  id             bigint generated always as identity primary key,
  submitter      text not null check (submitter ~ '^[0-9a-f]{32}$'),
  submitter_name text not null default '' check (length(submitter_name) <= 40),
  xuid           text not null check (xuid ~ '^[0-9]{10,20}$'),
  gamertag       text not null check (length(gamertag) between 1 and 32),
  twitch_id      text not null check (twitch_id ~ '^[0-9]{1,20}$'),
  twitch_login   text not null check (twitch_login ~ '^[a-z0-9_]{1,25}$'),
  twitch_name    text not null default '' check (length(twitch_name) <= 40),
  status         text not null default 'pending' check (status in ('pending', 'approved', 'rejected')),
  created_at     timestamptz not null default now(),
  unique (submitter, xuid, twitch_id)
);
create index if not exists submissions_status on public.submissions (status, created_at);

create table if not exists public.blocked_submitters (
  submitter  text primary key,
  reason     text not null default '',
  blocked_at timestamptz not null default now()
);

-- Limits for public suggestions. Runs as the table owner, so it can see rows the public key can not.
create or replace function public.check_submission() returns trigger
language plpgsql security definer set search_path = public as $$
begin
  new.status := 'pending';
  new.created_at := now();
  if exists (select 1 from blocked_submitters b where b.submitter = new.submitter) then
    raise exception 'This copy of the app is blocked from sending suggestions.';
  end if;
  if (select count(*) from submissions s
      where s.submitter = new.submitter and s.created_at > now() - interval '1 day') >= 500 then
    raise exception 'Daily limit reached (500 suggestions). Try again tomorrow.';
  end if;
  if (select count(*) from submissions s where s.status = 'pending') >= 20000 then
    raise exception 'Too many suggestions are waiting for review. Try again later.';
  end if;
  -- quietly skip what is already known: same suggestion from this copy, or already in the master list
  if exists (select 1 from submissions s where s.submitter = new.submitter
             and s.xuid = new.xuid and s.twitch_id = new.twitch_id)
     or exists (select 1 from master_links m where m.xuid = new.xuid and m.twitch_id = new.twitch_id) then
    return null;
  end if;
  return new;
end $$;

drop trigger if exists check_submission on public.submissions;
create trigger check_submission before insert on public.submissions
  for each row execute function public.check_submission();

create or replace function public.touch_master() returns trigger language plpgsql as $$
begin new.updated_at := now(); return new; end $$;
drop trigger if exists touch_master on public.master_links;
create trigger touch_master before insert or update on public.master_links
  for each row execute function public.touch_master();

-- Row-level security: without a policy a role sees and changes nothing.
alter table public.master_links       enable row level security;
alter table public.submissions        enable row level security;
alter table public.blocked_submitters enable row level security;

drop policy if exists "everyone reads the master list" on public.master_links;
create policy "everyone reads the master list" on public.master_links for select to anon using (true);

drop policy if exists "everyone may suggest" on public.submissions;
create policy "everyone may suggest" on public.submissions for insert to anon with check (status = 'pending');

-- Table rights on top of that: the public role may only read the master list and insert these columns.
revoke all on public.master_links, public.submissions, public.blocked_submitters from anon, authenticated;
grant select on public.master_links to anon;
grant insert (submitter, submitter_name, xuid, gamertag, twitch_id, twitch_login, twitch_name)
  on public.submissions to anon;
revoke all on function public.check_submission() from anon, authenticated;
