"""Synthetic fixtures. Every number, town and name is made up."""

from __future__ import annotations

from xml.sax.saxutils import escape

# The entered code, and the different label number the parcel carries late in
# its journey.
CODE = "CV000000001US"
LATE_LABEL = "CR000000002US"
OTHER_CODE = "AB000000003CD00010G"
PAGE_VALUE = "0123456789abcdef0123456789abcdef"
NEXT_PAGE_VALUE = "fedcba9876543210fedcba9876543210"


def row(
    code: str,
    when: str,
    *,
    own: str | None = CODE,
    barcode: str | None = None,
    number: str = "",
    transit: str = "",
    wording: str = "Parcel registered",
    city: str = "Testville",
    country: str = "UKRAINE",
    **extra: str,
) -> dict[str, str]:
    """One scan row as the lookup returns it (all fields are text)."""
    fields = {
        "ShipmentIdRef": "0xAAAA0001",
        "DocumentIdRef": "0xBBBB0001",
        "ShipmentNumber": number,
        "ShipmentNumberSender": own or "",
        "ShipmentNumberTransit": transit,
        "Barcode": barcode if barcode is not None else (own or ""),
        "DateTimeAction": when,
        "Country_EN": country,
        "City_EN": city,
        "StatusCode": code,
        "ActionId": "3",
        "AgentCode": "TEST",
        "ActionMessages_EN": wording,
        "DetailPlacesAction": "1/1",
        "CountryDel": "UA",
    }
    fields.update(extra)
    return fields


def lookup_xml(rows: list[dict[str, str]], error_code: str = "000") -> bytes:
    """Serialise rows the way the lookup does."""
    items = "".join(
        "<items>"
        + "".join(f"<{k}>{escape(v)}</{k}>" for k, v in r.items())
        + "</items>"
        for r in rows
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><return>'
        "<api>API-1C</api><apiversion>1</apiversion>"
        f"<result_table>{items}</result_table><result_uuid></result_uuid>"
        f"<errors><code>{error_code}</code><name> </name></errors></return>"
    ).encode()


def page_html(value: str = PAGE_VALUE) -> bytes:
    return f"<html><script>var salt = '{value}';</script></html>".encode()


def journey() -> list[dict[str, str]]:
    """A parcel that changes number: the last rows carry another label."""
    return [
        row("100", "2026-10-01 09:00:00", wording="Parcel registered"),
        row("606", "2026-10-02 10:00:00", wording="Shipment accepted"),
        row("1315", "2026-10-05 08:00:00", number="", barcode=LATE_LABEL),
        row(
            "1622",
            "2026-10-05 11:37:53",
            number="",
            barcode=LATE_LABEL,
            wording="Delivered",
        ),
    ]


# --- account source (synthetic; the real shapes are not confirmed) -----------

ACCOUNT_LOGIN = "user@example.test"


def account_record(**overrides) -> dict:
    """A received parcel record."""
    record = {
        "parcelID": "P-1001",
        "parcelNumber": "CV000000001US",
        "barCode": "CR000000002US",
        "statusCode": "1315",
        "statusName": "Handed to the courier",
        "direction": 1,
        "estimatedDeliveryDate": "2026-10-05",
        "delivery": {
            "branch": {"name": "Branch 12", "address": "1 Test Street"},
            "timeFrom": "10:00",
            "timeTo": "12:00",
        },
        "sender": {"name": "Sender Name"},
        "receiver": {"firstName": "Rec", "lastName": "Eiver"},
        "placesWeight": 2,
    }
    record.update(overrides)
    return record


def account_sent_record(**overrides) -> dict:
    """A sent parcel record."""
    record = {
        "parcelID": "P-2002",
        "parcelNumber": "AB000000003CD00010G",
        "statusCode": "606",
        "direction": 2,
        "sending": {
            "branch": "Drop-off 3",
            "date": "2026-10-04",
            "timeFrom": "14:00",
            "timeTo": "16:00",
        },
        "sender": {"name": "Me"},
    }
    record.update(overrides)
    return record


def account_events() -> list[dict]:
    return [
        {
            "appCode": 0,
            "eventDateTime": "2026-10-01T09:00:00",
            "messages": "Created",
            "city": "Testville",
            "country": "UA",
        },
        {"appCode": 1, "eventDateTime": "2026-10-02T10:00:00", "messages": "Sent"},
    ]


def backfilled_registration() -> list[dict[str, str]]:
    """A partner feed registers the parcel again after it has already moved."""
    return [
        row("101", "2026-10-01 09:00:00", wording="Parcel/shipment registered"),
        row("202", "2026-10-02 10:00:00", wording="Arrival at outward office"),
        row("405", "2026-10-03 00:00:00", wording="Departure from sorting center"),
        row("100", "2026-10-03 11:00:00", wording="Parcel registered", country="PL"),
        row("100", "2026-10-03 11:02:00", wording="To be delivered to a locker"),
    ]
