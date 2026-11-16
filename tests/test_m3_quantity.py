import threading
from decimal import Decimal

import pytest

import app.store as store
from app.errors import DomainError
from app.modules.m3_quantity.ledger import (
    ORDER,
    RECEIPT,
    RECOVERY,
    RETURN,
    SHRINKAGE,
    LedgerState,
    apply_quantity_event,
    build_snapshot,
)
from app.modules.m3_quantity.match import quantity_account, reconcile_lines
from app.modules.m3_quantity.units import same_dimension, to_pieces


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    from app.modules.m3_quantity import repository

    monkeypatch.setattr(store, "DATABASE_PATH", tmp_path / "quantity.sqlite3")
    return repository


def _seed(repo, key="SKU-A"):
    repo.apply_event(key, "PO-1", ORDER, "10")
    repo.apply_event(key, "GR-1", RECEIPT, "9")
    repo.apply_event(key, "RT-1", RETURN, "1")


# --- 单位 ---


def test_box_converts_to_pieces():
    assert to_pieces("3", "box", "12") == Decimal("36")


def test_kg_converts_to_grams():
    assert to_pieces("1.25", "kg") == Decimal("1250.00")


def test_negative_quantity_rejected():
    with pytest.raises(ValueError, match="NEGATIVE_QUANTITY"):
        to_pieces("-1", "piece")


def test_unknown_unit_rejected():
    with pytest.raises(ValueError, match="UNKNOWN_UNIT"):
        to_pieces("1", "pallet")


def test_same_dimension_groups_weight():
    assert same_dimension("kg", "g") and not same_dimension("kg", "piece")


# --- 双账规则 ---


def test_open_quantity_accounts_for_returns():
    result = quantity_account(Decimal("10"), Decimal("9"), Decimal("1"))
    assert result["net_received"] == "8" and result["open"] == "2"


def test_return_cannot_exceed_receipt():
    with pytest.raises(ValueError, match="RETURN_EXCEEDS_RECEIPT"):
        quantity_account(Decimal("10"), Decimal("1"), Decimal("2"))


def test_line_reconciliation_is_conservative():
    assert reconcile_lines([{"ordered": "10", "net_received": "8", "open": "2"}])


def test_line_reconciliation_checks_physical_balance():
    assert reconcile_lines([{"ordered": "10", "received": "9", "returned": "1", "shrinkage": "2", "recovered": "1", "net_received": "8", "open": "2", "on_hand": "7"}])


def test_shrinkage_is_separate_from_performance():
    state = LedgerState(ordered=Decimal("10"), accepted=Decimal("9"), returned=Decimal("1"))
    after = apply_quantity_event(state, SHRINKAGE, Decimal("2"))
    assert after.open_quantity == state.open_quantity == Decimal("2")
    assert after.on_hand == Decimal("6") and after.unrecovered == Decimal("2")


def test_recovery_reverses_shrinkage_only():
    state = LedgerState(ordered=Decimal("10"), accepted=Decimal("9"), returned=Decimal("1"), shrinkage=Decimal("2"))
    after = apply_quantity_event(state, RECOVERY, Decimal("1"))
    assert after.on_hand == Decimal("7") and after.unrecovered == Decimal("1")
    assert after.open_quantity == Decimal("2")


def test_recovery_cannot_exceed_unrecovered():
    state = LedgerState(accepted=Decimal("9"), shrinkage=Decimal("2"), recovered=Decimal("1"))
    with pytest.raises(DomainError, match="RECOVERY_EXCEEDS_UNRECOVERED"):
        apply_quantity_event(state, RECOVERY, Decimal("2"))


def test_real_return_reopens_open_quantity():
    state = LedgerState(ordered=Decimal("10"), accepted=Decimal("9"))
    after = apply_quantity_event(state, RETURN, Decimal("1"))
    assert after.open_quantity == Decimal("2") and after.on_hand == Decimal("8")


def test_return_limited_by_on_hand_not_by_accepted():
    state = LedgerState(ordered=Decimal("10"), accepted=Decimal("9"), shrinkage=Decimal("8"))
    with pytest.raises(DomainError, match="RETURN_EXCEEDS_ON_HAND"):
        apply_quantity_event(state, RETURN, Decimal("2"))
    assert apply_quantity_event(state, RETURN, Decimal("1")).on_hand == Decimal("0")


def test_shrinkage_cannot_drive_negative_on_hand():
    state = LedgerState(accepted=Decimal("3"))
    with pytest.raises(DomainError, match="SHRINKAGE_EXCEEDS_ON_HAND"):
        apply_quantity_event(state, SHRINKAGE, Decimal("4"))


def test_apply_event_does_not_mutate_state():
    state = LedgerState(ordered=Decimal("5"))
    apply_quantity_event(state, RECEIPT, Decimal("5"))
    assert state.accepted == Decimal("0") and state.version == 0


def test_snapshot_reconciles_both_ledgers():
    events = [
        {"event_id": "PO-1", "kind": ORDER, "quantity": "10", "version": 1},
        {"event_id": "GR-1", "kind": RECEIPT, "quantity": "9", "version": 2},
        {"event_id": "RT-1", "kind": RETURN, "quantity": "1", "version": 3},
        {"event_id": "LS-1", "kind": SHRINKAGE, "quantity": "2", "version": 4},
        {"event_id": "FD-1", "kind": RECOVERY, "quantity": "1", "version": 5},
    ]
    state = LedgerState(ordered=Decimal("10"), accepted=Decimal("9"), returned=Decimal("1"), shrinkage=Decimal("2"), recovered=Decimal("1"), version=5)
    snapshot = build_snapshot("SKU-A", state, events)
    assert snapshot["reconciled"]
    assert snapshot["sources"]["balanced"]
    assert snapshot["performance"]["open"] == "2"
    assert snapshot["physical"]["on_hand"] == "7"
    assert snapshot["physical"]["unrecovered"] == "1"


def test_snapshot_flags_balance_not_supported_by_events():
    state = LedgerState(accepted=Decimal("9"), version=1)
    snapshot = build_snapshot("SKU-A", state, [{"event_id": "GR-1", "kind": RECEIPT, "quantity": "8", "version": 1}])
    assert not snapshot["sources"]["balanced"] and not snapshot["reconciled"]


def test_snapshot_flags_missing_events():
    snapshot = build_snapshot("SKU-A", LedgerState(accepted=Decimal("9"), version=1), [])
    assert not snapshot["reconciled"]


# --- 持久化、幂等、并发、故障恢复 ---


def test_events_persist_and_snapshot_lists_sources(repo):
    _seed(repo)
    repo.apply_event("SKU-A", "LS-1", SHRINKAGE, "2")
    repo.apply_event("SKU-A", "FD-1", RECOVERY, "1", expected_version=4)
    snapshot = repo.get_snapshot("SKU-A")
    assert snapshot["version"] == 5
    assert snapshot["performance"]["open"] == "2"
    assert snapshot["physical"]["on_hand"] == "7"
    assert snapshot["sources"]["performance"]["RECEIPT"]["quantity"] == "9"
    assert snapshot["sources"]["physical"]["RECOVERY"]["quantity"] == "1"
    assert [event["event_id"] for event in snapshot["sources"]["events"]] == ["PO-1", "GR-1", "RT-1", "LS-1", "FD-1"]


def test_replaying_event_does_not_double_apply(repo):
    _seed(repo)
    result = repo.apply_event("SKU-A", "GR-1", RECEIPT, "9")
    assert result["replayed"] and not result["applied"]
    snapshot = repo.get_snapshot("SKU-A")
    assert snapshot["version"] == 3 and snapshot["performance"]["accepted"] == "9"


def test_replaying_recovery_never_double_credits(repo):
    _seed(repo)
    repo.apply_event("SKU-A", "LS-1", SHRINKAGE, "2")
    first = repo.apply_event("SKU-A", "FD-1", RECOVERY, "1", expected_version=4)
    replay = repo.apply_event("SKU-A", "FD-1", RECOVERY, "1", expected_version=4)
    assert first["applied"] and replay["replayed"]
    snapshot = repo.get_snapshot("SKU-A")
    assert snapshot["physical"]["recovered"] == "1" and snapshot["physical"]["on_hand"] == "7"


def test_event_id_with_different_quantity_is_conflict(repo):
    _seed(repo)
    with pytest.raises(DomainError, match="EVENT_ID_CONFLICT"):
        repo.apply_event("SKU-A", "GR-1", RECEIPT, "8")


def test_recovery_requires_expected_version(repo):
    _seed(repo)
    repo.apply_event("SKU-A", "LS-1", SHRINKAGE, "2")
    with pytest.raises(DomainError, match="VERSION_REQUIRED"):
        repo.apply_event("SKU-A", "FD-1", RECOVERY, "1")


def test_stale_recovery_rejected(repo):
    _seed(repo)
    repo.apply_event("SKU-A", "LS-1", SHRINKAGE, "2")
    repo.apply_event("SKU-A", "PO-2", ORDER, "1")
    with pytest.raises(DomainError, match="STALE_VERSION"):
        repo.apply_event("SKU-A", "FD-1", RECOVERY, "1", expected_version=4)


def test_concurrent_recovery_rejects_expired_state(repo):
    _seed(repo)
    repo.apply_event("SKU-A", "LS-1", SHRINKAGE, "2")
    version = repo.get_snapshot("SKU-A")["version"]
    barrier = threading.Barrier(2)
    outcomes = []

    def submit(event_id):
        barrier.wait()
        try:
            outcomes.append(("ok", repo.apply_event("SKU-A", event_id, RECOVERY, "1", expected_version=version)))
        except DomainError as error:
            outcomes.append(("rejected", error.code))

    threads = [threading.Thread(target=submit, args=(f"FD-{index}",)) for index in (1, 2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcome[0] for outcome in outcomes) == ["ok", "rejected"]
    rejected = [outcome[1] for outcome in outcomes if outcome[0] == "rejected"]
    assert rejected == ["STALE_VERSION"]
    snapshot = repo.get_snapshot("SKU-A")
    assert snapshot["physical"]["recovered"] == "1"
    assert snapshot["version"] == version + 1


def test_interrupted_write_keeps_balance_and_retry_applies_once(repo):
    _seed(repo)
    before = repo.get_snapshot("SKU-A")

    original = repo._upsert_state

    def explode(*args, **kwargs):
        raise RuntimeError("disk full")

    repo._upsert_state = explode
    try:
        with pytest.raises(RuntimeError, match="disk full"):
            repo.apply_event("SKU-A", "LS-1", SHRINKAGE, "2")
    finally:
        repo._upsert_state = original

    preserved = repo.get_snapshot("SKU-A")
    assert preserved == before

    applied = repo.apply_event("SKU-A", "LS-1", SHRINKAGE, "2")
    retry = repo.apply_event("SKU-A", "LS-1", SHRINKAGE, "2")
    assert applied["applied"] and retry["replayed"]
    after = repo.get_snapshot("SKU-A")
    assert after["physical"]["shrinkage"] == "2"
    assert after["version"] == before["version"] + 1
    assert len(after["sources"]["events"]) == len(before["sources"]["events"]) + 1


def test_batch_record_applies_each_event_once(repo):
    from app.modules.m3_quantity import service

    payload = {
        "business_key": "SKU-B",
        "events": [
            {"event_id": "PO-1", "kind": ORDER, "quantity": "4"},
            {"event_id": "GR-1", "kind": RECEIPT, "quantity": "4"},
            {"event_id": "GR-1", "kind": RECEIPT, "quantity": "4"},
        ],
    }
    first = service.record(payload)
    second = service.record(payload)
    assert first["applied"] == 2 and first["replayed"] == 1
    assert second["applied"] == 0 and second["replayed"] == 3
    assert repo.get_snapshot("SKU-B")["physical"]["on_hand"] == "4"


def test_concurrent_same_recovery_applies_once(repo):
    _seed(repo)
    repo.apply_event("SKU-A", "LS-1", SHRINKAGE, "2")
    version = repo.get_snapshot("SKU-A")["version"]
    barrier = threading.Barrier(2)
    outcomes = []

    def submit():
        barrier.wait()
        result = repo.apply_event("SKU-A", "FD-SAME", RECOVERY, "1", expected_version=version)
        outcomes.append(result["applied"])

    threads = [threading.Thread(target=submit) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == [False, True]
    snapshot = repo.get_snapshot("SKU-A")
    assert snapshot["physical"]["recovered"] == "1"
    assert snapshot["physical"]["on_hand"] == "7"
    assert snapshot["version"] == version + 1


def test_sample_demo_is_idempotent(repo):
    from app.modules.m3_quantity import service

    first = service.demo()
    second = service.demo()
    assert first["snapshot"] == second["snapshot"]
    assert first["snapshot"]["version"] == 5


# --- HTTP 入口 ---


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import app.main

    monkeypatch.setattr(store, "DATABASE_PATH", tmp_path / "api.sqlite3")
    return TestClient(app.main.app)


def test_api_records_events_and_returns_dual_balances(client):
    base = {"business_key": "SKU-A"}
    assert client.post("/api/quantity/events", json={**base, "event_id": "PO-1", "kind": ORDER, "quantity": "10"}).status_code == 200
    receipt = client.post("/api/quantity/events", json={**base, "event_id": "GR-1", "kind": RECEIPT, "quantity": "9"}).json()
    assert receipt["snapshot"]["performance"]["open"] == "1"
    loss = client.post("/api/quantity/events", json={**base, "event_id": "LS-1", "kind": SHRINKAGE, "quantity": "2"}).json()
    assert loss["snapshot"]["performance"]["open"] == "1"
    assert loss["snapshot"]["physical"]["on_hand"] == "7"
    recovery = client.post("/api/quantity/events", json={**base, "event_id": "FD-1", "kind": RECOVERY, "quantity": "1", "expected_version": 3}).json()
    assert recovery["snapshot"]["physical"]["on_hand"] == "8"
    assert recovery["snapshot"]["physical"]["unrecovered"] == "1"
    assert recovery["snapshot"]["reconciled"]


def test_api_rejects_stale_and_excess_recovery(client):
    base = {"business_key": "SKU-A"}
    for event_id, kind, quantity in (("PO-1", ORDER, "10"), ("GR-1", RECEIPT, "10"), ("LS-1", SHRINKAGE, "3")):
        assert client.post("/api/quantity/events", json={**base, "event_id": event_id, "kind": kind, "quantity": quantity}).status_code == 200
    stale = client.post("/api/quantity/events", json={**base, "event_id": "FD-1", "kind": RECOVERY, "quantity": "1", "expected_version": 1})
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "STALE_VERSION"
    excess = client.post("/api/quantity/events", json={**base, "event_id": "FD-1", "kind": RECOVERY, "quantity": "4", "expected_version": 3})
    assert excess.status_code == 422 and excess.json()["detail"]["code"] == "RECOVERY_EXCEEDS_UNRECOVERED"
    missing = client.post("/api/quantity/events", json={**base, "event_id": "FD-1", "kind": RECOVERY, "quantity": "1"})
    assert missing.status_code == 422 and missing.json()["detail"]["code"] == "VERSION_REQUIRED"


def test_api_replay_is_not_double_counted(client):
    payload = {"business_key": "SKU-A", "event_id": "GR-1", "kind": RECEIPT, "quantity": "5"}
    first = client.post("/api/quantity/events", json=payload).json()
    second = client.post("/api/quantity/events", json=payload).json()
    assert first["applied"] and second["replayed"]
    snapshot = client.get("/api/quantity/SKU-A").json()
    assert snapshot["physical"]["on_hand"] == "5" and snapshot["version"] == 1
    assert client.get("/api/quantity/NOPE").status_code == 404


def test_api_quantity_page_shows_both_ledgers(client):
    client.post("/api/quantity/events", json={"business_key": "SKU-A", "event_id": "GR-1", "kind": RECEIPT, "quantity": "5"})
    page = client.get("/quantity")
    assert page.status_code == 200
    for text in ("履约账", "实存账", "待收货 = 订单", "实存 = 验收", "SKU-A", "RECEIPT"):
        assert text in page.text

