"""Shared builders: a broker loaded with a BUY-qualifying candle history and the matching analysis."""
from __future__ import annotations

from typing import Any, Optional

from scalper_engine.config import EnvSettings, default_config, instrument_strategy
from scalper_engine.gateways import LiveGateway
from scalper_engine.models import TF_M1, TF_M5, Tick
from scalper_engine.monitoring_and_alerts import Alerts
from scalper_engine.order_executor import ExecContext, OrderExecutor
from scalper_engine.risk_manager import RiskInputs
from scalper_engine.strategy_engine import Analysis, evaluate
from scalper_engine.trade_journal import MemoryJournal

from .fakes import FakeBroker, T0, aggregate, make_bars

BUY_SEED = dict(start=2300.0, drift=0.10, noise=1.6, seed=9)
SELL_SEED = dict(start=2800.0, drift=-0.15, noise=1.8, seed=1)
LAST_BAR = T0 + 3600 - 60  # open time of the last COMPLETED M1 candle


def load_market(broker: FakeBroker, symbol: str = "XAUUSD", kind: dict[str, Any] = BUY_SEED) -> None:
    m1 = make_bars(600, kind["start"], kind["drift"], kind["noise"], LAST_BAR, seed=kind["seed"])
    broker.bars[(symbol, TF_M1)] = m1
    broker.bars[(symbol, TF_M5)] = aggregate(m1[:595])
    last = m1[-1]
    # quote 5 seconds after the candle closed, spread 25 points
    broker.ticks[symbol] = Tick(int((last.time + 65) * 1000), round(last.close - 0.10, 2), round(last.close + 0.15, 2))


def analysis_for(broker: FakeBroker, symbol: str = "XAUUSD", cfg: Optional[dict] = None) -> Analysis:
    cfg = cfg or default_config()
    inst = "XAUUSD" if symbol.startswith("XAU") else "BTCUSD"
    return evaluate(inst, symbol, broker.bars[(symbol, TF_M1)], broker.bars[(symbol, TF_M5)], None, broker.ticks[symbol],
                    broker.specs[symbol], instrument_strategy(cfg, inst), cfg["instruments"][inst], 2.0, cfg["risk"]["min_net_rr"])


def ok_risk_inputs(acct: Any, **kw: Any) -> RiskInputs:
    d = dict(equity=acct.equity, balance=acct.balance, free_margin=acct.margin_free, margin_used=acct.margin,
             day_start_equity=acct.balance, realized_today=0.0, floating_bot=0.0, consecutive_losses=0, last_loss_ts=0.0,
             trades_last_hour=0, risk_opened_today=0.0, open_positions=(), open_risk=0.0)
    d.update(kw)
    return RiskInputs(**d)


class Rig:
    """Executor + journal + alerts wired to a FakeBroker, ready to execute the BUY scenario."""

    def __init__(self, authorized: bool = True, cfg: Optional[dict] = None) -> None:
        self.cfg = cfg or default_config()
        self.broker = FakeBroker()
        load_market(self.broker)
        self.journal = MemoryJournal()
        self.alerts = Alerts(self.journal, dedupe_sec=0)
        self.ex = OrderExecutor(self.broker, self.journal, self.alerts, sleep=lambda s: None)
        self.gw = LiveGateway(self.broker, self.cfg["execution"]["magic"])
        self.analysis = analysis_for(self.broker, cfg=self.cfg)
        self.authorized = authorized
        self.risk_kw: dict[str, Any] = {}
        self.expected_login = self.broker.acct.login

    def ctx(self, analysis: Optional[Analysis] = None) -> ExecContext:
        a = analysis or self.analysis
        return ExecContext(self.gw, a, self.broker.specs[a.broker_symbol], self.cfg["instruments"][a.instrument], self.cfg,
                           self.authorized, self.expected_login, lambda acct: ok_risk_inputs(acct, **self.risk_kw))

    def run(self, analysis: Optional[Analysis] = None):
        return self.ex.execute_entry(self.ctx(analysis))

    def opens(self) -> list[dict]:
        """Order requests that OPEN a position (not stop changes or closes)."""
        return [r for r in self.broker.sent if r["action"] == 1 and "position" not in r]
