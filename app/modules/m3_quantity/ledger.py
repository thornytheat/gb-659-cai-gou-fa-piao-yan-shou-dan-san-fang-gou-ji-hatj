"""数量双账：履约账与实存账分开核算。

- 履约账回答“供应商还欠多少”：订单 -（验收 - 真实退货）。
- 实存账回答“仓库里还有多少”：验收 - 真实退货 - 盘亏 + 找回。
- 盘亏只减少实存，不重开待收货；找回冲回盘亏，且不得超过未找回数量。
- 只有真实退货同时减少实存并重开待收货。
"""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from app.errors import DomainError

ORDER = "ORDER"
RECEIPT = "RECEIPT"
RETURN = "RETURN"
SHRINKAGE = "SHRINKAGE"
RECOVERY = "RECOVERY"

EVENT_KINDS = (ORDER, RECEIPT, RETURN, SHRINKAGE, RECOVERY)
KIND_LABELS = {
    ORDER: "订单",
    RECEIPT: "验收",
    RETURN: "退货",
    SHRINKAGE: "盘亏",
    RECOVERY: "找回",
}
PERFORMANCE_SOURCES = (ORDER, RECEIPT, RETURN)
PHYSICAL_SOURCES = (RECEIPT, RETURN, SHRINKAGE, RECOVERY)

ZERO = Decimal("0")


def format_quantity(value: Decimal) -> str:
    """稳定输出 Decimal 字符串，不引入科学计数法或多余的尾零。"""
    text = format(value.normalize(), "f")
    return "0" if text in ("", "-0") else text


def parse_quantity(value) -> Decimal:
    try:
        quantity = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise DomainError("INVALID_QUANTITY", "数量必须是有效数字", 422)
    if not quantity.is_finite() or quantity <= ZERO:
        raise DomainError("INVALID_QUANTITY", "数量必须为正数", 422)
    return quantity


@dataclass(frozen=True)
class QuantityEvent:
    event_id: str
    business_key: str
    kind: str
    quantity: Decimal


def validate_event(event_id: str, business_key: str, kind: str, quantity) -> QuantityEvent:
    event_id = str(event_id or "").strip()
    business_key = str(business_key or "").strip()
    kind = str(kind or "").strip().upper()
    if not event_id:
        raise DomainError("MISSING_EVENT_ID", "事件编号不能为空", 422)
    if not business_key:
        raise DomainError("MISSING_BUSINESS_KEY", "业务键不能为空", 422)
    if kind not in EVENT_KINDS:
        raise DomainError("UNKNOWN_EVENT_KIND", f"未知事件类型：{kind}", 422)
    return QuantityEvent(event_id=event_id, business_key=business_key, kind=kind, quantity=parse_quantity(quantity))


@dataclass(frozen=True)
class LedgerState:
    ordered: Decimal = ZERO
    accepted: Decimal = ZERO
    returned: Decimal = ZERO
    shrinkage: Decimal = ZERO
    recovered: Decimal = ZERO
    version: int = 0

    @property
    def net_accepted(self) -> Decimal:
        return self.accepted - self.returned

    @property
    def open_quantity(self) -> Decimal:
        """待收货量只由订单、验收和真实退货决定，盘亏与找回不得触碰。"""
        return self.ordered - self.net_accepted

    @property
    def unrecovered(self) -> Decimal:
        return self.shrinkage - self.recovered

    @property
    def on_hand(self) -> Decimal:
        """实存余额：仓储损失扣实存，找回补回实存。"""
        return self.accepted - self.returned - self.shrinkage + self.recovered


def apply_quantity_event(state: LedgerState, kind: str, quantity: Decimal) -> LedgerState:
    """返回新状态，不修改入参，便于事务失败时保留原余额。"""
    if kind not in EVENT_KINDS:
        raise DomainError("UNKNOWN_EVENT_KIND", f"未知事件类型：{kind}", 422)
    quantity = parse_quantity(quantity)

    if kind == ORDER:
        return LedgerState(
            ordered=state.ordered + quantity,
            accepted=state.accepted,
            returned=state.returned,
            shrinkage=state.shrinkage,
            recovered=state.recovered,
            version=state.version + 1,
        )

    if kind == RECEIPT:
        return LedgerState(
            ordered=state.ordered,
            accepted=state.accepted + quantity,
            returned=state.returned,
            shrinkage=state.shrinkage,
            recovered=state.recovered,
            version=state.version + 1,
        )

    if kind == RETURN:
        # 退货必须有实物可退；它同时减少实存并重开待收货量。
        if quantity > state.on_hand:
            raise DomainError(
                "RETURN_EXCEEDS_ON_HAND",
                f"退货数量 {format_quantity(quantity)} 超过实存可退数量 {format_quantity(state.on_hand)}",
                422,
            )
        return LedgerState(
            ordered=state.ordered,
            accepted=state.accepted,
            returned=state.returned + quantity,
            shrinkage=state.shrinkage,
            recovered=state.recovered,
            version=state.version + 1,
        )

    if kind == SHRINKAGE:
        # 盘亏只扣实存；待收货量保持不变，避免把仓储损失算成供应商少交货。
        if quantity > state.on_hand:
            raise DomainError(
                "SHRINKAGE_EXCEEDS_ON_HAND",
                f"盘亏数量 {format_quantity(quantity)} 超过实存数量 {format_quantity(state.on_hand)}",
                422,
            )
        return LedgerState(
            ordered=state.ordered,
            accepted=state.accepted,
            returned=state.returned,
            shrinkage=state.shrinkage + quantity,
            recovered=state.recovered,
            version=state.version + 1,
        )

    # RECOVERY：冲回原盘亏，且不得超过未找回数量。
    if quantity > state.unrecovered:
        raise DomainError(
            "RECOVERY_EXCEEDS_UNRECOVERED",
            f"找回数量 {format_quantity(quantity)} 超过未找回数量 {format_quantity(state.unrecovered)}",
            422,
        )
    return LedgerState(
        ordered=state.ordered,
        accepted=state.accepted,
        returned=state.returned,
        shrinkage=state.shrinkage,
        recovered=state.recovered + quantity,
        version=state.version + 1,
    )


def _performance_status(state: LedgerState) -> str:
    if state.open_quantity < ZERO:
        return "OVER_RECEIVED"
    if state.open_quantity == ZERO:
        return "BALANCED"
    return "OPEN"


def _physical_status(state: LedgerState) -> str:
    if state.unrecovered > ZERO and state.on_hand == ZERO:
        return "SHORTAGE"
    if state.on_hand == ZERO:
        return "EMPTY"
    return "AVAILABLE"


def _source_summary(events: list[dict], kinds: tuple[str, ...]) -> dict:
    summary = {}
    for kind in kinds:
        rows = [event for event in events if event["kind"] == kind]
        if rows:
            total = sum((Decimal(event["quantity"]) for event in rows), start=ZERO)
            summary[kind] = {"event_count": len(rows), "quantity": format_quantity(total)}
    return summary


def replay_events(events: list[dict]) -> LedgerState | None:
    """按事件流重算余额，用于核对持久化余额确实由这些来源推出。"""
    state = LedgerState()
    try:
        for event in sorted(events, key=lambda item: (item.get("version", 0), item.get("event_id", ""))):
            state = apply_quantity_event(state, event["kind"], Decimal(str(event["quantity"])))
    except (DomainError, InvalidOperation):
        return None
    return state


def _state_matches(left: LedgerState, right: LedgerState) -> bool:
    return (
        left.ordered == right.ordered
        and left.accepted == right.accepted
        and left.returned == right.returned
        and left.shrinkage == right.shrinkage
        and left.recovered == right.recovered
    )


def build_snapshot(business_key: str, state: LedgerState, events: list[dict] | None = None) -> dict:
    events = list(events or [])
    # 两套账的唯一勾稽桥：实存 = 净验收 - 未找回盘亏。
    cross_balanced = state.on_hand == state.net_accepted - state.unrecovered
    derived = replay_events(events)
    sources_balanced = derived is not None and _state_matches(derived, state)

    performance_status = _performance_status(state)
    physical_status = _physical_status(state)
    performance = {
        "ordered": format_quantity(state.ordered),
        "accepted": format_quantity(state.accepted),
        "returned": format_quantity(state.returned),
        "net_accepted": format_quantity(state.net_accepted),
        "open": format_quantity(state.open_quantity),
        "status": performance_status,
        "formula": "待收货 = 订单 - 验收 + 退货",
        "sources": _source_summary(events, PERFORMANCE_SOURCES),
    }
    physical = {
        "accepted": format_quantity(state.accepted),
        "returned": format_quantity(state.returned),
        "shrinkage": format_quantity(state.shrinkage),
        "recovered": format_quantity(state.recovered),
        "on_hand": format_quantity(state.on_hand),
        "unrecovered": format_quantity(state.unrecovered),
        "status": physical_status,
        "formula": "实存 = 验收 - 退货 - 盘亏 + 找回",
        "sources": _source_summary(events, PHYSICAL_SOURCES),
    }
    return {
        "business_key": business_key,
        "version": state.version,
        "performance": performance,
        "physical": physical,
        "sources": {
            "performance": performance["sources"],
            "physical": physical["sources"],
            "events": events,
            "balanced": sources_balanced,
        },
        "reconciled": cross_balanced and sources_balanced,
    }
