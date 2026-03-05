"""Tests for CsvLogger: bar file rotation, decision log, round-trip with data_loader."""

import os
import tempfile
from datetime import datetime

from src.market_data.models import Bar
from src.live.csv_logger import CsvLogger
from src.backtest.data_loader import load_bars_from_csv


def _bar(dt_str, o=100, h=110, l=90, c=105, v=50):
    dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
    return Bar(symbol="TX00", dt=dt, open=o, high=h, low=l, close=c,
               volume=v, interval=60)


class TestCsvLoggerBars:
    def test_write_and_roundtrip(self, tmp_path):
        logger = CsvLogger(str(tmp_path), "TX00")
        bars = [
            _bar("2026-03-01 09:00", 22500, 22510, 22495, 22505, 150),
            _bar("2026-03-01 09:01", 22505, 22520, 22500, 22515, 200),
        ]
        for b in bars:
            logger.log_bar(b)
        logger.close()

        csv_path = tmp_path / "bars_1m_20260301.csv"
        assert csv_path.exists()

        loaded = load_bars_from_csv(csv_path, symbol="TX00", interval=60)
        assert len(loaded) == 2
        assert loaded[0].open == 22500
        assert loaded[0].high == 22510
        assert loaded[1].close == 22515
        assert loaded[1].volume == 200

    def test_daily_rotation(self, tmp_path):
        logger = CsvLogger(str(tmp_path), "TX00")
        logger.log_bar(_bar("2026-03-01 13:44"))
        logger.log_bar(_bar("2026-03-02 09:00"))
        logger.close()

        assert (tmp_path / "bars_1m_20260301.csv").exists()
        assert (tmp_path / "bars_1m_20260302.csv").exists()

        bars_d1 = load_bars_from_csv(tmp_path / "bars_1m_20260301.csv")
        bars_d2 = load_bars_from_csv(tmp_path / "bars_1m_20260302.csv")
        assert len(bars_d1) == 1
        assert len(bars_d2) == 1

    def test_append_to_existing(self, tmp_path):
        """Appending to same-day file without duplicating headers."""
        logger1 = CsvLogger(str(tmp_path), "TX00")
        logger1.log_bar(_bar("2026-03-01 09:00"))
        logger1.close()

        logger2 = CsvLogger(str(tmp_path), "TX00")
        logger2.log_bar(_bar("2026-03-01 09:01"))
        logger2.close()

        loaded = load_bars_from_csv(tmp_path / "bars_1m_20260301.csv")
        assert len(loaded) == 2

    def test_creates_base_dir(self, tmp_path):
        nested = str(tmp_path / "sub" / "dir")
        logger = CsvLogger(nested, "TX00")
        logger.log_bar(_bar("2026-03-01 09:00"))
        logger.close()
        assert os.path.exists(os.path.join(nested, "bars_1m_20260301.csv"))

    def test_bot_name_creates_subdirectory(self, tmp_path):
        """bot_name creates a {symbol}_{bot_name} subdirectory."""
        logger = CsvLogger(str(tmp_path), "TX00", bot_name="MyBot")
        logger.log_bar(_bar("2026-03-01 09:00"))
        logger.close()

        bot_dir = tmp_path / "TX00_MyBot"
        assert bot_dir.is_dir()
        assert (bot_dir / "bars_1m_20260301.csv").exists()


class TestCsvLoggerDecisions:
    def test_decision_log(self, tmp_path):
        logger = CsvLogger(str(tmp_path), "TX00")
        logger.log_decision(
            dt=datetime(2026, 3, 1, 9, 32),
            bar_dt=datetime(2026, 3, 1, 8, 0),
            strategy="H4BollingerAtr",
            action="ENTRY",
            side="LONG",
            tag="BB_Long",
            price=22480,
            reason="lower band touch",
        )
        logger.log_decision(
            dt=datetime(2026, 3, 1, 13, 28),
            bar_dt=datetime(2026, 3, 1, 12, 0),
            strategy="H4BollingerAtr",
            action="TRADE_CLOSE",
            side="LONG",
            tag="TP_Hit",
            price=22650,
            reason="PnL=+34000",
        )
        logger.close()

        path = tmp_path / "decisions.csv"
        assert path.exists()

        import csv
        with open(path, "r", encoding="utf-8") as f:
            reader = list(csv.reader(f))
        assert len(reader) == 3  # header + 2 rows
        assert reader[0][0] == "datetime"
        assert reader[1][3] == "ENTRY"
        assert reader[2][3] == "TRADE_CLOSE"
        assert reader[2][6] == "22650"

    def test_decision_append(self, tmp_path):
        """Multiple logger instances append to the same decision file."""
        logger1 = CsvLogger(str(tmp_path), "TX00")
        logger1.log_decision(
            datetime(2026, 3, 1, 9, 0), datetime(2026, 3, 1, 8, 0),
            "Strat", "ENTRY", "LONG", "tag1", 100, "reason1",
        )
        logger1.close()

        logger2 = CsvLogger(str(tmp_path), "TX00")
        logger2.log_decision(
            datetime(2026, 3, 1, 10, 0), datetime(2026, 3, 1, 8, 0),
            "Strat", "EXIT", "LONG", "tag2", 110, "reason2",
        )
        logger2.close()

        import csv
        with open(tmp_path / "decisions.csv", "r", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        # Header written only by first logger; second appends to existing file
        assert len(rows) == 3  # 1 header + 2 data rows
