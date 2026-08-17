# .github

`workflows/jobs-worker.yml` — free (public-repo) replacement for a paid
Render worker/cron service. Runs `uv run python -m app.worker --once` on a
schedule to drain the Phase E intelligence-jobs Redis Stream (see
`CLAUDE.md`'s Redis section for full context). Requires these repo secrets:
`SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `OPENAI_API_KEY`, `OPENAI_MODEL`,
`GROQ_API_KEY`, `GROQ_MODEL`, `QDRANT_URL`, `QDRANT_API_KEY`,
`QDRANT_COLLECTION`, `REDIS_URL`.
