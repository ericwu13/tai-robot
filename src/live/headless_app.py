"""Headless driver for the workbench's live-bot and backtest machinery.

``HeadlessBotApp`` is ``BacktestApp`` with a withdrawn (never shown) Tk
root: every widget still exists, so the ~3k lines of live glue in
run_backtest.py — tick drain, watchdog, reconnect ladder, fill polling,
session-end close, regime poll — run byte-identical to the GUI. Only the
things that need a human are overridden:

- ``_confirm`` / ``_alert``: answered from a ``HeadlessPolicy`` / logged
- the semi-auto order-confirm dialog: refused at arg-parse time, and
  logged as skipped if anything reaches it anyway
- the GitHub update check: disabled
- ``_live_log_msg``: mirrored to stdout so the console shows the event
  stream the GUI's Live tab would

The drivers (``run_deploy`` / ``run_backtest``) sequence login → ready →
deploy → poll on ``root.after`` and report an exit code. The STOP file in
the bot directory (``run_bot_cli.py stop``) and Ctrl+C both trigger the
GUI's normal ``_stop_live`` (force-close, session save, daily report).
A ``CONTROL`` file in that directory is consumed once per poll:
``reconnect`` calls ``_manual_reconnect``; ``set-mode paper`` calls
``_apply_mode_switch``. ``auto`` and ``real`` are rejected.
"""

from __future__ import annotations

import os
import signal
import sys
import time
import tkinter as tk
from datetime import datetime

import run_backtest as rb
from src.backtest.report import export_trades_csv, format_report
from src.live.deploy_request import DeployRequest
from src.live.headless import (
    EXIT_BACKTEST_FAILED, EXIT_BOT_STOPPED, EXIT_DEPLOY_REFUSED, EXIT_LOGIN,
    EXIT_NO_COM, EXIT_OK, EXIT_TIMEOUT, EXIT_USAGE,
    HeadlessConfigError, HeadlessPolicy, apply_control_command,
    build_deploy_request, clear_stop_file, consume_control_file,
    control_fingerprint, policy_from_args, quarantine_stale_control,
    request_stop, resolve_strategy_name, stop_requested,
)
from src.live.live_runner import LiveRunner
from src.live.session_store import load_session


def _now_str() -> str:
    return rb._taipei_now().strftime("%Y-%m-%d %H:%M:%S")


class HeadlessBotApp(rb.BacktestApp):
    """BacktestApp on a withdrawn root — see module docstring."""

    def __init__(self, root: tk.Tk, policy: HeadlessPolicy | None = None):
        self.policy = policy or HeadlessPolicy()
        self.alerts: list[tuple[str, str]] = []
        self.backtest_result = None
        self.backtest_error = None
        self._headless_backtest_active = False
        root.withdraw()
        super().__init__(root)
        root.withdraw()  # __init__ zooms the window; hide it again

    # ── dialog seams ──

    def _confirm(self, title: str, message: str, **_kwargs) -> bool:
        # ``default_no`` is a GUI button default. Headless answers from
        # the policy flag for this title and ignores it.
        answer = self.policy.answer(title)
        rb._log(f"[HEADLESS] {title} -> {'YES' if answer else 'NO'}")
        return answer

    def _alert(self, title: str, message: str) -> None:
        self.alerts.append((title, message))
        rb._log(f"[HEADLESS ERROR] {title}: {message}")

    def _show_order_confirm_dialog(self, buy_sell, order_symbol, order_desc,
                                   price, action_type, price_source="",
                                   new_close=2):
        # Only reachable via semi_auto (refused by the CLI) or the manual
        # order buttons (no GUI). Never send an unconfirmed order.
        self._live_log_msg(
            f"實單跳過 Order skipped (headless: no confirmation dialog) "
            f"{order_desc} {order_symbol}", "status")
        self._log_order_decision("REAL_ORDER_SKIPPED", "headless: no confirm dialog")

    # ── quiet startup ──

    def _check_for_updates_bg(self):
        return  # no update banner without a window

    # ── console mirror of the Live tab ──

    def _live_log_msg(self, msg, tag="status"):
        super()._live_log_msg(msg, tag)
        try:
            print(f"[{_now_str()}] [LIVE] {msg}", flush=True)
        except Exception:
            pass

    # ── backtest completion hooks ──

    def _on_backtest_done(self, result, bars, initial_balance):
        self.backtest_result = (result, bars, initial_balance)
        super()._on_backtest_done(result, bars, initial_balance)

    def _on_backtest_error(self, error, tb):
        self.backtest_error = (error, tb)
        super()._on_backtest_error(error, tb)

    def _enable_buttons(self):
        super()._enable_buttons()
        # Every backtest/fetch path — success or failure — re-enables the
        # buttons exactly once at its terminal point, so this is the
        # headless "backtest finished" signal.
        if self._headless_backtest_active:
            self._headless_backtest_active = False
            self.root.after(0, self.root.quit)


# ── shared driver plumbing ──

class _Driver:
    def __init__(self, app: HeadlessBotApp):
        self.app = app
        self.root = app.root
        self.exit_code: int | None = None
        self._stop_flag = False
        for sig_name in ("SIGINT", "SIGBREAK", "SIGTERM"):
            sig = getattr(signal, sig_name, None)
            if sig is not None:
                try:
                    signal.signal(sig, self._on_signal)
                except (ValueError, OSError):
                    pass

    def _on_signal(self, signum, frame):
        self._stop_flag = True

    def finish(self, code: int, message: str = "") -> None:
        if self.exit_code is not None:
            return
        self.exit_code = code
        if message:
            rb._log(f"[HEADLESS] {message} (exit {code})")
        self.root.after(0, self.root.quit)

    # login → Ready(3003) ────────────────────────────────────────────

    def login(self, timeout_s: int, then) -> None:
        app = self.app
        user = os.environ.get("TAI_USER_ID") or app._settings.get("user_id", "")
        pwd = os.environ.get("TAI_PASSWORD") or app._settings.get("password", "")
        if not user or not pwd:
            self.finish(EXIT_LOGIN, "no credentials (settings.yaml credentials.* "
                                    "or TAI_USER_ID/TAI_PASSWORD)")
            return
        app.login_user_var.set(user)
        app.login_pass_var.set(pwd)
        app._do_login(user, pwd)
        if not app._logged_in:
            self.finish(EXIT_LOGIN, f"login failed: {app.login_status_var.get()}")
            return
        deadline = time.monotonic() + timeout_s

        def _wait():
            if self.exit_code is not None:
                return
            if self._stop_flag:
                self.finish(EXIT_OK, "interrupted while connecting")
                return
            if app._quote_connected:
                rb._log("[HEADLESS] quote connection ready")
                then()
                return
            if time.monotonic() > deadline:
                self.finish(EXIT_LOGIN, f"quote connection not ready after {timeout_s}s")
                return
            self.root.after(500, _wait)

        self.root.after(500, _wait)


class DeployDriver(_Driver):
    ACCOUNT_TIMEOUT_S = 20
    POLL_MS = 2000

    def __init__(self, app: HeadlessBotApp, args):
        super().__init__(app)
        self.args = args
        self.req: DeployRequest | None = None
        self.bot_dir = ""
        self._last_heartbeat = time.monotonic()
        self._stop_via_us = False
        self._ignored_control: tuple | None = None
        self._ignored_control_logged = False

    def start(self) -> None:
        app, args = self.app, self.args
        base_dir = os.path.join(rb.project_root, "data", "live")
        self.bot_dir = LiveRunner.bot_dir_for(base_dir, args.symbol, args.bot)
        session = load_session(os.path.join(self.bot_dir, "session.json"))
        if args.resume and session is None:
            self.finish(EXIT_USAGE, f"--resume but no session.json in {self.bot_dir}")
            return
        if args.new and session is not None:
            self.finish(EXIT_USAGE, f"--new but {self.bot_dir} already has a session.json "
                                    "(drop --new to resume it)")
            return
        try:
            self.req = build_deploy_request(args, session)
            if args.strategy:
                app.strategy_var.set(resolve_strategy_name(args.strategy, rb.STRATEGIES))
            if args.regime and args.long_strategy:
                self.req.regime_long = resolve_strategy_name(args.long_strategy, rb.STRATEGIES)
            if args.regime and args.short_strategy:
                self.req.regime_short = resolve_strategy_name(args.short_strategy, rb.STRATEGIES)
        except HeadlessConfigError as e:
            self.finish(EXIT_USAGE, str(e))
            return
        if args.symbol not in list(app.symbol_combo["values"]):
            self.finish(EXIT_USAGE, f"unknown symbol {args.symbol!r}; "
                                    f"choose from {list(app.symbol_combo['values'])}")
            return
        app.symbol_var.set(args.symbol)
        app._on_symbol_changed()
        if args.point_value:
            app.pv_var.set(str(args.point_value))
        rb._log(f"[HEADLESS] deploy request: symbol={args.symbol} bot={args.bot} "
                f"mode={self.req.trading_mode} strategy={app.strategy_var.get()!r} "
                f"resume={'yes' if session else 'no'} regime={self.req.regime_enabled}")
        self.login(args.login_timeout, self._wait_account)

    def _wait_account(self) -> None:
        if self.req.trading_mode == "paper":
            self._deploy()
            return
        deadline = time.monotonic() + self.ACCOUNT_TIMEOUT_S

        def _wait():
            if self.exit_code is not None:
                return
            if self.app._futures_account:
                self._deploy()
            elif time.monotonic() > deadline:
                self.finish(EXIT_LOGIN, "futures account not received — refusing "
                                        "to deploy a real-order bot without one")
            else:
                self.root.after(500, _wait)

        _wait()

    def _deploy(self) -> None:
        clear_stop_file(self.bot_dir)  # a stale STOP would stop us instantly
        # Quarantine before the runner starts. A failed rename is an
        # OSError inside quarantine_stale_control; it must not escape
        # after the bot is already trading.
        self._note_stale_control()
        try:
            ok = self.app._deploy_live_from(self.req)
            runner = getattr(self.app, "_live_runner", None)
            if not ok or runner is None:
                why = "; ".join(f"{t}: {m.splitlines()[0]}" for t, m in self.app.alerts) \
                    or self.app.status_var.get()
                self.finish(EXIT_DEPLOY_REFUSED, f"deploy refused: {why}")
            else:
                rb._log(f"[HEADLESS] deployed — polling STOP file {self.bot_dir}\\STOP, "
                        f"CONTROL, and Ctrl+C every 2s")
        finally:
            # Once a runner exists, the poll is scheduled even if the
            # deploy log raised. A refused deploy sets exit_code and
            # does not schedule another turn.
            runner = getattr(self.app, "_live_runner", None)
            if runner is not None and getattr(self, "exit_code", None) is None:
                self.root.after(self.POLL_MS, self._poll)

    def _note_stale_control(self) -> None:
        status, detail = quarantine_stale_control(self.bot_dir)
        self._ignored_control = detail if status == "ignored" else None
        self._ignored_control_logged = False
        if status == "archived":
            rb._log(f"[HEADLESS] archived stale CONTROL to {detail}")
        elif status == "deleted":
            rb._log("[HEADLESS] warning: could not archive stale CONTROL; deleted it")
        elif status == "ignored":
            rb._log("[HEADLESS] warning: could not archive or delete stale CONTROL; "
                    "ignoring it until the file changes")

    def _poll(self) -> None:
        if self.exit_code is not None:
            return
        try:
            self._poll_once()
        except Exception as e:
            rb._log(f"[HEADLESS] poll failed: [{type(e).__name__}] {e}")
        finally:
            # A CONTROL decode error or a raising reconnect must not be
            # the last poll. Stop and bot-stopped set exit_code first,
            # so they do not schedule another turn.
            if self.exit_code is None:
                self.root.after(self.POLL_MS, self._poll)

    def _poll_once(self) -> None:
        app = self.app
        if self._stop_flag or stop_requested(self.bot_dir):
            why = "signal" if self._stop_flag else "STOP file"
            rb._log(f"[HEADLESS] stop requested via {why} — stopping bot")
            try:
                app._stop_live()
            except Exception as e:
                # Leave STOP in place (recreate it after a signal-only
                # stop) so the next poll retries. Do not finish EXIT_OK
                # while the bot may still be running.
                rb._log(f"[HEADLESS] stop failed: [{type(e).__name__}] {e}; "
                        f"STOP stays so the next poll retries")
                if not stop_requested(self.bot_dir):
                    try:
                        request_stop(self.bot_dir, "retry after stop failed")
                    except OSError as e2:
                        rb._log(f"[HEADLESS] could not recreate STOP: "
                                f"[{type(e2).__name__}] {e2}")
                return
            self._stop_via_us = True
            clear_stop_file(self.bot_dir)
            self.finish(EXIT_OK, "bot stopped")
            return
        if app._live_runner is None:
            self.finish(EXIT_BOT_STOPPED, "bot stopped on its own — see the debug log")
            return
        try:
            self._poll_control()
        except Exception as e:
            rb._log(f"[HEADLESS] CONTROL failed: [{type(e).__name__}] {e}")
        hb = self.args.heartbeat_min
        if hb and time.monotonic() - self._last_heartbeat >= hb * 60:
            self._last_heartbeat = time.monotonic()
            try:
                st = app._live_runner.get_status()
                link = ""
                host = getattr(app, "_link_host", None)
                if host is not None:
                    snap = host.snapshot()
                    link = (
                        f" link={snap['quote']}/{snap['reply']}"
                        f" degraded={snap['degraded']}"
                        f" entries_blocked={snap['entries_blocked']}"
                    )
                rb._log(f"[HEADLESS] heartbeat: state={st['state']} pos={st['position']} "
                        f"trades={st['trades']} pnl={st['pnl']:+,} "
                        f"bars={st['bars_1m']}/{st['bars_agg']} "
                        f"market={'open' if st['market_open'] else 'closed'} "
                        f"mode={app._trading_mode} connected={app._quote_connected}"
                        f"{link}")
            except Exception as e:
                rb._log(f"[HEADLESS] heartbeat failed: {e}")

    def _poll_control(self) -> None:
        """Apply ``CONTROL`` once. ``auto`` and ``real`` are rejected."""
        if self._stale_control_still_ignored():
            return
        text = consume_control_file(self.bot_dir)
        if text is None:
            return
        runner = self.app._live_runner

        def set_mode(mode: str) -> None:
            if runner is None:
                rb._log("[HEADLESS] CONTROL set-mode ignored — no runner")
                return
            runner._apply_mode_switch(mode)

        apply_control_command(
            text,
            reconnect=self.app._manual_reconnect,
            set_mode=set_mode,
            log=lambda msg: rb._log(f"[HEADLESS] {msg}"),
        )

    def _stale_control_still_ignored(self) -> bool:
        """Skip a CONTROL file whose archive and delete both failed.

        A matching fingerprint is the same file and is never applied.
        A missing file, or a different size or mtime, clears the ignore
        so a command written after deploy can run.
        """
        ignored = getattr(self, "_ignored_control", None)
        if ignored is None:
            return False
        current = control_fingerprint(self.bot_dir)
        if current == ignored and current != ("missing",):
            if not getattr(self, "_ignored_control_logged", False):
                rb._log("[HEADLESS] warning: stale CONTROL still present; "
                        "not applying it")
                self._ignored_control_logged = True
            return True
        self._ignored_control = None
        self._ignored_control_logged = False
        return False


class BacktestDriver(_Driver):
    def __init__(self, app: HeadlessBotApp, args):
        super().__init__(app)
        self.args = args

    def start(self) -> None:
        app, args = self.app, self.args
        try:
            app.strategy_var.set(resolve_strategy_name(args.strategy, rb.STRATEGIES))
        except HeadlessConfigError as e:
            self.finish(EXIT_USAGE, str(e))
            return
        if args.symbol not in list(app.symbol_combo["values"]):
            self.finish(EXIT_USAGE, f"unknown symbol {args.symbol!r}")
            return
        app.symbol_var.set(args.symbol)
        app._on_symbol_changed()
        app.start_var.set(args.start)
        app.end_var.set(args.end)
        if args.point_value:
            app.pv_var.set(str(args.point_value))
        if args.balance:
            app.balance_var.set(str(args.balance))
        app._headless_backtest_active = True
        self.root.after(args.timeout * 1000, self._timeout)
        if args.source == "tv":
            if not rb._tv_available:
                self.finish(EXIT_USAGE, "tvDatafeed not installed — use --source api")
                return
            self.root.after(0, app._do_fetch_tv)
        else:
            self.login(args.login_timeout, lambda: self.root.after(0, app._do_fetch_api))

    def _timeout(self) -> None:
        if self.exit_code is None and self.app.backtest_result is None:
            self.finish(EXIT_TIMEOUT, f"backtest did not finish within {self.args.timeout}s")

    def report(self) -> int:
        app = self.app
        if self.exit_code not in (None, EXIT_OK):
            return self.exit_code
        if app.backtest_error is not None:
            err, tb = app.backtest_error
            print(tb, file=sys.stderr)
            return EXIT_BACKTEST_FAILED
        if app.backtest_result is None:
            print(f"backtest produced no result: {app.status_var.get()}", file=sys.stderr)
            return EXIT_BACKTEST_FAILED
        result, bars, _balance = app.backtest_result
        print()
        print(format_report(app.strategy_var.get(), result.metrics, result.trades))
        if bars:
            print(f"bars: {len(bars)}  {bars[0].dt} ~ {bars[-1].dt}  source: {app._data_source}")
        if self.args.export:
            export_trades_csv(result.trades, self.args.export)
            print(f"trades exported: {self.args.export}")
        return EXIT_OK


# ── entry points used by run_bot_cli.py ──

def _boot(policy: HeadlessPolicy, need_com: bool) -> tuple[HeadlessBotApp | None, int]:
    rb._init_com()
    if need_com and not rb._com_available:
        print("Capital API COM not available (SDK libs missing or not registered)",
              file=sys.stderr)
        return None, EXIT_NO_COM
    rb.register_com_events()
    root = tk.Tk()
    app = HeadlessBotApp(root, policy)
    return app, EXIT_OK


def run_deploy(args) -> int:
    app, code = _boot(policy_from_args(args), need_com=True)
    if app is None:
        return code
    driver = DeployDriver(app, args)
    driver.start()
    if driver.exit_code is None:
        app.root.mainloop()
    _teardown(app)
    return driver.exit_code if driver.exit_code is not None else EXIT_BOT_STOPPED


def run_backtest(args) -> int:
    app, code = _boot(HeadlessPolicy(accept_data_range=True), need_com=(args.source == "api"))
    if app is None:
        return code
    driver = BacktestDriver(app, args)
    driver.start()
    if driver.exit_code is None:
        app.root.mainloop()
    code = driver.report()
    _teardown(app)
    return code


def _teardown(app: HeadlessBotApp) -> None:
    try:
        app._shutdown_link()
    except Exception:
        pass
    try:
        app.root.destroy()
    except Exception:
        pass
