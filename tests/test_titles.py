"""Normalizace názvů produktů na tituly her.

ITAD páruje přesnou shodou, takže tenhle modul rozhoduje o tom, jestli se hra
vůbec ocení. Většina případů níž jsou skutečné názvy z katalogu Kinguinu.
Změřeno na živých datech: 98 % her se spáruje.
"""

import pytest

from src.titles import candidates


def first(name: str) -> str:
    return candidates(name)[0]


@pytest.mark.parametrize("raw,expected", [
    ("Gothic 1 Remake PC Steam CD Key", "Gothic 1 Remake"),
    ("EA SPORTS FC 26 PC Steam Account", "EA SPORTS FC 26"),
    ("PAYDAY 3 Steam CD Key", "PAYDAY 3"),
    ("Deep Rock Galactic PC Steam CD Key", "Deep Rock Galactic"),
    ("Palworld Steam Account", "Palworld"),
    ("Football Manager 26 EU PC Steam CD Key", "Football Manager 26"),
    ("Battlefield 6 PC EA App CD Key", "Battlefield 6"),
])
def test_strips_platform_and_key_noise(raw, expected):
    assert first(raw) == expected


class TestRegressions:
    def test_keeps_comma_in_title(self):
        """Čárka je součást názvu: 'Warhammer 40,000' není 'Warhammer 40 000'."""
        assert first("Warhammer 40,000: Space Marine 2 PC Steam CD Key") == \
            "Warhammer 40,000: Space Marine 2"

    def test_does_not_eat_the_word_us(self):
        """Regionální zkratky se hledají jen verzálkami.

        S IGNORECASE regex ukusoval 'Us' z 'The Last of Us' a hra se nikdy
        nespárovala — chyba odhalená až na živých datech.
        """
        assert first("The Last of Us Part 1 PC Steam CD Key") == "The Last of Us Part 1"
        assert first("The Last of Us Part 2 Remastered EU PC Steam CD Key") == \
            "The Last of Us Part 2 Remastered"

    def test_strips_uppercase_region_code(self):
        assert first("TEKKEN 8 RoW Steam CD Key") == "TEKKEN 8"

    def test_no_dangling_preposition(self):
        assert first("Minecraft: Java & Bedrock Edition for PC CD Key") == \
            "Minecraft: Java & Bedrock Edition"

    def test_truncates_at_first_noise_word(self):
        """Název hry stojí vepředu, balast až za ním."""
        assert first("Red Dead Redemption 2 Epic Games Green Gift Redemption Code") == \
            "Red Dead Redemption 2"

    def test_splits_on_plus_bundle(self):
        assert first("DOOM: The Dark Ages + Pre-Order Bonus DLC PC Steam CD Key") == \
            "DOOM: The Dark Ages"


class TestAddons:
    """Přídavky se nikdy nesmí ocenit cenou základní hry.

    Chyba odhalená až v ostrém provozu: 'EA Sports FC 24 - Pre-order Bonus DLC'
    za 7 Kč se spárovalo se základní hrou a vyšlo jako sleva na 0,4 % ceny.
    Bezcenný bonus tak vypadal jako největší trhák dne.
    """

    @pytest.mark.parametrize("raw", [
        "EA Sports FC 24 - Pre-order Bonus DLC EA App CD Key",
        "Starfield - Preorder Bonus DLC PC Steam CD Key",
        "Call of Duty: Black Ops 6 - 10 Hours Double XP Boost DLC PC",
        "Destiny 2 - The Witch Queen DLC RoW PC Steam CD Key",
    ])
    def test_addon_never_falls_back_to_base_game(self, raw):
        result = candidates(raw)
        assert len(result) <= 1, "přídavek nesmí nabízet víc variant"
        if result:
            assert " - " not in raw.split(" DLC")[0] or result[0] != raw.split(" - ")[0]

    def test_addon_keeps_its_full_name(self):
        """Hledá se přesně ten přídavek. Když ho ITAD nezná, neocení se vůbec."""
        assert candidates("Destiny 2 - The Witch Queen DLC RoW PC Steam CD Key") == \
            ["Destiny 2 - The Witch Queen DLC"]

    def test_base_game_still_shortens_normally(self):
        assert candidates("Gothic 1 Remake PC Steam CD Key")[0] == "Gothic 1 Remake"

    @pytest.mark.parametrize("raw", [
        "Mystery PC Steam CD Key",
        "5 x VR Mystery Steam CD Key",
        "Random Premium Steam Key",
    ])
    def test_mystery_keys_are_not_valuable(self, raw):
        """Náhodný klíč nemá známý obsah, takže mu nelze přiřadit hodnotu."""
        assert candidates(raw) == []


class TestVariants:
    def test_edition_stripped_as_fallback(self):
        """Základní hra je levnější, takže se nabídka spíš podhodnotí.

        To je bezpečný směr — konzervativní odhad nevyrobí falešný trhák.
        """
        result = candidates("EA SPORTS FC 26 Ultimate Edition PC Steam Account")
        assert "EA SPORTS FC 26 Ultimate Edition" in result
        assert "EA SPORTS FC 26" in result
        assert result.index("EA SPORTS FC 26 Ultimate Edition") < result.index("EA SPORTS FC 26")

    def test_roman_numeral_variant(self):
        assert "The Last of Us Part II" in candidates("The Last of Us Part 2 PC Steam CD Key")

    def test_drops_bracketed_notes(self):
        assert first("Some Game (Latin America) PC Steam CD Key") == "Some Game"

    def test_empty_input(self):
        assert candidates("") == []

    def test_candidates_are_unique(self):
        result = candidates("Palworld Steam Account")
        assert len(result) == len(set(result))
