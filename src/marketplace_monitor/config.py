"""Load and validate ``config.yaml``.

FR-9: preferences, marketplaces, location, thresholds, and schedule are all
editable in one config file with no code changes. This module turns that YAML
into typed objects the rest of the pipeline consumes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .models import SearchSpec

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class LocationConfig:
    zip_code: str
    radius_mi: int = 40
    label: str | None = None
    latitude: float | None = None
    longitude: float | None = None


@dataclass
class PrefilterConfig:
    # A global ceiling applied when a search does not set its own max_price.
    max_price: float | None = None
    max_distance_mi: float | None = None
    exclude_keywords: list[str] = field(default_factory=list)
    exclude_categories: list[str] = field(default_factory=list)


@dataclass
class ScoringConfig:
    model: str = "claude-haiku-4-5"
    threshold: int = 60
    max_results: int = 40
    batch_size: int = 8          # listings per LLM request (micro-batch)
    use_batch_api: bool = False  # submit as a 50%-off Batch job
    preferences_path: str = "preferences.md"


@dataclass
class DeliveryConfig:
    method: str = "console"          # console | smtp | resend
    to: list[str] = field(default_factory=list)
    from_addr: str = "marketplace-monitor@localhost"
    subject_prefix: str = "Marketplace Monitor"
    send_when_empty: bool = True
    group_by: str = "category"        # category | source | none
    # SMTP-specific (values may reference env vars via ${VAR} in the YAML).
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None


@dataclass
class MarketplaceConfig:
    name: str
    enabled: bool = True
    searches: list[SearchSpec] = field(default_factory=list)
    options: dict = field(default_factory=dict)


@dataclass
class Config:
    location: LocationConfig
    marketplaces: list[MarketplaceConfig]
    prefilter: PrefilterConfig
    scoring: ScoringConfig
    delivery: DeliveryConfig
    raw: dict = field(default_factory=dict)

    def enabled_marketplaces(self) -> list[MarketplaceConfig]:
        return [m for m in self.marketplaces if m.enabled]


def _expand_env(value):
    """Recursively expand ${VAR} references so secrets can live in env vars."""
    if isinstance(value, str):
        return os.path.expandvars(value)
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    return value


def _parse_searches(raw_searches: list, default_max_price: float | None) -> list[SearchSpec]:
    specs: list[SearchSpec] = []
    for item in raw_searches or []:
        if isinstance(item, str):
            specs.append(SearchSpec(query=item, max_price=default_max_price))
            continue
        specs.append(
            SearchSpec(
                query=item["query"],
                category=item.get("category"),
                max_price=item.get("max_price", default_max_price),
                min_price=item.get("min_price"),
                extra={k: v for k, v in item.items()
                       if k not in {"query", "category", "max_price", "min_price"}},
            )
        )
    return specs


def load_config(path: str | os.PathLike | None = None) -> Config:
    path = Path(path) if path else REPO_ROOT / "config.yaml"
    with open(path, "r", encoding="utf-8") as fh:
        data = _expand_env(yaml.safe_load(fh) or {})

    loc = data.get("location", {})
    location = LocationConfig(
        zip_code=str(loc.get("zip_code", "")),
        radius_mi=int(loc.get("radius_mi", 40)),
        label=loc.get("label"),
        latitude=loc.get("latitude"),
        longitude=loc.get("longitude"),
    )

    pf = data.get("prefilter", {})
    prefilter = PrefilterConfig(
        max_price=pf.get("max_price"),
        max_distance_mi=pf.get("max_distance_mi", location.radius_mi),
        exclude_keywords=[k.lower() for k in pf.get("exclude_keywords", [])],
        exclude_categories=[c.lower() for c in pf.get("exclude_categories", [])],
    )

    sc = data.get("scoring", {})
    scoring = ScoringConfig(
        model=sc.get("model", "claude-haiku-4-5"),
        threshold=int(sc.get("threshold", 60)),
        max_results=int(sc.get("max_results", 40)),
        batch_size=int(sc.get("batch_size", 8)),
        use_batch_api=bool(sc.get("use_batch_api", False)),
        preferences_path=sc.get("preferences_path", "preferences.md"),
    )

    dv = data.get("delivery", {})
    delivery = DeliveryConfig(
        method=dv.get("method", "console"),
        to=dv.get("to", []) if isinstance(dv.get("to"), list) else [dv["to"]] if dv.get("to") else [],
        from_addr=dv.get("from", "marketplace-monitor@localhost"),
        subject_prefix=dv.get("subject_prefix", "Marketplace Monitor"),
        send_when_empty=bool(dv.get("send_when_empty", True)),
        group_by=dv.get("group_by", "category"),
        smtp_host=dv.get("smtp_host"),
        smtp_port=int(dv.get("smtp_port", 587)),
        smtp_user=dv.get("smtp_user"),
        smtp_password=dv.get("smtp_password"),
    )

    default_max = prefilter.max_price
    marketplaces: list[MarketplaceConfig] = []
    for m in data.get("marketplaces", []):
        marketplaces.append(
            MarketplaceConfig(
                name=m["name"],
                enabled=bool(m.get("enabled", True)),
                searches=_parse_searches(m.get("searches", []), default_max),
                options=m.get("options", {}),
            )
        )

    return Config(
        location=location,
        marketplaces=marketplaces,
        prefilter=prefilter,
        scoring=scoring,
        delivery=delivery,
        raw=data,
    )
