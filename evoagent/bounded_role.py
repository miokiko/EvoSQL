"""Budgeted JSON/tool role execution shared by the Text2SQL agents."""
import json
import time
from typing import Any, Dict, List, Optional
from .context_manager import ContextManager
from .llm import JsonChatClient
from .runtime import RuntimeBudgetExceeded, ToolRegistry
from .telemetry import ExecutionLedger


_FINAL_RESPONSE_FIELDS = frozenset(
    {
        "route",
        "clarification",
        "approve_plan",
        "final_candidate_index",
        "answer_text",
        "schema_plan",
        "query_spec",
        "sql_candidates",
        "decisions",
    }
)


class BoundedRole:
    def __init__(
        self, name: str, prompt: str, client: JsonChatClient,
        token_budget: int, time_budget: int, max_steps: int = 4,
        context_manager: Optional[ContextManager] = None,
        working_memory_supplier=None, observation_sink=None,
    ):
        self.name = name
        self.prompt = prompt
        self.client = client
        self.token_budget = token_budget
        self.time_budget = time_budget
        self.max_steps = max_steps
        self.context_manager = context_manager or ContextManager()
        self.working_memory_supplier = working_memory_supplier
        self.observation_sink = observation_sink

    def run(
        self, user_context: str, tools: ToolRegistry, ledger: ExecutionLedger,
    ) -> Dict[str, Any]:
        started = time.monotonic()
        observations: List[dict] = []
        response_contract_repairs = 0
        starting_tokens = sum(
            item.input_tokens + item.output_tokens
            for item in ledger.model_calls if item.role == self.name
        )
        ledger.trace(
            self.name, "started", token_budget=self.token_budget,
            time_budget_seconds=self.time_budget, tools=tools.names(),
        )
        for step in range(1, self.max_steps + 1):
            elapsed = time.monotonic() - started
            used = sum(
                item.input_tokens + item.output_tokens
                for item in ledger.model_calls if item.role == self.name
            ) - starting_tokens
            if elapsed >= self.time_budget or used >= self.token_budget:
                ledger.trace(self.name, "budget_exhausted", step=step, tokens_used=used)
                raise RuntimeBudgetExceeded("%s budget exhausted" % self.name)
            output_allowance = self.context_manager.output_token_limit(
                self.prompt, min(4000, max(256, self.token_budget - used))
            )
            current_context = user_context
            if self.working_memory_supplier is not None:
                try:
                    working = self.working_memory_supplier()
                    if working:
                        task_context = json.loads(user_context)
                        task_context["working_memory"] = working
                        current_context = json.dumps(task_context, ensure_ascii=False)
                except Exception as exc:
                    ledger.trace(
                        self.name, "working_memory_unavailable", error=str(exc)[:500],
                    )
            managed, context_stats = self.context_manager.build_managed_context(
                current_context, tools.catalog(), observations,
                max(0, self.token_budget - used),
                max(0, int(self.time_budget - elapsed)),
                system_prompt=self.prompt, max_output_tokens=output_allowance,
            )
            ledger.trace(
                self.name, "context_prepared", step=step,
                estimated_input_tokens=context_stats["estimated_input_tokens_after"],
                input_token_limit=context_stats["input_token_limit"],
                observations_summarized=context_stats["observations"]["summarized"],
                observations_dropped=context_stats["observations"]["dropped"],
            )
            action = self.client.complete_json(
                self.name, self.prompt,
                json.dumps(managed, ensure_ascii=False, default=str),
                ledger, max_tokens=output_allowance,
            )
            kind = str(action.get("action", "")).strip().lower()
            ledger.trace(
                self.name, "autonomous_decision", step=step, action=kind,
                tool=str(action.get("tool", "")), reason=str(action.get("reason", ""))[:500],
            )
            if kind == "final":
                action = dict(action)
                action["action"] = "final"
                action["_observations"] = observations
                action["_steps"] = step
                ledger.trace(self.name, "finished", step=step)
                return action
            if kind != "tool":
                # The domain payload can already be complete while a provider
                # omits or aliases only the tiny outer action discriminator.
                # Normalize that envelope without another model call; the
                # caller still validates the route/plan/SQL/critic contract.
                terminal_fields = sorted(
                    _FINAL_RESPONSE_FIELDS.intersection(action)
                )
                if terminal_fields:
                    action = dict(action)
                    action["action"] = "final"
                    action["_observations"] = observations
                    action["_steps"] = step
                    ledger.trace(
                        self.name,
                        "response_contract_envelope_normalized",
                        step=step,
                        invalid_action=kind[:100],
                        terminal_fields=terminal_fields,
                    )
                    ledger.trace(self.name, "finished", step=step)
                    return action
                # A provider can return the intended JSON payload while omitting
                # or misspelling the small ``action`` envelope.  Give the role
                # exactly one bounded chance to resend a protocol-valid response;
                # downstream domain contracts and deterministic gates still own
                # the contents.  Repeated malformed responses fail closed.
                if response_contract_repairs >= 1 or step >= self.max_steps:
                    raise ValueError("%s returned an invalid action" % self.name)
                response_contract_repairs += 1
                observations.append(
                    {
                        "step": step,
                        "tool": "response_contract",
                        "ok": False,
                        "error": (
                            'Invalid top-level action. Return the intended JSON again '
                            'with action="final", or use action="tool" only for one '
                            "registered tool call."
                        ),
                    }
                )
                ledger.trace(
                    self.name,
                    "response_contract_repair_requested",
                    step=step,
                    invalid_action=kind[:100],
                    repair=response_contract_repairs,
                )
                continue
            tool_name = str(action.get("tool", ""))
            arguments = action.get("arguments") or {}
            try:
                value = tools.invoke(tool_name, arguments)
                observation = {
                    "step": step, "tool": tool_name, "ok": True, "result": value,
                }
            except Exception as exc:
                observation = {
                    "step": step, "tool": tool_name, "ok": False,
                    "error": str(exc)[:1000],
                }
            observations.append(observation)
            if self.observation_sink is not None:
                try:
                    self.observation_sink(self.name, observation)
                except Exception as exc:
                    ledger.trace(
                        self.name, "working_memory_write_failed", error=str(exc)[:500],
                    )
            ledger.trace(
                self.name, "tool_observation", step=step, tool=tool_name,
                ok=observation["ok"],
            )
        ledger.trace(self.name, "budget_exhausted", budget="steps")
        raise RuntimeBudgetExceeded("%s step budget exhausted" % self.name)
