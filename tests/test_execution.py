"""Regressionstests für Paper- und Live-Broker.

Der echte ClobClient wird durch einen Fake ersetzt — es geht um die
Broker-Logik (Buchung nur bestätigter Fills, Tick-Quantisierung, Gebühren,
Gruppen-Abbruch/Unwind, Reconciliation), nicht um Netzwerk oder Signaturen.
"""

import time

import pytest
from py_clob_client_v2.clob_types import OrderType

from polybot.config import BotConfig
from polybot.execution import LiveBroker, PaperBroker, _quantize_price
from polybot.data.orderbook import Level, OrderBook
from polybot.portfolio import Fill, Portfolio, Position, RestingOrder
from polybot.strategies.base import Signal


class FakeApiRejection(Exception):
    """Definitive Server-Ablehnung wie PolyApiException (trägt status_code)."""

    status_code = 400

    def __init__(self, msg: str):
        super().__init__(msg)


class FakeClobClient:
    """Minimaler ClobClient-Ersatz: konfigurierbare Antworten, kein Netzwerk."""

    def __init__(self, fail_tokens=None, statuses=None, responses=None,
                 tick_fail_once=None, raise_tokens=None, text_response_tokens=None,
                 reject_400=None):
        self.fail_tokens = fail_tokens or set()
        self.statuses = statuses or {}            # token_id -> status in der post_order-Antwort
        self.responses = responses or {}          # token_id -> zusätzliche Response-Felder
        self.tick_fail_once = set(tick_fail_once or set())  # 1x "invalid tick size"
        self.raise_tokens = raise_tokens or set()           # post_order wirft Exception
        self.reject_400 = reject_400 or {}        # token_id -> Fehlertext (HTTP-400-Exception)
        self.text_response_tokens = text_response_tokens or set()  # 200 ohne JSON
        self.posted: list[str] = []               # token_ids in Sendereihenfolge
        self.posted_types: list[str] = []         # zugehörige OrderTypes
        self.created_prices: list[float] = []     # an create_order übergebene Preise
        self.created_sizes: list[float] = []      # an create_order übergebene Sizes
        self.created_ticks: list = []             # explizit übergebene tick_size-Optionen
        self.cancelled: list[str] = []            # gecancelte Order-IDs
        self.orders: dict[str, dict] = {}         # get_order-Antworten je orderID
        self.open_orders: list[dict] = []         # get_open_orders-Antwort
        self._n = 0

    def get_tick_size(self, token_id: str) -> str:
        return "0.01"

    def create_order(self, args, options=None):
        self.created_prices.append(args.price)
        self.created_sizes.append(args.size)
        self.created_ticks.append(options.tick_size if options else None)
        return {"token_id": args.token_id, "price": args.price, "size": args.size}

    def post_order(self, order, otype):
        tok = order["token_id"]
        self.posted.append(tok)
        self.posted_types.append(otype)
        if tok in self.raise_tokens:
            raise RuntimeError("Request exception!")
        if tok in self.reject_400:
            raise FakeApiRejection(self.reject_400[tok])
        if tok in self.text_response_tokens:
            return "Internal Server Error"
        if tok in self.tick_fail_once:
            self.tick_fail_once.discard(tok)
            return {"success": False, "errorMsg": "invalid tick size"}
        if tok in self.fail_tokens:
            return {"success": False, "errorMsg": "not enough balance"}
        self._n += 1
        oid = f"oid{self._n}"
        # FOK/FAK matchen sofort oder gar nicht; GTC ruht standardmäßig.
        default = "matched" if otype in (OrderType.FOK, OrderType.FAK) else "live"
        resp = {"success": True, "orderID": oid, "status": self.statuses.get(tok, default)}
        resp.update(self.responses.get(tok, {}))
        return resp

    def get_order(self, order_id: str) -> dict:
        return self.orders.get(order_id, {"status": "live", "size_matched": "0"})

    def get_open_orders(self, params=None, only_first_page=False, next_cursor=None):
        return self.open_orders

    def get_trades(self, params=None, only_first_page=False, next_cursor=None):
        return []

    def cancel_order(self, payload):
        self.cancelled.append(payload.orderID)

    def cancel_all(self):
        self.cancel_all_calls = getattr(self, "cancel_all_calls", 0) + 1

    def get_ok(self):
        return {"ok": True}


def make_live_broker(client: FakeClobClient, cooldown_s: float = 0.0) -> LiveBroker:
    # __init__ umgehen (verlangt Key + Netzwerk); nur die Felder setzen,
    # die execute() braucht. Cooldown default 0 = aus (Alt-Verhalten),
    # die Cooldown-Tests setzen ihn explizit.
    broker = LiveBroker.__new__(LiveBroker)
    broker.client = client
    broker._open_orders = {}
    broker._pending = {}
    broker.fallback_fee_rate = 0.0
    broker.reject_cooldown_s = cooldown_s
    broker._reject_until = {}
    broker._fatal_reject = None
    broker.DELAY_POLL_INTERVAL_S = 0  # Tests sollen nicht schlafen
    return broker


def mm_quote(side: str, price: float) -> Signal:
    return Signal(token_id="tok", side=side, price=price, size=10,
                  reason="MM", replace=True)


def arb_leg(token: str, group: str = "g1", price: float = 0.30) -> Signal:
    return Signal(token_id=token, side="BUY", price=price, size=10,
                  reason="arb", group=group)


# ---- LiveBroker: Order-Lifecycle ------------------------------------------

def test_livebroker_cancelt_alte_quotes_vor_neuquote():
    client = FakeClobClient()
    broker = make_live_broker(client)
    pf = Portfolio()
    pf.positions["tok"] = Position(token_id="tok", shares=100, cost_basis=50)

    # Tick 1: Bid+Ask platziert, nichts zu canceln
    broker.execute([mm_quote("BUY", 0.48), mm_quote("SELL", 0.52)], {}, pf)
    assert client.cancelled == []
    assert broker._open_orders["tok"] == ["oid1", "oid2"]

    # Tick 2: Alt-Quotes werden zuerst gecancelt (genau einmal pro Token)
    broker.execute([mm_quote("BUY", 0.47), mm_quote("SELL", 0.51)], {}, pf)
    assert client.cancelled == ["oid1", "oid2"]
    assert broker._open_orders["tok"] == ["oid3", "oid4"]


def test_livebroker_bucht_gtc_nicht_sofort_als_fill():
    # Regressionstest: resp["success"] hieß früher "Fill über volle Größe" —
    # eine ruhende GTC-Order ist aber noch gar nicht gefüllt.
    client = FakeClobClient()  # GTC -> status "live"
    broker = make_live_broker(client)
    pf = Portfolio(cash=100.0)
    sig = Signal(token_id="tok", side="BUY", price=0.50, size=10, reason="MM")
    fills = broker.execute([sig], {}, pf)
    assert fills == 0
    assert pf.positions == {}
    assert pf.cash == pytest.approx(100.0)
    assert "oid1" in broker._pending  # aber getrackt für die Reconciliation


def test_livebroker_reconciled_teilfills_ruhender_orders():
    client = FakeClobClient()
    broker = make_live_broker(client)
    pf = Portfolio(cash=100.0)
    broker.execute([Signal(token_id="tok", side="BUY", price=0.50, size=10,
                           reason="MM")], {}, pf)

    # Teil-Fill: der nächste Tick bucht genau das Delta (Maker -> Gebühr 0)
    client.orders["oid1"] = {"status": "live", "size_matched": "4", "price": "0.50"}
    assert broker.execute([], {}, pf) == 1
    assert pf.positions["tok"].shares == pytest.approx(4)
    assert pf.cash == pytest.approx(100.0 - 2.0)

    # Rest gefüllt -> nur das neue Delta, danach Tracking beendet
    client.orders["oid1"] = {"status": "matched", "size_matched": "10", "price": "0.50"}
    assert broker.execute([], {}, pf) == 1
    assert pf.positions["tok"].shares == pytest.approx(10)
    assert pf.cash == pytest.approx(100.0 - 5.0)
    assert "oid1" not in broker._pending


def test_livebroker_bucht_matched_mit_tatsaechlichen_mengen():
    # Regressionstest: gebucht wurde zum Signalpreis statt zu den realen
    # Beträgen aus der Response (makingAmount/takingAmount).
    client = FakeClobClient(responses={"tok": {"makingAmount": "2.85", "takingAmount": "10"}})
    broker = make_live_broker(client)
    pf = Portfolio(cash=100.0)
    fills = broker.execute([arb_leg("tok", price=0.30)], {}, pf)
    assert fills == 1
    assert pf.positions["tok"].shares == pytest.approx(10)
    assert pf.cash == pytest.approx(100.0 - 2.85)  # 0.285/Share statt 0.30


def test_livebroker_bucht_taker_gebuehr():
    # Regressionstest: FOK-Fills sind Taker-Fills, zahlten aber keine Gebühr.
    client = FakeClobClient()
    broker = make_live_broker(client)
    pf = Portfolio(cash=100.0)
    sig = Signal(token_id="tok", side="BUY", price=0.50, size=10, reason="arb", group="g1")
    broker.execute([sig], {}, pf, fee_rates={"tok": 0.04})
    # fee = 10 * 0.04 * 0.5 * (1-0.5) = 0.10 USDC
    assert pf.cash == pytest.approx(100.0 - 5.0 - 0.10)
    assert pf.realized_pnl == pytest.approx(-0.10)


# ---- LiveBroker: Tick-Raster ----------------------------------------------

def test_quantize_price_rundet_konservativ():
    assert _quantize_price(0.155, 0.01, "BUY") == pytest.approx(0.15)
    assert _quantize_price(0.155, 0.01, "SELL") == pytest.approx(0.16)
    assert _quantize_price(0.30, 0.01, "BUY") == pytest.approx(0.30)
    assert _quantize_price(0.1275, 0.0025, "BUY") == pytest.approx(0.1275)
    # Bereichsklemme: nie unter tick bzw. über 1-tick
    assert _quantize_price(0.004, 0.01, "BUY") == pytest.approx(0.01)
    assert _quantize_price(0.998, 0.01, "SELL") == pytest.approx(0.99)


def test_livebroker_quantisiert_preis_aufs_tick_raster():
    # Regressionstest: round(price, 3) ließ 0.155 stehen; der Client hätte
    # daraus per round_normal 0.16 gemacht — teurer als kalkuliert.
    client = FakeClobClient()
    broker = make_live_broker(client)
    pf = Portfolio(cash=100.0)
    broker.execute([arb_leg("tok", price=0.155)], {}, pf)
    assert client.created_prices == [pytest.approx(0.15)]  # BUY: abgerundet
    assert pf.positions["tok"].cost_basis == pytest.approx(1.5)  # gebucht zum Orderpreis


def test_livebroker_holt_frischen_tick_nach_ablehnung():
    # Regressionstest: der Client cacht den Tick für immer — nach einem
    # Tick-Wechsel wurde der Markt dauerhaft unhandelbar.
    client = FakeClobClient(tick_fail_once={"tok"})
    broker = make_live_broker(client)
    broker._fresh_tick = lambda token_id: "0.001"  # statt HTTP-Abfrage
    pf = Portfolio(cash=100.0)
    fills = broker.execute([arb_leg("tok", price=0.1275)], {}, pf)
    assert fills == 1
    assert client.posted == ["tok", "tok"]  # genau ein Retry
    # 1. Versuch: Cache-Tick 0.01 -> 0.12; Retry: frischer Tick 0.001 -> 0.127
    assert client.created_prices == [pytest.approx(0.12), pytest.approx(0.127)]
    assert client.created_ticks == [None, "0.001"]  # frischer Tick übersteuert Cache


# ---- LiveBroker: Gruppen-Atomarität ---------------------------------------

def test_livebroker_bricht_gruppe_nach_gescheitertem_bein_ab():
    # FOK sichert nur die Einzelorder: scheitert Bein 2, darf Bein 3
    # gar nicht mehr gesendet werden (halber Arb = offene Wette).
    client = FakeClobClient(fail_tokens={"t2"})
    broker = make_live_broker(client)
    group = [arb_leg("t1"), arb_leg("t2"), arb_leg("t3")]
    fills = broker.execute(group, {}, Portfolio())
    assert client.posted == ["t1", "t2"]  # t3 wurde nicht mehr gesendet
    assert fills == 1


def test_livebroker_gruppenabbruch_stoppt_nicht_andere_gruppen():
    client = FakeClobClient(fail_tokens={"a1"})
    broker = make_live_broker(client)
    signals = [arb_leg("a1", group="gA"), arb_leg("a2", group="gA"),
               arb_leg("b1", group="gB")]
    broker.execute(signals, {}, Portfolio())
    assert client.posted == ["a1", "b1"]


def test_livebroker_unwind_stellt_gefuelltes_bein_glatt():
    # Regressionstest: nach gescheitertem Bein blieb das bereits gefüllte
    # Bein als offene, ungehedgte Position stehen.
    client = FakeClobClient(fail_tokens={"t2"})
    broker = make_live_broker(client)
    pf = Portfolio(cash=100.0)
    books = {"t1": OrderBook(token_id="t1", bids=[Level(0.29, 100)], asks=[])}
    broker.execute([arb_leg("t1"), arb_leg("t2")], books, pf)
    # Bein t1 gefüllt (BUY 10 @0.30), t2 scheiterte -> FAK-Gegenorder zum Bid
    assert client.posted == ["t1", "t2", "t1"]
    assert client.posted_types[-1] == OrderType.FAK
    assert pf.positions == {}  # glattgestellt
    assert pf.realized_pnl == pytest.approx((0.29 - 0.30) * 10)


# ---- LiveBroker: unklare POST-Zustände & Matching-Delay --------------------

def test_livebroker_verifiziert_zustand_nach_post_exception():
    # Regressionstest: ein Read-Timeout nach angenommener Order hinterließ
    # eine hängende Live-Order, von der das Portfolio nichts wusste.
    client = FakeClobClient(raise_tokens={"tok"})
    client.open_orders = [{"id": "ghost1", "size_matched": "0"}]
    broker = make_live_broker(client)
    pf = Portfolio(cash=100.0)
    fills = broker.execute([arb_leg("tok")], {}, pf)
    assert fills == 0
    assert pf.positions == {}
    assert "ghost1" in client.cancelled  # hängende Order wurde aufgeräumt


def test_livebroker_uebersteht_antwort_ohne_json():
    # Regressionstest: eine 200-Antwort als String führte zu AttributeError
    # bei resp.get(...) — und die Order blieb ungebucht liegen.
    client = FakeClobClient(text_response_tokens={"tok"})
    client.open_orders = [{"id": "ghost2", "size_matched": "0"}]
    broker = make_live_broker(client)
    pf = Portfolio(cash=100.0)
    fills = broker.execute([arb_leg("tok")], {}, pf)
    assert fills == 0
    assert pf.positions == {}
    assert "ghost2" in client.cancelled


def test_livebroker_delayed_wartet_auf_bestaetigung():
    # Regressionstest: status "delayed" (Matching-Delay, z.B. Sport in-play)
    # wurde sofort als Fill über die volle Größe gebucht.
    client = FakeClobClient(statuses={"tok": "delayed"})
    client.orders["oid1"] = {"status": "matched", "size_matched": "10", "price": "0.30"}
    broker = make_live_broker(client)
    pf = Portfolio(cash=100.0)
    fills = broker.execute([arb_leg("tok")], {}, pf)
    assert fills == 1
    assert pf.positions["tok"].shares == pytest.approx(10)
    assert pf.cash == pytest.approx(100.0 - 3.0)


def test_livebroker_delayed_ohne_bestaetigung_wird_gecancelt():
    client = FakeClobClient(statuses={"tok": "delayed"})
    client.orders["oid1"] = {"status": "delayed", "size_matched": "0"}
    broker = make_live_broker(client)
    pf = Portfolio(cash=100.0)
    fills = broker.execute([arb_leg("tok")], {}, pf)
    assert fills == 0
    assert pf.positions == {}
    assert "oid1" in client.cancelled  # sonst könnte das Bein später ungehedged matchen


# ---- LiveBroker: Initialisierung (Deposit-Wallet-Flow) -----------------------

class _CapturingClobClient:
    """Fängt die ClobClient-Konstruktorargumente ab, kein Netzwerk."""

    captured: dict = {}

    def __init__(self, host, **kwargs):
        type(self).captured = {"host": host, **kwargs}

    def create_or_derive_api_key(self):
        return "creds"

    def set_api_creds(self, creds):
        self.creds = creds

    def get_address(self):
        return "0xEOA"

    def get_ok(self):
        return {"ok": True}

    def cancel_all(self):
        type(self).cancel_all_calls = getattr(type(self), "cancel_all_calls", 0) + 1


def _live_cfg(funder: str | None, signature_type: int) -> BotConfig:
    cfg = BotConfig()
    cfg.private_key = "0x" + "11" * 32
    cfg.funder_address = funder
    cfg.signature_type = signature_type
    cfg.risk.live_auto_merge = False  # kein MergeExecutor/RPC im Test
    return cfg


def test_livebroker_init_deposit_wallet_modus(monkeypatch):
    # POLY_FUNDER_ADDRESS + POLY_SIGNATURE_TYPE=3: der ClobClient bekommt
    # funder (Deposit Wallet) und signature_type 3 (POLY_1271).
    import py_clob_client_v2.client as clob_mod

    monkeypatch.setattr(clob_mod, "ClobClient", _CapturingClobClient)
    LiveBroker(_live_cfg("0x" + "d1" * 20, 3))
    captured = _CapturingClobClient.captured
    assert captured["funder"] == "0x" + "d1" * 20
    assert captured["signature_type"] == 3
    assert captured["key"] == "0x" + "11" * 32


def test_livebroker_init_ohne_funder_bleibt_eoa(monkeypatch):
    import py_clob_client_v2.client as clob_mod

    monkeypatch.setattr(clob_mod, "ClobClient", _CapturingClobClient)
    _CapturingClobClient.captured = {}
    LiveBroker(_live_cfg(None, 3))
    assert "funder" not in _CapturingClobClient.captured
    assert "signature_type" not in _CapturingClobClient.captured


def test_livebroker_init_uebernimmt_cooldown_config(monkeypatch):
    import py_clob_client_v2.client as clob_mod

    monkeypatch.setattr(clob_mod, "ClobClient", _CapturingClobClient)
    cfg = _live_cfg(None, 3)
    cfg.risk.order_reject_cooldown_s = 42.0
    broker = LiveBroker(cfg)
    assert broker.reject_cooldown_s == 42.0
    assert broker._reject_until == {}
    assert broker._fatal_reject is None


# ---- LiveBroker: Reject-Cooldown & Fatal-Sperre -----------------------------

def test_livebroker_cooldown_nach_http_400(monkeypatch):
    # Harte 400-Ablehnung: das Token bekommt einen Cooldown — im Fenster
    # geht KEINE weitere Order auf dieses Token raus, danach wieder.
    client = FakeClobClient(reject_400={"tok": "not enough balance"})
    broker = make_live_broker(client, cooldown_s=60.0)
    pf = Portfolio(cash=100.0)
    sig = Signal(token_id="tok", side="BUY", price=0.50, size=10, reason="MM")

    now = time.time()
    monkeypatch.setattr("polybot.execution.time.time", lambda: now)
    broker.execute([sig], {}, pf)
    assert client.posted == ["tok"]

    # Innerhalb des Cooldowns: kein erneuter POST-Versuch.
    broker.execute([sig], {}, pf)
    assert client.posted == ["tok"]

    # Nach Ablauf des Fensters wird wieder versucht.
    monkeypatch.setattr("polybot.execution.time.time", lambda: now + 60.1)
    broker.execute([sig], {}, pf)
    assert client.posted == ["tok", "tok"]


def test_livebroker_cooldown_auch_bei_success_false(monkeypatch):
    # Auch eine 200-Antwort mit success=false ist eine harte Ablehnung.
    client = FakeClobClient(fail_tokens={"tok"})  # success=false
    broker = make_live_broker(client, cooldown_s=60.0)
    pf = Portfolio(cash=100.0)
    sig = Signal(token_id="tok", side="BUY", price=0.50, size=10, reason="MM")
    now = time.time()
    monkeypatch.setattr("polybot.execution.time.time", lambda: now)
    broker.execute([sig], {}, pf)
    broker.execute([sig], {}, pf)
    assert client.posted == ["tok"]
    # Andere Tokens sind vom Cooldown NICHT betroffen.
    broker.execute([Signal(token_id="anders", side="BUY", price=0.50, size=10,
                           reason="MM")], {}, pf)
    assert client.posted == ["tok", "anders"]


def test_livebroker_cooldown_null_deaktiviert(monkeypatch):
    # cooldown_s=0 (Config aus): Alt-Verhalten, jeder Tick versucht erneut.
    client = FakeClobClient(fail_tokens={"tok"})
    broker = make_live_broker(client, cooldown_s=0.0)
    pf = Portfolio(cash=100.0)
    sig = Signal(token_id="tok", side="BUY", price=0.50, size=10, reason="MM")
    broker.execute([sig], {}, pf)
    broker.execute([sig], {}, pf)
    assert client.posted == ["tok", "tok"]


def test_livebroker_maker_not_allowed_sperrt_dauerhaft(caplog):
    # "maker address not allowed" ist ein Konfigurationsfehler: dauerhafte
    # Sperre bis Prozessende, EIN klarer Hinweis im Log statt Spam.
    import logging

    client = FakeClobClient(
        reject_400={"tok": '{"error":"maker address not allowed, please use '
                           'the deposit wallet flow"}'})
    broker = make_live_broker(client, cooldown_s=60.0)
    pf = Portfolio(cash=100.0)
    sig = Signal(token_id="tok", side="BUY", price=0.50, size=10, reason="MM")
    other = Signal(token_id="anders", side="BUY", price=0.50, size=10, reason="MM")

    with caplog.at_level(logging.ERROR, logger="polybot.execution"):
        broker.execute([sig], {}, pf)
        # Sperre gilt kontoweit: auch ANDERE Tokens werden nicht mehr versucht.
        broker.execute([other], {}, pf)
        broker.execute([sig, other], {}, pf)
    assert client.posted == ["tok"]
    hints = [r for r in caplog.records
             if "KONFIGURATIONSFEHLER" in r.getMessage()]
    assert len(hints) == 1  # genau EIN Hinweis
    assert "preflight" in hints[0].getMessage()
    assert "POLY_SIGNATURE_TYPE=3" in hints[0].getMessage()


def test_livebroker_fatal_sperre_reconciled_weiter():
    # Die Sperre stoppt nur NEUE Orders — Fills bereits ruhender Orders
    # werden weiterhin nachgebucht (Portfolio-Ehrlichkeit).
    client = FakeClobClient()
    broker = make_live_broker(client)
    pf = Portfolio(cash=100.0)
    broker.execute([Signal(token_id="tok", side="BUY", price=0.50, size=10,
                           reason="MM")], {}, pf)
    broker._fatal_reject = "maker address not allowed"
    client.orders["oid1"] = {"status": "matched", "size_matched": "10",
                             "price": "0.50"}
    assert broker.execute([Signal(token_id="neu", side="BUY", price=0.50,
                                  size=10, reason="MM")], {}, pf) == 1
    assert pf.positions["tok"].shares == pytest.approx(10)
    assert client.posted == ["tok"]  # "neu" wurde nie versucht


def test_livebroker_cooldown_bein_bricht_gruppe_ab(monkeypatch):
    # Ein Bein im Cooldown -> die ganze Arb-Gruppe wird nicht (weiter) gesendet.
    client = FakeClobClient(reject_400={"t1": "not enough balance"})
    broker = make_live_broker(client, cooldown_s=60.0)
    pf = Portfolio(cash=100.0)
    now = time.time()
    monkeypatch.setattr("polybot.execution.time.time", lambda: now)
    broker.execute([arb_leg("t1", group="g1")], {}, pf)
    assert client.posted == ["t1"]
    # Neuer Tick, gleiche Gruppe: t1 ist blockiert -> t2 darf nicht mehr raus.
    broker.execute([arb_leg("t1", group="g2"), arb_leg("t2", group="g2")], {}, pf)
    assert client.posted == ["t1"]


# ---- PaperBroker ------------------------------------------------------------

def test_paperbroker_fill_nur_gegen_buchliquiditaet():
    # bestehendes Verhalten abgesichert: BUY füllt nur, was im Ask liegt
    broker = PaperBroker()
    pf = Portfolio()
    book = OrderBook(token_id="tok", bids=[], asks=[Level(0.50, 7)])
    sig = Signal(token_id="tok", side="BUY", price=0.50, size=10, reason="test")
    assert broker.execute([sig], {"tok": book}, pf) == 1
    assert pf.positions["tok"].shares == pytest.approx(7)


def test_paperbroker_fuellt_zu_level_preisen():
    # Regressionstest: gefüllt wurde pauschal zum Limitpreis, obwohl
    # Liquidität günstiger im Buch lag — die Simulation überzahlte.
    broker = PaperBroker()
    pf = Portfolio(cash=100.0)
    book = OrderBook(token_id="tok", bids=[],
                     asks=[Level(0.50, 5), Level(0.52, 5), Level(0.60, 5)])
    sig = Signal(token_id="tok", side="BUY", price=0.55, size=10, reason="test")
    assert broker.execute([sig], {"tok": book}, pf) == 1
    # 5 @0.50 + 5 @0.52 = 5.10 USDC (VWAP 0.51), nicht 10 @0.55 = 5.50
    assert pf.positions["tok"].shares == pytest.approx(10)
    assert pf.cash == pytest.approx(100.0 - 5.10)
    assert pf.fills[-1].price == pytest.approx(0.51)


def test_paperbroker_signale_teilen_sich_buchliquiditaet():
    # Regressionstest: zwei Signale auf dasselbe Token konsumierten dieselbe
    # Buchliquidität doppelt (available je Signal frisch aus dem Snapshot).
    broker = PaperBroker()
    pf = Portfolio(cash=100.0)
    book = OrderBook(token_id="tok", bids=[], asks=[Level(0.50, 10)])
    sig = Signal(token_id="tok", side="BUY", price=0.50, size=7, reason="test")
    assert broker.execute([sig, sig], {"tok": book}, pf) == 2
    assert pf.positions["tok"].shares == pytest.approx(10)  # 7 + 3, nicht 7 + 7


def test_paperbroker_kappt_kauf_am_cash():
    # Regressionstest: Cash konnte unbegrenzt negativ werden (Paper-Margin).
    broker = PaperBroker()
    pf = Portfolio(cash=5.0)
    book = OrderBook(token_id="tok", bids=[], asks=[Level(0.50, 100)])
    sig = Signal(token_id="tok", side="BUY", price=0.50, size=100, reason="test")
    assert broker.execute([sig], {"tok": book}, pf) == 1
    assert pf.positions["tok"].shares == pytest.approx(10)  # 5 USDC / 0.50
    assert pf.cash == pytest.approx(0.0)


def test_paperbroker_kappt_sell_am_bestand():
    # Regressionstest: Oversell erzeugte früher Phantom-Gewinn im Portfolio.
    broker = PaperBroker()
    pf = Portfolio(cash=100.0)
    pf.apply_fill(Fill(ts=time.time(), token_id="tok", side="BUY",
                       price=0.50, size=5, reason=""))
    book = OrderBook(token_id="tok", bids=[Level(0.60, 100)], asks=[])
    sig = Signal(token_id="tok", side="SELL", price=0.60, size=50, reason="test")
    assert broker.execute([sig], {"tok": book}, pf) == 1
    assert pf.positions == {}  # nur die 5 gehaltenen Shares verkauft
    assert pf.cash == pytest.approx(100.0 - 2.5 + 3.0)


def test_paperbroker_verbucht_taker_gebuehr():
    # Regressionstest: Paper-Fills sind Taker-Fills, zahlten aber keine Gebühr.
    broker = PaperBroker()
    pf = Portfolio(cash=100.0)
    book = OrderBook(token_id="tok", bids=[], asks=[Level(0.50, 100)])
    sig = Signal(token_id="tok", side="BUY", price=0.50, size=10, reason="test")
    broker.execute([sig], {"tok": book}, pf, fee_rates={"tok": 0.04})
    # fee = 10 * 0.04 * 0.5 * (1-0.5) = 0.10 USDC
    assert pf.cash == pytest.approx(100.0 - 5.0 - 0.10)
    assert pf.realized_pnl == pytest.approx(-0.10)


def test_paperbroker_weist_fees_paid_im_portfolio_aus():
    # Gebühren müssen als eigenes Feld sichtbar sein, nicht nur im PnL versteckt.
    broker = PaperBroker()
    pf = Portfolio(cash=100.0)
    book = OrderBook(token_id="tok", bids=[], asks=[Level(0.50, 100)])
    sig = Signal(token_id="tok", side="BUY", price=0.50, size=10, reason="test")
    broker.execute([sig], {"tok": book}, pf, fee_rates={"tok": 0.04})
    assert pf.fees_paid == pytest.approx(0.10)  # 10 * 0.04 * 0.5 * 0.5


# ---- PaperBroker: FOK-Semantik für Signalgruppen ----------------------------

def make_book(token: str, ask: Level | None = None, bid: Level | None = None) -> OrderBook:
    return OrderBook(token_id=token, bids=[bid] if bid else [], asks=[ask] if ask else [])


def test_paperbroker_gruppe_fok_bucht_alle_beine():
    broker = PaperBroker()
    pf = Portfolio(cash=100.0)
    books = {"t1": make_book("t1", ask=Level(0.30, 20)),
             "t2": make_book("t2", ask=Level(0.60, 20))}
    fills = broker.execute([arb_leg("t1"), arb_leg("t2", price=0.60)], books, pf)
    assert fills == 2
    assert pf.positions["t1"].shares == pytest.approx(10)
    assert pf.positions["t2"].shares == pytest.approx(10)
    assert pf.cash == pytest.approx(100.0 - 3.0 - 6.0)


def test_paperbroker_gruppe_fok_kein_bein_bei_zu_wenig_liquiditaet():
    # FOK: Bein t2 ist nur teilweise füllbar -> KEIN Bein der Gruppe füllt,
    # sonst bliebe ein halber Arb als offene, ungehedgte Wette stehen.
    broker = PaperBroker()
    pf = Portfolio(cash=100.0)
    books = {"t1": make_book("t1", ask=Level(0.30, 20)),
             "t2": make_book("t2", ask=Level(0.60, 4))}  # nur 4 von 10 im Buch
    fills = broker.execute([arb_leg("t1"), arb_leg("t2", price=0.60)], books, pf)
    assert fills == 0
    assert pf.positions == {}
    assert pf.cash == pytest.approx(100.0)


def test_paperbroker_gruppe_fok_kein_bein_bei_zu_wenig_cash():
    # Bein 1 passt ins Cash, Bein 2 nicht mehr vollständig -> Gruppe weg.
    broker = PaperBroker()
    pf = Portfolio(cash=6.0)
    books = {"t1": make_book("t1", ask=Level(0.50, 100)),
             "t2": make_book("t2", ask=Level(0.50, 100))}
    fills = broker.execute([arb_leg("t1", price=0.50), arb_leg("t2", price=0.50)],
                           books, pf)
    assert fills == 0
    assert pf.positions == {}
    assert pf.cash == pytest.approx(6.0)


def test_paperbroker_gruppe_fok_sell_ueber_bestand_verwirft_gruppe():
    # SELL-Bein über den Bestand hinaus ist nicht voll füllbar (kein Shorting).
    broker = PaperBroker()
    pf = Portfolio(cash=100.0)
    pf.apply_fill(Fill(ts=time.time(), token_id="t1", side="BUY",
                       price=0.50, size=5, reason=""))
    books = {"t1": make_book("t1", bid=Level(0.60, 100)),
             "t2": make_book("t2", ask=Level(0.30, 100))}
    group = [Signal(token_id="t1", side="SELL", price=0.60, size=10,
                    reason="arb", group="g1"),
             arb_leg("t2")]
    assert broker.execute(group, books, pf) == 0
    assert pf.positions["t1"].shares == pytest.approx(5)  # nichts verkauft


def test_paperbroker_gescheiterte_gruppe_gibt_liquiditaet_und_cash_frei():
    # Das tentativ verplante Bein t1 der gescheiterten Gruppe darf die
    # Liquidität/das Cash für spätere unabhängige Signale nicht blockieren.
    broker = PaperBroker()
    pf = Portfolio(cash=100.0)
    books = {"t1": make_book("t1", ask=Level(0.30, 10))}  # t2 fehlt -> Gruppe weg
    signals = [arb_leg("t1"), arb_leg("t2"),
               Signal(token_id="t1", side="BUY", price=0.30, size=10, reason="solo")]
    assert broker.execute(signals, books, pf) == 1
    assert pf.positions["t1"].shares == pytest.approx(10)  # volle 10 fürs Solo-Signal
    assert pf.cash == pytest.approx(100.0 - 3.0)


# ---- PaperBroker: ruhende Orders (Maker-Simulation) -------------------------

def test_paperbroker_ruhende_order_fuellt_bei_preisdurchgang():
    # Nicht-marketable BUY ruht; erst wenn der beste Ask auf/unter den
    # Orderpreis fällt, füllt sie — zum ORDERpreis, als Maker (Gebühr 0).
    broker = PaperBroker()
    pf = Portfolio(cash=100.0)
    book = OrderBook(token_id="tok", bids=[Level(0.40, 50)], asks=[Level(0.50, 50)])
    sig = Signal(token_id="tok", side="BUY", price=0.45, size=10, reason="MM Bid")
    assert broker.execute([sig], {"tok": book}, pf) == 0
    assert len(pf.resting_orders) == 1
    assert pf.reserved_cash == pytest.approx(4.5)
    assert pf.cash == pytest.approx(100.0)  # reserviert, noch nicht abgebucht

    # Kein Preisdurchgang -> kein Fill (Ask weiterhin über dem Orderpreis)
    assert broker.execute([], {"tok": book}, pf) == 0
    assert pf.positions == {}
    assert len(pf.resting_orders) == 1

    # Ask fällt unter den Orderpreis -> Maker-Fill zum Orderpreis, Gebühr 0;
    # die bekannte Taker-Rate des Tokens bringt stattdessen ein Rebate
    # (20% der Taker-Fee, siehe MAKER_REBATE_SHARE).
    crossed = OrderBook(token_id="tok", bids=[Level(0.40, 50)], asks=[Level(0.44, 50)])
    assert broker.execute([], {"tok": crossed}, pf, fee_rates={"tok": 0.07}) == 1
    assert pf.positions["tok"].shares == pytest.approx(10)
    rebate = 10 * (0.2 * 0.07) * 0.45 * (1 - 0.45)
    assert pf.cash == pytest.approx(100.0 - 4.5 + rebate)
    assert pf.rebates_earned == pytest.approx(rebate)
    assert pf.fills[-1].price == pytest.approx(0.45)
    assert pf.fills[-1].fee == 0.0  # Maker zahlen keine Taker-Gebühr
    assert pf.resting_orders == []
    assert pf.reserved_cash == pytest.approx(0.0)


def test_paperbroker_ruhende_sell_fuellt_bei_preisdurchgang():
    broker = PaperBroker()
    pf = Portfolio(cash=100.0)
    pf.apply_fill(Fill(ts=time.time(), token_id="tok", side="BUY",
                       price=0.50, size=10, reason=""))
    # Ask-Quote 0.60 ist nicht marketable (bester Bid 0.50) -> ruht
    book = OrderBook(token_id="tok", bids=[Level(0.50, 50)], asks=[Level(0.62, 50)])
    sig = Signal(token_id="tok", side="SELL", price=0.60, size=10, reason="MM Ask")
    assert broker.execute([sig], {"tok": book}, pf) == 0
    assert len(pf.resting_orders) == 1
    assert pf.positions["tok"].shares == pytest.approx(10)  # noch nichts verkauft

    # Bid steigt über den Orderpreis -> Maker-Fill zum Orderpreis
    crossed = OrderBook(token_id="tok", bids=[Level(0.61, 50)], asks=[Level(0.62, 50)])
    assert broker.execute([], {"tok": crossed}, pf) == 1
    assert pf.positions == {}
    assert pf.cash == pytest.approx(100.0 - 5.0 + 6.0)
    assert pf.fills[-1].price == pytest.approx(0.60)
    assert pf.fills[-1].fee == 0.0


def test_paperbroker_replace_ersetzt_ruhende_order():
    # Neue replace-Quote ersetzt die alte ruhende Order desselben Tokens —
    # Quotes dürfen sich nicht stapeln (Semantik wie LiveBroker-Cancel).
    broker = PaperBroker()
    pf = Portfolio(cash=100.0)
    book = OrderBook(token_id="tok", bids=[Level(0.40, 50)], asks=[Level(0.50, 50)])
    q1 = Signal(token_id="tok", side="BUY", price=0.45, size=10,
                reason="MM Bid", replace=True)
    broker.execute([q1], {"tok": book}, pf)
    q2 = Signal(token_id="tok", side="BUY", price=0.44, size=10,
                reason="MM Bid", replace=True)
    broker.execute([q2], {"tok": book}, pf)
    assert len(pf.resting_orders) == 1
    assert pf.resting_orders[0].price == pytest.approx(0.44)
    assert pf.reserved_cash == pytest.approx(4.4)  # alte Reservierung wieder frei


def test_paperbroker_reserviertes_cash_deckt_keine_neuen_kaeufe():
    # Das von einer ruhenden BUY reservierte Cash steht Sofort-Orders
    # nicht zur Verfügung — sonst wäre der Maker-Fill später ungedeckt.
    broker = PaperBroker()
    pf = Portfolio(cash=10.0)
    book_a = OrderBook(token_id="a", bids=[], asks=[Level(0.90, 100)])
    quote = Signal(token_id="a", side="BUY", price=0.80, size=10, reason="MM Bid")
    broker.execute([quote], {"a": book_a}, pf)
    assert pf.reserved_cash == pytest.approx(8.0)

    book_b = OrderBook(token_id="b", bids=[], asks=[Level(0.50, 100)])
    buy = Signal(token_id="b", side="BUY", price=0.50, size=100, reason="test")
    broker.execute([buy], {"a": book_a, "b": book_b}, pf)
    assert pf.positions["b"].shares == pytest.approx(4.0)  # nur 2 USDC frei, nicht 10
    assert pf.cash - pf.reserved_cash == pytest.approx(0.0)

    # Der spätere Maker-Fill der ruhenden Order ist voll gedeckt
    crossed = OrderBook(token_id="a", bids=[], asks=[Level(0.75, 100)])
    assert broker.execute([], {"a": crossed}, pf) == 1
    assert pf.positions["a"].shares == pytest.approx(10)
    assert pf.cash == pytest.approx(0.0)


def test_paperbroker_ruhende_buy_wird_am_cash_gekappt():
    # Ohne Deckung keine Reservierung: nur der bezahlbare Teil ruht.
    broker = PaperBroker()
    pf = Portfolio(cash=4.0)
    book = OrderBook(token_id="tok", bids=[], asks=[Level(0.90, 100)])
    sig = Signal(token_id="tok", side="BUY", price=0.80, size=10, reason="MM Bid")
    broker.execute([sig], {"tok": book}, pf)
    assert pf.resting_orders[0].size == pytest.approx(5.0)  # 4 USDC / 0.80
    assert pf.reserved_cash == pytest.approx(4.0)


def test_paperbroker_rebate_wird_gutgeschrieben():
    # Konfigurierte Rebate-Rate wird auf Maker-Fills gutgeschrieben und
    # als rebates_earned ausgewiesen (Default 0.0 = aus).
    cfg = BotConfig()
    cfg.risk.taker_fee_rate = 0.0
    cfg.strategy.maker_rebate_rate = 0.01
    cfg.strategy.paper_fill_delay_ticks = 0  # hier zählt die Rebate-Logik
    broker = PaperBroker(cfg)
    pf = Portfolio(cash=100.0)
    book = OrderBook(token_id="tok", bids=[], asks=[Level(0.60, 50)])
    sig = Signal(token_id="tok", side="BUY", price=0.50, size=10, reason="MM Bid")
    assert broker.execute([sig], {"tok": book}, pf) == 0

    crossed = OrderBook(token_id="tok", bids=[], asks=[Level(0.50, 50)])
    assert broker.execute([], {"tok": crossed}, pf) == 1
    rebate = 10 * 0.01 * 0.50 * (1 - 0.50)  # rate * p * (1-p) pro Share
    assert pf.rebates_earned == pytest.approx(rebate)
    assert pf.cash == pytest.approx(100.0 - 5.0 + rebate)
    assert pf.realized_pnl == pytest.approx(rebate)


def test_paperbroker_ohne_rebate_rate_keine_gutschrift():
    # Default 0.0: konservativ kein simulierter Rebate-Verdienst.
    broker = PaperBroker()
    pf = Portfolio(cash=100.0)
    book = OrderBook(token_id="tok", bids=[], asks=[Level(0.60, 50)])
    broker.execute([Signal(token_id="tok", side="BUY", price=0.50, size=10,
                           reason="MM Bid")], {"tok": book}, pf)
    crossed = OrderBook(token_id="tok", bids=[], asks=[Level(0.50, 50)])
    broker.execute([], {"tok": crossed}, pf)
    assert pf.rebates_earned == 0.0
    assert pf.cash == pytest.approx(100.0 - 5.0)


def test_paperbroker_rebate_tokenspezifisch_20_prozent_der_taker_fee():
    # Polymarket zahlt Makern 20-25% der Taker-Fees des Marktes: bei bekannter
    # Taker-Rate gilt rebate_rate = 0.2 * taker_fee_rate(token) — die globale
    # maker_rebate_rate ist dann irrelevant (nur Fallback).
    cfg = BotConfig()
    cfg.strategy.maker_rebate_rate = 0.0   # Fallback aus — Rebate kommt trotzdem
    cfg.strategy.paper_fill_delay_ticks = 0  # hier zählt die Rebate-Logik
    broker = PaperBroker(cfg)
    pf = Portfolio(cash=100.0)
    fee_rates = {"tok": 0.05}
    book = OrderBook(token_id="tok", bids=[], asks=[Level(0.60, 50)])
    sig = Signal(token_id="tok", side="BUY", price=0.50, size=10, reason="MM Bid")
    assert broker.execute([sig], {"tok": book}, pf, fee_rates) == 0

    crossed = OrderBook(token_id="tok", bids=[], asks=[Level(0.50, 50)])
    assert broker.execute([], {"tok": crossed}, pf, fee_rates) == 1
    rebate = 10 * (0.2 * 0.05) * 0.50 * (1 - 0.50)  # 0.2*taker_rate * p*(1-p)
    assert pf.rebates_earned == pytest.approx(rebate)
    assert pf.cash == pytest.approx(100.0 - 5.0 + rebate)


def test_paperbroker_rebate_fallback_nur_ohne_bekannte_fee_rate():
    # Ist die Taker-Rate des Tokens bekannt, übersteuert sie den Fallback —
    # auch wenn der Fallback höher wäre (kein Rosinenpicken).
    cfg = BotConfig()
    cfg.strategy.maker_rebate_rate = 0.01
    broker = PaperBroker(cfg)
    assert broker._rebate_rate("tok", {"tok": 0.02}) == pytest.approx(0.2 * 0.02)
    assert broker._rebate_rate("tok", {}) == pytest.approx(0.01)
    # Bekannte Rate 0.0 (gebührenfreie Kategorie) heißt auch Rebate 0.0
    assert broker._rebate_rate("tok", {"tok": 0.0}) == 0.0


def test_paperbroker_ruhende_teilfuellung_bleibt_ruhen():
    # Reicht die Gegenliquidität nicht, füllt nur ein Teil — der Rest ruht.
    broker = PaperBroker()
    pf = Portfolio(cash=100.0)
    book = OrderBook(token_id="tok", bids=[], asks=[Level(0.60, 50)])
    broker.execute([Signal(token_id="tok", side="BUY", price=0.50, size=10,
                           reason="MM Bid")], {"tok": book}, pf)
    crossed = OrderBook(token_id="tok", bids=[], asks=[Level(0.48, 4)])
    assert broker.execute([], {"tok": crossed}, pf) == 1
    assert pf.positions["tok"].shares == pytest.approx(4)
    assert pf.resting_orders[0].size == pytest.approx(6)
    assert pf.reserved_cash == pytest.approx(6 * 0.50)


def test_ruhende_orders_und_rebates_ueberleben_neustart(tmp_path):
    # Ruhende Orders und Rebates sind Teil des persistierten Paper-States.
    pf = Portfolio(cash=100.0)
    pf.resting_orders.append(RestingOrder(
        ts=1.0, token_id="tok", side="BUY", price=0.45, size=10,
        reason="MM Bid", market_question="Frage?"))
    pf.rebates_earned = 1.23
    path = tmp_path / "state.json"
    pf.save(path)
    loaded = Portfolio.load(path)
    assert loaded.rebates_earned == pytest.approx(1.23)
    assert len(loaded.resting_orders) == 1
    assert loaded.resting_orders[0].price == pytest.approx(0.45)
    assert loaded.reserved_cash == pytest.approx(4.5)


# ---- PaperBroker: Latenz-Verzug (paper_fill_delay_ticks) --------------------

def delayed_broker(delay: int = 1) -> PaperBroker:
    # Gebührenfrei, damit die Tests nur den Verzug messen; der Delay kommt
    # bewusst über die Config-Plumbing (strategy.paper_fill_delay_ticks).
    cfg = BotConfig()
    cfg.risk.taker_fee_rate = 0.0
    cfg.strategy.paper_fill_delay_ticks = delay
    return PaperBroker(cfg)


def test_paperbroker_delay_default_aus_config_ohne_config_null():
    # Config-Default 1 (ehrlich); ohne Config (Tests) 0 = altes Verhalten.
    assert PaperBroker(BotConfig()).fill_delay_ticks == 1
    assert PaperBroker().fill_delay_ticks == 0


def test_paperbroker_delay_fuellt_erst_im_folgetick():
    # Live vergehen ~250ms zwischen Signal und Order-Ankunft — der Fill darf
    # deshalb erst im NÄCHSTEN execute() gegen das dann aktuelle Buch passieren.
    broker = delayed_broker()
    pf = Portfolio(cash=100.0)
    book = OrderBook(token_id="tok", bids=[], asks=[Level(0.50, 100)])
    sig = Signal(token_id="tok", side="BUY", price=0.50, size=10, reason="test")
    assert broker.execute([sig], {"tok": book}, pf) == 0
    assert pf.positions == {}
    assert pf.cash == pytest.approx(100.0)
    # Folge-Tick (Buch unverändert): jetzt füllt das Signal.
    assert broker.execute([], {"tok": book}, pf) == 1
    assert pf.positions["tok"].shares == pytest.approx(10)
    assert pf.cash == pytest.approx(100.0 - 5.0)
    assert broker._pending_signals == []


def test_paperbroker_delay_preis_weggelaufen_kein_fill():
    # Zwischen Signal und "Order-Ankunft" ist der Ask über das Limit gestiegen
    # -> kein Taker-Fill mehr; der ungruppierte Rest ruht als GTC-Quote.
    broker = delayed_broker()
    pf = Portfolio(cash=100.0)
    t0 = OrderBook(token_id="tok", bids=[], asks=[Level(0.50, 100)])
    sig = Signal(token_id="tok", side="BUY", price=0.50, size=10, reason="test")
    assert broker.execute([sig], {"tok": t0}, pf) == 0
    t1 = OrderBook(token_id="tok", bids=[], asks=[Level(0.52, 100)])
    assert broker.execute([], {"tok": t1}, pf) == 0
    assert pf.positions == {}
    assert len(pf.resting_orders) == 1  # GTC-Semantik wie ohne Verzug


def test_paperbroker_delay_ausgeduenntes_buch_kleinerer_fill():
    # Die Buchliquidität ist zwischenzeitlich geschrumpft -> es füllt nur,
    # was im NEUEN Buch liegt, nicht die Snapshot-Größe von der Signalerzeugung.
    broker = delayed_broker()
    pf = Portfolio(cash=100.0)
    t0 = OrderBook(token_id="tok", bids=[], asks=[Level(0.50, 100)])
    sig = Signal(token_id="tok", side="BUY", price=0.50, size=10, reason="test")
    assert broker.execute([sig], {"tok": t0}, pf) == 0
    t1 = OrderBook(token_id="tok", bids=[], asks=[Level(0.50, 4)])
    assert broker.execute([], {"tok": t1}, pf) == 1
    assert pf.positions["tok"].shares == pytest.approx(4)


def test_paperbroker_delay_fok_gruppe_fuellt_atomar_im_folgetick():
    broker = delayed_broker()
    pf = Portfolio(cash=100.0)
    books = {"t1": make_book("t1", ask=Level(0.30, 20)),
             "t2": make_book("t2", ask=Level(0.60, 20))}
    group = [arb_leg("t1"), arb_leg("t2", price=0.60)]
    assert broker.execute(group, books, pf) == 0
    assert pf.positions == {}
    # Folge-Tick: beide Beine füllen atomar gegen das dann aktuelle Buch.
    assert broker.execute([], books, pf) == 2
    assert pf.positions["t1"].shares == pytest.approx(10)
    assert pf.positions["t2"].shares == pytest.approx(10)
    assert pf.cash == pytest.approx(100.0 - 3.0 - 6.0)


def test_paperbroker_delay_fok_gruppe_kein_bein_bei_verschlechtertem_buch():
    # Beim Signal war die Gruppe voll füllbar; im Folge-Tick ist Bein t2
    # ausgedünnt -> FOK gegen das NEUE Buch: KEIN Bein füllt.
    broker = delayed_broker()
    pf = Portfolio(cash=100.0)
    books0 = {"t1": make_book("t1", ask=Level(0.30, 20)),
              "t2": make_book("t2", ask=Level(0.60, 20))}
    group = [arb_leg("t1"), arb_leg("t2", price=0.60)]
    assert broker.execute(group, books0, pf) == 0
    books1 = {"t1": make_book("t1", ask=Level(0.30, 20)),
              "t2": make_book("t2", ask=Level(0.60, 4))}  # nur noch 4 von 10
    assert broker.execute([], books1, pf) == 0
    assert pf.positions == {}
    assert pf.cash == pytest.approx(100.0)


def test_paperbroker_delay_null_fuellt_sofort_wie_bisher():
    # 0 = altes Verhalten (Vergleichsmessungen): Fill im selben Aufruf,
    # nichts landet in der Pending-Queue.
    broker = delayed_broker(0)
    pf = Portfolio(cash=100.0)
    book = OrderBook(token_id="tok", bids=[], asks=[Level(0.50, 100)])
    sig = Signal(token_id="tok", side="BUY", price=0.50, size=10, reason="test")
    assert broker.execute([sig], {"tok": book}, pf) == 1
    assert pf.positions["tok"].shares == pytest.approx(10)
    assert broker._pending_signals == []


def test_paperbroker_delay_ruhende_orders_fuellen_weiter_jeden_tick():
    # _match_resting läuft unabhängig vom Verzug in JEDEM execute():
    # eine bereits ruhende Quote füllt sofort beim Preisdurchgang.
    broker = delayed_broker()
    pf = Portfolio(cash=100.0)
    book = OrderBook(token_id="tok", bids=[Level(0.40, 50)], asks=[Level(0.50, 50)])
    quote = Signal(token_id="tok", side="BUY", price=0.45, size=10, reason="MM Bid")
    broker.execute([quote], {"tok": book}, pf)   # wartet in der Queue
    broker.execute([], {"tok": book}, pf)        # nicht marketable -> ruht jetzt
    assert len(pf.resting_orders) == 1
    crossed = OrderBook(token_id="tok", bids=[Level(0.40, 50)], asks=[Level(0.44, 50)])
    assert broker.execute([], {"tok": crossed}, pf) == 1
    assert pf.positions["tok"].shares == pytest.approx(10)


def test_paperbroker_fallback_fee_rate_aus_config():
    # Ohne abrufbare Fee-Rate gilt konservativ das konfigurierte Maximum.
    cfg = BotConfig()  # taker_fee_rate=0.07
    cfg.strategy.paper_fill_delay_ticks = 0  # hier zählt die Gebührenlogik
    broker = PaperBroker(cfg)
    pf = Portfolio(cash=100.0)
    book = OrderBook(token_id="tok", bids=[], asks=[Level(0.50, 100)])
    sig = Signal(token_id="tok", side="BUY", price=0.50, size=10, reason="test")
    broker.execute([sig], {"tok": book}, pf)
    assert pf.cash == pytest.approx(100.0 - 5.0 - 10 * 0.07 * 0.25)


# ---- Market-Order-Präzision (CLOB: "invalid amounts", 05.07.2026 live) ----------

def test_marketable_size_haelt_clob_praezision_ein():
    from polybot.execution import _marketable_size
    # BUY: Size*Preis darf max. 2 Nachkommastellen haben.
    assert _marketable_size(21.0, 0.43, "BUY") == pytest.approx(21.0)   # 9.03 ok
    assert _marketable_size(21.0, 0.435, "BUY") == pytest.approx(20.0)  # 9.135 -> 8.70
    assert _marketable_size(5.55, 0.31, "BUY") == pytest.approx(5.0)    # 1.7205 -> 1.55
    # SELL: Size*Preis darf max. 4 Nachkommastellen haben (lockerer).
    assert _marketable_size(21.0, 0.435, "SELL") == pytest.approx(21.0)
    # Keine gültige Size unterhalb -> 0 (Order wird verworfen statt abgelehnt).
    assert _marketable_size(1.0, 0.435, "BUY") == pytest.approx(0.0)
    assert _marketable_size(0.0, 0.43, "BUY") == pytest.approx(0.0)


def test_fok_gruppe_wird_auf_gemeinsame_konforme_size_quantisiert():
    client = FakeClobClient()
    broker = make_live_broker(client)
    pf = Portfolio(cash=1000.0)
    legs = [
        Signal(token_id="yes", side="BUY", price=0.43, size=10.5,
               reason="arb", group="g1"),
        Signal(token_id="no", side="BUY", price=0.57, size=10.5,
               reason="arb", group="g1"),
    ]
    books = {t: OrderBook(token_id=t, bids=[], asks=[Level(0.99, 100)])
             for t in ("yes", "no")}
    assert broker.execute(legs, books, pf) == 2
    # 10.5*0.43=4.5150 (4 NK) wäre abgelehnt worden -> beide Beine auf 10.0.
    assert client.created_sizes == [pytest.approx(10.0), pytest.approx(10.0)]


def test_fok_gruppe_ohne_konforme_size_wird_verworfen():
    client = FakeClobClient()
    broker = make_live_broker(client)
    pf = Portfolio(cash=1000.0)
    # Preis 0.435 gibt es bei Tick 0.01 nicht — der Fake-Client liefert Tick
    # 0.01, also quantisiert der Vorlauf auf 0.43: dafür ist 0.5 zu klein
    # (kleinste konforme Size ist 1.0 bei p=0.43).
    legs = [Signal(token_id="yes", side="BUY", price=0.43, size=0.5,
                   reason="arb", group="g1")]
    books = {"yes": OrderBook(token_id="yes", bids=[], asks=[Level(0.99, 100)])}
    assert broker.execute(legs, books, pf) == 0
    assert client.posted == []  # nichts gesendet, nichts abgelehnt


# ---- Delayed-Orders: size_matched ist die Wahrheit (erster Live-Trade 05.07.) --

def test_delayed_order_trotz_cancel_gefuellt_wird_gebucht():
    """Race verloren: Order matcht, obwohl der Bot sie cancelt. Der Status
    sagt "canceled", size_matched sagt 20 — gebucht werden muss der Fill."""
    client = FakeClobClient(statuses={"tok": "delayed"})
    broker = make_live_broker(client)
    pf = Portfolio(cash=100.0)
    # get_order liefert dauerhaft "live" -> Poll-Fenster läuft ab -> Cancel;
    # der Zustand danach zeigt canceled MIT gefüllter Größe.
    client.orders["oid1"] = {"status": "canceled", "size_matched": "20",
                             "price": "0.06"}
    sig = Signal(token_id="tok", side="BUY", price=0.06, size=20, reason="arb",
                 group="g1")
    book = OrderBook(token_id="tok", bids=[Level(0.05, 100)], asks=[Level(0.06, 100)])
    fills = broker.execute([sig], {"tok": book}, pf)
    assert fills == 1
    assert pf.positions["tok"].shares == pytest.approx(20)
    assert pf.cash == pytest.approx(100.0 - 1.20)


def test_delayed_order_ungefuellt_setzt_cooldown():
    client = FakeClobClient(statuses={"tok": "delayed"})
    broker = make_live_broker(client, cooldown_s=60)
    pf = Portfolio(cash=100.0)
    client.orders["oid1"] = {"status": "canceled", "size_matched": "0"}
    sig = Signal(token_id="tok", side="BUY", price=0.06, size=20, reason="arb",
                 group="g1")
    fills = broker.execute([sig], {}, pf)
    assert fills == 0
    assert pf.positions == {}
    assert broker._token_blocked("tok")  # kein sofortiges Neu-Feuern


# ---- Persistenter Liquiditätsverbrauch (Befund Agenten-Flotte 05.07.2026) ----


def test_paperbroker_gleiches_level_fuellt_nicht_doppelt_ueber_ticks():
    """DER Inflations-Fix: dasselbe stehende Ask-Level über viele Ticks
    darf nur EINMAL gekauft werden (Beleg: 74 identische Merges à +54.72)."""
    broker = PaperBroker()
    pf = Portfolio(cash=1_000.0)
    book = OrderBook(token_id="tok", bids=[], asks=[Level(0.50, 10)])
    sig = Signal(token_id="tok", side="BUY", price=0.50, size=10, reason="test")
    assert broker.execute([sig], {"tok": book}, pf) == 1
    for _ in range(5):  # 0.5s-Ticks mit unverändertem Buch
        assert broker.execute([sig], {"tok": book}, pf) == 0
    assert pf.positions["tok"].shares == pytest.approx(10)  # nicht 60


def test_paperbroker_neues_level_ist_neue_liquiditaet():
    broker = PaperBroker()
    pf = Portfolio(cash=1_000.0)
    sig = Signal(token_id="tok", side="BUY", price=0.51, size=10, reason="test")
    book1 = OrderBook(token_id="tok", bids=[], asks=[Level(0.50, 10)])
    assert broker.execute([sig], {"tok": book1}, pf) == 1
    # Level 0.50 verschwindet (Markt bewegt sich), neues Level 0.51 erscheint.
    book2 = OrderBook(token_id="tok", bids=[], asks=[Level(0.51, 10)])
    assert broker.execute([sig], {"tok": book2}, pf) == 1
    # Und wenn 0.50 später WIEDER auftaucht, ist das neue Liquidität.
    assert broker.execute([sig], {"tok": book1}, pf) == 1
    assert pf.positions["tok"].shares == pytest.approx(30)


def test_paperbroker_aufgestocktes_level_gibt_nur_den_zuwachs():
    broker = PaperBroker()
    pf = Portfolio(cash=1_000.0)
    sig = Signal(token_id="tok", side="BUY", price=0.50, size=100, reason="test")
    assert broker.execute(
        [sig], {"tok": OrderBook("tok", asks=[Level(0.50, 10)])}, pf) == 1
    # Jemand legt nach: Level zeigt jetzt 25 — wir haben 10 konsumiert,
    # verfügbar sind nur die 15 Zuwachs.
    assert broker.execute(
        [sig], {"tok": OrderBook("tok", asks=[Level(0.50, 25)])}, pf) == 1
    assert pf.positions["tok"].shares == pytest.approx(25)


def test_paperbroker_fok_rollback_gibt_levelverbrauch_frei():
    broker = PaperBroker()
    pf = Portfolio(cash=1_000.0)
    books = {
        "a": OrderBook("a", asks=[Level(0.50, 10)]),
        "b": OrderBook("b", asks=[Level(0.40, 2)]),   # Bein B nicht voll füllbar
    }
    grp = [Signal(token_id="a", side="BUY", price=0.50, size=10, reason="t", group="g"),
           Signal(token_id="b", side="BUY", price=0.40, size=10, reason="t", group="g")]
    assert broker.execute(grp, books, pf) == 0        # FOK: Gruppe verworfen
    # Der tentative Verbrauch auf Token a wurde zurückgegeben: ein
    # ungruppiertes Signal kann die vollen 10 Shares kaufen.
    solo = Signal(token_id="a", side="BUY", price=0.50, size=10, reason="t")
    assert broker.execute([solo], books, pf) == 1
    assert pf.positions["a"].shares == pytest.approx(10)


def test_paperbroker_maker_fill_verbraucht_gegenseite_nur_einmal():
    """Ruhende Quote gegen eine STEHENDE Gegenseite: füllt nur einmal,
    nicht bei jedem Tick erneut (dieselbe Inflation wie bei Taker-Fills)."""
    broker = PaperBroker()
    pf = Portfolio(cash=1_000.0)
    pf.resting_orders.append(RestingOrder(ts=0.0, token_id="tok", side="BUY",
                                          price=0.50, size=30, reason="MM Bid"))
    book = OrderBook(token_id="tok", bids=[], asks=[Level(0.49, 10)])
    assert broker.execute([], {"tok": book}, pf) == 1
    assert pf.positions["tok"].shares == pytest.approx(10)
    # Unverändertes Buch: die Rest-Quote (20) darf NICHT erneut füllen.
    assert broker.execute([], {"tok": book}, pf) == 0
    assert pf.positions["tok"].shares == pytest.approx(10)


# ---- cancel_all bei Start/Stop (Befund Agenten-Flotte 05.07.2026) ------------


def test_livebroker_init_cancelt_alt_orders(monkeypatch):
    """Orders eines abgestürzten Vorgänger-Prozesses leben auf der Börse
    weiter — der Start muss mit einem sauberen Orderbuch beginnen."""
    import py_clob_client_v2.client as clob_mod

    monkeypatch.setattr(clob_mod, "ClobClient", _CapturingClobClient)
    _CapturingClobClient.cancel_all_calls = 0
    LiveBroker(_live_cfg(None, 3))
    assert _CapturingClobClient.cancel_all_calls == 1


def test_cancel_all_orders_leert_tracking():
    client = FakeClobClient()
    broker = make_live_broker(client)
    broker._pending["oid1"] = object()
    broker._open_orders["tok"] = ["oid1"]
    assert broker.cancel_all_orders("Test") is True
    assert client.cancel_all_calls == 1
    assert broker._pending == {} and broker._open_orders == {}


def test_cancel_all_orders_wirft_nie(caplog):
    import logging

    class BoomClient(FakeClobClient):
        def cancel_all(self):
            raise RuntimeError("CLOB down")

    broker = make_live_broker(BoomClient())
    with caplog.at_level(logging.ERROR):
        assert broker.cancel_all_orders("Test") is False
    assert any("VON HAND" in r.message for r in caplog.records)


# ---- Verifikations-Flotte Runde 2: Lifecycle-Befunde 9/13/15/21/28/33 --------


def test_cancel_all_reconciled_vor_und_nach_dem_cancel():
    """Befund 13/25/31: Fills der letzten Sekunden dürfen beim Prozessende
    nicht verloren gehen — _pending wird erst nach finalem Reconcile geleert."""
    from polybot.execution import _PendingOrder

    client = FakeClobClient()
    client.orders["oid1"] = {"status": "canceled", "size_matched": "7",
                             "price": "0.5"}
    broker = make_live_broker(client)
    broker._pending["oid1"] = _PendingOrder(order_id="oid1", token_id="tok",
                                            side="BUY", price=0.5, reason="r")
    pf = Portfolio(cash=100.0)
    assert broker.cancel_all_orders("Test", pf) is True
    assert pf.positions["tok"].shares == pytest.approx(7)   # nachgebucht
    assert broker._pending == {}


def test_boersenfill_wird_forciert_gebucht_statt_verworfen():
    """Befund 21/29: Chain-Fill kollidiert mit Buchhaltung (z.B. SELL nach
    externem Verkauf) -> kappen/forcieren statt verlieren."""
    from polybot.execution import LiveBroker
    from polybot.portfolio import Fill

    pf = Portfolio(cash=100.0)
    pf.apply_fill(Fill(ts=0, token_id="tok", side="BUY", price=0.5, size=5,
                       reason="r"))
    # Börse bestätigt SELL 8 > Bestand 5: normal würde apply_fill werfen.
    LiveBroker._apply_fill_safe(pf, Fill(ts=1, token_id="tok", side="SELL",
                                         price=0.6, size=8, reason="r"))
    assert "tok" not in pf.positions          # Bestand sauber ausgebucht
    assert pf.cash == pytest.approx(100.0 - 2.5 + 5 * 0.6)


def test_delayed_cancel_fehlschlag_haelt_tracking():
    """Befund 28: Cancel scheitert, Order lebt evtl. weiter — sie bleibt im
    Reconcile-Tracking statt vergessen zu werden."""
    class NoCancelClient(FakeClobClient):
        def cancel_order(self, payload):
            raise RuntimeError("cancel down")

    client = NoCancelClient(statuses={"tok": "delayed"})
    broker = make_live_broker(client)
    broker.DELAY_POLL_ATTEMPTS = 1
    pf = Portfolio(cash=100.0)
    sig = Signal(token_id="tok", side="BUY", price=0.5, size=10,
                 reason="r", group="g")
    broker.execute([sig], {}, pf)
    assert len(broker._pending) == 1          # Order wird weiter beobachtet


def test_gc_verschont_verbrauch_bei_synthetischem_buch():
    """Befund 9: Grösse-0-Bücher (synthetisches Top-of-Book, Flicker) sind
    kein Markt-Update — der Verbrauch bleibt, die Inflation kehrt nicht
    zurück."""
    broker = PaperBroker()
    pf = Portfolio(cash=1_000.0)
    real = OrderBook("tok", asks=[Level(0.5, 10)])
    sig = Signal(token_id="tok", side="BUY", price=0.5, size=10, reason="t")
    assert broker.execute([sig], {"tok": real}, pf) == 1
    synth = OrderBook("tok", asks=[Level(0.5, 0.0)])   # synthetisch
    broker.execute([], {"tok": synth}, pf)             # GC-Durchlauf
    assert broker.execute([sig], {"tok": real}, pf) == 0  # Verbrauch lebt


def test_init_cancel_fehlschlag_wird_pro_tick_nachgeholt():
    """Befund 15: Bot startet trotz cancel_all-Fehlschlag, holt das Cancel
    aber bei jedem execute() nach, bis es gelingt."""
    class FlakyClient(FakeClobClient):
        def __init__(self):
            super().__init__()
            self.attempts = 0

        def cancel_all(self):
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("CLOB down")

    client = FlakyClient()
    broker = make_live_broker(client)
    broker._start_cancel_pending = True       # wie nach gescheitertem Init
    pf = Portfolio(cash=100.0)
    broker.execute([], {}, pf)
    assert client.attempts >= 1
    broker.execute([], {}, pf)
    assert broker._start_cancel_pending is False


# ---- Keep-Alive-Heartbeat (Flotten-Befund 06.07.2026) -----------------------

def test_heartbeat_pingt_und_stoppt_sauber(monkeypatch):
    import py_clob_client_v2.client as clob_mod

    monkeypatch.setattr(clob_mod, "ClobClient", _CapturingClobClient)
    b = LiveBroker(_live_cfg(None, 3))
    b.HEARTBEAT_S = 0.01
    import time as _t
    _t.sleep(0.05)                       # ein paar Pings laufen lassen
    b.stop_heartbeat()
    assert b._hb_stop.is_set()
    b._hb_thread.join(timeout=1)
    assert not b._hb_thread.is_alive()   # Thread endet sauber
