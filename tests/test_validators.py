import pytest

from evo_tiktok.validators import (
    GuardrailError,
    brand_price_check,
    free_entry_wording,
    giveaway_check,
    require,
    rrp_check,
    stock_check,
    up_to_claim_check,
)

BRANDS = ["adidas", "Nike", "Under Armour", "TRUE", "PXG"]


class TestBrandPrice:
    @pytest.mark.parametrize(
        "text",
        [
            "Nike polos from £25 for members",
            "£25 adidas shoes",
            "Under Armour trousers 40% off",
            "UNDER ARMOUR jumpers, 50 % off this week",
            "adidas spikes at half price",
            "Grab TRUE shoes for 30 quid",
        ],
    )
    def test_fails_when_brand_near_price(self, text):
        assert brand_price_check(text, BRANDS, 40)

    @pytest.mark.parametrize(
        "text",
        [
            "Best golf shoes under £50? Comment your pick",
            "Nike, adidas or Under Armour: which polo wins?",
            "Is it true these are 40–50% off for members?",  # 'true' lowercase is not the brand
            "Member prices are in the Evo Members Club, link in bio",
        ],
    )
    def test_passes_without_brand_price_pair(self, text):
        assert brand_price_check(text, BRANDS, 40) == []

    def test_window_is_respected(self):
        far = "adidas " + "x" * 41 + " £20"
        near = "adidas " + "x" * 30 + " £20"
        assert brand_price_check(far, BRANDS, 40) == []
        assert brand_price_check(near, BRANDS, 40)

    def test_brand_inside_word_does_not_count(self):
        assert brand_price_check("Nikeish vibes £20", ["Nike"], 40) == []


class TestUpTo:
    def test_no_claim_passes(self, make_line):
        assert up_to_claim_check("40–50% off for members", [], 0.3) == []

    def test_claim_needs_share_at_level(self, make_line):
        lines = [make_line(f"S{i}", price="50", rrp="100") for i in range(3)] + [
            make_line(f"T{i}", price="80", rrp="100") for i in range(7)
        ]
        assert up_to_claim_check("Up to 50% off for members", lines, 0.3) == []
        assert up_to_claim_check("Up to 50% off for members", lines, 0.31)
        assert up_to_claim_check("up to 60% off", lines, 0.3)

    def test_member_price_is_used_when_known(self, make_line):
        lines = [make_line("A", price="80", rrp="100", member="50")]
        assert up_to_claim_check("up to 50% off", lines, 0.3) == []

    def test_lines_without_rrp_never_count(self, make_line):
        lines = [make_line("A", rrp=None)]
        assert up_to_claim_check("up to 10% off", lines, 0.3)

    def test_claim_with_no_lines_fails(self):
        assert up_to_claim_check("up to 50% off", [], 0.3)


class TestRrp:
    def test_rrp_for_sku_without_compare_at_fails(self, make_line):
        stock = {"A": make_line("A", rrp=None), "B": make_line("B")}
        assert rrp_check("RRP £100, members pay less", ["A"], stock)
        assert rrp_check("was £100", ["A"], stock)
        assert rrp_check("RRP £100", ["B"], stock) == []

    def test_no_rrp_mention_passes(self, make_line):
        assert rrp_check("Members save loads", ["A"], {"A": make_line("A", rrp=None)}) == []


class TestGiveaway:
    def test_wording_is_loaded_from_content_rules(self):
        assert free_entry_wording().startswith("Free to enter, no purchase needed.")

    def test_passes_with_wording_and_date(self):
        caption = (
            "Win these shoes! Follow, like and tag a mate. Free to enter, no purchase needed. "
            "UK 18+ only. Closes 12 October 2026. One winner picked at random from eligible "
            "entries. Full T&Cs at the link in bio."
        )
        assert giveaway_check(caption) == []

    def test_fails_without_wording(self):
        assert giveaway_check("Win these shoes! Follow and tag a mate.")

    def test_fails_with_unfilled_date(self):
        caption = (
            "Free to enter, no purchase needed. UK 18+ only. Closes [date]. One winner picked at "
            "random from eligible entries. Full T&Cs at the link in bio."
        )
        assert giveaway_check(caption)


class TestStock:
    def test_zero_and_unknown_fail(self, make_line):
        stock = {"A": make_line("A", units=2), "B": make_line("B", units=0)}
        assert stock_check(["A"], stock) == []
        assert len(stock_check(["A", "B", "C"], stock)) == 2


def test_require_raises_with_errors():
    require([])
    with pytest.raises(GuardrailError) as exc:
        require(["bad"])
    assert exc.value.errors == ["bad"]


def test_settings_brands_are_strings(settings):
    assert "TRUE" in settings.restricted_brands


def test_unquoted_yaml_brand_is_rejected(settings):
    import yaml

    from evo_tiktok.config import ConfigError, Settings

    data = dict(settings.data, restricted_brands=yaml.safe_load("[adidas, TRUE]"))
    with pytest.raises(ConfigError):
        Settings(data=data, source=settings.source).restricted_brands
