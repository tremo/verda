"""Execution is chosen per action, independently of the specialist's name."""
KINDS = {
    "manager": {"read_case": "connector", "read_timeline": "connector", "propose_plan": "model"},
    "sahibinden": {"discover": "connector", "read_listing": "connector", "read_thread": "connector", "propose_question": "model"},
    "parcel": {"resolve_parcel": "connector", "read_official_parcel": "connector"},
    "protection": {"natural_sit": "connector", "archaeological_sit": "connector"},
    "access": {"research_access": "model"},
    "geography": {"route": "connector", "elevation": "connector"},
    "assessment": {"assess": "deterministic", "explain": "model"},
}
