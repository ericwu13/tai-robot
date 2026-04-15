"""Tests for StrategyStore.import_external_file / preview_external_file."""

from __future__ import annotations

import json
import os
import textwrap

import pytest

from src.ai.strategy_store import StrategyStore


VALID_STRATEGY = textwrap.dedent('''
    """A test strategy that does nothing."""
    from src.backtest.strategy import BacktestStrategy
    from src.backtest.broker import BrokerContext
    from src.market_data.models import Bar
    from src.market_data.data_store import DataStore

    class MyTestStrategy(BacktestStrategy):
        """One-line summary for the test strategy."""
        kline_type = 0
        kline_minute = 15

        def __init__(self, **kwargs):
            self.period = kwargs.get("period", 14)

        def required_bars(self) -> int:
            return self.period

        def on_bar(self, bar: Bar, data_store: DataStore, broker: BrokerContext) -> None:
            pass
''').lstrip()


def _write(tmp_path, name, content):
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return str(p)


@pytest.fixture
def store(tmp_path):
    """Fresh StrategyStore in a tmp dir."""
    return StrategyStore(str(tmp_path / "strategies"))


class TestPreview:
    def test_valid_file(self, store, tmp_path):
        path = _write(tmp_path, "my_test.py", VALID_STRATEGY)
        info = store.preview_external_file(path)

        assert info["class_name"] == "MyTestStrategy"
        assert "One-line summary" in info["docstring"]
        assert info["kline_minute"] == 15
        assert info["exists"] is False

    def test_file_not_found(self, store, tmp_path):
        with pytest.raises(ValueError, match="File not found"):
            store.preview_external_file(str(tmp_path / "nope.py"))

    def test_not_python_file(self, store, tmp_path):
        path = _write(tmp_path, "notes.txt", "hello")
        with pytest.raises(ValueError, match="must be a .py"):
            store.preview_external_file(path)

    def test_syntax_error(self, store, tmp_path):
        path = _write(tmp_path, "broken.py", "class X(:\n  pass")
        with pytest.raises(ValueError, match="Validation failed"):
            store.preview_external_file(path)

    def test_forbidden_import(self, store, tmp_path):
        bad = textwrap.dedent('''
            import os
            from src.backtest.strategy import BacktestStrategy
            from src.backtest.broker import BrokerContext
            from src.market_data.models import Bar
            from src.market_data.data_store import DataStore

            class BadStrategy(BacktestStrategy):
                """Uses forbidden import."""
                kline_type = 0
                kline_minute = 1
                def __init__(self, **kwargs): pass
                def required_bars(self): return 1
                def on_bar(self, bar, data_store, broker): pass
        ''').lstrip()
        path = _write(tmp_path, "bad.py", bad)
        with pytest.raises(ValueError, match="Forbidden import"):
            store.preview_external_file(path)

    def test_no_strategy_class(self, store, tmp_path):
        # Has a class but not a BacktestStrategy subclass
        bad = "class NotAStrategy:\n    pass\n"
        path = _write(tmp_path, "no_strategy.py", bad)
        with pytest.raises(ValueError, match="No BacktestStrategy"):
            store.preview_external_file(path)

    def test_exists_flag_set_when_already_in_index(self, store, tmp_path):
        path = _write(tmp_path, "my_test.py", VALID_STRATEGY)
        store.import_external_file(path, description="first")

        info = store.preview_external_file(path)
        assert info["exists"] is True


class TestImport:
    def test_import_creates_file_and_updates_index(self, store, tmp_path):
        src = _write(tmp_path, "my_test.py", VALID_STRATEGY)

        cls, fname = store.import_external_file(src, description="my desc")

        assert cls == "MyTestStrategy"
        assert fname == "my_test_strategy.py"
        # File written to store_dir
        assert os.path.exists(os.path.join(store.store_dir, fname))
        # Index updated
        listed = store.list_strategies()
        assert any(e["class_name"] == cls for e in listed)
        entry = [e for e in listed if e["class_name"] == cls][0]
        assert entry["description"] == "my desc"
        # Source loadable via load_source
        loaded = store.load_source(cls)
        assert loaded is not None
        assert "MyTestStrategy" in loaded

    def test_import_collision_without_overwrite_raises(self, store, tmp_path):
        src = _write(tmp_path, "my_test.py", VALID_STRATEGY)
        store.import_external_file(src, description="v1")

        with pytest.raises(FileExistsError, match="already exists"):
            store.import_external_file(src, description="v2", overwrite=False)

    def test_import_collision_with_overwrite_replaces(self, store, tmp_path):
        src = _write(tmp_path, "my_test.py", VALID_STRATEGY)
        store.import_external_file(src, description="v1")

        cls, _ = store.import_external_file(src, description="v2", overwrite=True)

        listed = store.list_strategies()
        entries = [e for e in listed if e["class_name"] == cls]
        assert len(entries) == 1  # not duplicated
        assert entries[0]["description"] == "v2"

    def test_index_persists_to_disk(self, store, tmp_path):
        src = _write(tmp_path, "my_test.py", VALID_STRATEGY)
        store.import_external_file(src, description="persist test")

        # Re-instantiate store from same dir → index loads from disk
        store2 = StrategyStore(store.store_dir)
        listed = store2.list_strategies()
        assert any(e["class_name"] == "MyTestStrategy" for e in listed)

    def test_import_validation_failure_does_not_pollute_store(self, store, tmp_path):
        bad = "class NotAStrategy:\n    pass\n"
        src = _write(tmp_path, "bad.py", bad)

        with pytest.raises(ValueError):
            store.import_external_file(src)

        # No file created, no index entry added
        assert not os.listdir(store.store_dir) or os.listdir(store.store_dir) == ["index.json"]
        assert store.list_strategies() == []
