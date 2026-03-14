"""Combinatorial testing — 2-way coverage for quote derivation and date parsing."""
import pytest
from collections import OrderedDict

from allpairspy import AllPairs

from kalshi.data import _derive_quotes
from kalshi.providers import KalshiInstrumentProvider


# ===========================================================================
# Quote derivation: 2-way coverage
# ===========================================================================

QUOTE_PARAMS = OrderedDict({
    "yes_state": ["empty", "single", "multi"],
    "no_state": ["empty", "single", "multi"],
    "yes_price": [0.01, 0.32, 0.50, 0.99],
    "no_price": [0.01, 0.32, 0.50, 0.99],
})


QUOTE_CASES = list(AllPairs(QUOTE_PARAMS, n=2))


def _build_book(state: str, price: float) -> dict[float, float]:
    if state == "empty":
        return {}
    elif state == "single":
        return {price: 5.0}
    else:  # multi
        lower = round(price - 0.01, 2)
        book = {price: 5.0}
        if lower >= 0.01:
            book[lower] = 2.5
        return book


def _quote_id(case) -> str:
    return f"y{case[0][0]}-n{case[1][0]}-yp{case[2]}-np{case[3]}"


@pytest.mark.parametrize("case", QUOTE_CASES, ids=[_quote_id(c) for c in QUOTE_CASES])
def test_derive_quotes_ct(case):
    """Every 2-way combination must produce valid quotes or None."""
    yes_book = _build_book(case[0], case[2])
    no_book = _build_book(case[1], case[3])

    result = _derive_quotes("TICKER", yes_book, no_book)

    if not yes_book and not no_book:
        assert result is None, "Both books empty → must return None"
        return

    assert result is not None
    for side_name in ("YES", "NO"):
        q = result[side_name]
        assert 0.0 <= q["bid"] <= 1.0, f"{side_name} bid out of range: {q['bid']}"
        assert 0.0 <= q["ask"] <= 1.0, f"{side_name} ask out of range: {q['ask']}"
        assert q["bid_size"] >= 0.0, f"{side_name} bid_size negative"
        assert q["ask_size"] >= 0.0, f"{side_name} ask_size negative"

    # Domain invariants: YES/NO are complements
    if no_book:
        expected_yes_ask = round(1.0 - result["NO"]["bid"], 10)
        assert result["YES"]["ask"] == pytest.approx(expected_yes_ask, abs=1e-9)
    if yes_book:
        expected_no_ask = round(1.0 - result["YES"]["bid"], 10)
        assert result["NO"]["ask"] == pytest.approx(expected_no_ask, abs=1e-9)


# ===========================================================================
# Date parsing: 2-way coverage
# ===========================================================================

DATE_PARAMS = OrderedDict({
    "city": ["CHI", "NYC", "LAX", "DEN", "MIA"],
    "month": ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
              "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"],
    "day": ["01", "14", "28"],
    "threshold": ["T40", "T55", "T80", "T100"],
})

# No filter needed — all valid combos (removed "31" to avoid FEB-31 etc.)

DATE_CASES = list(AllPairs(DATE_PARAMS, n=2))

_MONTH_NUM = {
    "JAN": "01", "FEB": "02", "MAR": "03", "APR": "04",
    "MAY": "05", "JUN": "06", "JUL": "07", "AUG": "08",
    "SEP": "09", "OCT": "10", "NOV": "11", "DEC": "12",
}


def _date_id(case) -> str:
    return f"{case[0]}-{case[1]}{case[2]}-{case[3]}"


@pytest.mark.parametrize("case", DATE_CASES, ids=[_date_id(c) for c in DATE_CASES])
def test_observation_date_parsing_ct(case):
    """Every valid city × month × day × threshold parses to correct date."""
    ticker = f"KXHIGH{case[0]}-26{case[1]}{case[2]}-{case[3]}"
    result = KalshiInstrumentProvider._parse_observation_date(ticker)
    assert result == f"2026-{_MONTH_NUM[case[1]]}-{case[2]}"
