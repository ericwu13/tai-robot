"""Tests for AI code extraction and validation."""

import pytest

from src.ai.code_sandbox import extract_python_code, validate_code


class TestExtractPythonCode:
    """Tests for extract_python_code()."""

    def test_standard_python_block(self):
        response = '''Here is the strategy:

```python
class MyStrategy:
    def on_bar(self):
        pass
```

This strategy does nothing.'''
        code = extract_python_code(response)
        assert code is not None
        assert "class MyStrategy" in code
        assert "def on_bar" in code

    def test_no_code_block(self):
        response = "Just some text without any code."
        assert extract_python_code(response) is None

    def test_bare_backticks_no_python_tag(self):
        """Bare ``` without 'python' tag should not be extracted."""
        response = '''Here:

```
class Foo:
    def bar(self):
        pass
```
'''
        assert extract_python_code(response) is None

    def test_other_language_block(self):
        """```javascript or other languages should not match."""
        response = '''```javascript
function hello() {}
```'''
        assert extract_python_code(response) is None

    def test_truncated_response_with_class(self):
        """Truncated response (no closing ```) with valid class should still extract."""
        response = '''```python
from src.backtest.strategy import BacktestStrategy

class TruncatedStrategy(BacktestStrategy):
    def on_bar(self):
        pass'''
        code = extract_python_code(response)
        assert code is not None
        assert "class TruncatedStrategy" in code

    def test_truncated_response_without_class(self):
        """Truncated response without class/def should not extract."""
        response = '''```python
x = 42
y = x + 1'''
        assert extract_python_code(response) is None

    def test_multiple_code_blocks_takes_first(self):
        """Should extract the first ```python block."""
        response = '''```python
class First:
    def f(self): pass
```

```python
class Second:
    def g(self): pass
```'''
        code = extract_python_code(response)
        assert "class First" in code
        assert "class Second" not in code

    def test_code_with_surrounding_text(self):
        """Code extraction should work with Chinese/English text around it."""
        response = '''好的，以下是策略：

```python
class TestStrat:
    def required_bars(self):
        return 20
```

以上是完整的程式碼。'''
        code = extract_python_code(response)
        assert code is not None
        assert "class TestStrat" in code

    def test_empty_code_block(self):
        response = '''```python
```'''
        code = extract_python_code(response)
        # Empty or whitespace-only should return None or empty
        assert not code


class TestValidateCode:
    """Tests for validate_code()."""

    def test_valid_strategy_code(self):
        source = '''
from src.backtest.strategy import BacktestStrategy
from src.backtest.broker import BrokerContext, OrderSide

class GoodStrategy(BacktestStrategy):
    def on_bar(self, bar, data_store, broker):
        pass
'''
        errors = validate_code(source)
        assert errors == []

    def test_forbidden_import_os(self):
        source = '''
import os
class Bad:
    pass
'''
        errors = validate_code(source)
        assert any("os" in e for e in errors)

    def test_forbidden_import_subprocess(self):
        source = '''
from subprocess import run
class Bad:
    pass
'''
        errors = validate_code(source)
        assert any("subprocess" in e for e in errors)

    def test_forbidden_builtin_exec(self):
        source = '''
class Bad:
    def run(self):
        exec("print(1)")
'''
        errors = validate_code(source)
        assert any("exec" in e for e in errors)

    def test_forbidden_builtin_eval(self):
        source = '''
class Bad:
    def run(self):
        eval("1+1")
'''
        errors = validate_code(source)
        assert any("eval" in e for e in errors)

    def test_no_class_definition(self):
        source = '''
def standalone_function():
    return 42
'''
        errors = validate_code(source)
        assert any("No class" in e for e in errors)

    def test_syntax_error(self):
        source = '''
class Bad:
    def broken(self)
        pass
'''
        errors = validate_code(source)
        assert any("Syntax" in e for e in errors)

    def test_math_import_allowed(self):
        source = '''
import math
class Good:
    def calc(self):
        return math.floor(1.5)
'''
        errors = validate_code(source)
        assert errors == []

    def test_indicator_imports_allowed(self):
        source = '''
from src.strategy.indicators import sma, ema, rsi, bollinger_bands
from src.strategy.indicators.atr import atr, true_range

class Good:
    pass
'''
        errors = validate_code(source)
        assert errors == []

    def test_new_indicator_imports_allowed(self):
        source = '''
from src.strategy.indicators import adx, plus_di, minus_di, stochastic

class Good:
    pass
'''
        errors = validate_code(source)
        assert errors == []

    def test_unsupported_indicator_error(self):
        """Importing a non-existent indicator should produce a user-friendly error."""
        source = '''
from src.strategy.indicators import sma, vwap

class Bad:
    pass
'''
        errors = validate_code(source)
        assert len(errors) == 1
        assert "Unsupported indicator" in errors[0]
        assert "'vwap'" in errors[0]
        assert "github.com" in errors[0]

    def test_unsupported_indicator_submodule(self):
        """Importing from a non-existent indicator submodule should also be caught."""
        source = '''
from src.strategy.indicators.adx import adx, dmi_oscillator

class Bad:
    pass
'''
        errors = validate_code(source)
        assert len(errors) == 1
        assert "Unsupported indicator" in errors[0]
        assert "'dmi_oscillator'" in errors[0]
