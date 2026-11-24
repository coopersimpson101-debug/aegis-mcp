"""Load the orchestrator/router TOML config (see config/aegis.example.toml)."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .orchestrator import OrchestratorPolicy, ServerSpec
from .providers import make_provider
from .router import RouterConfig
from .types import Provider


@dataclass
class AegisConfig:
    router: RouterConfig
    providers: dict[str, Provider]
    servers: list[ServerSpec]
    policy: OrchestratorPolicy
    system_prompt: str | None
    audit_log: str | None


def load_config(path: str | os.PathLike[str], *, providers: dict[str, Provider] | None = None) -> AegisConfig:
    with Path(path).expanduser().open("rb") as fh:
        raw: dict[str, Any] = tomllib.load(fh)
    return parse_config(raw, providers=providers)


def parse_config(raw: dict[str, Any], *, providers: dict[str, Provider] | None = None) -> AegisConfig:
    router = RouterConfig(**raw.get("router", {}))
    provs = providers if providers is not None else {name: make_provider(spec) for name, spec in raw.get("providers", {}).items()}
    orch = raw.get("orchestrator", {})
    return AegisConfig(
        router=router,
        providers=provs,
        servers=[ServerSpec(**s) for s in raw.get("servers", [])],
        policy=OrchestratorPolicy(**{k: v for k, v in orch.items() if k in OrchestratorPolicy.model_fields}),
        system_prompt=orch.get("system_prompt"),
        audit_log=orch.get("audit_log"),
    )
