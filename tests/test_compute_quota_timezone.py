from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.compute.router import get_service
from app.compute.service import ComputeOperationsService
from app.core.clock import FrozenClock, business_day_window, to_storage
from app.core.config import Settings
from app.core.errors import ConflictError, ValidationError
from app.database import close_connection, get_connection

SHANGHAI = "Asia/Shanghai"

TEMPLATE = {
    "code": "solver-tz",
    "name": "迁徙观测计算模板",
    "algorithm": "solver-tz",
    "parameter_schema": {
        "iterations": {"type": "integer", "required": True, "minimum": 1, "maximum": 10000},
    },
    "default_parameters": {},
    "max_runtime_seconds": 300,
    "max_attempts": 2,
}


def submit_payload(key: str, *, user: str = "nightowl") -> dict:
    return {
        "template_code": "solver-tz",
        "project_code": "reserve-a",
        "requested_by": user,
        "parameters": {"iterations": 100},
        "priority": 50,
        "idempotency_key": key,
    }


@pytest.fixture()
def frozen_compute(client):
    """冻结业务时钟并显式指定业务时区，所有 /api/compute 请求共用同一时钟。"""
    client.post("/api/compute/templates?actor=administrator", json=TEMPLATE).raise_for_status()
    clock = FrozenClock(datetime(2026, 9, 29, 0, 0, tzinfo=UTC))

    def factory() -> ComputeOperationsService:
        return ComputeOperationsService(get_connection(), clock, SHANGHAI)

    client.app.dependency_overrides[get_service] = factory
    try:
        yield client, clock
    finally:
        client.app.dependency_overrides.pop(get_service, None)


def set_daily_quota(client, daily: int, *, user: str = "nightowl") -> None:
    response = client.put(
        "/api/compute/quotas?actor=administrator",
        json={"subject_type": "user", "subject_key": user, "max_queued": 10, "max_running": 10, "daily_submissions": daily},
    )
    assert response.status_code == 200, response.text


def test_daily_quota_cuts_at_business_timezone_midnight_exact_boundary(frozen_compute):
    # 北京时间（UTC+8）自然日零点 = UTC 16:00；配额必须在该绝对时刻切日。
    client, clock = frozen_compute
    set_daily_quota(client, daily=1)

    # 北京 2026-09-29 23:59:59：当日最后一次提交成功
    clock.current = datetime(2026, 9, 29, 15, 59, 59, tzinfo=UTC)
    tail = client.post("/api/compute/tasks", json=submit_payload("day-a-tail"))
    assert tail.status_code == 202, tail.text

    # 一秒后即北京 2026-09-30 00:00:00（UTC 仍是 9 日）：新自然日配额生效，
    # 旧实现按 UTC 零点切日会在这里错误地报“配额已用尽”。
    clock.current = datetime(2026, 9, 29, 16, 0, 0, tzinfo=UTC)
    head = client.post("/api/compute/tasks", json=submit_payload("day-b-head"))
    assert head.status_code == 202, head.text

    # 北京 9 月 30 日 23:59:30：与 day-b-head 同一业务自然日，必须被拒绝
    clock.current = datetime(2026, 9, 30, 15, 59, 30, tzinfo=UTC)
    blocked = client.post("/api/compute/tasks", json=submit_payload("day-b-tail"))
    assert blocked.status_code == 409
    assert blocked.json()["error"]["message"] == "用户当日提交配额已用尽"

    # 北京 10 月 1 日 00:00:30：再次切日，配额重新可用
    clock.current = datetime(2026, 9, 30, 16, 0, 30, tzinfo=UTC)
    next_day = client.post("/api/compute/tasks", json=submit_payload("day-c-head"))
    assert next_day.status_code == 202, next_day.text


def test_daily_quota_does_not_reset_at_utc_midnight(frozen_compute):
    # 同一北京时间自然日被 UTC 零点（北京 08:00）劈成两半：旧实现会在 UTC 零点
    # 错误地重置配额，从而“绕过限制”；这里第二次提交必须仍被拒绝。
    client, clock = frozen_compute
    set_daily_quota(client, daily=1)

    clock.current = datetime(2026, 9, 29, 23, 0, 0, tzinfo=UTC)  # 北京 09-30 07:00
    first = client.post("/api/compute/tasks", json=submit_payload("utc-split-1"))
    assert first.status_code == 202, first.text

    clock.current = datetime(2026, 9, 30, 1, 0, 0, tzinfo=UTC)  # 北京 09-30 09:00（UTC 已跨日）
    second = client.post("/api/compute/tasks", json=submit_payload("utc-split-2"))
    assert second.status_code == 409
    assert second.json()["error"]["message"] == "用户当日提交配额已用尽"


def test_quota_window_survives_restart_and_matches_historical_tasks(client):
    # 重启前写入历史任务
    client.post("/api/compute/templates?actor=administrator", json=TEMPLATE).raise_for_status()
    clock = FrozenClock(datetime(2026, 9, 29, 15, 30, tzinfo=UTC))  # 北京 09-29 23:30
    service = ComputeOperationsService(get_connection(), clock, SHANGHAI)
    service.set_quota(
        {"subject_type": "user", "subject_key": "nightowl", "max_queued": 10, "max_running": 10, "daily_submissions": 1},
        "administrator",
    )
    assert service.submit(submit_payload("restart-day-a"))["status"] == "queued"

    # 模拟进程重启：关闭线程连接，下一次取连接得到全新的 sqlite3 连接
    close_connection()
    clock.current = datetime(2026, 9, 29, 16, 30, tzinfo=UTC)  # 北京 09-30 00:30
    restarted = ComputeOperationsService(get_connection(), clock, SHANGHAI)
    first_new_day = restarted.submit(submit_payload("restart-day-b"))
    assert first_new_day["status"] == "queued"

    # 同一业务自然日内再次提交：历史计数经重启后仍被正确读取，不能漏计
    clock.current = datetime(2026, 9, 30, 15, 59, tzinfo=UTC)  # 北京 09-30 23:59
    with pytest.raises(ConflictError, match="当日提交配额"):
        restarted.submit(submit_payload("restart-day-c"))

    # 直接复核 SQLite 里按业务日窗口统计的结果：两个业务日各恰好 1 条，不重不漏
    repository = restarted.repository
    assert repository.count_user_submissions_between(
        "nightowl", to_storage(datetime(2026, 9, 29, 16, 0, tzinfo=UTC)), to_storage(datetime(2026, 9, 30, 16, 0, tzinfo=UTC))
    ) == 1
    assert repository.count_user_submissions_between(
        "nightowl", to_storage(datetime(2026, 9, 28, 16, 0, tzinfo=UTC)), to_storage(datetime(2026, 9, 29, 16, 0, tzinfo=UTC))
    ) == 1


def test_failure_retry_and_idempotent_resubmit_do_not_consume_quota_again(frozen_compute):
    # 失败重试（自动退避、幂等重放、人工重试）都不是新提交，不能重复消耗当日配额。
    client, clock = frozen_compute
    set_daily_quota(client, daily=1)
    clock.current = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)

    created = client.post("/api/compute/tasks", json=submit_payload("retry-key"))
    assert created.status_code == 202, created.text
    task_id = created.json()["id"]

    claimed = client.post("/api/compute/tasks/claim", json={"worker_id": "w1", "capabilities": ["solver-tz"], "lease_seconds": 60})
    assert claimed.status_code == 200 and claimed.json()["task"]["id"] == task_id
    failed = client.post(
        f"/api/compute/tasks/{task_id}/fail",
        json={"worker_id": "w1", "error_code": "numeric_error", "message": "不收敛", "retryable": True},
    )
    assert failed.status_code == 200 and failed.json()["status"] == "queued"

    # 退避结束后自动重试第二次尝试
    clock.advance(seconds=2)
    claimed_again = client.post("/api/compute/tasks/claim", json={"worker_id": "w1", "capabilities": ["solver-tz"], "lease_seconds": 60})
    assert claimed_again.status_code == 200 and claimed_again.json()["task"]["attempt_count"] == 2
    exhausted = client.post(
        f"/api/compute/tasks/{task_id}/fail",
        json={"worker_id": "w1", "error_code": "numeric_error", "message": "仍不收敛", "retryable": True},
    )
    assert exhausted.status_code == 200 and exhausted.json()["status"] == "failed"

    # 同一幂等键重放：返回原任务，不报配额错误，幂等语义不变
    replay = client.post("/api/compute/tasks", json=submit_payload("retry-key"))
    assert replay.status_code == 202 and replay.json()["id"] == task_id

    # 新的幂等键是真正的第二次提交：仍须被当日配额拒绝
    blocked = client.post("/api/compute/tasks", json=submit_payload("brand-new-key"))
    assert blocked.status_code == 409
    assert blocked.json()["error"]["message"] == "用户当日提交配额已用尽"

    # 人工重试把任务重新排队，同样不应消耗配额
    manual = client.post(
        f"/api/compute/tasks/{task_id}/retry",
        json={"actor": "administrator", "reason": "算法更新后重算", "priority": 90},
    )
    assert manual.status_code == 200 and manual.json()["status"] == "queued"
    still_blocked = client.post("/api/compute/tasks", json=submit_payload("another-new-key"))
    assert still_blocked.status_code == 409


def test_business_timezone_is_configurable_via_environment(client, monkeypatch):
    # 加德满都 UTC+05:45：自然日零点 = UTC 前一日 18:15，验证时区来自显式配置而非主机环境
    client.post("/api/compute/templates?actor=administrator", json=TEMPLATE).raise_for_status()
    monkeypatch.setenv("TOWNSHIP_BUSINESS_TIMEZONE", "Asia/Kathmandu")
    clock = FrozenClock(datetime(2026, 9, 29, 18, 14, 59, tzinfo=UTC))
    client.app.dependency_overrides[get_service] = lambda: ComputeOperationsService(get_connection(), clock)
    client.put(
        "/api/compute/quotas?actor=administrator",
        json={"subject_type": "user", "subject_key": "nightowl", "max_queued": 10, "max_running": 10, "daily_submissions": 1},
    ).raise_for_status()

    tail = client.post("/api/compute/tasks", json=submit_payload("ktm-day-a"))
    assert tail.status_code == 202, tail.text
    clock.current = datetime(2026, 9, 29, 18, 15, 0, tzinfo=UTC)  # 加德满都新一天零点
    head = client.post("/api/compute/tasks", json=submit_payload("ktm-day-b"))
    assert head.status_code == 202, head.text
    clock.current = datetime(2026, 9, 29, 18, 15, 30, tzinfo=UTC)
    blocked = client.post("/api/compute/tasks", json=submit_payload("ktm-day-b-tail"))
    assert blocked.status_code == 409


def test_business_day_window_handles_fixed_offset_and_dst_transitions():
    sh = ZoneInfo("Asia/Shanghai")
    moment = datetime(2026, 9, 29, 16, 30, tzinfo=UTC)  # 北京 09-30 00:30
    start, end = business_day_window(moment, sh)
    assert start == datetime(2026, 9, 29, 16, 0, tzinfo=UTC)
    assert end == datetime(2026, 9, 30, 16, 0, tzinfo=UTC)

    berlin = ZoneInfo("Europe/Berlin")
    spring = datetime(2026, 3, 29, 12, 0, tzinfo=UTC)   # 柏林夏令时跳转日，自然日长 23 小时
    start, end = business_day_window(spring, berlin)
    assert start.astimezone(UTC) == datetime(2026, 3, 28, 23, 0, tzinfo=UTC)
    assert end.astimezone(UTC) == datetime(2026, 3, 29, 22, 0, tzinfo=UTC)
    # 两端共用同一 tzinfo 对象，直接相减按挂钟时间计算；比较绝对时长须先转 UTC
    assert end.astimezone(UTC) - start.astimezone(UTC) == timedelta(hours=23)
    autumn = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)  # 柏林回拨日，自然日长 25 小时
    start, end = business_day_window(autumn, berlin)
    assert end.astimezone(UTC) - start.astimezone(UTC) == timedelta(hours=25)


def test_invalid_business_timezone_configuration_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("TOWNSHIP_DATABASE_PATH", str(tmp_path / "invalid.db"))
    monkeypatch.setenv("TOWNSHIP_BUSINESS_TIMEZONE", "Mars/Olympus")
    with pytest.raises(ValidationError):
        Settings.load()
