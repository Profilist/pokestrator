# Pokestrator

Asynchronous FastMCP server for Poke. It exposes exactly one tool: `orchestrate`.
The tool stores subagent definitions in PostgreSQL, selects an execution path,
and sends the final result back to Poke via webhook callback.

## Runtime architecture

- MCP entrypoint: `src/server.py`
- Orchestrator + execution logic: `src/agent.py`
- Postgres persistence: `src/db.py`
- Poke callback sender: `src/poke.py`
- Templates: `src/templates/*.json`

## Flow

1. Poke invokes MCP tool `orchestrate(task_description)`.
2. Server returns immediate ack (`{"status": "accepted", ...}`) and starts a background job.
3. Orchestrator queries PostgreSQL `subagents` table.
4. It routes to one of:
   - `match`: run an existing DB subagent
   - `template`: load matching JSON template
   - `build_new`: persist a generated subagent and ask user to retry
5. Subagent execution streams through Claude Agent SDK with headless permissions.
6. Result is POSTed back to Poke callback.

## Environment variables

- `DATABASE_URL`: Render PostgreSQL connection string (required in deployment)
- `POKE_API_KEY`: callback auth token for Poke
- `POKE_WEBHOOK_URL`: optional override for Poke inbound webhook URL
- `POKE_DRY_RUN`: set to `1` for local non-network callback tests
- `POKESTRATOR_AGENT_TIMEOUT`: Claude execution timeout in seconds (default `90`)
- `DB_POOL_MIN_SIZE`: minimum DB connections in asyncpg pool (default `1`)
- `DB_POOL_MAX_SIZE`: maximum DB connections in asyncpg pool (default `5`)
- `LOG_LEVEL`: logging verbosity (default `INFO`)

Optional DB tuning:

Render compatibility note: `postgres://` and `postgresql://` URLs are both accepted.

## Local setup

```bash
pip install -r requirements.txt
python src/server.py
```

## Deploy (Render)

`render.yaml` already includes a web service and startup command.

1. Add a Render PostgreSQL service to your project.
2. Set one of these env vars on the web service: `DATABASE_URL`, `POSTGRES_URL`, `POSTGRESQL_URL`, `RENDER_DATABASE_URL`, `DB_URL`.
3. Set `POKE_API_KEY` and `POKE_WEBHOOK_URL` in web service env.
4. Deploy.

Verification after deploy:

- In web logs: `PostgreSQL initialized and subagents table is ready` (or acceptable: `database init failed...` if running degraded mode for local fallback).
- In logs for live runs: `accepted` request logs followed by callback status.

Server endpoint should be reachable at:

- `https://<your-service>.onrender.com/mcp`

## Render/PostgreSQL checklist

1. Confirm the Postgres service is `Running` in Render.
2. Copy the external/managed connection URL and set it as `DATABASE_URL` in the web service.
3. Confirm migrations are auto-created on boot; no manual DDL is required.
4. Use `POKE_DRY_RUN=1` for local or Render logless smoke checks if needed.

## Notes

- The repository is intentionally minimal; template and dynamic subagent behavior is a starting implementation.
- `build_new` branch persists generated subagents so future matching can improve quickly.
