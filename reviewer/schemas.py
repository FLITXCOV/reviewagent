"""Data contracts. Every line number in this project is a NEW-SIDE line number
(the line number in the file AFTER the change, i.e. the right-hand side of a diff)."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

Category = Literal["security", "error_handling", "tests", "logic"]
Severity = Literal["low", "medium", "high"]
Stratum = Literal["deletion_only", "modification"]
CATEGORIES = ("security", "error_handling", "tests", "logic")
SEVERITIES = ("low", "medium", "high")


class Finding(BaseModel):
    file: str
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    category: Category
    severity: Severity
    confidence: float = Field(ge=0.0, le=1.0)
    message: str = Field(min_length=1)
    evidence: list[str] = Field(default_factory=list)
    suggested_fix: str | None = None

    @model_validator(mode="after")
    def _ordered(self) -> "Finding":
        if self.line_end < self.line_start:
            raise ValueError("line_end must be >= line_start")
        return self


class JudgeVerdict(BaseModel):
    matches_root_cause: bool
    reasoning: str = ""


class ReviewOutput(BaseModel):
    summary: str = ""
    findings: list[Finding] = Field(default_factory=list)
    failed_nodes: list[str] = Field(default_factory=list)
    retries: int = 0
    hunk_splits: int = 0
    dropped_findings: int = 0
    llm_calls: int = 0


def _clean_path(p: str) -> str:
    p = p.strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def coerce_findings(raw: Any) -> tuple[list[Finding], int]:
    """Tolerantly turn LLM output (a list of dicts) into valid Findings.

    Repairs: swapped line_start/line_end, confidence outside [0,1], './' path prefix,
    severity/category casing. Anything still invalid is DROPPED and counted.
    Returns (findings, dropped_count). Never raises on bad model output."""
    if not isinstance(raw, list):
        return [], 0
    out: list[Finding] = []
    dropped = 0
    for item in raw:
        try:
            if not isinstance(item, dict):
                raise ValueError("not an object")
            d = dict(item)
            d["file"] = _clean_path(str(d.get("file", "")))
            for key in ("category", "severity"):
                if isinstance(d.get(key), str):
                    d[key] = d[key].strip().lower()
            ls, le = int(d["line_start"]), int(d.get("line_end", d["line_start"]))
            d["line_start"], d["line_end"] = (ls, le) if le >= ls else (le, ls)
            d["confidence"] = min(1.0, max(0.0, float(d.get("confidence", 0.5))))
            out.append(Finding(**d))
        except Exception:
            dropped += 1
    return out, dropped
