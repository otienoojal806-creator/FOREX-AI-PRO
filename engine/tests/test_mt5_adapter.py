"""Exercises the real MT5Broker adapter against a stand-in `MetaTrader5` module (the true package is Windows-only)."""
import time
import unittest
from types import SimpleNamespace as NS
from unittest import mock

from scalper_engine.gateways import LiveGateway
from scalper_engine.models import Deal, Position
from scalper_engine.mt5_connection import MT5Broker

from .fakes import FakeBroker


class FakeMT5:
    ORDER_TYPE_BUY, ORDER_TYPE_SELL = 0, 1

    def __init__(self):
        self.init_ok = True
        self.init_calls = []
        self.err = (1, "Success")
        self.positions = ()
        self.order_result = NS(retcode=10009, order=11, deal=12, volume=0.1, price=2400.5, comment="done", request_id=5)
        self.raise_on_send = False

    def initialize(self, **kw):
        self.init_calls.append(kw)
        return self.init_ok

    def shutdown(self): return True
    def last_error(self): return self.err
    def terminal_info(self): return NS(connected=True, trade_allowed=True, company="Acme", build=4500)
    def account_info(self):
        return NS(login=77, server="Acme-Demo", currency="USD", balance=1000.0, equity=1001.0, margin=0.0, margin_free=1001.0,
                  leverage=100, trade_mode=0, trade_allowed=True, trade_expert=True, margin_mode=2, company="Acme")
    def symbols_get(self): return (NS(name="XAUUSD.r", path="Metals\\XAUUSD.r", description="Gold vs US Dollar", visible=True, trade_mode=4),)
    def symbol_select(self, s, enable): return True
    def symbol_info(self, s):
        if s != "XAUUSD.r":
            return None
        return NS(name=s, digits=2, point=0.01, trade_tick_size=0.0, trade_tick_value=1.0, trade_tick_value_profit=1.0,
                  trade_tick_value_loss=1.0, trade_contract_size=100.0, volume_min=0.01, volume_max=50.0, volume_step=0.01,
                  trade_stops_level=30, trade_freeze_level=0, filling_mode=2, trade_mode=4, currency_profit="USD",
                  currency_base="XAU", path="Metals\\XAUUSD.r", description="Gold")
    def symbol_info_tick(self, s): return NS(time=1_760_000_000, bid=2400.0, ask=2400.3, last=0.0)
    def copy_rates_from_pos(self, s, tf, pos, n):
        return [{"time": 1_760_000_000 + i * 60, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "tick_volume": 10, "spread": 3} for i in range(n)]
    def positions_get(self, **kw): return self.positions
    def history_deals_get(self, *a, **k): return None
    def order_calc_profit(self, t, s, v, o, c): return (c - o) * 100 * v
    def order_calc_margin(self, t, s, v, p): return p * 100 * v / 100
    def order_check(self, req): return NS(retcode=0, margin=10.0, margin_free=990.0, comment="ok")
    def order_send(self, req):
        if self.raise_on_send:
            raise RuntimeError("terminal closed")
        return self.order_result


def make():
    fake = FakeMT5()
    b = MT5Broker(path="C:/mt5/terminal64.exe", login=77, password="s3cretPassw0rd", server="Acme-Demo")
    b._mt5 = fake
    return b, fake


class MT5AdapterTests(unittest.TestCase):
    def test_connect_passes_credentials_to_initialize_and_records_heartbeat(self):
        b, f = make()
        self.assertEqual(b.connect(), (True, "connected"))
        self.assertEqual(f.init_calls[0]["login"], 77)
        self.assertGreater(b.last_ok, 0)

    def test_failed_connect_retries_boundedly_and_never_leaks_the_password(self):
        b, f = make()
        f.init_ok, f.err = False, (-10005, "IPC timeout")
        with mock.patch.object(time, "sleep"):
            ok, msg = b.connect()
        self.assertFalse(ok)
        self.assertEqual(len(f.init_calls), 3)
        self.assertNotIn("s3cretPassw0rd", msg + b.last_error())
        self.assertNotIn("s3cretPassw0rd", repr(b.__dict__.get("_err")))

    def test_symbol_spec_maps_all_contract_fields_and_falls_back_to_point_for_tick_size(self):
        b, _ = make()
        s = b.symbol_spec("XAUUSD.r")
        self.assertEqual((s.digits, s.contract_size, s.volume_step, s.stops_level), (2, 100.0, 0.01, 30))
        self.assertEqual(s.tick_size, 0.01)  # trade_tick_size was 0 -> point
        self.assertIsNone(b.symbol_spec("NOPE"))

    def test_tick_rates_account_terminal_mapping(self):
        b, _ = make()
        self.assertEqual(b.tick("XAUUSD.r").time_msc, 1_760_000_000_000)
        bars = b.rates("XAUUSD.r", 1, 1, 5)
        self.assertEqual(len(bars), 5)
        self.assertEqual(bars[0].high, 2.0)
        self.assertEqual(b.account_info().type_name, "DEMO")
        self.assertTrue(b.terminal_info().trade_allowed)
        self.assertEqual(b.symbols()[0].name, "XAUUSD.r")

    def test_calculations_and_check_and_send_map_results(self):
        b, _ = make()
        self.assertAlmostEqual(b.calc_profit("BUY", "XAUUSD.r", 0.1, 2400.0, 2397.0), -30.0)
        self.assertAlmostEqual(b.calc_margin("BUY", "XAUUSD.r", 0.1, 2400.0), 240.0)
        self.assertEqual(b.order_check({}).retcode, 0)
        r = b.order_send({"action": 1})
        self.assertEqual((r.retcode, r.order, r.deal), (10009, 11, 12))

    def test_order_send_exception_becomes_none_so_the_caller_treats_it_as_ambiguous(self):
        b, f = make()
        f.raise_on_send = True
        self.assertIsNone(b.order_send({"action": 1}))
        self.assertIn("RuntimeError", b.last_error())

    def test_no_positions_is_an_empty_list_but_an_error_is_none(self):
        b, f = make()
        f.positions = ()
        self.assertEqual(b.positions(), [])
        f.positions, f.err = None, (1, "Success")
        self.assertEqual(b.positions(), [])      # "none open" reported as None with RES_S_OK
        f.positions, f.err = None, (-2, "Terminal: Invalid params")
        self.assertIsNone(b.positions())         # a real failure must NOT look like "flat"

    def test_position_and_deal_conversion(self):
        b, f = make()
        f.positions = (NS(ticket=5, symbol="XAUUSD.r", type=1, volume=0.2, price_open=2400.0, sl=2403.0, tp=2390.0,
                          price_current=2399.0, profit=20.0, swap=0.0, magic=9, comment="FAIPabc", time=1),)
        p = b.positions()[0]
        self.assertEqual((p.side, p.sl, p.magic), ("SELL", 2403.0, 9))


class CommentMatchTests(unittest.TestCase):
    def test_live_gateway_finds_orders_even_if_the_broker_appends_text_to_the_comment(self):
        fb = FakeBroker()
        fb.pos[1] = Position(1, "XAUUSD", "BUY", 0.1, 2400.0, 2397.0, 0.0, magic=20260117, comment="FAIPabc123 [sl 2397.00]")
        fb.deal_list.append(Deal(2, 2, 1, "XAUUSD", "BUY", 0, 0.1, 2400.0, 0, 0, 0, 0, 0, 1, 20260117, "FAIPabc123 [sl 2397.00]"))
        gw = LiveGateway(fb, 20260117)
        pos, deals = gw.find_by_comment("FAIPabc123", 0)
        self.assertEqual(pos.ticket, 1)
        self.assertEqual(len(deals), 1)
        self.assertEqual(gw.find_by_comment("FAIPzzz999", 0), (None, []))
        self.assertEqual(gw.find_by_comment("", 0), (None, []))  # an empty comment must never match everything

    def test_other_magic_numbers_are_ignored(self):
        fb = FakeBroker()
        fb.pos[1] = Position(1, "XAUUSD", "BUY", 0.1, 2400.0, 2397.0, 0.0, magic=111, comment="FAIPabc123")
        self.assertEqual(LiveGateway(fb, 20260117).find_by_comment("FAIPabc123", 0)[0], None)
        self.assertEqual(LiveGateway(fb, 20260117).positions(), [])  # a manual / foreign trade is never managed


if __name__ == "__main__":
    unittest.main()
