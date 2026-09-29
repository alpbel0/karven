# TCMB Connector Completion Implementation Plan

## Goal

Close the remaining TCMB connector gaps without bypassing the shared HTTP,
snapshot, persistence, backfill, and operational boundaries.

## Source of Truth

- Requirements: user request to complete the previously identified TCMB gaps.
- Reviewed baseline: current working tree on 2026-09-18; existing TCMB review,
  roadmap notes, implementation, and test suite.

## Scope

### Included

- Make EVDS2 compatibility validation explicit and safe.
- Expose rate-limit/retry observations from the shared HTTP client.
- Add a bounded, explicit multi-PDF ingestion path.
- Update TCMB documentation and tests with the completed behavior.
- Run focused and full offline validation; attempt live Compose validation.

### Excluded

- Unbounded archive crawling.
- Automatic backfill of every TCMB series.
- Deployment or committing user changes.

## Architecture

EVDS2 remains an opt-in source selected by an explicit feature flag and URL.
The HTTP client records only operational counters, never credentials or raw
payloads. PDF batches reuse `TcmbSource`, `TcmbConnector`, snapshot capture,
common persistence, and the existing operational Celery wrapper.

## Global Constraints

- Preserve credential stripping across redirects.
- Keep PDF work explicit and bounded by caller-provided URLs and a maximum.
- Keep historical observations immutable by vintage.
- Do not turn document channels into observation rows.

## File and Responsibility Map

| Path | Action | Responsibility |
|---|---|---|
| `src/backend/app/ingestion/http.py` | Modify | Operational request/retry counters |
| `src/backend/app/ingestion/data/tcmb/connector.py` | Modify | EVDS2 URL validation and PDF batch source construction |
| `src/backend/app/infrastructure/celery/tasks/connectors.py` | Modify | Explicit PDF batch wiring and EVDS2 validation |
| `tests/unit/ingestion/test_http.py` | Modify | Counter behavior |
| `tests/unit/ingestion/test_tcmb_connector.py` | Modify | EVDS2/PDF batch contracts |
| `tests/unit/infrastructure/test_scheduled_connector.py` | Modify | Operational factory coverage |
| `docs/kaynak-profilleri/tcmb.md` | Modify | Verified implementation status and limits |

## Task Graph

| Task | Depends on | Produces |
|---|---|---|
| 1. HTTP operational observations | None | Retry/status counters |
| 2. EVDS2 and PDF batch contracts | 1 | Validated source builders and factory path |
| 3. Documentation and regression validation | 1, 2 | Updated profile and test evidence |

## Tasks

### Task 1: Add HTTP operational observations

**Outcome:** Each client instance exposes request, retry, 429, 5xx, and transport-error counts without logging payloads or secrets.

**Development mode:** `TDD` - counters are deterministic and independently testable.

**Files:** `src/backend/app/ingestion/http.py`, `tests/unit/ingestion/test_http.py`

**Validation:** focused HTTP tests, then all tests.

### Task 2: Close EVDS2 and bounded PDF batch gaps

**Outcome:** EVDS2 URLs are validated before use and an explicit `pdf_batch` spec creates at most the requested number of PDF sources.

**Development mode:** `TDD` - source construction and rejection behavior are contract boundaries.

**Files:** TCMB connector, Celery factory, TCMB and infrastructure tests.

**Validation:** focused TCMB/infrastructure tests and offline contract suite.

### Task 3: Update documentation and verify integration boundary

**Outcome:** The source profile records what is implemented, what remains intentionally bounded, and which live checks require Compose.

**Development mode:** `Validation-only` - documentation and environment-gated live checks are validated by inspection and commands.

**Validation:** full pytest; Compose integration attempt with exact failure recorded if Docker is unavailable.

## Final Integration Validation

- Run: `.\\.venv\\Scripts\\python.exe -m pytest -q`
- Verify: no offline regressions; live integration status is reported separately.

## Open Decisions

- None.

## Execution Record

### Task 1: Add HTTP operational observations

- Status: Completed
- Validation: focused HTTP tests -> 23 passed; counters cover requests,
  retries, 429, 5xx, and transport errors.
- Deviations: None.

### Task 2: Close EVDS2 and bounded PDF batch gaps

- Status: Completed
- Validation: focused TCMB/HTTP/contract suite -> 48 passed; EVDS2 rejects
  non-HTTPS, non-TCMB, credential-bearing, and userinfo URLs; PDF batches are
  bounded and remain document rows.
- Deviations: The operational factory uses the existing shared Celery task;
  no new task name was introduced.

### Task 3: Update documentation and verify integration boundary

- Status: Completed
- Validation: full offline suite -> 181 passed, 12 skipped; compileall and
  `git diff --check` passed. After rebuilding the backend image and applying
  migration 0006, live backend integration -> 12 passed; browser smoke -> 2
  passed.
- Deviations: The first live attempt used a stale image and stale host-port
  state; both were corrected before the final validation.

### Final Validation

- `.\\.venv\\Scripts\\python.exe -m pytest -q` -> 181 passed, 12 skipped,
  2 warnings.
- `.\\.venv\\Scripts\\python.exe -m compileall -q src/backend/app` -> passed.
- `git diff --check` -> passed.
- `KARVEN_RUN_DOCKER_INTEGRATION=1 POSTGRES_HOST_PORT=55432 .\\.venv\\Scripts\\python.exe -m pytest tests/integration -q` -> 12 passed.
- `KARVEN_RUN_BROWSER_INTEGRATION=1 .\\.venv\\Scripts\\python.exe -m pytest tests/e2e/test_health_page.py -q` -> 2 passed.
