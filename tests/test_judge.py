"""AI soudce.

Testuje se to křehké: rozebrání odpovědi modelu a pojistky proti utracení kreditu.
Modely rády obalí JSON markdownem nebo povídáním, a výpadek API nesmí shodit běh.
"""

from src.config import load_config
from src.oracles.judge import JudgeOracle, _parse_json_array
from src.sources.base import CATALOG, Offer
from src.store import Store


class TestParseResponse:
    def test_plain_array(self):
        assert _parse_json_array('[{"id":"a","real_value_czk":100}]') == [
            {"id": "a", "real_value_czk": 100}
        ]

    def test_markdown_fenced(self):
        content = '```json\n[{"id":"a","real_value_czk":100}]\n```'
        assert _parse_json_array(content) == [{"id": "a", "real_value_czk": 100}]

    def test_wrapped_in_chatter(self):
        content = 'Jasně, tady je výsledek:\n[{"id":"a","real_value_czk":100}]\nHotovo.'
        assert _parse_json_array(content) == [{"id": "a", "real_value_czk": 100}]

    def test_object_with_items_key(self):
        content = '{"items":[{"id":"a","real_value_czk":100}]}'
        assert _parse_json_array(content) == [{"id": "a", "real_value_czk": 100}]

    def test_garbage_returns_none(self):
        assert _parse_json_array("promiň, nerozumím") is None


class FakeHttp:
    def __init__(self, response=None, raises=False):
        self.response = response
        self.raises = raises
        self.calls = 0

    def post_json(self, url, payload, headers=None, timeout_s=None):
        self.calls += 1
        if self.raises:
            raise RuntimeError("API spadlo")
        return {"choices": [{"message": {"content": self.response}}]}


def _offer(uid="a", name="Neznámá věc", price=100.0):
    return Offer(source="kinguin", kind=CATALOG, uid=uid, name=name,
                 price_czk=price, url="http://x")


def _judge(tmp_path, http):
    cfg = load_config()
    cfg.openrouter_key = "test-key"
    return JudgeOracle(http, Store(tmp_path / "j.db"), cfg)


class TestJudge:
    def test_valid_response_becomes_values(self, tmp_path):
        http = FakeHttp('[{"id":"a","real_value_czk":8820,'
                        '"ships_to_cz":true,"why":"18 měsíců Gemini"}]')
        values = _judge(tmp_path, http).judge([_offer()])

        assert values["a"].real_value_czk == 8820
        assert values["a"].origin == "ai"
        assert values["a"].note == "18 měsíců Gemini"

    def test_zero_value_is_ignored(self, tmp_path):
        """Model má bezcennou šuntu ocenit nulou a ta nesmí projít jako hodnota."""
        http = FakeHttp('[{"id":"a","real_value_czk":0,"why":"bezcenné"}]')
        assert _judge(tmp_path, http).judge([_offer()]) == {}

    def test_api_failure_does_not_raise(self, tmp_path):
        """Spadlé API nesmí shodit běh — bot dojede na heuristice."""
        assert _judge(tmp_path, FakeHttp(raises=True)).judge([_offer()]) == {}

    def test_no_key_means_no_call(self, tmp_path):
        cfg = load_config()
        cfg.openrouter_key = ""
        http = FakeHttp("[]")
        assert JudgeOracle(http, Store(tmp_path / "j.db"), cfg).judge([_offer()]) == {}
        assert http.calls == 0

    def test_daily_cap_stops_further_calls(self, tmp_path):
        cfg = load_config()
        cfg.openrouter_key = "test-key"
        cfg.raw["judge"]["max_calls_per_day"] = 2
        store = Store(tmp_path / "j.db")
        http = FakeHttp('[{"id":"a","real_value_czk":100}]')
        judge = JudgeOracle(http, store, cfg)

        judge.judge([_offer()])
        judge.judge([_offer()])
        judge.judge([_offer()])

        assert http.calls == 2, "třetí volání má zastavit denní strop"

    def test_batch_is_capped(self, tmp_path):
        cfg = load_config()
        cfg.openrouter_key = "test-key"
        cfg.raw["judge"]["max_items_per_call"] = 3
        http = FakeHttp("[]")
        judge = JudgeOracle(http, Store(tmp_path / "j.db"), cfg)
        judge.judge([_offer(uid=str(i)) for i in range(50)])

        assert http.calls == 1, "jeden dávkový dotaz, ne padesát"
