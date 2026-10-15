"""Token and cost accounting, per task."""

import threading
from dataclasses import dataclass

from janus_agent.config import Pricing
from janus_agent.llm.base import Usage


@dataclass
class Line:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    reported_cost: float = 0.0
    has_reported_cost: bool = False


class CostTracker:
    def __init__(self, pricing: Pricing | None = None):
        self.pricing = pricing or Pricing()
        self.lines = {}
        self._lock = threading.Lock()

    def add(self, tag: str, usage: Usage) -> None:
        with self._lock:
            line = self.lines.setdefault(tag or "model", Line())
            line.calls += 1
            line.input_tokens += usage.input_tokens
            line.output_tokens += usage.output_tokens
            if usage.cost is not None:
                line.reported_cost += usage.cost
                line.has_reported_cost = True

    def cost(self, line: Line) -> float | None:
        if line.has_reported_cost:
            return line.reported_cost
        p = self.pricing
        if p.input_per_mtok is None or p.output_per_mtok is None:
            return None
        return (line.input_tokens * p.input_per_mtok + line.output_tokens * p.output_per_mtok) / 1e6

    def total(self) -> Line:
        total = Line()
        for line in self.lines.values():
            total.calls += line.calls
            total.input_tokens += line.input_tokens
            total.output_tokens += line.output_tokens
            total.reported_cost += line.reported_cost
            total.has_reported_cost = total.has_reported_cost or line.has_reported_cost
        return total

    def as_dict(self) -> dict:
        out = {}
        for tag, line in {**self.lines, "total": self.total()}.items():
            out[tag] = {"calls": line.calls, "input_tokens": line.input_tokens,
                        "output_tokens": line.output_tokens, "cost_usd": self.cost(line)}
        return out

    def render(self) -> str:
        if not self.lines:
            return "Model usage: no model call."
        rows = [("task", "calls", "tokens in", "tokens out", "cost")]
        for tag, data in self.as_dict().items():
            cost = data["cost_usd"]
            rows.append((tag, str(data["calls"]), str(data["input_tokens"]), str(data["output_tokens"]),
                         "unknown" if cost is None else f"${cost:.4f}"))
        widths = [max(len(r[i]) for r in rows) for i in range(5)]
        lines = ["Model usage:"]
        for i, row in enumerate(rows):
            lines.append("  " + "  ".join(c.ljust(w) if j == 0 else c.rjust(w)
                                          for j, (c, w) in enumerate(zip(row, widths))))
            if i == 0 or i == len(rows) - 2:
                lines.append("  " + "  ".join("-" * w for w in widths))
        if self.cost(self.total()) is None:
            lines.append("  (set [pricing] in the config to see an estimated cost)")
        return "\n".join(lines)
