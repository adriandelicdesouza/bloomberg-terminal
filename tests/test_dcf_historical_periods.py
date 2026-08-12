import sys
import types

import pytest

from backend import main


def mock_dcf_data():
    years = [str(year) for year in range(2016, 2026)]

    # Revenue deliberately increases by a different amount each year.
    revenue = {
        year: (i + 1) * 1000
        for i, year in enumerate(years)
    }

    # Each metric has a deliberately distinct value by year so that
    # selecting the wrong historical period produces a different average.
    ebit = {
        year: revenue[year] * ((10 + i) / 100)
        for i, year in enumerate(years)
    }

    da = {
        year: revenue[year] * ((2 + i) / 100)
        for i, year in enumerate(years)
    }

    capex = {
        year: revenue[year] * ((3 + i) / 100)
        for i, year in enumerate(years)
    }

    pretax = {
        year: revenue[year] * 0.8
        for year in years
    }

    tax = {
        year: pretax[year] * ((15 + i) / 100)
        for i, year in enumerate(years)
    }

    return {
        "revenue": revenue,
        "ebit": ebit,
        "da": da,
        "capex": capex,
        "pretax": pretax,
        "tax": tax,
    }


class FakeTicker:
    def __init__(self, ticker):
        self.info = {
            "longName": "Test Company",
            "totalDebt": 0,
            "totalCash": 0,
            "sharesOutstanding": 1_000_000,
            "currentPrice": 100,
            "beta": 1.0,
            "currency": "USD",
        }


def mock_yfinance(monkeypatch):
    fake_yfinance = types.ModuleType("yfinance")
    fake_yfinance.Ticker = FakeTicker

    monkeypatch.setitem(
        sys.modules,
        "yfinance",
        fake_yfinance,
    )


@pytest.fixture
def dcf_result(monkeypatch):
    monkeypatch.setattr(
        main,
        "fetch_sec_historicals",
        lambda ticker: mock_dcf_data(),
    )

    monkeypatch.setattr(
        main,
        "fetch_fred_series",
        lambda series, limit=1: [{"value": 4.5}],
    )

    mock_yfinance(monkeypatch)

    return main.fetch_dcf_historicals("TEST")


def test_frontend_rows_remain_newest_first(dcf_result):
    years = [row["year"] for row in dcf_result["rows"]]

    assert years == [
        "2025",
        "2024",
        "2023",
        "2022",
        "2021",
        "2020",
        "2019",
        "2018",
        "2017",
        "2016",
    ]


def test_recent_5_year_revenue_growth_uses_latest_five(dcf_result):
    assumptions = dcf_result["defaults"]["rev_growth_1_5"]

    expected = pytest.approx(
        (20.0 + 16.7 + 14.3 + 12.5 + 11.1) / 5,
        rel=1e-6,
    )

    assert assumptions["avg"] == expected


def test_recent_3_year_revenue_growth_uses_latest_three(dcf_result):
    assumptions = dcf_result["defaults"]["rev_growth_1_5"]

    expected = pytest.approx(
        (14.3 + 12.5 + 11.1) / 3,
        rel=1e-6,
    )

    assert assumptions["3y"] == expected


def test_older_5_year_revenue_growth_uses_years_6_to_10(dcf_result):
    assumptions = dcf_result["defaults"]["rev_growth_6_10"]

    expected = pytest.approx(
        (100.0 + 50.0 + 33.3 + 25.0) / 4,
        rel=1e-6,
    )

    assert assumptions["avg"] == expected


@pytest.mark.parametrize(
    "key,expected",
    [
        ("ebit_margin", (17 + 18 + 19) / 3),
        ("da_pct", (9 + 10 + 11) / 3),
        ("capex_pct", (10 + 11 + 12) / 3),
        ("tax_rate", (22 + 23 + 24) / 3),
    ],
)
def test_recent_3_year_operating_assumptions_use_latest_three(
    dcf_result,
    key,
    expected,
):
    assert dcf_result["defaults"][key]["3y"] == pytest.approx(expected)


def test_missing_historical_values_do_not_break_period_calculations(
    monkeypatch,
):
    data = mock_dcf_data()

    # Remove one recent value from each metric.
    data["ebit"]["2025"] = None
    data["da"]["2024"] = None
    data["capex"]["2023"] = None
    data["tax"]["2025"] = None

    monkeypatch.setattr(
        main,
        "fetch_sec_historicals",
        lambda ticker: data,
    )

    monkeypatch.setattr(
        main,
        "fetch_fred_series",
        lambda series, limit=1: [{"value": 4.5}],
    )

    mock_yfinance(monkeypatch)

    result = main.fetch_dcf_historicals("TEST")

    assert result["defaults"]["ebit_margin"]["3y"] is not None
    assert result["defaults"]["da_pct"]["3y"] is not None
    assert result["defaults"]["capex_pct"]["3y"] is not None
    assert result["defaults"]["tax_rate"]["3y"] is not None