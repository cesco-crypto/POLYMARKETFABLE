import requests

from polybot.data.orderbook import BookClient


def book_row(token_id: str) -> dict:
    return {
        "asset_id": token_id,
        "bids": [{"price": "0.40", "size": "100"}, {"price": "0.41", "size": "50"}],
        "asks": [{"price": "0.45", "size": "80"}, {"price": "0.44", "size": "10"}],
    }


class FakeResponse:
    def __init__(self, status: int, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else []

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)

    def json(self):
        return self._payload


class FakeSession:
    """Stub-Session: gibt vorbereitete Antworten für POST/GET zurück."""

    def __init__(self, post_responses=None, get_responses=None):
        self.headers: dict = {}
        self.post_responses = list(post_responses or [])
        self.get_responses = list(get_responses or [])
        self.post_calls = 0
        self.get_calls = 0

    def post(self, url, json=None, timeout=None):
        self.post_calls += 1
        return self.post_responses.pop(0)

    def get(self, url, params=None, timeout=None):
        self.get_calls += 1
        return self.get_responses.pop(0)


def test_get_books_ueberspringt_fehlerhafte_rows():
    # Regressionstest: eine kaputte Row (fehlender Key, Nicht-Zahl, fehlende
    # asset_id) darf nicht den ganzen Batch verwerfen — loggen + überspringen.
    rows = [
        book_row("tok-gut"),
        {"asset_id": "tok-ohne-price", "bids": [{"size": "10"}], "asks": []},
        {"asset_id": "tok-nan", "bids": [{"price": "abc", "size": "10"}], "asks": []},
        {"bids": [], "asks": []},  # keine asset_id
        book_row("tok-gut2"),
    ]
    session = FakeSession(post_responses=[FakeResponse(200, rows)])
    bc = BookClient(session=session)
    out = bc.get_books(["tok-gut", "tok-ohne-price", "tok-nan", "tok-x", "tok-gut2"])
    assert set(out) == {"tok-gut", "tok-gut2"}
    assert out["tok-gut"].best_bid.price == 0.41  # Sortierung bleibt korrekt
    assert out["tok-gut"].best_ask.price == 0.44


def test_get_books_bricht_bei_rate_limit_ab_statt_einzeln_zu_hämmern():
    # Regressionstest: bei 429 darf der Fallback NICHT in bis zu ~5000
    # Einzelrequests ausarten — restlichen Batch-Lauf abbrechen und das
    # bisherige Teilergebnis zurückgeben.
    tokens = [f"tok{i}" for i in range(60)]  # 2 Chunks à 50/10
    session = FakeSession(post_responses=[
        FakeResponse(200, [book_row(t) for t in tokens[:50]]),
        FakeResponse(429),
    ])
    bc = BookClient(session=session)
    out = bc.get_books(tokens)
    assert set(out) == set(tokens[:50])  # Teilergebnis des ersten Chunks
    assert session.get_calls == 0  # kein Einzel-Fallback bei Rate-Limit


def test_get_books_faellt_bei_anderen_fehlern_pro_chunk_einzeln_zurueck():
    session = FakeSession(
        post_responses=[FakeResponse(500)],
        get_responses=[FakeResponse(200, book_row("tok0")),
                       FakeResponse(200, book_row("tok1"))],
    )
    bc = BookClient(session=session)
    out = bc.get_books(["tok0", "tok1"])
    assert set(out) == {"tok0", "tok1"}
    assert session.get_calls == 2


def test_get_book_liefert_none_bei_kaputten_rohdaten():
    # Regressionstest: Vertrag ist "None + Warnung bei Problemen" — auch
    # Parse-Fehler dürfen nicht aus get_book propagieren.
    session = FakeSession(get_responses=[
        FakeResponse(200, {"bids": [{"price": "abc", "size": "10"}], "asks": []}),
    ])
    bc = BookClient(session=session)
    assert bc.get_book("tok0") is None
