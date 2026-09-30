from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.compute import router as compute_router
from app.compute.service import ComputeOperationsService
from app.core.clock import FrozenClock, business_day_window, to_storage
from app.database import get_connection


TEMPLATE = {
    "code": "solver-tz",
    "name": "时区边界求解模板",
    "algorithm": "solver-tz",
    "parameter_schema": {
        "iterations": {"type": "integer", "required": True, "minimum": 1, "maximum": 10000},
        "mode": {"type": "string", "required": True, "choices": ["fast", "accurate"]},
    },
    "default_parameters": {},
    "max_runtime_seconds": 300,
    "max_attempts": 2,
}

SHANGHAI = "Asia/Shanghai"
# 上海自然日 2026-09-30 对应的 UTC 窗口是 [16:00, 次日 16:00)
DAY_START_UTC = datetime(2026, 9, 29, 16, 0, tzinfo=UTC)
DAY_END_UTC = datetime(2026, 9, 30, 16, 0, tzinfo=UTC)


def submit_payload(key: str, *, user: str = "boundary-user", priority: int = 50) -> dict:
    return {
        "template_code": "solver-tz",
        "project_code": "project-a",
        "requested_by": user,
        "parameters": {"iterations": 100, "mode": "accurate"},
        "priority": priority,
        "idempotency_key": key,
    }


def install_frozen_service(monkeypatch, moment: datetime, *, timezone: str = SHANGHAI) -> FrozenClock:
    """注入固定时钟与显式业务时区；每次调用都新建服务实例，等价于进程重启。"""
    clock = FrozenClock(moment)

    def factory() -> ComputeOperationsService:
        return ComputeOperationsService(get_connection(), clock, timezone)

    monkeypatch.setattr(compute_router, "service", factory)
    return clock


def prepare(client, monkeypatch, moment: datetime, *, daily: int = 2) -> FrozenClock:
    clock = install_frozen_service(monkeypatch, moment)
    response = client.post("/api/compute/templates?actor=administrator", json=TEMPLATE)
    assert response.status_code == 201, response.text
    quota = client.put(
        "/api/compute/quotas?actor=administrator",
        json={
            "subject_type": "user",
            "subject_key": "boundary-user",
            "max_queued": 10,
            "max_running": 10,
            "daily_submissions": daily,
        },
    )
    assert quota.status_code == 200, quota.text
    return clock


def test_business_day_window_is_local_midnight_half_open_in_utc():
    start, end = business_day_window(datetime(2026, 9, 30, 7, 59, tzinfo=UTC), SHANGHAI)
    assert start == DAY_START_UTC
    assert end == DAY_END_UTC
    # 恰好落在本地零点的瞬时属于新的一天
    assert business_day_window(DAY_START_UTC, SHANGHAI)[0] == DAY_START_UTC
    assert to_storage(start) == "2026-09-29T16:00:00+00:00"
    assert to_storage(end) == "2026-09-30T16:00:00+00:00"


def test_daily_quota_resets_at_business_timezone_midnight_not_utc(client, monkeypatch):
    clock = prepare(client, monkeypatch, datetime(2026, 9, 29, 16, 30, tzinfo=UTC))

    first = client.post("/api/compute/tasks", json=submit_payload("tz-day-000001"))
    second = client.post("/api/compute/tasks", json=submit_payload("tz-day-000002"))
    assert first.status_code == second.status_code == 202

    # 同一上海自然日内（含跨过 UTC 零点）配额已耗尽
    clock.current = datetime(2026, 9, 29, 23, 59, tzinfo=UTC)  # 上海 09-30 07:59
    blocked_before_utc_midnight = client.post("/api/compute/tasks", json=submit_payload("tz-day-000003"))
    assert blocked_before_utc_midnight.status_code == 409
    assert blocked_before_utc_midnight.json()["error"]["message"] == "用户当日提交配额已用尽"

    clock.current = datetime(2026, 9, 30, 0, 0, tzinfo=UTC)  # 上海 08:00，UTC 已切日但业务日未变
    blocked_at_utc_midnight = client.post("/api/compute/tasks", json=submit_payload("tz-day-000004"))
    assert blocked_at_utc_midnight.status_code == 409

    clock.current = datetime(2026, 9, 30, 15, 59, 59, tzinfo=UTC)  # 上海 23:59:59
    blocked_last_second = client.post("/api/compute/tasks", json=submit_payload("tz-day-000005"))
    assert blocked_last_second.status_code == 409

    # 模拟重启：新建服务实例并让历史任务留在 SQLite 中，上海零点（UTC 16:00）后配额重置
    install_frozen_service(monkeypatch, datetime(2026, 9, 30, 16, 0, tzinfo=UTC))
    next_day_one = client.post("/api/compute/tasks", json=submit_payload("tz-day-000006"))
    assert next_day_one.status_code == 202, next_day_one.text
    next_day_two = client.post("/api/compute/tasks", json=submit_payload("tz-day-000007"))
    assert next_day_two.status_code == 202, next_day_two.text

    # 新的一天同样只有 daily_submissions 个名额，旧任务不会重复计数到新窗口
    blocked_again = client.post("/api/compute/tasks", json=submit_payload("tz-day-000008"))
    assert blocked_again.status_code == 409


def test_idempotent_retry_across_midnight_does_not_consume_new_day_quota(client, monkeypatch):
    clock = prepare(client, monkeypatch, datetime(2026, 9, 29, 15, 59, tzinfo=UTC), daily=1)

    original = client.post("/api/compute/tasks", json=submit_payload("tz-retry-000001"))
    assert original.status_code == 202
    original_id = original.json()["id"]

    # 客户端在响应丢失后于次日零点后用同一幂等键重试，必须返回原任务且不占用新一天配额
    clock.current = datetime(2026, 9, 29, 16, 0, 1, tzinfo=UTC)  # 上海 10-01 00:00:01
    retried = client.post("/api/compute/tasks", json=submit_payload("tz-retry-000001"))
    assert retried.status_code == 202
    assert retried.json()["id"] == original_id
    assert retried.json()["created_at"] == original.json()["created_at"]

    fresh = client.post("/api/compute/tasks", json=submit_payload("tz-retry-000002", priority=90))
    assert fresh.status_code == 202, fresh.text

    # 工作者失败后可重试地重新排队不会新增提交计数：新一天的日配额仍为 1
    claimed = client.post(
        "/api/compute/tasks/claim",
        json={"worker_id": "w-tz", "capabilities": ["solver-tz"], "lease_seconds": 60},
    )
    assert claimed.status_code == 200 and claimed.json()["task"]["id"] == fresh.json()["id"]
    failed = client.post(
        f"/api/compute/tasks/{fresh.json()['id']}/fail",
        json={"worker_id": "w-tz", "error_code": "numeric_error", "message": "数值不收敛", "retryable": True},
    )
    assert failed.status_code == 200 and failed.json()["status"] == "queued"

    exhausted = client.post("/api/compute/tasks", json=submit_payload("tz-retry-000003"))
    assert exhausted.status_code == 409


def test_explicit_fixed_offset_timezone_is_used_for_midnight(client, monkeypatch):
    # 加德满都 UTC+05:45：本地零点对应 UTC 18:15（前一天），证明边界不依赖 UTC 整点
    clock = install_frozen_service(monkeypatch, datetime(2026, 9, 29, 18, 14, tzinfo=UTC), timezone="Asia/Kathmandu")
    client.post("/api/compute/templates?actor=administrator", json=TEMPLATE)
    client.put(
        "/api/compute/quotas?actor=administrator",
        json={"subject_type": "user", "subject_key": "boundary-user", "max_queued": 10, "max_running": 10, "daily_submissions": 1},
    )
    accepted = client.post("/api/compute/tasks", json=submit_payload("tz-ktm-000001"))
    assert accepted.status_code == 202
    blocked = client.post("/api/compute/tasks", json=submit_payload("tz-ktm-000002"))
    assert blocked.status_code == 409

    # UTC 18:15 即加德满都次日 00:00，配额重置
    clock.current = datetime(2026, 9, 29, 18, 15, tzinfo=UTC)
    reopened = client.post("/api/compute/tasks", json=submit_payload("tz-ktm-000003"))
    assert reopened.status_code == 202, reopened.text


def test_invalid_business_timezone_is_rejected():
    from app.core.clock import as_timezone

    with pytest.raises(ValueError):
        as_timezone("Antarctica/Reserve-Does-Not-Exist")
