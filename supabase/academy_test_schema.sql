-- =====================================================================================
-- Academy Support Agent - schema for the SEPARATE TEST Supabase project
--
--   RUN THIS ONLY IN THE TEST PROJECT (for example one named "academy-agent-test").
--   NEVER run it in the real EduChess project.
--
-- It uses plain `create table` on purpose: if a table with the same name already exists
-- (as it might in the real project) the script stops with an error instead of changing it.
--
-- What it creates: four small read-only tables modelled on what the academy answers
-- parents about: fee plans, batch timings, workshops, tournaments. No student, parent or
-- payment tables exist here (minimal child data, hard rule 5).
--
-- Who can do what (hard rule 4, enforced in the database as well as in the code):
--   anon  (the public key the agent uses)  -> SELECT only
--   nobody gets an INSERT, UPDATE or DELETE policy, and those privileges are revoked.
--   Row-level security is ON for every table, so a table without a SELECT policy is unreadable.
--
-- All rows are made up. They are generated from src/email_assistant/academy/config.py:
--   python -m email_assistant.academy.seed_sql --write
-- =====================================================================================

create table plans (
  id bigint generated always as identity primary key,
  name text not null,
  classes_per_month int not null check (classes_per_month > 0),
  fee_rupees int not null check (fee_rupees >= 0),
  active boolean not null default true
);

create table batches (
  id bigint generated always as identity primary key,
  name text not null,
  days text not null,            -- for example 'Mon, Wed'
  time_range text not null,      -- for example '17:00-18:00' (India time)
  level text not null,
  seats_left int not null check (seats_left >= 0)
);

create table workshops (
  id bigint generated always as identity primary key,
  name text not null,
  event_date date not null,
  fee_rupees int not null check (fee_rupees >= 0),
  seats_left int not null check (seats_left >= 0)
);

create table tournaments (
  id bigint generated always as identity primary key,
  name text not null,
  event_date date not null,
  entry_fee_rupees int not null check (entry_fee_rupees >= 0),
  seats_left int not null check (seats_left >= 0)
);

-- ---- row-level security: on everywhere --------------------------------------------------
alter table plans enable row level security;
alter table batches enable row level security;
alter table workshops enable row level security;
alter table tournaments enable row level security;

-- ---- privileges: take everything away, then give back SELECT to anon only ---------------
revoke all on plans, batches, workshops, tournaments from anon, authenticated;
grant select on plans, batches, workshops, tournaments to anon;

-- ---- the only policies: anon may SELECT. There is no insert/update/delete policy. --------
create policy "anon can read plans" on plans for select to anon using (true);
create policy "anon can read batches" on batches for select to anon using (true);
create policy "anon can read workshops" on workshops for select to anon using (true);
create policy "anon can read tournaments" on tournaments for select to anon using (true);

-- ---- made-up rows -----------------------------------------------------------------------
-- BEGIN SEED (generated from src/email_assistant/academy/config.py by seed_sql.py - do not edit by hand)
insert into plans (name, classes_per_month, fee_rupees) values
  ('Starter (SYNTHETIC)', 4, 1234),
  ('Regular (SYNTHETIC)', 8, 2222);
insert into batches (name, days, time_range, level, seats_left) values
  ('Batch A (SYNTHETIC)', 'Mon, Wed', '17:00-18:00', 'beginner', 3),
  ('Batch B (SYNTHETIC)', 'Sat', '10:00-11:30', 'beginner', 5);
insert into workshops (name, event_date, fee_rupees, seats_left) values
  ('Sample Weekend Workshop (SYNTHETIC)', '2026-11-22', 150, 12);
insert into tournaments (name, event_date, entry_fee_rupees, seats_left) values
  ('Sample Rapid Tournament (SYNTHETIC)', '2026-12-06', 99, 20);
-- END SEED
