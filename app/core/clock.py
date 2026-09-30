from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_BUSINESS_TIMEZONE = "Asia/Shanghai"


def utc_now() -> datetime:
    return datetime.now(UTC)


def resolve_timezone(name: str) -> tzinfo:
    """按 IANA 名称加载业务时区，名称非法时给出明确的配置错误。"""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"不支持的业务时区：{name!r}") from exc


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


def business_day_start(value: datetime, timezone: tzinfo) -> datetime:
    """返回 value 所在业务自然日的零点（绝对时刻，带时区）。

    先把时刻转换到业务时区再取当地零点；若输入为朴素时间则按 UTC 解释。
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    local = value.astimezone(timezone)
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


def business_day_window(value: datetime, timezone: tzinfo) -> tuple[datetime, datetime]:
    """返回业务自然日的半开区间 [当日零点, 次日零点)。

    次日零点按本地挂钟时间推进一天再附着时区，避免夏令时切换日被当成固定的 24 小时长度。
    """
    start = business_day_start(value, timezone)
    end = (start.replace(tzinfo=None) + timedelta(days=1)).replace(tzinfo=timezone)
    return start, end


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
