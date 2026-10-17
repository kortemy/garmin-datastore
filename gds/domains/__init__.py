"""Domain registry (design §5). Each domain = how to fetch + how to normalise one data type."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from gds.source import Source

Rows = dict[str, list[dict[str, Any]]]
"""Table name → rows. Special keys:
- "work_units": units to create ({"domain", "scope_key", "priority", "requeue"?}).
- "_replace": [{"table", "scope": {col: value}}]: after upserting, delete rows of `table`
  in `scope` that this payload no longer contains (e.g. a deleted workout)."""


@dataclass(frozen=True)
class Domain:
    name: str
    # "daily" (YYYY-MM-DD), "range" (YYYY-MM), "snapshot" ("latest"),
    # "index" (activity list), "activity" (activity id), "entity" (an entity id)
    scope: str
    fetch: Callable[[Source, str], Any]
    # None = raw-only (stored, not yet mapped to typed tables)
    normalize: Callable[[Any, str], Rows] | None = None
    priority: int = 10
    binary: bool = False  # payload is bytes (e.g. a FIT zip), not JSON
    refresh_hours: float | None = None  # snapshot/range: re-fetch "live" units this often
    months_ahead: int = 0  # range: also keep units for future months (calendar)


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


def domains_in(scope: str) -> list[Domain]:
    return [d for d in REGISTRY.values() if d.scope == scope]


from gds.domains import activities as _activities  # noqa: E402,F401  (registers domains)
from gds.domains import daily as _daily  # noqa: E402,F401
from gds.domains import ranges as _ranges  # noqa: E402,F401
from gds.domains import snapshots as _snapshots  # noqa: E402,F401
