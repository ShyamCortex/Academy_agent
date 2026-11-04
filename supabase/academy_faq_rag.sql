-- =====================================================================================
-- Academy Support Agent - FAQ search (pgvector) for the SEPARATE TEST Supabase project
--
--   RUN THIS ONLY IN THE TEST PROJECT (the one you ran academy_test_schema.sql in).
--   NEVER run it in the real EduChess project.
--
-- Run it once, after academy_test_schema.sql. Then load the FAQ from your laptop:
--   uv run python scripts/ingest_faq.py
--
-- What it creates:
--   the pgvector extension, one table (faq_chunks) and one search function (match_faq).
--
-- Who can do what (hard rule 4, same pattern as the other tables):
--   anon  (the public key the agent uses)  -> SELECT on faq_chunks, and EXECUTE on match_faq
--   nobody gets an INSERT, UPDATE or DELETE policy; those privileges are revoked.
--   The loader writes with the TEST project's own service key (it bypasses row-level security).
--   That key never goes in the agent, in tests, or in the cloud.
--
-- match_faq is `stable` on purpose: PostgREST then lets the agent call it with a plain GET
-- (the agent's database client can only send GET) and runs it in a read-only transaction.
--
-- The vector size 384 must equal EMBEDDING_DIM in src/email_assistant/academy/embeddings.py.
-- There is no index: with about 30 rows an exact scan is instant. Add an HNSW index only if the
-- FAQ grows to thousands of rows.
-- =====================================================================================

create extension if not exists vector with schema extensions;

create table faq_chunks (
  id text primary key,            -- stable id from data/faq/academy_faq.md, for example 'demo-free'
  lang text not null check (lang in ('en', 'tenglish')),
  topic text not null,
  question text not null,
  answer text not null,
  content_hash text not null,     -- hash of question + answer; shows which rows changed
  embedding extensions.vector(384) not null,
  loaded_at timestamptz not null default now()
);

alter table faq_chunks enable row level security;

revoke all on faq_chunks from anon, authenticated;
grant select on faq_chunks to anon;

create policy "anon can read faq_chunks" on faq_chunks for select to anon using (true);

-- The closest chunks to a question vector, best first. Never more than 5 rows.
create function match_faq(query_embedding extensions.vector(384), match_count int default 3)
returns table (id text, lang text, topic text, question text, answer text, similarity float)
language sql
stable
set search_path = public, extensions
as $$
  select f.id, f.lang, f.topic, f.question, f.answer,
         (1 - (f.embedding <=> query_embedding))::float as similarity
  from faq_chunks f
  order by f.embedding <=> query_embedding
  limit least(greatest(match_count, 1), 5);
$$;

revoke all on function match_faq(extensions.vector, int) from public;
grant execute on function match_faq(extensions.vector, int) to anon;
