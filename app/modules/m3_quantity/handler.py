from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.web import page

from .repository import get_snapshot
from .service import demo, kinds, ledger, record

router = APIRouter()


def _rows() -> list[dict]:
    rows = []
    for snapshot in ledger():
        performance = snapshot["performance"]
        physical = snapshot["physical"]
        rows.append(
            {
                "key": snapshot["business_key"],
                "status": f"待收 {performance['open']} · 实存 {physical['on_hand']}",
                "value": f"v{snapshot['version']}",
            }
        )
    return rows


@router.get("/quantity", response_class=HTMLResponse)
def view(request: Request):
    return page(
        request,
        "m3.html",
        "数量勾稽",
        "/api/quantity/match",
        _rows(),
        snapshots=ledger(),
        kinds=kinds(),
    )


@router.post("/api/quantity/match")
def execute(payload: dict[str, Any] | None = Body(default=None)):
    """空请求保留“运行样例”，带 business_key 时录入事件。"""
    if not payload:
        return demo()
    return record(payload)


@router.post("/api/quantity/events")
def create_event(payload: dict[str, Any] = Body(...)):
    return record(payload)


@router.get("/api/quantity/ledger")
def quantity_ledger():
    return {"entries": ledger()}


@router.get("/api/quantity/{business_key}")
def quantity_snapshot(business_key: str):
    snapshot = get_snapshot(business_key)
    if snapshot is None:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": f"没有业务键 {business_key} 的数量记录"})
    return snapshot
