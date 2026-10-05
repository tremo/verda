"""Capability definitions. No model loop or external connector runs at import."""
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class AgentSpec:
    key: str
    label: str
    actions: tuple[str, ...]
    resource: str | None
    implementation: str = "contract_only"


AGENTS = (
    AgentSpec("manager", "Yönetici", ("read_case", "read_timeline", "propose_plan"), None),
    AgentSpec("sahibinden", "Sahibinden", ("discover", "read_listing", "read_thread", "propose_question"), "sahibinden"),
    AgentSpec("parcel", "Parsel ve TKGM", ("resolve_parcel", "read_official_parcel"), "tkgm"),
    AgentSpec("protection", "Koruma statüsü", ("natural_sit", "archaeological_sit"), "official_layers"),
    AgentSpec("access", "Erişim", ("research_access",), "tkgm"),
    AgentSpec("geography", "Coğrafya", ("route", "elevation"), "geography"),
    AgentSpec("assessment", "Değerlendirme", ("assess", "explain"), None),
)


def catalog() -> list[dict]:
    return [asdict(agent) for agent in AGENTS]
