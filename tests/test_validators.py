import pytest

from evo_tiktok.validators import (
    member_discount_check,
    price_claim_check,
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

    TERMS = "https://www.evolutiongolf.co.uk/pages/giveaway-terms"

    def test_passes_with_wording_date_and_terms_link(self):
        caption = (
            "Win these shoes! Follow, like and tag a mate. Free to enter, no purchase needed. "
            "UK 18+ only. Closes 12 October 2026. One winner picked at random from eligible "
            f"entries. Full T&Cs: {self.TERMS}. This promotion is not sponsored, endorsed or run by TikTok."
        )
        assert giveaway_check(caption, terms_url=self.TERMS) == []
        assert giveaway_check(caption.replace(self.TERMS, "link in bio"), terms_url=self.TERMS)
        assert giveaway_check(caption) == ["No T&Cs link configured (giveaway.terms_url), so the giveaway can't run"]

    def test_fails_without_wording(self):
        assert giveaway_check("Win these shoes! Follow and tag a mate.", terms_url=self.TERMS)

    def test_fails_with_unfilled_date(self):
        caption = (
            "Free to enter, no purchase needed. UK 18+ only. Closes [date]. One winner picked at "
            f"random from eligible entries. Full T&Cs: {self.TERMS}. This promotion is not sponsored, "
            "endorsed or run by TikTok."
        )
        assert giveaway_check(caption, terms_url=self.TERMS)


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


class TestPriceClaims:
    """Every £ figure must be true at live website price (feedback of 12 Oct 2026)."""

    def test_live_prices_pass_and_member_prices_fail(self, make_line):
        putter = make_line("PUTT", price="119", rrp=None, member="77.35")
        assert price_claim_check("This putter is £119", [putter]) == []
        assert price_claim_check("Putter for £119.00", [putter]) == []
        assert price_claim_check("Putters under £90", [putter])        # the 12 Oct bug: a member-price band
        assert price_claim_check("Only £77.35 this week", [putter])    # the member price itself

    def test_rounding_to_the_pound_is_fine_within_50p(self, make_line):
        glove = make_line("G", price="25.95", rrp=None)
        assert price_claim_check("Gloves for £26", [glove]) == []
        assert price_claim_check("Gloves for £25", [glove])

    def test_tiers_are_fixed_and_need_ten_percent_headroom(self, make_line):
        a, b = make_line("A", price="25.95", rrp=None), make_line("B", price="39.95", rrp=None)
        assert price_claim_check("Best gloves under £50", [a, b]) == []
        assert "fixed price tier" in price_claim_check("Best gloves under £40", [a, b])[0]  # auto-rounded cap
        spike = make_line("S", price="79.95", rrp=None)
        assert "10% below" in price_claim_check("Spikeless under £75", [spike])[0]

    def test_gaps_and_savings(self, make_line):
        a, b = make_line("A", price="25.95", rrp="60"), make_line("B", price="39.95", rrp=None)
        assert price_claim_check("£26 vs £40. Is it worth £14 more?", [a, b]) == []
        assert price_claim_check("£60 of gloves for £26", [a, b]) == []    # compare-at saving
        assert price_claim_check("Was £40, now £26", [a]) != []            # no such compare-at
        close = make_line("C", price="27.95", rrp=None)
        assert "price gap" in price_claim_check("£26 vs £28", [a, close])[0]

    def test_member_discount_is_judged_per_video(self, make_line):
        big = make_line("BIG", price="100", rrp=None, member="65")    # 35% for members
        small = make_line("SM", price="100", rrp=None, member="85")   # 15%
        assert member_discount_check("Up to 35% off for members", [big, small]) == []
        assert member_discount_check("Up to 35% off for members", [small])
        assert member_discount_check("35% off for members", [big, small])  # flat claim: every product
