"""issue #108 — the evolution codegen loop must survive an AI API outage.

`_start_evolution_pipeline` advertises "Phase 2: candidate codegen (one
retry)", but the `one_shot()` call used to sit OUTSIDE the try block: a
transport failure (Gemini 503 "This model is currently experiencing high
demand") escaped the `for attempt in (1, 2)` loop entirely, so the retry
never happened for API failures and the error died in the outer
`except Exception` — logged, no Discord notification, no EVO FAIL card.

The loop lives inside a Tkinter method with closures over local state, so
it cannot be called directly. These tests assert its CONTROL FLOW from the
AST instead, which is exactly the property that regressed.
"""

from __future__ import annotations

import ast
import inspect
import textwrap

import run_backtest as rb


def _codegen_loop() -> ast.For:
    """The `for attempt in (1, 2)` candidate-codegen loop."""
    src = textwrap.dedent(
        inspect.getsource(rb.BacktestApp._start_evolution_pipeline))
    tree = ast.parse(src)
    loops = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.For)
        and isinstance(node.target, ast.Name)
        and node.target.id == "attempt"
    ]
    assert len(loops) == 1, (
        f"expected exactly one `for attempt in ...` codegen loop, "
        f"found {len(loops)}")
    return loops[0]


def _calls(node: ast.AST) -> list[str]:
    """Attribute names of every function call under ``node``."""
    return [
        n.func.attr for n in ast.walk(node)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    ]


class TestCodegenLoopSurvivesApiFailure:

    def test_one_shot_is_inside_a_try_in_the_loop(self):
        """Pre-#108 the one_shot() assignment was a bare statement in the
        loop body and the try wrapped only load_strategy_from_source()."""
        loop = _codegen_loop()
        guarded = [
            stmt for stmt in loop.body
            if isinstance(stmt, ast.Try)
            and any("one_shot" in c for c in _calls(ast.Module(
                body=stmt.body, type_ignores=[])))
        ]
        assert guarded, (
            "one_shot() must run inside a try/except in the codegen loop so "
            "an API failure consumes an attempt instead of escaping the loop")

    def test_no_unguarded_one_shot_statement_in_the_loop(self):
        """No direct child of the loop body may call one_shot() outside a try."""
        loop = _codegen_loop()
        for stmt in loop.body:
            if isinstance(stmt, ast.Try):
                continue
            assert "one_shot" not in _calls(stmt), (
                "one_shot() sits unguarded in the codegen loop — a transport "
                "RuntimeError would escape the `for attempt in (1, 2)` retry")

    def test_transport_errors_are_caught_and_recorded(self):
        """The guarding try must catch broad Exception (httpx errors are NOT
        RuntimeError) and record it in last_err so the final failure is
        reported via the EVO FAIL / Discord path."""
        loop = _codegen_loop()
        tries = [s for s in loop.body if isinstance(s, ast.Try)]
        assert tries, "codegen loop has no try/except"
        broad = [
            h for t in tries for h in t.handlers
            if isinstance(h.type, ast.Name) and h.type.id == "Exception"
        ]
        assert broad, (
            "codegen loop must catch Exception so an AI API/transport failure "
            "consumes an attempt rather than aborting the pipeline")
        # The handler has to feed last_err, which is what the
        # "🧬 EVO FAIL: candidate generation failed twice — {last_err}"
        # message (and its Discord notification) reports.
        assigned = {
            t.id
            for h in broad for n in ast.walk(h)
            if isinstance(n, ast.Assign)
            for t in n.targets if isinstance(t, ast.Name)
        }
        assert "last_err" in assigned

    def test_code_validation_errors_still_handled(self):
        """The original (CodeValidationError, CodeExecutionError) handling
        must not be lost by the restructure."""
        loop = _codegen_loop()
        names = {
            n.id for s in loop.body if isinstance(s, ast.Try)
            for h in s.handlers for n in ast.walk(h) if isinstance(n, ast.Name)
        }
        assert {"CodeValidationError", "CodeExecutionError"} <= names
