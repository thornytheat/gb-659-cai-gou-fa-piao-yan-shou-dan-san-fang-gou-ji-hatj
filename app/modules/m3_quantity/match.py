from decimal import Decimal

from app.errors import DomainError


def _decimal(value) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def quantity_account(
    ordered: Decimal,
    received: Decimal,
    returned: Decimal,
    shrinkage: Decimal = Decimal("0"),
    recovered: Decimal = Decimal("0"),
) -> dict:
    """一次算清履约账与实存账。

    履约账只用订单、验收和真实退货；盘亏与找回只影响实存账。
    """
    ordered, received, returned = _decimal(ordered), _decimal(received), _decimal(returned)
    shrinkage, recovered = _decimal(shrinkage), _decimal(recovered)
    if min(ordered, received, returned, shrinkage, recovered) < 0:
        raise DomainError("NEGATIVE_QUANTITY", "数量不能为负数", 422)
    net_received = received - returned
    if net_received < 0:
        raise DomainError("RETURN_EXCEEDS_RECEIPT", "退货数量不能超过验收数量", 422)
    unrecovered = shrinkage - recovered
    if unrecovered < 0:
        raise DomainError("RECOVERY_EXCEEDS_UNRECOVERED", "找回数量不能超过盘亏数量", 422)
    on_hand = net_received - unrecovered
    if on_hand < 0:
        raise DomainError("SHRINKAGE_EXCEEDS_ON_HAND", "盘亏数量不能超过实存数量", 422)
    open_qty = ordered - net_received
    status = "OVER_RECEIVED" if open_qty < 0 else "BALANCED" if open_qty == 0 else "OPEN"
    return {
        "ordered": str(ordered),
        "received": str(received),
        "returned": str(returned),
        "net_received": str(net_received),
        "open": str(open_qty),
        "status": status,
        "shrinkage": str(shrinkage),
        "recovered": str(recovered),
        "unrecovered": str(unrecovered),
        "on_hand": str(on_hand),
    }


def reconcile_lines(lines: list[dict]) -> bool:
    for line in lines:
        if Decimal(line["ordered"]) != Decimal(line["net_received"]) + Decimal(line["open"]):
            return False
        if "on_hand" in line:
            expected = (
                Decimal(line["received"])
                - Decimal(line["returned"])
                - Decimal(line.get("shrinkage", "0"))
                + Decimal(line.get("recovered", "0"))
            )
            if Decimal(line["on_hand"]) != expected:
                return False
    return True
