from __future__ import annotations

import asyncio
from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

_DOMAIN_BY_TOOL = {
    "get_order": "order",
    "get_order_items": "item",
    "get_payment_timeline": "payment",
    "get_shipment_summary": "shipment",
    "get_sellers": "seller",
    "get_refund_timeline": "refund",
    "get_policy": "policy",
}

_SELLER_ISSUES = {"late_delivery_seller", "unavailable_order_paid"}
_REFUND_ISSUES = {"refund_pending", "refund_failed"}


def _as_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _as_money(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _money_number(value: Any) -> float:
    return float(_as_money(value).quantize(Decimal("0.01")))


def _unique(values: Iterable[str | None]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if isinstance(value, str) and value and value not in seen:
            result.append(value)
            seen.add(value)
    return result


def _event_in_scope(
    event: dict[str, Any], purchase_at: datetime | None, opened_at: datetime | None
) -> bool:
    event_at = _as_datetime(event.get("event_at"))
    if event_at is None:
        return False
    if purchase_at is not None and event_at < purchase_at:
        return False
    return opened_at is None or event_at <= opened_at


def _select_current_items(
    raw_items: Any, purchase_at: datetime | None
) -> list[dict[str, Any]]:
    """Select the lifecycle-consistent row for every item identifier."""

    if not isinstance(raw_items, list):
        return []
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in raw_items:
        if not isinstance(row, dict):
            continue
        item_id = row.get("order_item_id")
        if isinstance(item_id, str) and item_id:
            groups.setdefault(item_id, []).append(row)

    selected: list[dict[str, Any]] = []
    for rows in groups.values():
        candidates: list[tuple[datetime, dict[str, Any]]] = []
        for row in rows:
            limit = _as_datetime(row.get("shipping_limit_date"))
            if limit is not None and (purchase_at is None or limit >= purchase_at):
                candidates.append((limit, row))
        selected.append(min(candidates, key=lambda pair: pair[0])[1] if candidates else rows[0])
    return selected


def _captured_events(
    payment_data: Any, purchase_at: datetime | None, opened_at: datetime | None
) -> list[dict[str, Any]]:
    if not isinstance(payment_data, dict):
        return []
    events = payment_data.get("events", [])
    if not isinstance(events, list):
        return []
    return [
        event
        for event in events
        if isinstance(event, dict)
        and event.get("event_type") == "captured"
        and event.get("status") == "confirmed"
        and _event_in_scope(event, purchase_at, opened_at)
    ]


def _expected_order_total(items: list[dict[str, Any]]) -> Decimal:
    return sum(
        (_as_money(item.get("price")) + _as_money(item.get("freight_value")) for item in items),
        Decimal("0"),
    )


def _shipment_issue(
    shipment_data: Any,
    items: list[dict[str, Any]],
    purchase_at: datetime | None,
    opened_at: datetime | None,
) -> str | None:
    if not isinstance(shipment_data, dict):
        return None

    carrier_at = _as_datetime(shipment_data.get("delivered_carrier_at"))
    estimated_at = _as_datetime(shipment_data.get("estimated_delivery_at"))
    delivered_at = _as_datetime(shipment_data.get("delivered_customer_at"))
    limits = [
        limit
        for limit in (_as_datetime(item.get("shipping_limit_date")) for item in items)
        if limit is not None
    ]
    shipping_limit = min(limits) if limits else None

    if carrier_at is not None and shipping_limit is not None and carrier_at > shipping_limit:
        return "late_delivery_seller"

    reference_at = opened_at or delivered_at
    late_at_reference = (
        estimated_at is not None
        and reference_at is not None
        and reference_at > estimated_at
        and (delivered_at is None or delivered_at > estimated_at)
    )
    if late_at_reference and (
        carrier_at is None or shipping_limit is None or carrier_at <= shipping_limit
    ):
        return "late_delivery_logistics"

    # A lifecycle event is a fallback only when the authoritative timestamps are
    # incomplete. This prevents a conflicting synthetic event from overriding a
    # delivery that is demonstrably on time.
    timestamps_complete = all(
        value is not None for value in (carrier_at, estimated_at, delivered_at, shipping_limit)
    )
    events = shipment_data.get("events", [])
    if not timestamps_complete and isinstance(events, list):
        for event in events:
            if not isinstance(event, dict) or not _event_in_scope(event, purchase_at, opened_at):
                continue
            if event.get("event_type") != "delivered_late" or event.get("status") != "confirmed":
                continue
            if event.get("actor") == "seller":
                return "late_delivery_seller"
            if event.get("actor") == "logistics_provider":
                return "late_delivery_logistics"
    return None


def _refund_issue(
    refund_data: Any, purchase_at: datetime | None, opened_at: datetime | None
) -> str | None:
    if not isinstance(refund_data, dict):
        return None
    events = refund_data.get("events", [])
    if not isinstance(events, list):
        return None
    scoped = [
        event
        for event in events
        if isinstance(event, dict) and _event_in_scope(event, purchase_at, opened_at)
    ]
    for event in sorted(
        scoped, key=lambda item: _as_datetime(item.get("event_at")) or datetime.min, reverse=True
    ):
        if event.get("status") == "failed":
            return "refund_failed"
        if event.get("status") == "pending":
            return "refund_pending"
    return None


def _derive_primary_issue(
    *,
    order_data: Any,
    items: list[dict[str, Any]],
    payment_data: Any,
    shipment_data: Any,
    refund_data: Any,
    opened_at: datetime | None,
) -> str:
    if not isinstance(order_data, dict):
        return "insufficient_evidence"
    purchase_at = _as_datetime(order_data.get("order_purchase_timestamp"))
    captured = _captured_events(payment_data, purchase_at, opened_at)
    order_status = order_data.get("order_status")

    if captured and order_status == "canceled":
        return "canceled_order_paid"
    if captured and order_status == "unavailable":
        return "unavailable_order_paid"

    refund_issue = _refund_issue(refund_data, purchase_at, opened_at)
    if refund_issue is not None:
        return refund_issue

    if isinstance(payment_data, dict):
        events = payment_data.get("events", [])
        if isinstance(events, list) and any(
            isinstance(event, dict)
            and event.get("event_type") == "reconciliation_mismatch"
            and event.get("status") == "open"
            and _event_in_scope(event, purchase_at, opened_at)
            for event in events
        ):
            return "payment_mismatch"

    shipment_issue = _shipment_issue(shipment_data, items, purchase_at, opened_at)
    if shipment_issue is not None:
        return shipment_issue

    if len(captured) > 1:
        captured_total = sum(
            (_as_money(event.get("amount_brl")) for event in captured), Decimal("0")
        )
        expected_total = _expected_order_total(items)
        if expected_total > 0 and abs(captured_total - expected_total) <= Decimal("0.01"):
            return "valid_split_payment"
        amounts = [_as_money(event.get("amount_brl")) for event in captured]
        if len(set(amounts)) < len(amounts) or captured_total > expected_total:
            return "duplicate_charge"

    return "unsupported_claim"


async def _call_with_retry(
    *,
    gateway: EvidenceGateway,
    trace: TraceWriter,
    case_id: str,
    actor: str,
    tool_name: str,
    arguments: dict[str, str],
    attempts: int = 3,
) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            evidence = await gateway.call(tool_name, case_id=case_id, **arguments)
            expected_domain = _DOMAIN_BY_TOOL[tool_name]
            if evidence.get("domain") != expected_domain:
                raise ValueError(
                    f"{tool_name} returned domain {evidence.get('domain')!r}, "
                    f"expected {expected_domain!r}"
                )
            trace.emit(
                case_id=case_id,
                event_type="tool_result_consumed",
                actor=actor,
                tool_name=tool_name,
                evidence_refs=[evidence["evidence_ref"]],
                attributes={"attempt": attempt, "domain": expected_domain},
            )
            return evidence
        except (RuntimeError, ValueError) as exc:
            last_error = exc
            if attempt < attempts:
                await asyncio.sleep(0.25 * attempt)
    raise RuntimeError(f"{tool_name} failed after {attempts} attempts: {last_error}")


async def _run_agent(
    *,
    gateway: EvidenceGateway,
    trace: TraceWriter,
    case_id: str,
    actor: str,
    decision_code: str,
    calls: list[tuple[str, dict[str, str]]],
) -> dict[str, dict[str, Any]]:
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target=actor,
        decision_code=decision_code,
        attributes={"tool_count": len(calls)},
    )
    results: dict[str, dict[str, Any]] = {}
    for tool_name, arguments in calls:
        results[tool_name] = await _call_with_retry(
            gateway=gateway,
            trace=trace,
            case_id=case_id,
            actor=actor,
            tool_name=tool_name,
            arguments=arguments,
        )
    refs = [result["evidence_ref"] for result in results.values()]
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor=actor,
        target="coordinator",
        decision_code="SPECIALIST_RESULT_READY",
        evidence_refs=refs,
        attributes={"result_count": len(results)},
    )
    return results


def _refs(results: dict[str, dict[str, Any]], tool_names: Iterable[str]) -> list[str]:
    return _unique(results[name]["evidence_ref"] for name in tool_names if name in results)


def _claim_verdict(topic: str, primary_issue: str) -> str:
    if topic == primary_issue:
        return "supported"
    if topic == "requested_full_refund":
        if primary_issue in {"canceled_order_paid", "unavailable_order_paid", "refund_failed"}:
            return "supported"
        if primary_issue in {
            "late_delivery_seller",
            "late_delivery_logistics",
            "payment_mismatch",
            "duplicate_charge",
            "refund_pending",
        }:
            return "partially_supported"
        if primary_issue == "insufficient_evidence":
            return "insufficient_evidence"
        return "unsupported"
    if primary_issue == "insufficient_evidence":
        return "insufficient_evidence"
    return "unsupported"


def _claim_evidence(
    topic: str, primary_issue: str, results: dict[str, dict[str, Any]]
) -> list[str]:
    issue_tools = {
        "canceled_order_paid": ["get_order", "get_payment_timeline"],
        "unavailable_order_paid": ["get_order", "get_payment_timeline"],
        "late_delivery_seller": [
            "get_order",
            "get_order_items",
            "get_shipment_summary",
            "get_sellers",
        ],
        "late_delivery_logistics": [
            "get_order",
            "get_order_items",
            "get_shipment_summary",
        ],
        "valid_split_payment": ["get_order", "get_order_items", "get_payment_timeline"],
        "payment_mismatch": ["get_order", "get_order_items", "get_payment_timeline"],
        "duplicate_charge": ["get_order", "get_order_items", "get_payment_timeline"],
        "refund_pending": ["get_order", "get_payment_timeline", "get_refund_timeline"],
        "refund_failed": ["get_order", "get_payment_timeline", "get_refund_timeline"],
        "unsupported_claim": [
            "get_order",
            "get_order_items",
            "get_payment_timeline",
            "get_shipment_summary",
        ],
        "insufficient_evidence": [],
    }
    tools = list(issue_tools[primary_issue])
    if topic == "requested_full_refund":
        tools.append("get_policy")
    return _refs(results, tools)


def _verify_output(
    output: dict[str, Any], case: dict[str, Any], available_refs: set[str]
) -> None:
    if output["case_id"] != case["case_id"]:
        raise ValueError("verifier: case_id mismatch")
    output_refs = set(output["evidence_refs"])
    if not output_refs or not output_refs <= available_refs:
        raise ValueError("verifier: output contains missing or unconsumed evidence")
    claim_ids = {claim["claim_id"] for claim in case["customer_request"]["claims"]}
    output_claim_ids = {claim["claim_id"] for claim in output.get("claim_assessments", [])}
    if output_claim_ids != claim_ids:
        raise ValueError("verifier: claim assessment inventory mismatch")
    refund = _as_money(output["financial_resolution"]["recommended_refund_brl"])
    line_total = sum(
        (_as_money(line["amount_brl"]) for line in output["financial_resolution"]["refund_lines"]),
        Decimal("0"),
    )
    if refund != line_total:
        raise ValueError("verifier: refund total does not equal refund lines")
    status = output["assessment"]["case_status"]
    if status == "no_action" and refund != 0:
        raise ValueError("verifier: no_action case cannot recommend a refund")
    for claim in output.get("claim_assessments", []):
        if not set(claim["evidence_refs"]) <= output_refs:
            raise ValueError("verifier: claim cites evidence outside top-level evidence_refs")


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Investigate one L3A case with scoped specialists and authoritative MCP evidence."""

    case_id = case["case_id"]
    request = case["customer_request"]
    order_id = request["claimed_order_id"]
    policy_version = case["policy_version"]
    claimed_topics = {
        claim.get("topic") for claim in request.get("claims", []) if isinstance(claim, dict)
    }

    results: dict[str, dict[str, Any]] = {}
    results.update(
        await _run_agent(
            gateway=gateway,
            trace=trace,
            case_id=case_id,
            actor="order-agent",
            decision_code="INVESTIGATE_ORDER_AND_ITEMS",
            calls=[
                ("get_order", {"order_id": order_id}),
                ("get_order_items", {"order_id": order_id}),
            ],
        )
    )
    results.update(
        await _run_agent(
            gateway=gateway,
            trace=trace,
            case_id=case_id,
            actor="payment-agent",
            decision_code="INVESTIGATE_PAYMENT_LIFECYCLE",
            calls=[("get_payment_timeline", {"order_id": order_id})],
        )
    )

    order_data = results["get_order"]["data"]
    purchase_at = _as_datetime(
        order_data.get("order_purchase_timestamp") if isinstance(order_data, dict) else None
    )
    items = _select_current_items(results["get_order_items"]["data"], purchase_at)

    order_status = order_data.get("order_status") if isinstance(order_data, dict) else None
    needs_shipment = order_status == "delivered" or bool(
        claimed_topics & {"late_delivery_seller", "late_delivery_logistics", "unsupported_claim"}
    )
    if needs_shipment:
        results.update(
            await _run_agent(
                gateway=gateway,
                trace=trace,
                case_id=case_id,
                actor="shipment-agent",
                decision_code="INVESTIGATE_SHIPMENT_TIMELINE",
                calls=[("get_shipment_summary", {"order_id": order_id})],
            )
        )

    if claimed_topics & _REFUND_ISSUES:
        results.update(
            await _run_agent(
                gateway=gateway,
                trace=trace,
                case_id=case_id,
                actor="refund-agent",
                decision_code="INVESTIGATE_REFUND_LIFECYCLE",
                calls=[("get_refund_timeline", {"order_id": order_id})],
            )
        )

    if claimed_topics & _SELLER_ISSUES:
        results.update(
            await _run_agent(
                gateway=gateway,
                trace=trace,
                case_id=case_id,
                actor="seller-agent",
                decision_code="RESOLVE_SELLER_SCOPE",
                calls=[("get_sellers", {"order_id": order_id})],
            )
        )

    results.update(
        await _run_agent(
            gateway=gateway,
            trace=trace,
            case_id=case_id,
            actor="policy-agent",
            decision_code="RESOLVE_POLICY_ENTITLEMENT",
            calls=[("get_policy", {"policy_version": policy_version})],
        )
    )

    opened_at = _as_datetime(case.get("opened_at"))
    primary_issue = _derive_primary_issue(
        order_data=order_data,
        items=items,
        payment_data=results["get_payment_timeline"]["data"],
        shipment_data=results.get("get_shipment_summary", {}).get("data"),
        refund_data=results.get("get_refund_timeline", {}).get("data"),
        opened_at=opened_at,
    )

    policy_data = results["get_policy"]["data"]
    rules = policy_data.get("rules", {}) if isinstance(policy_data, dict) else {}
    rule = rules.get(primary_issue, {}) if isinstance(rules, dict) else {}
    if not isinstance(rule, dict) or not rule:
        primary_issue = "insufficient_evidence"
        rule = {
            "case_status": "needs_investigation",
            "recommended_action": "manual_investigation",
            "refund_brl": 0,
            "responsible_parties": [{"party_type": "unknown", "party_id": None}],
        }

    parties = rule.get("responsible_parties", [])
    if not isinstance(parties, list):
        parties = [{"party_type": "unknown", "party_id": None}]
    responsible_parties = [
        {"party_type": party.get("party_type", "unknown"), "party_id": party.get("party_id")}
        for party in parties
        if isinstance(party, dict)
    ]
    if not responsible_parties:
        responsible_parties = [{"party_type": "unknown", "party_id": None}]

    item_ids = _unique(item.get("order_item_id") for item in items)
    item_seller_ids = _unique(item.get("seller_id") for item in items)
    policy_seller_ids = _unique(
        party.get("party_id")
        for party in responsible_parties
        if party.get("party_type") == "seller"
    )
    seller_ids = _unique([*item_seller_ids, *policy_seller_ids])
    conflicts: list[dict[str, Any]] = []
    if policy_seller_ids and set(policy_seller_ids) != set(item_seller_ids):
        conflicts.append(
            {
                "field": "root_cause_analysis.responsible_parties.seller_id",
                "sources": ["get_order_items", "get_policy"],
                "selected_source": "get_policy",
                "resolution_code": "POLICY_AUTHORITY_SELECTED",
            }
        )

    confidence = 0.4 if primary_issue == "insufficient_evidence" else (0.99 if conflicts else 1.0)
    claim_assessments = []
    for claim in request.get("claims", []):
        if not isinstance(claim, dict):
            continue
        topic = str(claim.get("topic", ""))
        claim_assessments.append(
            {
                "claim_id": claim["claim_id"],
                "verdict": _claim_verdict(topic, primary_issue),
                "confidence": confidence,
                "evidence_refs": _claim_evidence(topic, primary_issue, results),
            }
        )

    output_evidence = _unique(
        ref for claim in claim_assessments for ref in claim["evidence_refs"]
    )
    # Entity fields are conclusions too: retain the item evidence that supports
    # item/seller IDs, and seller evidence whenever that specialist was used.
    output_evidence = _unique(
        [
            *output_evidence,
            *_refs(results, ["get_order_items", "get_sellers"]),
        ]
    )
    if primary_issue != "insufficient_evidence":
        output_evidence = _unique([*output_evidence, results["get_policy"]["evidence_ref"]])

    refund_brl = _money_number(rule.get("refund_brl", 0))
    refund_lines = (
        [{"reason_code": primary_issue, "amount_brl": refund_brl, "entity_id": order_id}]
        if refund_brl > 0
        else []
    )
    action = str(rule.get("recommended_action", "manual_investigation"))
    output = {
        "schema_version": "day09-l3a-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": primary_issue,
            "case_status": rule.get("case_status", "needs_investigation"),
            "confidence": confidence,
        },
        "affected_entities": {
            "order_ids": _unique([order_data.get("order_id", order_id)]),
            "item_ids": item_ids,
            "seller_ids": seller_ids,
            "payment_references": [],
            "shipment_ids": [],
        },
        "claim_assessments": claim_assessments,
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": primary_issue.upper(), "rank": 1}],
            "responsible_parties": responsible_parties,
        },
        "evidence_refs": output_evidence,
        "data_conflicts": conflicts,
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": refund_brl,
            "refund_lines": refund_lines,
        },
        "resolution_actions": [action],
    }

    trace.emit(
        case_id=case_id,
        event_type="policy_decided",
        actor="policy-agent",
        target="coordinator",
        decision_code=primary_issue.upper(),
        evidence_refs=[results["get_policy"]["evidence_ref"]],
        attributes={"case_status": output["assessment"]["case_status"], "refund_brl": refund_brl},
    )
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="coordinator",
        target="verifier",
        decision_code="VERIFY_CANDIDATE_OUTPUT",
        evidence_refs=output_evidence,
        attributes={"primary_issue": primary_issue},
    )
    available_refs = {evidence["evidence_ref"] for evidence in results.values()}
    _verify_output(output, case, available_refs)
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier",
        target="coordinator",
        decision_code="VERIFICATION_PASSED",
        evidence_refs=output_evidence,
        attributes={"checks": 7, "conflict_count": len(conflicts)},
    )
    return output
