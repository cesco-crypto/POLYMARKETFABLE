import pytest

from polybot.config import BotConfig


def write_config(tmp_path, text: str):
    p = tmp_path / "config.yaml"
    p.write_text(text)
    return p


def test_leerer_abschnitt_faellt_auf_defaults_zurueck(tmp_path):
    # Regressionstest: 'risk:' ohne Wert (z.B. auskommentierter Block-Rest)
    # liefert yaml None statt {} — RiskConfig(**None) crashte mit TypeError.
    p = write_config(tmp_path, "mode: paper\nrisk:\nstrategy:\n")
    cfg = BotConfig.load(p)
    assert cfg.risk.max_order_usdc == 50.0
    assert cfg.strategy.max_markets == 50


def test_unbekanntes_feld_nennt_den_betroffenen_abschnitt(tmp_path):
    # Tippfehler in einem Feldnamen soll auf den YAML-Abschnitt zeigen,
    # statt einen nackten TypeError zu werfen.
    p = write_config(tmp_path, "risk:\n  max_order_usd: 10\n")
    with pytest.raises(SystemExit, match="risk"):
        BotConfig.load(p)


def test_expliziter_fehlender_config_pfad_bricht_ab(tmp_path):
    # Regressionstest: ein Tippfehler in --config darf den Bot nicht
    # stillschweigend mit Built-in-Defaults (Risikolimits!) starten.
    with pytest.raises(SystemExit, match="nicht gefunden"):
        BotConfig.load(tmp_path / "gibtsnicht.yaml")


def test_impliziter_default_toleriert_fehlende_config_yaml(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = BotConfig.load()
    assert cfg.mode == "paper"


def test_impliziter_default_liest_config_yaml_im_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("poll_interval_s: 42\n")
    assert BotConfig.load().poll_interval_s == 42.0


@pytest.mark.parametrize("yaml_text", [
    "poll_interval_s: 0\n",      # Busy-Loop: sleep(0) -> Dauerfeuer gegen die API
    "poll_interval_s: -5\n",
    "risk:\n  max_order_usdc: -1\n",
    "risk:\n  taker_fee_rate: 0.5\n",  # weit über dem Polymarket-Maximum 0.07
    "risk:\n  taker_fee_rate: -0.01\n",
    "strategy:\n  stream_event_window_s: -1\n",  # 0 = aus, negativ = Fehler
    "strategy:\n  paper_fill_delay_ticks: -1\n",  # 0 = Sofort-Fill, negativ = Fehler
])
def test_unsinnige_werte_werden_abgewiesen(tmp_path, yaml_text):
    p = write_config(tmp_path, yaml_text)
    with pytest.raises(SystemExit):
        BotConfig.load(p)


def test_stream_event_window_default_und_yaml_ladbar(tmp_path):
    # Ereignisfenster fürs WSS-Abo: Default 2h (Messbefund 05.07.2026 —
    # Profit lebt in Märkten mit endDate < 2h), per YAML änderbar (0 = aus).
    assert BotConfig().strategy.stream_event_window_s == 2 * 3600.0
    p = write_config(tmp_path, "strategy:\n  stream_event_window_s: 0\n")
    assert BotConfig.load(p).strategy.stream_event_window_s == 0.0


def test_paper_fill_delay_default_und_yaml_ladbar(tmp_path):
    # Ehrliche Fill-Simulation: Default 1 (Signale füllen erst im Folge-Tick
    # gegen das dann aktuelle Buch); 0 = altes Sofort-Fill-Verhalten.
    assert BotConfig().strategy.paper_fill_delay_ticks == 1
    p = write_config(tmp_path, "strategy:\n  paper_fill_delay_ticks: 0\n")
    assert BotConfig.load(p).strategy.paper_fill_delay_ticks == 0


def test_signature_type_3_wird_akzeptiert(tmp_path, monkeypatch):
    # Deposit-Wallet-Flow (POLY_1271) — seit dem Exchange-Upgrade der einzige
    # Weg, wie der V2-CLOB Orders akzeptiert.
    monkeypatch.setenv("POLY_SIGNATURE_TYPE", "3")
    p = write_config(tmp_path, "mode: paper\n")
    assert BotConfig.load(p).signature_type == 3


@pytest.mark.parametrize("value", ["4", "-1", "kaputt"])
def test_unbekannter_signature_type_wird_abgewiesen(tmp_path, monkeypatch, value):
    monkeypatch.setenv("POLY_SIGNATURE_TYPE", value)
    p = write_config(tmp_path, "mode: paper\n")
    with pytest.raises(SystemExit):
        BotConfig.load(p)


def test_order_reject_cooldown_default_und_validierung(tmp_path):
    # Default 60s; negativ ist ein Konfigurationsfehler (0 = aus).
    assert BotConfig().risk.order_reject_cooldown_s == 60.0
    p = write_config(tmp_path, "risk:\n  order_reject_cooldown_s: -1\n")
    with pytest.raises(SystemExit):
        BotConfig.load(p)
    p = write_config(tmp_path, "risk:\n  order_reject_cooldown_s: 0\n")
    assert BotConfig.load(p).risk.order_reject_cooldown_s == 0
