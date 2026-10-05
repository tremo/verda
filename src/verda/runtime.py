"""Per-agent provider selection; deterministic work never calls a model."""
from __future__ import annotations

from pathlib import Path
import tomllib
from typing import Callable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from verda.agents import AGENTS
from verda.execution import KINDS
from verda.policy import ScreeningFacts, evaluate
from verda.providers import CodexProvider, ModelProvider, ProviderError


class AgentModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    provider: str = Field(default="codex", pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    model: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
    timeout_seconds: int = Field(default=120, ge=1, le=300)


class RuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    default: AgentModel = Field(default_factory=AgentModel)
    agents: dict[str, AgentModel] = Field(default_factory=dict)

    @model_validator(mode="after")
    def known_agents(self):
        if set(self.agents) - {agent.key for agent in AGENTS}:
            raise ValueError("Unknown agent in runtime configuration")
        return self

    @classmethod
    def load(cls, path: Path | None = None):
        return cls() if path is None else cls.model_validate(tomllib.loads(path.read_text()))

    def for_agent(self, agent: str) -> AgentModel:
        override = self.agents.get(agent)
        return AgentModel.model_validate({**self.default.model_dump(),
                                         **(override.model_dump(exclude_unset=True) if override else {})})


class Runtime:
    def __init__(self, config: RuntimeConfig | None = None, *, providers: dict[str, ModelProvider] | None = None,
                 handlers: dict[tuple[str, str], Callable[[dict], dict]] | None = None):
        self.config = config or RuntimeConfig()
        self.providers = {"codex": CodexProvider()} if providers is None else dict(providers)
        self.handlers = dict(handlers or {})

    def describe(self) -> dict:
        return {agent.key: {
            "model_profile": self.config.for_agent(agent.key).model_dump(),
            "actions": {action: {"executor": kind,
                        "transport_available": (self.config.for_agent(agent.key).provider in self.providers
                        if kind == "model" else (agent.key, action) in self.handlers
                        or (agent.key, action) == ("assessment", "assess"))}
                        for action, kind in KINDS[agent.key].items()}}
                for agent in AGENTS}

    def execute(self, agent: str, action: str, context: dict, *, instructions: str = "",
                output_type: type[BaseModel] | None = None) -> dict:
        kind = KINDS.get(agent, {}).get(action)
        if kind is None:
            raise ValueError("Action is outside this agent's capabilities")
        if kind == "deterministic":
            # The rule engine stays authoritative regardless of provider settings.
            return {"executor": kind, "provider": None,
                    "output": evaluate(ScreeningFacts.model_validate(context)).model_dump(mode="json")}
        if kind == "connector":
            handler = self.handlers.get((agent, action))
            if handler is None:
                raise ProviderError("connector_not_implemented")
            return {"executor": kind, "provider": None, "output": handler(context)}
        if output_type is None or not instructions:
            raise ValueError("Model actions require explicit instructions and a result schema")
        profile = self.config.for_agent(agent)
        provider = self.providers.get(profile.provider)
        if provider is None:
            raise ProviderError("provider_not_registered:" + profile.provider)
        generation = provider.generate(instructions=instructions, context=context, output_type=output_type,
                                       model=profile.model, timeout_seconds=profile.timeout_seconds)
        # Every provider must pass the same boundary, including future adapters.
        validated = output_type.model_validate(generation.output.model_dump())
        return {"executor": kind, **generation.as_dict(), "provider": profile.provider,
                "output": validated.model_dump(mode="json")}


class ManagerAdvice(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    explanation: str
    next_steps: list[str]
    questions_for_user: list[str]


def manager_preview(runtime: Runtime, case_summary: dict, allowed_next_steps: list[str]) -> dict:
    from verda.control import MANAGER_PROMPT
    result = runtime.execute("manager", "propose_plan",
                             {"case": case_summary, "allowed_next_steps": allowed_next_steps},
                             instructions=MANAGER_PROMPT,
                             output_type=ManagerAdvice)
    steps = result["output"]["next_steps"]
    if len(steps) != len(set(steps)) or set(steps) - set(allowed_next_steps):
        raise ProviderError("manager_proposed_unavailable_action")
    return {"advisory_only": True, **result}
