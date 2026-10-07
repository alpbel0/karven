"""The first agent (Task 3.3): news -> visual candidates + relation ideas.

Public entry point: :func:`app.first_agent.service.run_first_agent`. The graph agent
(Task 3.4) plugs in through :class:`app.first_agent.ports.GraphAgentPort`.
"""

from app.first_agent.ports import GraphAgentAnswer, GraphAgentPort, GraphIdeaRequest, StubGraphAgent
from app.first_agent.service import FirstAgentResult, ensure_default_prompt, run_first_agent

__all__ = [
    "FirstAgentResult",
    "GraphAgentAnswer",
    "GraphAgentPort",
    "GraphIdeaRequest",
    "StubGraphAgent",
    "ensure_default_prompt",
    "run_first_agent",
]
