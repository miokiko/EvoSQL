"""Token budgets and bounded tool observations for Text2SQL roles."""
import json
import math
from typing import Any, Dict, List, Optional, Sequence, Tuple


def estimate_tokens(value: Any) -> int:
    """Conservative dependency-free token estimate suitable for preflight limits."""
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))
    if not value:
        return 0
    ascii_chars = sum(ord(char) < 128 for char in value)
    non_ascii = len(value) - ascii_chars
    # Code tokenizers average roughly 3-4 ASCII chars/token; CJK is closer to
    # one character/token.  The fixed overhead covers JSON punctuation.
    return max(1, math.ceil(ascii_chars / 3.2 + non_ascii + 4))


def _clip(value: str, limit: int) -> str:
    value = str(value)
    if len(value) <= limit:
        return value
    if limit < 20:
        return value[:limit]
    left = max(1, int(limit * 0.7))
    right = max(1, limit - left - 15)
    return value[:left] + "\n...<omitted>...\n" + value[-right:]


class ContextManager:
    def __init__(self, context_window_tokens=32768, input_token_budget=20000,
                 observation_token_budget=4000, recent_observations=2):
        self.context_window_tokens = max(2048, int(context_window_tokens))
        self.input_token_budget = max(1024, min(int(input_token_budget), self.context_window_tokens - 512))
        self.observation_token_budget = max(256, int(observation_token_budget))
        self.recent_observations = max(0, min(int(recent_observations), 8))

    def build_managed_context(
        self, task: str, tools: Sequence[Dict[str, Any]], observations: Sequence[Dict[str, Any]],
        remaining_token_budget: int, remaining_time_seconds: int, system_prompt: str = "",
        max_output_tokens: int = 4000,
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        system_tokens = estimate_tokens(system_prompt)
        hard_input_limit = max(
            512,
            min(
                self.input_token_budget,
                self.context_window_tokens - max(256, max_output_tokens) - system_tokens - 512,
            ),
        )
        tools_tokens = estimate_tokens(tools)
        observation_budget = min(
            self.observation_token_budget,
            max(128, hard_input_limit - tools_tokens - 512),
        )
        compact_observations, observation_stats = self.compact_observations(
            observations, observation_budget,
        )
        managed = {
            "task": task,
            "available_tools": list(tools),
            "observations": compact_observations,
            "remaining_token_budget": max(0, int(remaining_token_budget)),
            "remaining_time_seconds": max(0, int(remaining_time_seconds)),
            "context_policy": {
                "input_token_limit": hard_input_limit,
                "observation_policy": "semantic summaries plus recent sliding window",
            },
        }
        original_tokens = estimate_tokens(managed)
        if original_tokens > hard_input_limit:
            managed["task"] = self._compact_task(task, max(256, hard_input_limit - tools_tokens - observation_budget - 256))
        # A final defensive reduction handles unexpectedly large tool schemas or
        # task metadata while preserving JSON structure and list indices.
        while estimate_tokens(managed) > hard_input_limit and managed["observations"]:
            managed["observations"].pop(0)
            observation_stats["dropped"] += 1
        if estimate_tokens(managed) > hard_input_limit:
            managed["task"] = self._minimal_task(
                managed["task"], max(192, hard_input_limit - tools_tokens - 320)
            )
        if estimate_tokens(managed) > hard_input_limit:
            managed["context_policy"] = {"input_token_limit": hard_input_limit}
        stats = {
            "estimated_input_tokens_before": original_tokens,
            "estimated_input_tokens_after": estimate_tokens(managed),
            "input_token_limit": hard_input_limit,
            "observations": observation_stats,
        }
        return managed, stats

    def output_token_limit(self, system_prompt: str, requested: int) -> int:
        """Reserve enough of the configured window for a minimally useful input."""
        capacity = self.context_window_tokens - estimate_tokens(system_prompt) - 1024
        return max(128, min(int(requested), max(128, capacity)))

    def compact_observations(
        self, observations: Sequence[Dict[str, Any]], max_tokens: Optional[int] = None,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
        budget = max(128, int(max_tokens or self.observation_token_budget))
        values = [dict(item) for item in observations]
        if estimate_tokens(values) <= budget:
            return values, {"total": len(values), "summarized": 0, "dropped": 0}
        split = max(0, len(values) - self.recent_observations)
        compact = [self._summarize_observation(item) for item in values[:split]]
        compact.extend(self._bound_observation(item) for item in values[split:])
        summarized = split
        # Convert recent raw observations to summaries before dropping anything.
        index = split
        while estimate_tokens(compact) > budget and index < len(compact):
            compact[index] = self._summarize_observation(values[index])
            summarized += 1
            index += 1
        dropped = 0
        while estimate_tokens(compact) > budget and len(compact) > 1:
            compact.pop(0)
            dropped += 1
        if estimate_tokens(compact) > budget and compact:
            compact[0] = self._summarize_observation(compact[0], content_limit=160)
        if dropped:
            compact.insert(0, {
                "kind": "observation_rollup", "dropped": dropped,
                "note": "Old observations were removed after semantic summarization budget was exhausted.",
            })
        return compact, {"total": len(values), "summarized": min(len(values), summarized), "dropped": dropped}

    @staticmethod
    def _summarize_observation(
        item: Dict[str, Any], content_limit: int = 600,
    ) -> Dict[str, Any]:
        result = item.get("result")
        evidence_id = result.get("evidence_id", "") if isinstance(result, dict) else ""
        output = result.get("output") if isinstance(result, dict) else result
        if isinstance(output, dict):
            shape = {key: ContextManager._shape(value) for key, value in list(output.items())[:20]}
            salient = " ".join(
                str(value) for value in output.values()
                if isinstance(value, (str, int, float, bool))
            )
        elif isinstance(output, list):
            shape = {"items": len(output), "sample": output[:2]}
            salient = ""
        else:
            shape = {"value": _clip(str(output), content_limit)}
            salient = str(output)
        return {
            "step": item.get("step"), "tool": item.get("tool", ""),
            "ok": bool(item.get("ok")), "evidence_id": evidence_id,
            "semantic_summary": shape,
            "salient_text": _clip(salient, content_limit),
            "error": _clip(str(item.get("error", "")), 500),
            "compacted": True,
        }

    @staticmethod
    def _shape(value: Any) -> Any:
        if isinstance(value, list):
            return {"type": "list", "count": len(value), "sample": value[:1]}
        if isinstance(value, dict):
            return {"type": "object", "keys": list(value)[:20]}
        if isinstance(value, str):
            return _clip(value, 240)
        return value

    @staticmethod
    def _bound_observation(item: Dict[str, Any]) -> Dict[str, Any]:
        value = dict(item)
        result = value.get("result")
        if estimate_tokens(result) > 1200:
            return ContextManager._summarize_observation(value, content_limit=900)
        return value

    @staticmethod
    def _compact_task(task: str, budget: int) -> str:
        if estimate_tokens(task) <= budget:
            return task
        try:
            value = json.loads(task)
        except (TypeError, ValueError, json.JSONDecodeError):
            return _clip(task, max(256, budget * 2))

        def reduce(item: Any, depth: int = 0) -> Any:
            if isinstance(item, dict):
                result = {}
                for key, child in item.items():
                    result[key] = child if key in {"approved_query_plan", "bound_query_plan", "schema_plan", "query_spec", "sql", "candidate_id"} else reduce(child, depth + 1)
                return result
            if isinstance(item, list):
                # Preserve list cardinality and indices used by Lead/Critic.
                return [reduce(child, depth + 1) for child in item]
            if isinstance(item, str):
                return _clip(item, 600 if depth < 3 else 300)
            return item

        compact = reduce(value)
        rendered = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
        if estimate_tokens(rendered) <= budget:
            return rendered
        # Repeatedly shrink prose without removing candidate entries.
        limit = 180
        while estimate_tokens(rendered) > budget and limit >= 40:
            def shrink(item: Any) -> Any:
                if isinstance(item, dict):
                    return {key: child if key in {"approved_query_plan", "bound_query_plan", "schema_plan", "query_spec", "sql", "candidate_id"} else shrink(child) for key, child in item.items()}
                if isinstance(item, list):
                    return [shrink(child) for child in item]
                return _clip(item, limit) if isinstance(item, str) else item
            rendered = json.dumps(shrink(compact), ensure_ascii=False, separators=(",", ":"))
            limit //= 2
        return rendered

    @staticmethod
    def _minimal_task(task: str, budget: int) -> str:
        """Keep domain objects and candidate indices intact under budget pressure."""
        try:
            value = json.loads(task)
        except (TypeError, ValueError):
            return json.dumps({"context_compacted": True, "content_preview": _clip(str(task), max(80, budget * 2))}, ensure_ascii=False)
        protected = {"approved_query_plan", "bound_query_plan", "schema_plan", "query_spec", "sql", "candidate_id", "candidate_index"}
        def compact(item, key=""):
            if key in protected:
                return item
            if isinstance(item, dict):
                return {name: compact(child, name) for name, child in item.items()}
            if isinstance(item, list):
                return [compact(child) for child in item]
            return _clip(item, 80) if isinstance(item, str) else item
        rendered = json.dumps(compact(value), ensure_ascii=False, separators=(",", ":"))
        if estimate_tokens(rendered) > budget:
            raise ValueError("Text2SQL task exceeds available context budget")
        return rendered
