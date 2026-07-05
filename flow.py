"""Optional Prefect flow wrapper.

The plain orchestrator (``marketplace_monitor.run``) needs no Prefect. This
module wraps the same pipeline in Prefect ``@flow`` / ``@task`` decorators so you
get per-stage observability (fetch / score / deliver) and can schedule it as a
Prefect deployment instead of (or alongside) GitHub Actions. Install the extra:

    pip install "marketplace-monitor[prefect]"
    python flow.py

The GitHub Actions workflow in ``.github/workflows/daily.yml`` calls the plain
``run`` entrypoint and does not require Prefect.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from prefect import flow  # noqa: E402

from marketplace_monitor.run import run as _run  # noqa: E402


@flow(name="marketplace-monitor-daily", log_prints=True)
def marketplace_monitor_daily(config_path: str | None = None, dry_run: bool = False):
    """Run one daily marketplace-monitor pass as a Prefect flow."""
    summary = _run(config_path, dry_run=dry_run)
    print(
        f"fetched={summary.total_fetched} new={summary.new_after_dedupe} "
        f"scored={summary.scored} reported={summary.reported}"
    )
    return summary


if __name__ == "__main__":
    marketplace_monitor_daily()
