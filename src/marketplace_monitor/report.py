"""Render the daily digest (section 9, FR-7).

An HTML email, ranked highest-score first, optionally grouped by category or
source. Each item shows a score badge, linked title, price, distance, source,
posted time, thumbnail, and the LLM's one-line reason. A header line summarizes
coverage: how many were scanned vs reported, per marketplace.
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from datetime import datetime

from .models import ScoredListing


@dataclass
class RunSummary:
    fetched_by_source: dict[str, int] = field(default_factory=dict)
    total_fetched: int = 0
    new_after_dedupe: int = 0
    after_near_dup: int = 0
    near_dups_collapsed: int = 0
    after_prefilter: int = 0
    scored: int = 0
    reported: int = 0
    alerts_sent: int = 0
    adapter_errors: list[str] = field(default_factory=list)
    date: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d"))

    def to_dict(self) -> dict:
        """Structured per-run log (NFR observability, section 4)."""
        return {
            "date": self.date,
            "fetched_by_source": self.fetched_by_source,
            "total_fetched": self.total_fetched,
            "new_after_dedupe": self.new_after_dedupe,
            "near_dups_collapsed": self.near_dups_collapsed,
            "after_near_dup": self.after_near_dup,
            "after_prefilter": self.after_prefilter,
            "scored": self.scored,
            "reported": self.reported,
            "alerts_sent": self.alerts_sent,
            "adapter_errors": self.adapter_errors,
        }


def _badge_color(score: int) -> str:
    if score >= 80:
        return "#1a7f37"  # green
    if score >= 60:
        return "#9a6700"  # amber
    return "#57606a"      # grey


def _group_key(item: ScoredListing, group_by: str) -> str:
    if group_by == "source":
        return item.listing.source.title()
    if group_by == "category":
        return (item.listing.category or "Other").title()
    return "Matches"


def _fmt_price(item: ScoredListing) -> str:
    p = item.listing.price
    return f"${p:,.0f}" if p is not None else "price n/a"


def render_html(items: list[ScoredListing], summary: RunSummary, group_by: str = "category") -> str:
    coverage = " · ".join(f"{src}: {n}" for src, n in sorted(summary.fetched_by_source.items()))
    header = (
        f"<p style='color:#57606a;font-size:13px;margin:0 0 16px'>"
        f"{summary.date} — scanned {summary.total_fetched} "
        f"({html.escape(coverage) or 'no sources'}), "
        f"{summary.new_after_dedupe} new, {summary.scored} scored, "
        f"<b>{summary.reported} reported</b>.</p>"
    )

    if not items:
        body = "<p>Nothing cleared the threshold today.</p>"
    else:
        groups: dict[str, list[ScoredListing]] = {}
        for item in items:
            groups.setdefault(_group_key(item, group_by), []).append(item)

        sections = []
        for group in sorted(groups, key=lambda g: -max(i.score for i in groups[g])):
            rows = "".join(_render_item(item) for item in groups[group])
            sections.append(
                f"<h2 style='font-size:16px;border-bottom:1px solid #d0d7de;"
                f"padding-bottom:4px;margin:24px 0 8px'>{html.escape(group)}</h2>{rows}"
            )
        body = "".join(sections)

    errors = ""
    if summary.adapter_errors:
        items_html = "".join(f"<li>{html.escape(e)}</li>" for e in summary.adapter_errors)
        errors = (
            "<p style='color:#cf222e;font-size:12px;margin-top:24px'>"
            f"Some sources had trouble:</p><ul style='color:#cf222e;font-size:12px'>{items_html}</ul>"
        )

    return (
        "<div style='font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;"
        "max-width:640px;margin:0 auto;color:#1f2328'>"
        "<h1 style='font-size:20px;margin:0 0 4px'>Local Marketplace Monitor</h1>"
        f"{header}{body}{errors}</div>"
    )


def _render_item(item: ScoredListing) -> str:
    l = item.listing
    color = _badge_color(item.score)
    thumb = ""
    if l.image_url:
        thumb = (
            f"<img src='{html.escape(l.image_url)}' alt='' "
            "style='width:72px;height:72px;object-fit:cover;border-radius:6px;"
            "margin-right:12px;flex:none'>"
        )
    meta_bits = [_fmt_price(item)]
    if l.distance_mi is not None:
        meta_bits.append(f"{l.distance_mi:.0f} mi")
    if l.location:
        meta_bits.append(html.escape(l.location))
    meta_bits.append(html.escape(l.source))
    if l.posted_at:
        meta_bits.append(l.posted_at.strftime("%b %d"))
    meta = " · ".join(meta_bits)

    badge = (
        f"<span style='display:inline-block;min-width:34px;text-align:center;"
        f"background:{color};color:#fff;border-radius:12px;font-size:12px;"
        f"font-weight:600;padding:2px 8px;margin-right:10px;flex:none'>{item.score}</span>"
    )
    title = (
        f"<a href='{html.escape(l.url)}' style='color:#0969da;text-decoration:none;"
        f"font-weight:600;font-size:15px'>{html.escape(l.title)}</a>"
    )
    reason = (
        f"<div style='color:#57606a;font-size:13px;margin-top:2px'>{html.escape(item.reason)}</div>"
        if item.reason else ""
    )
    cross = l.raw.get("cross_posts") if isinstance(l.raw, dict) else None
    if cross:
        reason += (
            f"<div style='color:#8250df;font-size:12px;margin-top:2px'>"
            f"also cross-posted on {len(cross)} other marketplace(s)</div>"
        )
    return (
        "<div style='display:flex;align-items:flex-start;padding:10px 0;"
        "border-bottom:1px solid #eaeef2'>"
        f"{thumb}<div>{badge}{title}"
        f"<div style='color:#57606a;font-size:12px;margin-top:2px'>{meta}</div>"
        f"{reason}</div></div>"
    )


def render_text(items: list[ScoredListing], summary: RunSummary) -> str:
    """Plaintext fallback (used for the Phase-0 email and console delivery)."""
    lines = [
        f"Local Marketplace Monitor — {summary.date}",
        f"scanned {summary.total_fetched}, {summary.new_after_dedupe} new, "
        f"{summary.scored} scored, {summary.reported} reported.",
        "",
    ]
    if not items:
        lines.append("Nothing cleared the threshold today.")
    for item in items:
        l = item.listing
        lines.append(f"[{item.score}] {l.title} — {_fmt_price(item)} ({l.source})")
        if item.reason:
            lines.append(f"      {item.reason}")
        lines.append(f"      {l.url}")
    if summary.adapter_errors:
        lines.append("")
        lines.append("Source errors: " + "; ".join(summary.adapter_errors))
    return "\n".join(lines)
