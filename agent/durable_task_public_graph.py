"""Bounded public projection for H5 task graphs.

The durable audit may retain larger summaries and many historical dependency
relations. Harness needs only a bounded operational projection so one session
cannot inflate a graph response beyond the existing BFF response budget.
"""
from __future__ import annotations

from typing import Any

from agent.durable_task_orchestration import DurableTaskGraphProjection

_MAX_RELATIONS_PER_TASK = 64
_MAX_RUN_TEXT = 2_000


class PublicDurableTaskGraphProjection(DurableTaskGraphProjection):
    """Sanitize the read-only graph into a bounded API representation."""

    def graph(self, parent: str, *, limit: int = 100) -> dict[str, Any]:
        graph = super().graph(parent, limit=limit)
        for task in graph.get("tasks", []):
            for field in ("blocked_by", "dependents"):
                values = list(task.get(field) or [])
                if len(values) > _MAX_RELATIONS_PER_TASK:
                    task[f"{field}_truncated"] = True
                    task[field] = values[:_MAX_RELATIONS_PER_TASK]
                else:
                    task[f"{field}_truncated"] = False
            run = task.get("last_run")
            if isinstance(run, dict):
                for field in ("summary", "error"):
                    value = run.get(field)
                    if value is not None:
                        text = str(value)
                        run[field] = text[:_MAX_RUN_TEXT]
                        run[f"{field}_truncated"] = len(text) > _MAX_RUN_TEXT
        # Edges are already limited to relations among the <=100 returned
        # nodes, so their theoretical maximum stays bounded by the node set.
        return graph


__all__ = ["PublicDurableTaskGraphProjection"]
