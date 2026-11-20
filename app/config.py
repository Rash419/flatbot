"""Settings from the environment."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ConfigError(Exception):
    pass


DEFAULT_MAIL_SENDERS = (
    "swiggy", "zomato", "blinkit", "zepto", "bigbasket", "amazon", "flipkart", "instamart",
    "dunzo", "dmart", "jiomart", "licious", "countrydelight", "eatsure", "dominos", "magicpin",
)
# Bank alert emails: matched against the From address like MAIL_SENDERS, read like bank SMS.
# "bank.in" covers banks on the RBI-mandated .bank.in domains.
DEFAULT_BANK_SENDERS = (
    "hdfcbank", "icicibank", "sbi.co.in", "sbicard", "axisbank", "kotak", "idfcfirstbank", "yesbank",
    "indusind", "federalbank", "aubank", "bankofbaroda", "pnb", "canarabank", "unionbank", "rblbank",
    "bank.in",
)
EFFORTS = ("minimal", "low", "medium", "high", "xhigh")
MIN_MAIL_POLL = 30


@dataclass(frozen=True)
class Config:
    telegram_token: str
    public_url: str = ""
    port: int = 8080
    db_path: str = "data/bot.db"
    currency: str = "INR"
    timezone: str = "Asia/Kolkata"
    telegram_api_base: str = "https://api.telegram.org"
    codex_model: str = ""          # empty: Codex's default model
    codex_effort: str = "low"
    ai_timeout: float = 120
    mail_senders: tuple[str, ...] = DEFAULT_MAIL_SENDERS
    bank_senders: tuple[str, ...] = DEFAULT_BANK_SENDERS
    mail_poll_seconds: float = 120


def load_config(env: Mapping[str, str] | None = None) -> Config:
    """Read settings from `env` (default: os.environ). Raises ConfigError listing every problem."""
    env = os.environ if env is None else env

    def get(name: str, default: str = "") -> str:
        return (env.get(name) or "").strip() or default

    problems = []
    token = get("TELEGRAM_BOT_TOKEN")
    if not token:
        problems.append("TELEGRAM_BOT_TOKEN is not set (create a bot with @BotFather and copy its token)")

    port_raw = get("PORT", "8080")
    try:
        port = int(port_raw)
        if not 0 < port < 65536:
            raise ValueError
    except ValueError:
        problems.append(f"PORT must be a number between 1 and 65535, got {port_raw!r}")
        port = 0

    public_url = get("PUBLIC_URL").rstrip("/")
    if public_url and not public_url.startswith(("http://", "https://")):
        problems.append(f"PUBLIC_URL must start with http:// or https://, got {public_url!r}")

    tz = get("TIMEZONE", "Asia/Kolkata")
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        problems.append(f"TIMEZONE is not a known time zone, got {tz!r} (example: Asia/Kolkata)")

    effort = get("CODEX_EFFORT", "low").lower()
    if effort not in EFFORTS:
        problems.append(f"CODEX_EFFORT must be one of {', '.join(EFFORTS)}, got {effort!r}")

    def seconds(name: str, default: float) -> float:
        raw = get(name, str(default))
        try:
            value = float(raw)
            if not value > 0 or value == float("inf"):
                raise ValueError
            return value
        except ValueError:
            problems.append(f"{name} must be a number of seconds, got {raw!r}")
            return default

    ai_timeout = seconds("AI_TIMEOUT", 120)
    mail_poll = max(seconds("MAIL_POLL_SECONDS", 120), MIN_MAIL_POLL)

    def sender_list(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
        raw = env.get(name)
        if raw is None or not raw.strip():
            return default
        return tuple(dict.fromkeys(s.strip().lower() for s in raw.split(",") if s.strip()))

    if problems:
        raise ConfigError("Invalid configuration:\n  - " + "\n  - ".join(problems))

    return Config(
        telegram_token=token,
        public_url=public_url,
        port=port,
        db_path=get("DB_PATH", "data/bot.db"),
        currency=get("CURRENCY", "INR").upper(),
        timezone=tz,
        telegram_api_base=get("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/"),
        codex_model=get("CODEX_MODEL"),
        codex_effort=effort,
        ai_timeout=ai_timeout,
        mail_senders=sender_list("MAIL_SENDERS", DEFAULT_MAIL_SENDERS),
        bank_senders=sender_list("BANK_SENDERS", DEFAULT_BANK_SENDERS),
        mail_poll_seconds=mail_poll,
    )
