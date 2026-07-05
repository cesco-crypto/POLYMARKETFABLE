

def test_kein_mark_warnung_ist_gedrosselt(caplog):
    """Stream-Betrieb ruft value() mehrmals pro Sekunde auf — ein markloser
    Altbestand darf das Log nicht fluten (Befund 05.07.2026)."""
    import logging

    from polybot.portfolio import Fill, Portfolio

    pf = Portfolio(cash=100.0)
    pf.apply_fill(Fill(ts=0.0, token_id="t1", side="BUY", price=0.5, size=10,
                       reason="test"))
    with caplog.at_level(logging.WARNING, logger="polybot.portfolio"):
        for _ in range(50):
            pf.value(marks={})               # t1 hat keinen Mark
    warnings = [r for r in caplog.records if "Kein Mark" in r.message]
    assert len(warnings) == 1
