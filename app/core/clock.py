from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo


def utc_now() -> datetime:
    return datetime.now(UTC)


def to_storage(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="seconds")


def from_storage(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def as_timezone(value: ZoneInfo | str) -> ZoneInfo:
    """把时区名称统一解析为 ZoneInfo，业务时区必须是显式、可复核的 IANA 名称。"""
    if isinstance(value, ZoneInfo):
        return value
    if not isinstance(value, str) or not value.strip():
        raise ValueError("业务时区不能为空")
    try:
        return ZoneInfo(value.strip())
    except Exception as exc:  # ZoneInfoNotFoundError 是 KeyError 的子类
        raise ValueError(f"无法识别的业务时区：{value}") from exc


def business_day_window(value: datetime, timezone: ZoneInfo | str) -> tuple[datetime, datetime]:
    """返回 value 所在业务自然日的半开区间 [当日零点, 次日零点)，统一换算为 UTC。

    存储层的 created_at 是 UTC ISO 字符串，因此把本地零点换算成 UTC 瞬时后，
    SQLite 中直接做字符串比较即可，跨夏令时也不会重复计数或漏计。
    """
    tz = as_timezone(timezone)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    local_midnight = value.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    next_midnight = local_midnight + timedelta(days=1)
    return local_midnight.astimezone(UTC), next_midnight.astimezone(UTC)


class Clock(Protocol):
    def now(self) -> datetime: ...


@dataclass(slots=True)
class SystemClock:
    def now(self) -> datetime:
        return utc_now()


@dataclass(slots=True)
class FrozenClock:
    current: datetime

    def now(self) -> datetime:
        return self.current

    def advance(self, **values: int) -> datetime:
        self.current += timedelta(**values)
        return self.current
