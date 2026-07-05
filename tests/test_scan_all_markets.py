"""Tests für den Vollmarkt-Scan (strategy.scan_all_markets) und
das konfigurierbare Paper-Start-Kapital (risk.paper_start_cash)."""

import pytest

import polybot.main as main
from polybot.config import BotConfig
from polybot.data.gamma import Market
from polybot.portfolio import Portfolio


def market(i: int, volume_24h: float = 10_000.0, liquidity: float = 50_000.0) -> Market:
    return Market(condition_id=f"c{i}", question=f"Frage {i}?", slug=f"frage-{i}",
                  yes_token=f"yes{i}", no_token=f"no{i}",
                  liquidity=liquidity, volume_24h=volume_24h, neg_risk=False)


class FakeGamma:
    """Fake mit BEIDEN Markt-Endpunkten — zeichnet auf, welcher genutzt wird."""

    def __init__(self, markets: list[Market]):
        self.markets = markets
        self.all_calls: list[tuple[float, float]] = []
        self.top_calls: list[tuple[float, int]] = []

    def active_markets(self, min_liquidity=0.0, limit=500):
        self.top_calls.append((min_liquidity, limit))
        return self.markets

    def all_active_markets(self, min_liquidity=0.0, min_volume=0.0):
        self.all_calls.append((min_liquidity, min_volume))
        return self.markets

    def negrisk_events(self, min_liquidity=0.0, limit=200):
        return {}


class FakeBooks:
    """Nur volle Bücher (kein get_top_prices) -> _load_books fällt zurück."""

    def __init__(self):
        self.book_requests: list[list[str]] = []

    def get_books(self, token_ids):
        self.book_requests.append(sorted(token_ids))
        return {}


class FakeTwoStageBooks(FakeBooks):
    """Mit Batch-Top-of-Book: der Zweistufen-Scan greift."""

    def __init__(self, top: dict[str, tuple[float | None, float | None]]):
        super().__init__()
        self.top = top
        self.top_requests: list[list[str]] = []

    def get_top_prices(self, token_ids):
        self.top_requests.append(sorted(token_ids))
        return self.top


# ---- Config-Felder ----------------------------------------------------------

def test_scan_all_markets_default_aus_und_yaml_ladbar(tmp_path):
    assert BotConfig().strategy.scan_all_markets is False
    p = tmp_path / "config.yaml"
    p.write_text("strategy:\n  scan_all_markets: true\n")
    assert BotConfig.load(p).strategy.scan_all_markets is True


def test_paper_start_cash_default_und_yaml_ladbar(tmp_path):
    assert BotConfig().risk.paper_start_cash == 1000.0
    p = tmp_path / "config.yaml"
    p.write_text("risk:\n  paper_start_cash: 100000\n")
    assert BotConfig.load(p).risk.paper_start_cash == 100_000.0


@pytest.mark.parametrize("value", ["0", "-500"])
def test_unsinniges_paper_start_cash_wird_abgewiesen(tmp_path, value):
    # Ohne Start-Cash kann der Paper-Bot nichts kaufen — Fehlkonfiguration
    # soll beim Start auffallen, nicht als stiller Dauerläufer ohne Fills.
    p = tmp_path / "config.yaml"
    p.write_text(f"risk:\n  paper_start_cash: {value}\n")
    with pytest.raises(SystemExit, match="paper_start_cash"):
        BotConfig.load(p)


# ---- build_snapshot mit scan_all_markets ------------------------------------

def test_scan_all_nutzt_vollscan_ohne_max_markets_deckel():
    # max_markets darf beim Vollscan NICHT kappen; der 24h-Volumen-Filter
    # läuft weiterhin clientseitig (Gamma filtert nur die Obermenge).
    cfg = BotConfig()
    cfg.strategy.scan_all_markets = True
    cfg.strategy.max_markets = 2
    cfg.strategy.min_volume_24h_usdc = 500.0
    gamma = FakeGamma([market(1), market(2), market(3),
                       market(4, volume_24h=100.0)])  # fällt dem 24h-Filter zum Opfer
    snap = main.build_snapshot(cfg, gamma, FakeBooks())
    assert [m.condition_id for m in snap.markets] == ["c1", "c2", "c3"]
    # Vollscan-Endpunkt mit Liquiditäts- UND Volumen-Obermenge, Top-N unbenutzt:
    assert gamma.all_calls == [(cfg.strategy.min_liquidity_usdc, 500.0)]
    assert gamma.top_calls == []


def test_scan_all_false_bleibt_beim_top_n_pfad():
    cfg = BotConfig()
    cfg.strategy.max_markets = 2
    gamma = FakeGamma([market(1), market(2), market(3)])
    snap = main.build_snapshot(cfg, gamma, FakeBooks())
    assert [m.condition_id for m in snap.markets] == ["c1", "c2"]
    assert gamma.all_calls == []
    assert gamma.top_calls == [(cfg.strategy.min_liquidity_usdc, 4)]


def test_scan_all_laedt_buecher_zweistufig_nur_fuer_kandidaten():
    # Stufe 1: Batch-Top-of-Book für ALLE Tokens; Stufe 2: volle Bücher nur
    # für Märkte mit Arb-Verdacht (YES-Ask + NO-Ask < 1 + Puffer). Markt 2
    # summiert auf 1.10 > 1.02 -> kein Kandidat, bekommt aber ein
    # synthetisches Top-of-Book für Marks/Kill-Switch.
    cfg = BotConfig()
    cfg.strategy.scan_all_markets = True
    gamma = FakeGamma([market(1), market(2)])
    books = FakeTwoStageBooks({
        "yes1": (0.40, 0.45), "no1": (0.50, 0.54),   # 0.45+0.54=0.99 -> Kandidat
        "yes2": (0.50, 0.55), "no2": (0.50, 0.55),   # 1.10 -> kein Kandidat
    })
    snap = main.build_snapshot(cfg, gamma, books)
    assert books.top_requests == [["no1", "no2", "yes1", "yes2"]]
    assert books.book_requests == [["no1", "yes1"]]
    # Nicht-Kandidaten: synthetisches Top-of-Book mit Größe 0
    assert snap.books["yes2"].best_ask.price == 0.55
    assert snap.books["yes2"].best_ask.size == 0.0


def test_scan_all_mit_market_making_laedt_nur_mm_kandidaten_voll():
    # Aktives market_making darf NICHT mehr auf den Voll-Fallback kippen
    # (alle Bücher laden = Tick massiv langsamer): zusätzlich zu den
    # Arb-Kandidaten werden nur die YES-Tokens der Top-mm_max_markets
    # nach 24h-Volumen voll geladen.
    cfg = BotConfig()
    cfg.strategy.scan_all_markets = True
    cfg.strategy.enabled = ["complement_arb", "market_making"]
    cfg.strategy.mm_max_markets = 1
    gamma = FakeGamma([market(1, volume_24h=90_000), market(2, volume_24h=10_000),
                       market(3, volume_24h=50_000)])
    books = FakeTwoStageBooks({
        # kein Markt ist Arb-Kandidat (alle Summen > 1.02) ...
        "yes1": (0.50, 0.51), "no1": (0.49, 0.52),
        "yes2": (0.50, 0.55), "no2": (0.50, 0.55),
        "yes3": (0.50, 0.55), "no3": (0.50, 0.55),
    })
    snap = main.build_snapshot(cfg, gamma, books)
    # ... voll geladen wird nur der YES-Token des volumenstärksten Markts
    assert books.book_requests == [["yes1"]]
    # die übrigen Tokens behalten ihr synthetisches Top-of-Book (Marks)
    assert snap.books["yes3"].best_ask.size == 0.0


def test_scan_all_mm_kandidaten_ergaenzen_arb_kandidaten():
    # Arb-Kandidat (Markt 2) und MM-Kandidat (Markt 1, Top-Volumen) werden
    # gemeinsam in Stufe 2 geladen — keiner verdrängt den anderen.
    cfg = BotConfig()
    cfg.strategy.scan_all_markets = True
    cfg.strategy.enabled = ["complement_arb", "market_making"]
    cfg.strategy.mm_max_markets = 1
    gamma = FakeGamma([market(1, volume_24h=90_000), market(2, volume_24h=10_000)])
    books = FakeTwoStageBooks({
        "yes1": (0.50, 0.55), "no1": (0.50, 0.55),   # 1.10 -> kein Arb-Kandidat
        "yes2": (0.40, 0.45), "no2": (0.50, 0.54),   # 0.99 -> Arb-Kandidat
    })
    main.build_snapshot(cfg, gamma, books)
    assert books.book_requests == [["no2", "yes1", "yes2"]]


def test_candidate_tokens_teilmengen_no_bei_unvollstaendigem_negrisk_event():
    # Unvollständige Events (Ask fehlt) verwirft negrisk_arb — für die
    # Teilmengen-NO-Messung (negrisk_partial_no) brauchen die verfügbaren
    # NO-Beine trotzdem volle Bücher, wenn ihre Summe < (k-1) + Puffer liegt.
    ev = [market(1), market(2), market(3)]
    asks = {"no1": 0.40, "no2": 0.45, "yes1": 0.62, "yes2": 0.58}  # Markt 3 ohne Asks
    assert main._candidate_tokens([], {"ev": ev}, asks) == {"no1", "no2"}
    # Summe 1.20 >= 1 + Puffer -> kein Kandidat; ebenso bei nur EINEM NO-Bein.
    assert main._candidate_tokens([], {"ev": ev}, {"no1": 0.60, "no2": 0.60}) == set()
    assert main._candidate_tokens([], {"ev": ev}, {"no1": 0.10}) == set()
    # Vollständiges Event bleibt beim bisherigen Pfad (alle Tokens laden):
    asks_voll = {f"{s}{i}": p for i in (1, 2, 3)
                 for s, p in (("yes", 0.40), ("no", 0.63))}  # NO-Summe 1.89 < 2.02
    assert main._candidate_tokens([], {"ev": ev}, asks_voll) == {
        t for m in ev for t in (m.yes_token, m.no_token)}


def test_scan_all_kurzlebige_maerkte_bekommen_volle_buecher():
    # Recorder-Sichtbarkeit (05.07.2026): kurzlebige Märkte im Stream-
    # Ereignisfenster bekommen volle Bücher (extra_full), auch ohne
    # Arb-Verdacht — sonst bleibt die Lebensdauer-Messung auf genau den
    # Profitmärkten zensiert (synthetische Größe-0-Bücher werden verworfen).
    import time as _time
    cfg = BotConfig()
    cfg.strategy.scan_all_markets = True
    now = _time.time()
    m_live, m_daily = market(1), market(2)
    m_live.end_ts = now + 1_800  # endet in 30 Min -> im 2h-Fenster
    gamma = FakeGamma([m_live, m_daily])
    books = FakeTwoStageBooks({
        # kein Markt ist Arb-Kandidat (Summen > 1.02):
        "yes1": (0.50, 0.55), "no1": (0.50, 0.55),
        "yes2": (0.50, 0.55), "no2": (0.50, 0.55),
    })
    main.build_snapshot(cfg, gamma, books)
    # Voll geladen: nur die Tokens des kurzlebigen Markts.
    assert books.book_requests == [["no1", "yes1"]]


def test_scan_all_faellt_ohne_batchpreise_auf_volle_buecher_zurueck():
    # Book-Clients ohne get_top_prices (oder Batch-Komplettausfall) laden
    # weiterhin alle Bücher voll — kein stiller Datenverlust.
    cfg = BotConfig()
    cfg.strategy.scan_all_markets = True
    gamma = FakeGamma([market(1)])
    books = FakeBooks()
    main.build_snapshot(cfg, gamma, books)
    assert books.book_requests == [["no1", "yes1"]]


# ---- Paper-Start-Kapital -----------------------------------------------------

def test_portfolio_erstanlage_nutzt_konfiguriertes_start_cash(tmp_path):
    state = tmp_path / "paper_state.json"
    pf = Portfolio.load(state, start_cash=100_000.0)
    assert pf.cash == 100_000.0
    assert pf.daily_pnl() == 0.0  # Start-Cash ist kein Tagesgewinn


def test_bestehender_state_behaelt_sein_cash(tmp_path):
    # paper_start_cash greift NUR beim ersten Anlegen — ein existierender
    # State darf durch eine Config-Änderung kein frisches Kapital bekommen.
    state = tmp_path / "paper_state.json"
    pf = Portfolio.load(state, start_cash=1_000.0)
    pf.cash = 123.45
    pf.save(state)
    pf2 = Portfolio.load(state, start_cash=100_000.0)
    assert pf2.cash == 123.45
