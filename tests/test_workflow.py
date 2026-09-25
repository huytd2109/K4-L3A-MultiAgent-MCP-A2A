from datetime import datetime

from student_agent.workflow import _derive_primary_issue, _shipment_issue


def dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def test_conflicting_late_event_does_not_override_on_time_timestamps() -> None:
    purchase = dt("2018-08-14T09:00:00-03:00")
    opened = dt("2018-08-26T09:00:00-03:00")
    items = [{"shipping_limit_date": "2018-08-17T09:00:00-03:00"}]
    shipment = {
        "delivered_carrier_at": "2018-08-16T09:00:00-03:00",
        "delivered_customer_at": "2018-08-23T09:00:00-03:00",
        "estimated_delivery_at": "2018-08-24T09:00:00-03:00",
        "events": [
            {
                "event_at": "2018-08-20T09:00:00-03:00",
                "event_type": "delivered_late",
                "actor": "logistics_provider",
                "status": "confirmed",
            }
        ],
    }
    assert _shipment_issue(shipment, items, purchase, opened) is None


def test_shipping_timestamps_distinguish_seller_and_logistics_delay() -> None:
    purchase = dt("2018-03-23T09:00:00-03:00")
    opened = dt("2018-04-04T09:00:00-03:00")
    items = [{"shipping_limit_date": "2018-03-26T09:00:00-03:00"}]
    base = {
        "delivered_customer_at": "2018-04-07T09:00:00-03:00",
        "estimated_delivery_at": "2018-04-02T09:00:00-03:00",
        "events": [],
    }
    assert (
        _shipment_issue(
            {**base, "delivered_carrier_at": "2018-03-27T09:00:00-03:00"},
            items,
            purchase,
            opened,
        )
        == "late_delivery_seller"
    )
    assert (
        _shipment_issue(
            {**base, "delivered_carrier_at": "2018-03-25T09:00:00-03:00"},
            items,
            purchase,
            opened,
        )
        == "late_delivery_logistics"
    )


def test_payment_amounts_distinguish_valid_split_and_duplicate() -> None:
    order = {
        "order_status": "delivered",
        "order_purchase_timestamp": "2018-04-23T09:00:00-03:00",
    }
    items = [{"price": "79.00", "freight_value": "10.00"}]
    opened = dt("2018-05-05T09:00:00-03:00")

    def payment(first: str, second: str) -> dict:
        return {
            "events": [
                {
                    "event_at": "2018-04-23T10:00:00-03:00",
                    "event_type": "captured",
                    "amount_brl": first,
                    "status": "confirmed",
                },
                {
                    "event_at": "2018-04-23T11:00:00-03:00",
                    "event_type": "captured",
                    "amount_brl": second,
                    "status": "confirmed",
                },
            ]
        }

    common = {
        "order_data": order,
        "items": items,
        "shipment_data": None,
        "refund_data": None,
        "opened_at": opened,
    }
    assert (
        _derive_primary_issue(payment_data=payment("44.50", "44.50"), **common)
        == "valid_split_payment"
    )
    assert (
        _derive_primary_issue(payment_data=payment("64.00", "64.00"), **common)
        == "duplicate_charge"
    )
