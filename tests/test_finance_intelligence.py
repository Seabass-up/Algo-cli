from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest

from algo_cli.intelligence.construction.labor_units import (
    LaborUnitDatabase,
    build_database,
)
from algo_cli.intelligence.finance.ap_ar import (
    APDuplicateDetector,
    APInvoice,
    VendorChangeEvent,
)
from algo_cli.intelligence.finance.cash import ThirteenWeekCashForecast
from algo_cli.intelligence.finance.common import dateize, datetimeize
from algo_cli.intelligence.finance.exceptions import make_exception


def test_finance_models_normalize_decimal_date_and_datetime_inputs() -> None:
    invoice = APInvoice(
        id="inv-1",
        vendor="Example Electric",
        invoice_number="A-100",
        amount="$1,250.00",  # type: ignore[arg-type]
        invoice_date="2026-07-14T09:30:00",  # type: ignore[arg-type]
    )
    change = VendorChangeEvent(
        id="change-1",
        vendor_id="vendor-1",
        field="bank_account",
        changed_by="controller",
        changed_at="2026-07-14T09:30:00",  # type: ignore[arg-type]
    )

    assert invoice.amount == Decimal("1250.00")
    assert invoice.invoice_date == date(2026, 7, 14)
    assert change.changed_at == datetime(2026, 7, 14, 9, 30)
    assert change.source_refs == []


def test_required_finance_dates_fail_early_with_field_context() -> None:
    with pytest.raises(ValueError, match="invoice date is required"):
        dateize(None, field_name="invoice date")
    with pytest.raises(ValueError, match="posted at must be an ISO datetime"):
        datetimeize(123, field_name="posted at")  # type: ignore[arg-type]


def test_duplicate_detector_coerces_external_rows_at_the_boundary() -> None:
    candidates = APDuplicateDetector().find_duplicates(
        [
            {
                "id": "inv-1",
                "vendor": "Example Electric LLC",
                "invoice_number": "A-100",
                "amount": "100.00",
                "invoice_date": "2026-07-10",
            },
            {
                "id": "inv-2",
                "vendor": "Example Electric",
                "invoice_number": "A100",
                "amount": "$100.00",
                "invoice_date": "2026-07-11",
            },
        ]
    )

    assert len(candidates) == 1
    assert candidates[0].invoice_ids == ["inv-1", "inv-2"]
    assert candidates[0].confidence == Decimal("1.00")


def test_cash_variance_uses_forecast_week_values() -> None:
    engine = ThirteenWeekCashForecast()
    forecast = engine.forecast(
        open_ar=[{"id": "ar-1", "amount": "100", "due_date": "2026-07-14"}],
        open_ap=[{"id": "ap-1", "amount": "20", "due_date": "2026-07-15"}],
        payroll=[],
        recurring=[],
        starting_cash="1000",
        start_date="2026-07-13",
    )

    report = engine.variance(
        [{"week_number": 1, "receipts": "90", "disbursements": "30"}],
        forecast,
    )

    assert len(report.variances) == 1
    variance = report.variances[0]
    assert variance.forecast_receipts == Decimal("100")
    assert variance.forecast_disbursements == Decimal("20")
    assert variance.net_difference == Decimal("-20")
    assert report.total_net_difference == Decimal("-20")


def test_exception_amount_is_normalized_before_scoring() -> None:
    exception = make_exception("B90", "Unexpected balance", amount="$2,500.00")

    assert exception.amount == Decimal("2500.00")


def test_labor_database_accepts_path_sequences_and_builds_search_index(tmp_path) -> None:
    csv_path = tmp_path / "labor.csv"
    csv_path.write_text(
        "source,section,subsection,description,normal,unit,table_index\n"
        "NECA,Conduit,Raceway,Install rigid conduit,1.25,LF,1\n",
        encoding="utf-8",
    )
    db_path = tmp_path / "labor.db"

    built = build_database((csv_path,), db_path, force=True)
    with LaborUnitDatabase(db_path=built, csv_paths=(csv_path,), auto_build=False) as database:
        results = database.search("rigid conduit")

    assert built == db_path
    assert len(results) == 1
    assert results[0].labor_unit.description == "Install rigid conduit"
    assert results[0].labor_unit.normal == 1.25
