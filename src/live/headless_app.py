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
from src.live.degraded_restart import (
    AlertOnly, DegradedRestartPolicy, RestartDecision, save_auto_restart,
)
from src.live.headless import (
    EXIT_BACKTEST_FAILED, EXIT_BOT_STOPPED, EXIT_DEPLOY_REFUSED, EXIT_LOGIN,
    EXIT_NO_COM, EXIT_OK, EXIT_TIMEOUT, EXIT_USAGE,
    HeadlessConfigError, HeadlessPolicy, apply_control_command,
    build_deploy_request, clear_stop_file, cli_stdout_path, consume_control_file,
    control_fingerprint, policy_from_args, quarantine_stale_control,
    remove_headless_marker_if_ours, request_stop, resolve_strategy_name,
    restart_command, spawn_detached, stop_requested, write_headless_marker,
)
from src.live.live_runner import LiveRunner, arm_headless_file_guard
from src.live import mode_request as mreq
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

    def _drop_stale_headless_marker(self, bot_dir: str) -> None:
        # The GUI hook deletes a crashed run's marker. This process writes
        # its own after the lock is held, and must not drop one that is
        # already there before that write.
        return

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

    # ── display-only work nobody can see (#285) ──

    def _render_report_view(self, title, metrics, trades, regime_info,
                            header_lines, message=None,
                            real_panel=True) -> None:
        # The root is withdrawn for the life of the process, so the Report
        # tab is never on screen. Rebuilding it destroys and recreates ~70
        # ttk widgets on the thread that drains COM ticks and runs tick
        # exits, and it was the sampled frame in 3 of 4 #285 hang dumps.
        # The trade tree and every runner/broker path are unchanged; only
        # the invisible card rebuild is skipped.
        return

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
        self._pending_restart_cmd: list[str] | None = None
        self._restart_policy = DegradedRestartPolicy()

    def start(self) -> None:
        app, args = self.app, self.args
        base_dir = os.path.join(rb.project_root, "data", "live")
        self.bot_dir = LiveRunner.bot_dir_for(base_dir, args.symbol, args.bot)
        # The marker is written in _deploy, after acquire_lock. Writing it
        # here would let a refused second instance replace, then delete,
        # the running bot's marker.
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
        # A mode request left from a previous run must not apply to this one.
        mreq.clear_stale_request(self.bot_dir)
        try:
            ok = self.app._deploy_live_from(self.req)
            runner = getattr(self.app, "_live_runner", None)
            if runner is not None:
                # This process is the headless driver. Arm even when the
                # deploy request was tagged gui — a file drop must not
                # escalate it. deploy --mode auto already set trading_mode.
                # Guarded setattr: a bare object() fake must not raise.
                arm_headless_file_guard(runner)
            if not ok or runner is None:
                why = "; ".join(f"{t}: {m.splitlines()[0]}" for t, m in self.app.alerts) \
                    or self.app.status_var.get()
                self.finish(EXIT_DEPLOY_REFUSED, f"deploy refused: {why}")
            else:
                # acquire_lock has run. OSError is logged inside the helper.
                # mode_request lives on the marker. A separate
                # capabilities.json is not written.
                write_headless_marker(self.bot_dir, mode_request=True)
                rb._log(f"[HEADLESS] deployed — polling STOP file {self.bot_dir}\\STOP, "
                        f"CONTROL, and Ctrl+C every 2s")
        finally:
            # Once a runner exists, the poll is scheduled even if the
            # deploy log raised. A refused deploy sets exit_code and
            # does not schedule another turn. Arm again here so a runner
            # created before that raise is still guarded.
            runner = getattr(self.app, "_live_runner", None)
            if runner is not None:
                arm_headless_file_guard(runner)
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
            self._maybe_degraded_restart()
        except Exception as e:
            rb._log(f"[HEADLESS] degraded-restart check failed: "
                    f"[{type(e).__name__}] {e}")
        if self.exit_code is not None:
            return
        try:
            app._service_mode_request(self.bot_dir)
        except Exception as e:
            rb._log(f"[HEADLESS] mode request failed: [{type(e).__name__}] {e}")
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
                guard = getattr(app, "_trading_guard", None)
                # entries_blocked on the link snapshot is the LINK gate only.
                # A daily-loss-limit pause also blocks entries, and reading
                # entries_blocked=False beside a blocked bot is how the
                # 2026-11-04 pause went unnoticed for a day. Report the
                # composite, and name the gate that is actually holding.
                paused = bool(getattr(guard, "paused", False))
                if host is not None:
                    snap = host.snapshot()
                    gates = []
                    if snap["entries_blocked"]:
                        gates.append("link")
                    if paused:
                        gates.append("loss_limit")
                    link = (
                        f" link={snap['quote']}/{snap['reply']}"
                        f" degraded={snap['degraded']}"
                        f" entries_blocked={bool(gates)}"
                        f"{'(' + '+'.join(gates) + ')' if gates else ''}"
                    )
                elif paused:
                    link = " entries_blocked=True(loss_limit)"
                rb._log(f"[HEADLESS] heartbeat: state={st['state']} pos={st['position']} "
                        f"trades={st['trades']} pnl={st['pnl']:+,} "
                        f"bars={st['bars_1m']}/{st['bars_agg']} "
                        f"market={'open' if st['market_open'] else 'closed'} "
                        f"mode={app._trading_mode} connected={app._quote_connected}"
                        f"{link}")
            except Exception as e:
                rb._log(f"[HEADLESS] heartbeat failed: {e}")

    def _restart_policy_or_new(self) -> DegradedRestartPolicy:
        policy = getattr(self, "_restart_policy", None)
        if policy is None:
            policy = DegradedRestartPolicy()
            self._restart_policy = policy
        return policy

    def _maybe_degraded_restart(self) -> None:
        """One keep-position restart per open session, or one alert."""
        if self.exit_code is not None:
            return
        app = self.app
        if getattr(app, "_live_runner", None) is None:
            return
        outcome = self._restart_policy_or_new().observe_app(
            app, bot_dir=self.bot_dir)
        if isinstance(outcome, RestartDecision):
            self._begin_degraded_restart(outcome)
        elif isinstance(outcome, AlertOnly):
            self._alert_restart_skipped(outcome)

    def _begin_degraded_restart(self, decision: RestartDecision) -> None:
        app = self.app
        runner = app._live_runner
        mode = str(getattr(app, "_trading_mode", "") or "")
        keep = (
            mode == "auto"
            and bool(getattr(app._trading_guard, "real_entry_confirmed", False))
            and int(getattr(runner.broker, "position_size", 0) or 0) > 0
        )
        raw = list(getattr(self.args, "_raw_argv", None) or [])
        if not raw:
            raw = ["deploy", "--symbol", self.args.symbol, "--bot", self.args.bot,
                   "--mode", mode]
        from src.paths import project_root
        cmd = restart_command(
            raw,
            frozen=bool(getattr(sys, "frozen", False)),
            executable=sys.executable,
            script=os.path.join(project_root(), "run_bot_cli.py"),
            trading_mode=mode,
            old_pid=os.getpid(),
            keep_confirmed_real_position=keep,
        )
        save_auto_restart(self.bot_dir, decision.session_key, old_pid=os.getpid())
        self._notify_restart_starting(decision, keep)
        app._stop_live(keep_position=True)
        self._pending_restart_cmd = cmd
        self.finish(EXIT_OK, "degraded auto-restart")

    def _position_kept_text(self, keep: bool) -> str:
        runner = self.app._live_runner
        broker = getattr(runner, "broker", None)
        try:
            size = int(getattr(broker, "position_size", 0) or 0)
        except (TypeError, ValueError):
            size = 0
        side = getattr(broker, "position_side", None) if broker is not None else None
        side_s = getattr(side, "value", "") if side is not None else ""
        if size <= 0:
            return "flat"
        if keep:
            return f"real {side_s} x{size} kept".strip()
        return f"sim {side_s} x{size} kept".strip()

    def _strategy_label(self) -> str:
        strat = getattr(self.app._live_runner, "strategy", None)
        if strat is None:
            return ""
        named = getattr(strat, "name", "") or ""
        return str(named or strat.__class__.__name__)

    def _notify_restart_starting(self, decision: RestartDecision, keep: bool) -> None:
        app = self.app
        runner = app._live_runner
        bot = str(getattr(runner, "bot_name", "") or getattr(self.args, "bot", ""))
        mode = str(getattr(app, "_trading_mode", "") or "")
        rb._log(f"[HEADLESS] degraded auto-restart session={decision.session_key} "
                f"mode={mode} keep_real={keep} pid={os.getpid()}")
        discord = getattr(rb, "_discord", None)
        if discord is None or not getattr(discord, "enabled", False):
            return
        try:
            discord.degraded_restart_starting(
                bot=bot,
                session_key=decision.session_key,
                mode=mode,
                position_kept=self._position_kept_text(keep),
                old_pid=os.getpid(),
                strategy=self._strategy_label(),
            )
        except Exception as exc:
            rb._log(f"[HEADLESS] restart-starting alert failed: "
                    f"[{type(exc).__name__}] {exc}")

    def _alert_restart_skipped(self, outcome: AlertOnly) -> None:
        app = self.app
        runner = getattr(app, "_live_runner", None)
        bot = str(getattr(runner, "bot_name", "") or getattr(self.args, "bot", ""))
        mode = str(getattr(app, "_trading_mode", "") or "")
        rb._log(f"[HEADLESS] degraded auto-restart skipped ({outcome.reason}) "
                f"session={outcome.session_key} mode={mode}")
        discord = getattr(rb, "_discord", None)
        if discord is None or not getattr(discord, "enabled", False):
            return
        try:
            discord.degraded_restart_skipped(
                bot=bot,
                session_key=outcome.session_key,
                mode=mode,
                reason=outcome.reason,
            )
        except Exception as exc:
            rb._log(f"[HEADLESS] restart-skipped alert failed: "
                    f"[{type(exc).__name__}] {exc}")

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


def _deploy_bot_dir(args) -> str:
    base = os.path.join(rb.project_root, "data", "live")
    return LiveRunner.bot_dir_for(base, args.symbol, args.bot)


def _resolve_discord(bot: str = "", symbol: str = ""):
    """The live notifier, or one built from settings when deploy never reached it.

    A replacement that dies at pid-wait, relogin, login, or the pre-deploy
    position prompt has not run ``_deploy_live_continue``, so ``rb._discord``
    is still None. An already installed notifier is returned as-is.
    """
    current = getattr(rb, "_discord", None)
    if current is not None:
        return current
    load = getattr(rb, "_load_settings", None)
    if not callable(load):
        return None
    try:
        cfg = load() or {}
    except Exception as exc:
        rb._safe_log(f"[HEADLESS] discord settings unreadable: "
                     f"[{type(exc).__name__}] {exc}")
        return None
    if not isinstance(cfg, dict):
        return None
    token = str(cfg.get("discord_bot_token") or "").strip()
    channel = str(cfg.get("discord_channel_id") or "").strip()
    if not token or not channel:
        return None
    from src.live.discord_notify import DiscordNotifier
    return DiscordNotifier(token, channel, bot_name=bot, symbol=symbol)


# ce93800a flushed 5s after spawn. The child is already in wait_for_old_pid.
_POST_SPAWN_FLUSH_S = 5.0


def _log_discord_flush(discord, timeout_s: float) -> bool:
    if discord is None or not getattr(discord, "enabled", False):
        return True
    flush = getattr(discord, "flush", None)
    if not callable(flush):
        return True
    ok = False
    try:
        ok = bool(flush(timeout_s=timeout_s))
    except Exception as exc:
        rb._safe_log(
            f"[HEADLESS] discord flush failed: [{type(exc).__name__}] {exc}")
    rb._safe_log(f"[HEADLESS] discord flush {ok}")
    if not ok:
        rb._safe_log("[HEADLESS] discord send may have been cut off")
    return ok


def _flush_discord(discord) -> None:
    _log_discord_flush(discord, _POST_SPAWN_FLUSH_S)


def _emit_restart_still_waiting(old_pid: int, waited_s: float, *,
                                bot: str = "", symbol: str = "") -> None:
    """Priority "still waiting" alert. Does not clear ``pending_notify``.

    Distinct from ``_emit_restart_failed`` so operators can tell a live
    wait from a give-up. Flushed with the same helper as the failure alert.
    """
    rb._safe_log(
        f"[HEADLESS] pid-wait still waiting old={old_pid} "
        f"after {int(waited_s)}s symbol={symbol}"
    )
    discord = _resolve_discord(bot=bot, symbol=symbol)
    if discord is None or not getattr(discord, "enabled", False):
        return
    try:
        method = getattr(discord, "degraded_restart_still_waiting", None)
        if callable(method):
            method(old_pid=int(old_pid), waited_s=float(waited_s), bot=bot)
        else:
            discord.notify(
                f"仍在等待舊行程 Still waiting for old process "
                f"pid {old_pid} after {int(waited_s)}s",
                priority=True,
            )
        from src.live.discord_notify import FLUSH_EXIT_TIMEOUT_S
        _log_discord_flush(discord, FLUSH_EXIT_TIMEOUT_S)
    except Exception as exc:
        rb._safe_log(f"[HEADLESS] still-waiting alert failed: "
                     f"[{type(exc).__name__}] {exc}")


def _emit_restart_failed(reason: str, *, detail: str = "", bot: str = "",
                         symbol: str = "") -> None:
    rb._safe_log("[HEADLESS] degraded auto-restart failed: "
                 + reason + (f" — {detail}" if detail else ""))
    discord = _resolve_discord(bot=bot, symbol=symbol)
    if discord is None or not getattr(discord, "enabled", False):
        return
    try:
        method = getattr(discord, "degraded_restart_failed", None)
        if callable(method):
            method(reason, detail=detail, bot=bot)
        else:
            discord.notify(
                f"自動重啟失敗 Auto-restart failed: {reason} {detail}",
                priority=True,
            )
        from src.live.discord_notify import FLUSH_EXIT_TIMEOUT_S
        _log_discord_flush(discord, FLUSH_EXIT_TIMEOUT_S)
    except Exception as exc:
        rb._safe_log(f"[HEADLESS] restart-failed alert failed: "
                     f"[{type(exc).__name__}] {exc}")


def _fail_pending_replacement(bot_dir: str, bot_name: str, reason: str,
                             detail: str, symbol: str = "") -> None:
    """Alert when a ``--wait-for-pid`` child never reached live ticks.

    ``pending_notify`` stays true only until tick subscription. Clearing
    it here keeps the session budget consumed and stops a later manual
    ``--resume`` from posting a false recovery message. A record that is
    already clear is left alone, so a bot that did recover and later
    stops does not get a failure alert.
    """
    from src.live.degraded_restart import clear_pending_notify, load_auto_restart
    data = load_auto_restart(bot_dir)
    if not isinstance(data, dict) or data.get("pending_notify") is not True:
        return
    _emit_restart_failed(reason, detail=detail, bot=bot_name, symbol=symbol)
    clear_pending_notify(bot_dir)


def _spawn_pending_restart(driver, code: int) -> int:
    cmd = getattr(driver, "_pending_restart_cmd", None) if driver is not None else None
    if not cmd:
        return code
    bot_dir = getattr(driver, "bot_dir", "") or ""
    stdout_path = (cli_stdout_path(bot_dir) if bot_dir
                   else os.path.join(os.getcwd(), "cli_stdout.log"))
    from src.live.discord_notify import FLUSH_EXIT_TIMEOUT_S
    _log_discord_flush(getattr(rb, "_discord", None), FLUSH_EXIT_TIMEOUT_S)
    try:
        from src.paths import project_root
        proc = spawn_detached(cmd, stdout_path, cwd=project_root())
    except OSError as exc:
        symbol = str(getattr(getattr(driver, "args", None), "symbol", "") or "")
        _emit_restart_failed(
            "spawn failed",
            detail=f"{type(exc).__name__}: {exc}",
            bot=os.path.basename(bot_dir),
            symbol=symbol,
        )
        if bot_dir:
            from src.live.degraded_restart import clear_pending_notify
            clear_pending_notify(bot_dir)
        return EXIT_BOT_STOPPED
    rb._safe_log(f"[HEADLESS] spawned replacement pid={proc.pid}")
    _flush_discord(getattr(rb, "_discord", None))
    return code


def run_deploy(args) -> int:
    from src.live.degraded_restart import (
        DEFAULT_RELOGIN_WAIT_S, PID_WAIT_S, acquire_relogin_slot,
        clear_pending_notify, poll_old_pid_until_exit_or_budget,
        release_relogin_slot, wait_for_old_pid,
    )

    bot_dir = _deploy_bot_dir(args)
    bot_name = str(getattr(args, "bot", "") or "")
    symbol = str(getattr(args, "symbol", "") or "")
    old_pid = int(getattr(args, "wait_for_pid", 0) or 0)
    if old_pid > 0:
        # Before _boot, so a pid-wait warning is on stdout. Deploy's
        # own attach runs only after the wait succeeds.
        rb.attach_app_log_handlers()
        rb._safe_log(f"[HEADLESS] pid-wait start old={old_pid}")
        # The 60s primitive stays. A miss does not give up and does not
        # clear pending_notify: the recovery line still has to fire if
        # this process later reaches tick subscription.
        if not wait_for_old_pid(old_pid, timeout_s=PID_WAIT_S):
            _emit_restart_still_waiting(
                old_pid, PID_WAIT_S, bot=bot_name, symbol=symbol)
            outcome, mins_left = poll_old_pid_until_exit_or_budget(
                old_pid, symbol)
            if outcome == "budget":
                rb._safe_log(
                    f"[HEADLESS] pid-wait budget gone old={old_pid} "
                    f"symbol={symbol} mins_left={mins_left}"
                )
                _emit_restart_failed(
                    "old process still alive",
                    detail=(f"pid {old_pid} still alive after "
                            f"{int(PID_WAIT_S)}s"),
                    bot=bot_name,
                    symbol=symbol,
                )
                clear_pending_notify(bot_dir)
                return EXIT_TIMEOUT
            exited = rb._taipei_now().strftime("%Y-%m-%d %H:%M:%S")
            rb._safe_log(
                f"[HEADLESS] old pid {old_pid} exited at {exited} TPE")
    stagger = bool(getattr(args, "relogin_stagger", False))
    held_slot = False
    if stagger:
        if not acquire_relogin_slot(bot_dir, wait_s=DEFAULT_RELOGIN_WAIT_S):
            _emit_restart_failed(
                "relogin stagger timed out",
                detail=(f"could not lock .relogin.slot within "
                        f"{int(DEFAULT_RELOGIN_WAIT_S)}s"),
                bot=bot_name,
                symbol=symbol,
            )
            clear_pending_notify(bot_dir)
            return EXIT_TIMEOUT
        held_slot = True
    app = None
    driver = None
    code = EXIT_BOT_STOPPED
    try:
        app, code = _boot(policy_from_args(args), need_com=True)
        if app is not None:
            if stagger:
                app._release_relogin_slot = lambda: release_relogin_slot(bot_dir)
            driver = DeployDriver(app, args)
            code = _drive_until_exit(driver, app)
    except Exception as exc:
        if old_pid > 0:
            _fail_pending_replacement(
                bot_dir, bot_name, "replacement deploy failed",
                f"{type(exc).__name__}: {exc}", symbol)
        raise
    finally:
        # LeaveMonitor and root.destroy finish here, before any spawn.
        if app is not None:
            _teardown(app)
        if held_slot:
            release_relogin_slot(bot_dir)
    if old_pid > 0 and code != EXIT_OK:
        _fail_pending_replacement(
            bot_dir, bot_name, "replacement deploy failed", f"exit {code}",
            symbol)
    return _spawn_pending_restart(driver, code)


def _drive_until_exit(driver, app) -> int:
    """Run deploy until the bot exits. The marker is removed in ``finally``."""
    try:
        driver.start()
        if driver.exit_code is None:
            app.root.mainloop()
        return driver.exit_code if driver.exit_code is not None else EXIT_BOT_STOPPED
    finally:
        bot_dir = getattr(driver, "bot_dir", "") or ""
        if bot_dir:
            remove_headless_marker_if_ours(bot_dir)


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
    rb._safe_log("[HEADLESS] root.destroy done")
