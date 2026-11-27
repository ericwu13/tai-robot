"""TAIFEX margin sync for the real-order entry gate (src/live/margin_table.py)."""

from __future__ import annotations

import dataclasses
import json
import os

import pytest

from src.live import margin_table as mt
from src.market_data.kline_config import SYMBOL_CONFIG

# Rows as TAIFEX served them on 2026/08/12 (indexMargingDown), plus the
# 客製化 row that shares the 小型臺指 text.
ROWS_0812 = [
    "臺股期貨,519000,538000,701000,",
    "小型臺指,129750,134500,175250,",
    "客製化小型臺指期貨,129750,134500,175250,",
    "微型臺指期貨,25950,26900,35050,",
    "臺指選擇權風險保證金(A)值,138000,143000,187000,",
    "電子期貨,724000,750000,978000,",
    "小型電子期貨,90500,93750,122250,",
]


def _csv(date="2026/08/12", rows=ROWS_0812,
         header="商品別,結算保證金,維持保證金,原始保證金,,", sep="\r\n"):
    return sep.join([f"更新日期:{date}", header, *rows, ""])


def _table(published="2026/12/01", tx=(550_000, 570_000, 740_000),
           fetched_at=1_000_000.0):
    s, m, i = tx
    return mt.MarginTable(
        published=published,
        margins={
            "TX": mt.Margin(s, m, i),
            "MTX": mt.Margin(s // 4, m // 4, i // 4),
            "TMF": mt.Margin(s // 20, m // 20, i // 20),
        },
        source="taifex", fetched_at=fetched_at)


@pytest.fixture(autouse=True)
def _clear_memo():
    mt._MEMO.clear()
    yield
    mt._MEMO.clear()


# ── built-in table ──

def test_builtin_is_valid_and_matches_symbol_config():
    assert mt.validate(mt.BUILTIN) == ""
    for cfg in SYMBOL_CONFIG.values():
        assert cfg["init_margin"] == mt.BUILTIN.margins[cfg["taifex_id"]].initial


# ── parsing ──

def test_parse_0812_download():
    table, reason = mt.parse_margin_text(_csv())
    assert reason == ""
    assert table.published == "2026/08/12"
    assert table.margins == mt.BUILTIN.margins
    assert table.source == "taifex"


@pytest.mark.parametrize("sep", ["\n", "\r", " "])
def test_parse_does_not_depend_on_line_breaks(sep):
    table, reason = mt.parse_margin_text(_csv(sep=sep))
    assert reason == ""
    assert table.margins["TMF"].initial == 35_050


def test_custom_mtx_row_is_not_taken_for_mtx():
    rows = ["客製化小型臺指期貨,1,2,3,"] + [r for r in ROWS_0812
                                         if not r.startswith("客製化")]
    table, reason = mt.parse_margin_text(_csv(rows=rows))
    assert reason == ""
    assert table.margins["MTX"].initial == 175_250


def test_columns_are_mapped_by_header_name():
    rows = ["臺股期貨,701000,519000,538000,", "小型臺指,175250,129750,134500,",
            "微型臺指期貨,35050,25950,26900,"]
    table, reason = mt.parse_margin_text(_csv(
        rows=rows, header="商品別,原始保證金,結算保證金,維持保證金,,"))
    assert reason == ""
    assert table.margins == mt.BUILTIN.margins


@pytest.mark.parametrize("text, why", [
    ("", "empty"),
    ("<!DOCTYPE html><html>error</html>", "HTML"),
    (_csv().replace("更新日期:2026/08/12", ""), "更新日期"),
    (_csv(date="2026/13/40"), "更新日期"),
    (_csv(header="商品別,A,B,C,,"), "columns"),
    (_csv(rows=[r for r in ROWS_0812 if not r.startswith("微型")]), "微型臺指期貨: 0"),
    (_csv(rows=ROWS_0812 + ["微型臺指期貨,25950,26900,35050,"]), "微型臺指期貨: 2"),
])
def test_parse_rejects(text, why):
    table, reason = mt.parse_margin_text(text)
    assert table is None
    assert why in reason


def test_rejects_tmf_off_the_tx_ratio():
    # Ordered and plausible, but TX/40 instead of TX/20.
    rows = [r.replace("25950,26900,35050", "12975,13450,17525")
            for r in ROWS_0812]
    table, reason = mt.parse_margin_text(_csv(rows=rows))
    assert table is None and "TMF 原始保證金 17,525 is not TX×0.05" in reason


def test_rejects_swapped_order_of_margins():
    rows = [r.replace("25950,26900", "26900,25950") for r in ROWS_0812]
    table, reason = mt.parse_margin_text(_csv(rows=rows))
    assert table is None and "原始 > 維持 > 結算" in reason


def test_decode_cp950_and_utf8_bom():
    text = _csv()
    assert mt.decode_download(text.encode("cp950")) == text
    assert mt.decode_download(b"\xef\xbb\xbf" + text.encode("utf-8")) == text
    assert mt.decode_download(b"") is None


# ── cache and resolution ──

def test_no_cache_uses_builtin(tmp_path):
    path = str(tmp_path / "taifex_margin.json")
    assert mt.current_table(path) is mt.BUILTIN
    assert mt.initial_margin("TMF", path=path) == (35_050, "內建 2026/08/12")


def test_newer_cache_wins_older_cache_loses(tmp_path):
    path = str(tmp_path / "taifex_margin.json")
    assert mt.write_cache(path, _table("2026/12/01"))
    assert mt.initial_margin("TMF", path=path) == (37_000, "期交所 2026/12/01")
    assert mt.write_cache(path, _table("2026/05/27", (428_000, 443_000, 578_000)))
    assert mt.initial_margin("TMF", path=path)[0] == 35_050


def test_same_date_cache_is_used(tmp_path):
    path = str(tmp_path / "taifex_margin.json")
    table, _ = mt.parse_margin_text(_csv())
    assert mt.write_cache(path, dataclasses.replace(table, fetched_at=1.0))
    assert mt.current_table(path).source == "taifex"


@pytest.mark.parametrize("mutate", [
    lambda d: d.update(version=99),
    lambda d: d.update(fetched_at="yesterday"),
    lambda d: d["margins"]["TMF"].update(initial=16_100),
    lambda d: d["margins"].pop("MTX"),
    lambda d: d.update(published="2026-12-01"),
])
def test_bad_cache_falls_back_to_builtin(tmp_path, mutate):
    path = str(tmp_path / "taifex_margin.json")
    data = mt.table_to_json(_table("2026/12/01"))
    mutate(data)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    assert mt.current_table(path) is mt.BUILTIN


def test_garbage_cache_falls_back(tmp_path):
    path = tmp_path / "taifex_margin.json"
    path.write_bytes(b"{not json")
    assert mt.initial_margin("TX", path=str(path)) == (701_000, "內建 2026/08/12")


def test_unknown_product_uses_fallback(tmp_path):
    assert mt.initial_margin("ZZZ", path=str(tmp_path / "x.json"),
                             fallback=12_345) == (12_345, "SYMBOL_CONFIG")


def test_initial_margin_never_raises(monkeypatch):
    def boom(_path):
        raise RuntimeError("disk on fire")
    monkeypatch.setattr(mt, "load_cache", boom)
    assert mt.initial_margin("TMF", path="whatever")[0] == 35_050


def test_cache_round_trip_keeps_fetch_time(tmp_path):
    path = str(tmp_path / "taifex_margin.json")
    table = _table(fetched_at=1_764_000_000.5)
    assert mt.write_cache(path, table)
    assert mt.load_cache(path) == table
    assert not [p for p in os.listdir(tmp_path) if p.endswith(".tmp")]


def test_write_cache_retries_while_target_is_held(tmp_path, monkeypatch):
    path = str(tmp_path / "taifex_margin.json")
    calls = []
    real_replace = os.replace

    def flaky(src, dst):
        calls.append(dst)
        if len(calls) < 3:
            raise PermissionError(32, "sharing violation")
        real_replace(src, dst)
    monkeypatch.setattr(mt.os, "replace", flaky)
    assert mt.write_cache(path, _table(), sleep=lambda _s: None)
    assert len(calls) == 3


def test_write_cache_gives_up_and_cleans_temp(tmp_path, monkeypatch):
    path = str(tmp_path / "taifex_margin.json")

    def always(src, dst):
        raise PermissionError(32, "sharing violation")
    monkeypatch.setattr(mt.os, "replace", always)
    assert not mt.write_cache(path, _table(), sleep=lambda _s: None)
    assert os.listdir(tmp_path) == []


# ── download ──

class _Resp:
    def __init__(self, body: bytes):
        self._body = body

    def read(self, n=-1):
        return self._body if n < 0 else self._body[:n]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_fetch_parses_cp950_and_stamps_time():
    seen = {}

    def opener(req, timeout):
        seen["url"], seen["timeout"] = req.full_url, timeout
        return _Resp(_csv().encode("cp950"))
    table, reason = mt.fetch_table(opener=opener, clock=lambda: 123.0)
    assert reason == "" and table.fetched_at == 123.0
    assert seen == {"url": mt.MARGIN_URL, "timeout": mt.FETCH_TIMEOUT_S}


def test_fetch_failure_is_a_reason_not_a_raise():
    def opener(req, timeout):
        raise OSError("connection reset")
    table, reason = mt.fetch_table(opener=opener)
    assert table is None and "connection reset" in reason


def test_fetch_rejects_oversized_download():
    def opener(req, timeout):
        return _Resp(b"x" * (mt.MAX_DOWNLOAD_BYTES + 10))
    table, reason = mt.fetch_table(opener=opener)
    assert table is None and "larger" in reason


# ── MarginSync ──

class _Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def _sync(tmp_path, results, clock, notes, logs):
    calls = []

    def fetch():
        calls.append(clock())
        table = results.pop(0)
        if isinstance(table, str):
            return None, table
        return dataclasses.replace(table, fetched_at=clock()), ""
    sync = mt.MarginSync(
        str(tmp_path / "taifex_margin.json"), fetch=fetch,
        notify=notes.append, log=logs.append, clock=clock,
        start_thread=lambda target: target())   # run inline
    return sync, calls


def test_sync_fetches_when_missing_then_waits_for_refresh(tmp_path):
    clock, notes, logs = _Clock(), [], []
    same, _ = mt.parse_margin_text(_csv())
    sync, calls = _sync(tmp_path, [same, same], clock, notes, logs)
    assert sync.poll() is True
    assert mt.current_table(sync.cache_path).source == "taifex"
    assert notes == []                      # same values as built-in
    clock.t += mt.RETRY_S + 1
    assert sync.poll() is False             # cache still fresh
    clock.t += mt.REFRESH_S
    assert sync.poll() is True
    assert len(calls) == 2


def test_sync_notifies_once_on_change(tmp_path):
    clock, notes, logs = _Clock(), [], []
    sync, _ = _sync(tmp_path, [_table("2026/12/01")], clock, notes, logs)
    assert sync.poll() is True
    assert len(notes) == 1
    assert "微台 TMF：原始 35,050 → 37,000" in notes[0]
    assert mt.initial_margin("TMF", path=sync.cache_path)[0] == 37_000


def test_sync_failure_keeps_table_and_backs_off(tmp_path):
    clock, notes, logs = _Clock(), [], []
    sync, calls = _sync(tmp_path, ["download failed: timeout",
                                   _table("2026/12/01")], clock, notes, logs)
    assert sync.poll() is True
    assert mt.current_table(sync.cache_path) is mt.BUILTIN
    assert any("not used" in line for line in logs)
    clock.t += mt.RETRY_S - 1
    assert sync.poll() is False             # backoff
    clock.t += 2
    assert sync.poll() is True
    assert len(calls) == 2 and len(notes) == 1


def test_sync_skips_while_another_process_holds_the_lock(tmp_path):
    clock, notes, logs = _Clock(), [], []
    sync, calls = _sync(tmp_path, [_table()], clock, notes, logs)
    fd = mt._open_lock_fd(sync.cache_path + ".lock")
    try:
        assert mt._lock_nb(fd)
        assert sync.poll() is True          # thread started …
        assert calls == []                  # … and saw the lock held
    finally:
        mt._unlock_fd(fd)
        os.close(fd)


def test_sync_rechecks_freshness_under_the_lock(tmp_path):
    clock, notes, logs = _Clock(), [], []
    sync, calls = _sync(tmp_path, [_table()], clock, notes, logs)
    mt.write_cache(sync.cache_path, _table(fetched_at=clock()))
    sync._refresh_under_lock()
    assert calls == []


def test_poll_does_not_start_a_second_thread(tmp_path):
    started = []
    sync = mt.MarginSync(str(tmp_path / "m.json"), fetch=lambda: (None, "x"),
                         clock=_Clock(), start_thread=started.append,
                         retry_s=0)
    assert sync.poll() is True
    assert sync.poll() is False             # first one still running
    assert len(started) == 1


def test_notify_error_is_logged_not_raised(tmp_path):
    clock, logs = _Clock(), []

    def bad_notify(_msg):
        raise RuntimeError("discord down")
    sync = mt.MarginSync(
        str(tmp_path / "m.json"),
        fetch=lambda: (dataclasses.replace(_table(), fetched_at=clock()), ""),
        notify=bad_notify, log=logs.append, clock=clock,
        start_thread=lambda target: target())
    assert sync.poll() is True
    assert any("notify failed" in line for line in logs)


def test_failed_attempt_backs_off_other_bots_on_the_host(tmp_path):
    clock, notes, logs = _Clock(), [], []
    first, calls1 = _sync(tmp_path, ["got an HTML page, not the CSV"],
                          clock, notes, logs)
    second, calls2 = _sync(tmp_path, [_table()], clock, notes, logs)
    assert first.poll() is True and len(calls1) == 1
    clock.t += 60
    assert second.poll() is True            # its own first try …
    assert calls2 == []                     # … sees the host stamp and waits
    clock.t += mt.RETRY_S
    assert second.poll() is True
    assert len(calls2) == 1
    assert mt.current_table(second.cache_path).published == "2026/12/01"
