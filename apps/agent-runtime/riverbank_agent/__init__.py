"""Stable RiverBank Agent Runtime client and adapter surface."""

from .client import DirectAgentRuntime, SocketAgentRuntime
from .embedded_hermes import EmbeddedHermesRuntime
from .hermes import HermesCLIAdapter
from .protocol import (
    AgentCancelled,
    AgentResult,
    AgentRunRequest,
    AgentRuntimeError,
    AgentTimeout,
    PROTOCOL_SCHEMA,
)

__all__ = [
    "AgentCancelled",
    "AgentResult",
    "AgentRunRequest",
    "AgentRuntimeError",
    "AgentTimeout",
    "DirectAgentRuntime",
    "EmbeddedHermesRuntime",
    "HermesCLIAdapter",
    "PROTOCOL_SCHEMA",
    "SocketAgentRuntime",
]
