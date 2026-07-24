"""Parsování cen. Pepper míchá čtyři národní zápisy čísel v jednom feedu."""

from src import money


class TestParsePrice:
    def test_german_format(self):
        assert money.parse_price("1.259,29€") == (1259.29, "EUR")

    def test_uk_format(self):
        assert money.parse_price("£328.50") == (328.50, "GBP")

    def test_polish_format(self):
        amount, currency = money.parse_price("1 259,29 zł")
        assert currency == "PLN"
        assert abs(amount - 1259.29) < 0.01

    def test_simple(self):
        assert money.parse_price("€68") == (68.0, "EUR")

    def test_symbol_after_number(self):
        assert money.parse_price("99€") == (99.0, "EUR")

    def test_ignores_bare_numbers(self):
        """Regrese: z 'modern 4 hotel ... from €34' vypadávala cena 4 místo 34."""
        title = "Boutique Hanoi stay: modern 4 hotel with breakfast from €34/double"
        assert money.parse_price(title) == (34.0, "EUR")

    def test_no_price(self):
        assert money.parse_price("Žádná cena tady není") is None

    def test_empty(self):
        assert money.parse_price("") is None


class TestOriginalPrice:
    def test_german_statt(self):
        assert money.find_original_price("Super Deal statt 1.599€ jetzt billig") == (1599.0, "EUR")

    def test_uk_rrp(self):
        assert money.find_original_price("Nice shirt RRP £89.99") == (89.99, "GBP")

    def test_polish_zamiast(self):
        amount, currency = money.find_original_price("Promocja zamiast 199 zł")
        assert (amount, currency) == (199.0, "PLN")

    def test_no_hint_means_no_original(self):
        """Bez klíčového slova se cena za původní nepovažuje — jinak by se bralo cokoliv."""
        assert money.find_original_price("Laptop za 25.999€ super") is None


class TestDiscountPercent:
    def test_finds_percent(self):
        assert money.find_discount_percent("-70% Rabatt auf alles") == 70

    def test_takes_highest(self):
        assert money.find_discount_percent("20% až 65% sleva") == 65

    def test_rejects_nonsense(self):
        assert money.find_discount_percent("100% bavlna") is None
        assert money.find_discount_percent("2% něco") is None
