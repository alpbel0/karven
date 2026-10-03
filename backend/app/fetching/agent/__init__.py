"""The data-fetch agent (Task 1.6): diagnose failed fetches and answer questions.

The agent sees metadata only (never observation values); code owns monitoring,
status changes, waiting, proposal validation and execution. Public entry points
live in :mod:`app.fetching.agent.service`.
"""

from app.fetching.agent.service import (
    DiagnoseResult,
    FetchAgentAnswer,
    ask_fetch_agent,
    diagnose_job,
    ensure_default_prompt,
    list_failures,
)

__all__ = [
    "DiagnoseResult",
    "FetchAgentAnswer",
    "ask_fetch_agent",
    "diagnose_job",
    "ensure_default_prompt",
    "list_failures",
]
