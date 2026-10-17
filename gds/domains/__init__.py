"""Domain registry (design §5). Each domain = how to fetch + how to normalise one data type."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from gds.source import Source

Rows = dict[str, list[dict[str, Any]]]


@dataclass(frozen=True)
class Domain:
    name: str
    scope: str  # "daily" for now; "activity", "range", "snapshot" arrive in M2/M3
    fetch: Callable[[Source, str], Any]
    # None = raw-only (stored, not yet mapped to typed tables)
    normalize: Callable[[Any, str], Rows] | None = None
    priority: int = 10


REGISTRY: dict[str, Domain] = {}


def register(domain: Domain) -> Domain:
    if domain.name in REGISTRY:
        raise ValueError(f"duplicate domain {domain.name}")
    REGISTRY[domain.name] = domain
    return domain


def daily_domains() -> list[Domain]:
    return [d for d in REGISTRY.values() if d.scope == "daily"]


from gds.domains import daily as _daily  # noqa: E402,F401  (registers domains)
