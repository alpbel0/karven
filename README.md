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

`worker` and `beat` are behind compose profiles and do not start on a plain
`docker compose up`. The one-shot `migrator` runs automatically before `api`.

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
