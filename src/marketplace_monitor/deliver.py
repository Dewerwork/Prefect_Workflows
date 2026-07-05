"""Deliver the digest (section 9.2, FR-8).

Three transports, chosen by ``delivery.method`` in the config:
  * ``console`` — print the plaintext digest (default; zero setup, good for dev)
  * ``smtp``    — plain SMTP, e.g. a Gmail app password
  * ``resend``  — Resend's transactional API (least-friction free tier)
"""

from __future__ import annotations

import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from .config import DeliveryConfig

logger = logging.getLogger(__name__)


def deliver(cfg: DeliveryConfig, subject: str, html_body: str, text_body: str) -> None:
    method = (cfg.method or "console").lower()
    if method == "console":
        print("\n" + "=" * 60)
        print(f"SUBJECT: {subject}")
        print("=" * 60)
        print(text_body)
        print("=" * 60 + "\n")
        return
    if method == "smtp":
        _deliver_smtp(cfg, subject, html_body, text_body)
        return
    if method == "resend":
        _deliver_resend(cfg, subject, html_body, text_body)
        return
    raise ValueError(f"unknown delivery method: {cfg.method}")


def _deliver_smtp(cfg: DeliveryConfig, subject: str, html_body: str, text_body: str) -> None:
    if not cfg.to:
        raise ValueError("delivery.to is required for smtp")
    host = cfg.smtp_host or os.environ.get("SMTP_HOST")
    user = cfg.smtp_user or os.environ.get("SMTP_USER")
    password = cfg.smtp_password or os.environ.get("SMTP_PASSWORD")
    if not host:
        raise ValueError("smtp_host (or SMTP_HOST) is required for smtp delivery")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = cfg.from_addr
    msg["To"] = ", ".join(cfg.to)
    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP(host, cfg.smtp_port) as server:
        server.starttls()
        if user and password:
            server.login(user, password)
        server.sendmail(cfg.from_addr, cfg.to, msg.as_string())
    logger.info("sent digest via SMTP to %s", ", ".join(cfg.to))


def _deliver_resend(cfg: DeliveryConfig, subject: str, html_body: str, text_body: str) -> None:
    import requests

    if not cfg.to:
        raise ValueError("delivery.to is required for resend")
    api_key = os.environ.get("RESEND_API_KEY")
    if not api_key:
        raise ValueError("RESEND_API_KEY env var is required for resend delivery")

    resp = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "from": cfg.from_addr,
            "to": cfg.to,
            "subject": subject,
            "html": html_body,
            "text": text_body,
        },
        timeout=30,
    )
    resp.raise_for_status()
    logger.info("sent digest via Resend to %s", ", ".join(cfg.to))
