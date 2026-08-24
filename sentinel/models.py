"""Shared typed records used across all SENTINEL stages (JSON round-trippable)."""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any


def to_jsonable(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {k: to_jsonable(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return repr(obj)


@dataclass
class Param:
    name: str
    annotation: str | None = None
    default_repr: str | None = None  # None means "no default"
    has_default: bool = False
    kind: str = "positional_or_keyword"  # POSITIONAL_ONLY|POSITIONAL_OR_KEYWORD|VAR_POSITIONAL|KEYWORD_ONLY|VAR_KEYWORD


@dataclass
class TargetIR:
    module_path: str
    qualname: str
    kind: str = "function"  # function | method | async_function
    signature: str = "()"
    params: list[Param] = field(default_factory=list)
    return_annotation: str | None = None
    docstring: str | None = None
    source_segment: str = ""
    module_functions: list[str] = field(default_factory=list)
    decorators: list[str] = field(default_factory=list)
    skip_reason: str | None = None

    @property
    def param_names(self) -> list[str]:
        return [p.name for p in self.params if not p.kind.startswith("VAR")]

    @property
    def callable_label(self) -> str:
        return f"{self.module_path}::{self.qualname}"


@dataclass
class ExecResult:
    status: str  # OK | EXCEPTION | TIMEOUT | OOM | WORKER_CRASH | INTERNAL
    value_repr: str | None = None
    exc_type: str | None = None
    exc_repr: str | None = None
    traceback_tail: list[str] = field(default_factory=list)
    arcs: list[list[int]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "OK"


@dataclass
class Property:
    name: str
    kind: str  # exception_contract | determinism | idempotence | range_bound | round_trip | snapshot
    description: str
    params: dict[str, Any] = field(default_factory=dict)
    provenance: str = "template"  # heuristic | template | llm | user | snapshot
    confidence: float = 0.5

    def to_dict(self) -> dict:
        return to_jsonable(self)

    @staticmethod
    def from_dict(d: dict) -> "Property":
        return Property(
            name=d["name"], kind=d["kind"], description=d.get("description", ""),
            params=d.get("params", {}), provenance=d.get("provenance", "template"),
            confidence=float(d.get("confidence", 0.5)),
        )


@dataclass
class Finding:
    id: str
    severity_class: str  # CONFIRMED | SUSPECTED
    minimal_input: str
    invariant_violated: str
    expected: str
    actual: str
    root_cause_guess: str = ""
    suggested_patch: str = ""
    provenance: str = ""
    original_input: str = ""
    minimality_ratio: float = 1.0

    def to_dict(self) -> dict:
        return to_jsonable(self)


@dataclass
class ScanReport:
    target: str
    coverage: dict = field(default_factory=dict)
    properties_checked: list[dict] = field(default_factory=list)
    bugs: list[dict] = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return to_jsonable(self)
