"""Domain registry (design §5). Each domain = how to fetch + how to normalise one data type."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from gds.source import Source

Rows = dict[str, list[dict[str, Any]]]
"""Table name → rows. The special key "work_units" asks the engine to create units
({"domain", "scope_key", "priority", "requeue"?}) instead of upserting a table."""


@dataclass(frozen=True)
class Domain:
    name: str
    scope: str  # "daily" (date), "index" (activity list), "activity" (activity id)
    fetch: Callable[[Source, str], Any]
    # None = raw-only (stored, not yet mapped to typed tables)
    normalize: Callable[[Any, str], Rows] | None = None
    priority: int = 10
    binary: bool = False  # payload is bytes (e.g. a FIT zip), not JSON


REGISTRY: dict[str, Domain] = {}


def register(domain: Domain) -> Domain:
    if domain.name in REGISTRY:
        raise ValueError(f"duplicate domain {domain.name}")
    REGISTRY[domain.name] = domain
    return domain


def daily_domains() -> list[Domain]:
    return [d for d in REGISTRY.values() if d.scope == "daily"]


def activity_domains() -> list[Domain]:
    return [d for d in REGISTRY.values() if d.scope == "activity"]


from gds.domains import activities as _activities  # noqa: E402,F401  (registers domains)
from gds.domains import daily as _daily  # noqa: E402,F401
