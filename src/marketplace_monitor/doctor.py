"""Config + credential readiness check (``--check``).

A quick operational sanity pass before a real run: does the config parse, which
marketplaces are enabled, and are the credentials each enabled source / delivery
channel / the LLM needs actually present? Reports ok / warn / fail per item so
you catch a missing secret before the scheduled run does.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from .adapters.registry import _REGISTRY
from .config import Config

OK, WARN, FAIL = "ok", "warn", "fail"


@dataclass
class Check:
    status: str
    label: str
    detail: str = ""


def _env_present(names: list[str]) -> tuple[bool, list[str]]:
    missing = [n for n in names if not os.environ.get(n)]
    return (not missing, missing)


def run_checks(cfg: Config) -> list[Check]:
    checks: list[Check] = []

    enabled = cfg.enabled_marketplaces()
    if not enabled:
        checks.append(Check(WARN, "marketplaces", "no marketplaces enabled"))

    # LLM scoring key — needed whenever there's anything to score.
    ok, _ = _env_present(["ANTHROPIC_API_KEY"])
    checks.append(
        Check(OK if ok else FAIL, "scoring", "ANTHROPIC_API_KEY present"
              if ok else "ANTHROPIC_API_KEY missing (required to score listings)")
    )

    # Per-marketplace credential readiness.
    for mc in enabled:
        cls = _REGISTRY.get(mc.name)
        if cls is None:
            checks.append(Check(FAIL, f"marketplace:{mc.name}", "unknown marketplace"))
            continue
        needed = cls.required_env(mc.options)
        ok, missing = _env_present(needed)
        n_searches = len(mc.searches)
        if not n_searches:
            checks.append(Check(WARN, f"marketplace:{mc.name}", "enabled but no searches configured"))
        elif ok:
            detail = f"{n_searches} searches" + (f", env: {', '.join(needed)} ✓" if needed else "")
            checks.append(Check(OK, f"marketplace:{mc.name}", detail))
        else:
            checks.append(
                Check(FAIL, f"marketplace:{mc.name}", f"missing env: {', '.join(missing)}")
            )

    # Delivery readiness.
    method = (cfg.delivery.method or "console").lower()
    if method == "console":
        checks.append(Check(OK, "delivery", "console (prints digest)"))
    elif method == "smtp":
        ok, missing = _env_present(["SMTP_HOST"])
        has_to = bool(cfg.delivery.to)
        if ok and has_to:
            checks.append(Check(OK, "delivery", f"smtp -> {', '.join(cfg.delivery.to)}"))
        else:
            problems = missing + ([] if has_to else ["delivery.to"])
            checks.append(Check(FAIL, "delivery", f"smtp missing: {', '.join(problems)}"))
    elif method == "resend":
        ok, missing = _env_present(["RESEND_API_KEY"])
        has_to = bool(cfg.delivery.to)
        if ok and has_to:
            checks.append(Check(OK, "delivery", f"resend -> {', '.join(cfg.delivery.to)}"))
        else:
            problems = missing + ([] if has_to else ["delivery.to"])
            checks.append(Check(FAIL, "delivery", f"resend missing: {', '.join(problems)}"))
    else:
        checks.append(Check(FAIL, "delivery", f"unknown method: {cfg.delivery.method}"))

    # Instant alerts (optional).
    if cfg.alerts.enabled:
        needed = {
            "telegram": ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"],
            "discord": ["DISCORD_WEBHOOK_URL"],
        }.get(cfg.alerts.channel, [])
        ok, missing = _env_present(needed)
        if not needed:
            checks.append(Check(FAIL, "alerts", f"unknown channel: {cfg.alerts.channel}"))
        elif ok:
            checks.append(Check(OK, "alerts", f"{cfg.alerts.channel} (>= {cfg.alerts.min_score})"))
        else:
            checks.append(Check(FAIL, "alerts", f"missing env: {', '.join(missing)}"))

    return checks


def format_checks(checks: list[Check]) -> str:
    icon = {OK: "✓", WARN: "!", FAIL: "✗"}
    lines = [f"  [{icon[c.status]}] {c.label}: {c.detail}" for c in checks]
    return "\n".join(lines)


def worst_status(checks: list[Check]) -> str:
    if any(c.status == FAIL for c in checks):
        return FAIL
    if any(c.status == WARN for c in checks):
        return WARN
    return OK
