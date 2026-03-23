"""Code validation and dynamic loading for AI-generated strategies."""

from __future__ import annotations

import ast
import re
from typing import Type


# Imports that AI-generated strategies are allowed to use
ALLOWED_IMPORTS = {
    "src.backtest.strategy",
    "src.backtest.broker",
    "src.market_data.models",
    "src.market_data.data_store",
    "src.market_data.sessions",
    "src.strategy.indicators",
    "src.strategy.indicators.ma",
    "src.strategy.indicators.rsi",
    "src.strategy.indicators.macd",
    "src.strategy.indicators.bollinger",
    "src.strategy.indicators.atr",
    "src.strategy.indicators.adx",
    "src.strategy.indicators.stochastic",
    "math",
}

# All indicator names exported from src.strategy.indicators
AVAILABLE_INDICATORS = {
    "sma", "ema", "rsi", "macd", "bollinger_bands",
    "atr", "true_range", "adx", "plus_di", "minus_di", "stochastic",
}

# Built-in names that are forbidden in AI-generated code
FORBIDDEN_BUILTINS = {
    "exec", "eval", "compile", "__import__", "open",
    "breakpoint", "exit", "quit", "input", "globals", "locals",
    "getattr", "setattr", "delattr",
}

# Modules that are forbidden even in import-from statements
FORBIDDEN_MODULES = {
    "os", "sys", "subprocess", "shutil", "pathlib",
    "socket", "http", "urllib", "requests", "httpx",
    "pickle", "shelve", "ctypes", "importlib",
}


class CodeValidationError(Exception):
    pass


class CodeExecutionError(Exception):
    pass


def extract_python_code(response: str) -> str | None:
    """Extract the first ```python ... ``` code block from an AI response.

    Also handles truncated responses where the closing ``` is missing.
    """
    # Try complete code block first
    pattern = r"```python\s*\n(.*?)```"
    match = re.search(pattern, response, re.DOTALL)
    if match:
        return match.group(1).strip()

    # Handle truncated response (no closing ```)
    pattern_open = r"```python\s*\n(.*)"
    match = re.search(pattern_open, response, re.DOTALL)
    if match:
        code = match.group(1).strip()
        if "class " in code and "def " in code:
            return code

    return None


def validate_code(source: str) -> list[str]:
    """Validate AI-generated strategy code using AST analysis.

    Returns a list of error strings. Empty list means code is valid.
    """
    errors: list[str] = []

    # 1. Check syntax
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        errors.append(f"Syntax error: {e}")
        return errors

    # 2. Check imports
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name not in ALLOWED_IMPORTS and alias.name not in {"math"}:
                    errors.append(f"Forbidden import: {alias.name}")

        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            # Allow any sub-import from allowed modules
            allowed = any(
                module == m or module.startswith(m + ".")
                for m in ALLOWED_IMPORTS
            )
            if not allowed:
                errors.append(f"Forbidden import: from {module}")
            if module in FORBIDDEN_MODULES:
                errors.append(f"Dangerous module: {module}")

            # Check for unsupported indicator names
            if module == "src.strategy.indicators" or module.startswith("src.strategy.indicators."):
                for alias in (node.names or []):
                    name = alias.name
                    if name != "*" and name not in AVAILABLE_INDICATORS:
                        errors.append(
                            f"Unsupported indicator: '{name}' is not available. "
                            f"Available: {', '.join(sorted(AVAILABLE_INDICATORS))}. "
                            f"Please create an issue at https://github.com/ericwu13/tai-robot/issues "
                            f"to request support for '{name}'."
                        )

    # 3. Check for forbidden built-in calls
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in FORBIDDEN_BUILTINS:
                errors.append(f"Forbidden call: {node.func.id}()")

    # 4. Check that there's at least one class definition
    class_defs = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
    if not class_defs:
        errors.append("No class definition found")

    return errors


def load_strategy_from_source(source: str) -> Type:
    """Validate, execute, and extract a BacktestStrategy subclass from source code.

    Returns the strategy class.
    Raises CodeValidationError if code fails validation.
    Raises CodeExecutionError if code fails to execute or no strategy class found.
    """
    errors = validate_code(source)
    if errors:
        raise CodeValidationError("Code validation failed:\n" + "\n".join(f"  - {e}" for e in errors))

    # Import the base class for isinstance checking
    from src.backtest.strategy import BacktestStrategy

    # Execute in a namespace with access to all src modules
    namespace: dict = {}
    try:
        exec(source, namespace)
    except Exception as e:
        raise CodeExecutionError(f"Code execution failed: {e}") from e

    # Find BacktestStrategy subclass(es)
    strategy_classes = []
    for obj in namespace.values():
        if (isinstance(obj, type)
                and issubclass(obj, BacktestStrategy)
                and obj is not BacktestStrategy):
            strategy_classes.append(obj)

    if not strategy_classes:
        raise CodeExecutionError(
            "No BacktestStrategy subclass found in generated code. "
            "The class must inherit from BacktestStrategy."
        )

    # Return the first (typically only) strategy class
    return strategy_classes[0]
