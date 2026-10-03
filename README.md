# karven

Monorepo skeleton for the karven project.

## Layout

```
backend/    FastAPI app (Python 3.12, managed with uv)
frontend/   Next.js + Tailwind app (TypeScript, npm)
.env.example
```

## Backend

From `backend/`:

```bash
uv sync
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

## Frontend

From `frontend/`:

```bash
npm install
npm run test
npm run lint
npm run build
```

## Docker

Copy `.env.example` to `.env` at the repo root and fill in the secret values
before starting the stack.

```bash
docker compose up -d --build     # start everything (build images)
docker compose ps                # check health
docker compose down              # stop, keep volumes
docker compose down -v           # stop AND delete volumes (data loss)
```

All host ports bind to `127.0.0.1` only.

| Service | Host port | Container port |
|---|---|---|
| postgres | 15432 | 5432 |
| neo4j HTTP (browser) | 17474 | 7474 |
| neo4j Bolt | 17687 | 7687 |
| redis | 16379 | 6379 |
| minio S3 API | 19000 | 9000 |
| minio console | 19001 | 9001 |
| api | 18000 | 8000 |
| frontend | 13000 | 3000 |

### Service profiles

Only the services the current phase needs start by default. Every definition
stays in `compose.yaml`; the rest are behind compose profiles:

| Group | Services | Needed from |
|---|---|---|
| default (no profile) | postgres, minio, migrator, api | now |
| `graph` | neo4j | Phase 3 (graph agent) |
| `queue` | redis, worker, beat | Task 1.5 (on-demand fetch) |
| `web` | frontend | Phase 4-5 |

Enable a group with `COMPOSE_PROFILES` (comma-separated), either inline:

```bash
COMPOSE_PROFILES=graph,queue docker compose up -d
```

or by setting it in the root `.env` (see `.env.example`). The one-shot
`migrator` runs first and `api` waits for it.

Enabling `graph` also requires `NEO4J_ENABLED=true` so the migrator applies
graph migrations. Keep the two consistent: `COMPOSE_PROFILES` contains `graph`
<=> `NEO4J_ENABLED=true` (they are deliberately separate — profiles control
which containers run, `NEO4J_ENABLED` controls whether the migrator touches the
graph — so a mismatch is always visible). With `graph` off the migrator logs
`neo4j: disabled (NEO4J_ENABLED=false), graph migrations skipped`; with
`NEO4J_ENABLED=true` but Neo4j not running it fails loudly.

Turning a group **off** again: `docker compose up` (even with `--remove-orphans`)
does NOT stop containers of a disabled profile. Stop and remove them explicitly
(volumes are kept):

```bash
COMPOSE_PROFILES=graph,queue,web docker compose rm -sf neo4j redis frontend
```

### Resource limits and restart policy

Each container is capped (the WSL VM is limited to 4 CPUs / 12 GB):

| Service | Memory | Restart |
|---|---|---|
| postgres | 1g | unless-stopped |
| minio | 1g | unless-stopped |
| redis | 256m | unless-stopped |
| neo4j | 2g (heap 1g + pagecache 512m) | unless-stopped |
| api | 1g | unless-stopped |
| worker | 1g | unless-stopped |
| beat | 512m | unless-stopped |
| frontend | 1536m | unless-stopped |
| migrator | 1g | no (one-shot) |

On Windows, check reserved TCP port ranges before choosing host ports:

```bash
netsh int ipv4 show excludedportrange protocol=tcp
```

## Integration tests

From `backend/`, one command builds and runs a completely isolated test stack
(its own compose project `karven-test`, ports, databases, bucket and image tag
`karven-backend:test`), runs the migrator, starts the api, runs the integration
tests, and always tears the stack down (`down -v`):

```bash
uv run python scripts/integration.py
```

It guarantees:

- Live (`karven`) is never touched — separate project, ports (25xxx/29xxx/28xxx),
  databases (`karven_test`), bucket (`karven-test-raw`) and image tag.
- The test stack enables `COMPOSE_PROFILES=graph` and `NEO4J_ENABLED=true`, so
  Neo4j and the graph migration runner are exercised even though the default
  live stack starts without them.
- The live `karven-backend:dev` image is never built, retagged or removed.
- Integration tests refuse to run unless the environment is the test one
  (guarded in `tests/integration/conftest.py`).
- Cleanup happens even on failure or Ctrl-C; the exit code is pytest's.

`uv run pytest` deselects integration tests; `compose.test.env` holds the
test-only values.

## Environment

Copy `.env.example` to `.env` at the repo root and fill in the values.
The backend reads `.env` from the repo root (one level above `backend/`).
In `.env`, the DB/Redis/MinIO values are host-side; `docker compose` overrides
the host/port with in-network service names for the containers.
