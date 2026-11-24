"""aegis-core: cost-aware model router + multi-server MCP orchestrator, extracted from the AEGIS assistant."""

from .budget import BudgetExceeded, BudgetTracker
from .config import AegisConfig, load_config, parse_config
from .orchestrator import ApprovalRequest, Orchestrator, OrchestratorPolicy, RunResult, ServerSpec
from .router import DifficultyEstimator, RouteDecision, RouteRequest, Router, RouterConfig, Tier
from .types import ChatRequest, ChatResponse, Message, Provider, ProviderError, ToolCall, ToolSpec, Usage

__all__ = [
    "AegisConfig", "ApprovalRequest", "BudgetExceeded", "BudgetTracker", "ChatRequest", "ChatResponse",
    "DifficultyEstimator", "Message", "Orchestrator", "OrchestratorPolicy", "Provider", "ProviderError",
    "RouteDecision", "RouteRequest", "Router", "RouterConfig", "RunResult", "ServerSpec", "Tier", "ToolCall",
    "ToolSpec", "Usage", "load_config", "parse_config",
]
