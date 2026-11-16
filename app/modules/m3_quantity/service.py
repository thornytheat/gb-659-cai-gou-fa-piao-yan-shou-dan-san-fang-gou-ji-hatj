from app.errors import DomainError

from .ledger import EVENT_KINDS, KIND_LABELS, ORDER, RECEIPT, RECOVERY, RETURN, SHRINKAGE
from .repository import apply_event, get_snapshot, list_snapshots

DEMO_KEY = "SKU-A"
DEMO_EVENTS = (
    ("DEMO-PO-1", ORDER, "10"),
    ("DEMO-GR-1", RECEIPT, "9"),
    ("DEMO-RT-1", RETURN, "1"),
    ("DEMO-LOSS-1", SHRINKAGE, "2"),
    ("DEMO-FIND-1", RECOVERY, "1"),
)

def _record(payload: dict) -> dict:
    return apply_event(
        payload.get("business_key", ""),
        payload.get("event_id", ""),
        payload.get("kind", ""),
        payload.get("quantity", ""),
        payload.get("expected_version"),
    )


def run(payload: dict | None = None) -> dict:
    """录入一笔数量事件并返回最新双账快照；同一事件重放不会重复扣补。"""
    return _record(payload or {})


def record(payload: dict) -> dict:
    payload = payload or {}
    events = payload.get("events")
    if events is None:
        return run(payload)
    if not isinstance(events, list) or not events:
        raise DomainError("INVALID_EVENTS", "events 必须是非空数组", 422)
    results = [
        _record({**event, "business_key": event.get("business_key", payload.get("business_key", ""))})
        for event in events
    ]
    return {
        "applied": sum(1 for item in results if item["applied"]),
        "replayed": sum(1 for item in results if item["replayed"]),
        "snapshot": results[-1]["snapshot"],
    }


def demo() -> dict:
    """幂等样例：订单 10、验收 9、退货 1、盘亏 2、找回 1。"""
    for event_id, kind, quantity in DEMO_EVENTS:
        for attempt in range(3):
            current = get_snapshot(DEMO_KEY)
            expected = current["version"] if current and kind == RECOVERY else None
            try:
                apply_event(DEMO_KEY, event_id, kind, quantity, expected_version=expected)
                break
            except DomainError as error:
                # 并发点击样例时另一请求可能刚推进版本；重读后重试，最多三轮。
                if error.code != "STALE_VERSION" or attempt == 2:
                    raise
    return {"business_key": DEMO_KEY, "snapshot": get_snapshot(DEMO_KEY)}


def ledger() -> list[dict]:
    return list_snapshots()


def kinds() -> list[dict]:
    return [{"kind": kind, "label": KIND_LABELS[kind]} for kind in EVENT_KINDS]
