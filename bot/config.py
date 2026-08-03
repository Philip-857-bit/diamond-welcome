"""
Configuration — environment variables and constants.

All tunables live here so handlers never touch os.environ directly.
"""

import logging
import math
import os

from dotenv import load_dotenv

load_dotenv()

# ─── Telegram ─────────────────────────────────────────────────────────────────

BOT_TOKEN: str = os.environ["BOT_TOKEN"]

# The owner is the only user allowed to grant/revoke operator access.
OWNER_USER_ID: int = int(os.environ["OWNER_USER_ID"])
if OWNER_USER_ID <= 0:
    raise ValueError("OWNER_USER_ID must be a positive Telegram user ID")

# ─── Webhook (Render) ────────────────────────────────────────────────────────

PORT: int = int(os.getenv("PORT", "8080"))
RENDER_EXTERNAL_URL: str = os.environ["RENDER_EXTERNAL_URL"].rstrip("/")
WEBHOOK_PATH: str = os.getenv("WEBHOOK_PATH", "webhook").strip("/")
WEBHOOK_SECRET: str = os.environ["WEBHOOK_SECRET"]

# ─── SolDucks / Solana alerts ────────────────────────────────────────────────

DATABASE_URL: str = os.environ["DATABASE_URL"]
HELIUS_API_KEY: str = os.environ["HELIUS_API_KEY"]
HELIUS_WEBHOOK_SECRET: str = os.environ["HELIUS_WEBHOOK_SECRET"]
HELIUS_WEBHOOK_PATH: str = os.getenv("HELIUS_WEBHOOK_PATH", "helius/webhook").strip("/")

if not RENDER_EXTERNAL_URL.startswith("https://"):
    raise ValueError("RENDER_EXTERNAL_URL must be a public HTTPS URL")
if not WEBHOOK_PATH or not HELIUS_WEBHOOK_PATH:
    raise ValueError("Webhook paths must not be empty")
if not WEBHOOK_SECRET.strip() or not HELIUS_WEBHOOK_SECRET.strip():
    raise ValueError("Webhook secrets must not be empty")
ALERT_CHAT_ID: int | None = (
    int(os.environ["ALERT_CHAT_ID"]) if os.getenv("ALERT_CHAT_ID") else None
)
ALERT_ANIMATION_PATH: str = os.getenv(
    "ALERT_ANIMATION_PATH",
    os.path.join(os.path.dirname(__file__), "..", "solducks_buy.mp4"),
)
HELIUS_MAX_PAYLOAD_BYTES: int = int(os.getenv("HELIUS_MAX_PAYLOAD_BYTES", "1048576"))
ALERT_WORKER_POLL_SECONDS: float = float(os.getenv("ALERT_WORKER_POLL_SECONDS", "1"))
EVENT_CLEANUP_SECONDS: float = float(os.getenv("EVENT_CLEANUP_SECONDS", "3600"))
EVENT_RETENTION_DAYS: int = int(os.getenv("EVENT_RETENTION_DAYS", "30"))
DEAD_EVENT_RETENTION_DAYS: int = int(os.getenv("DEAD_EVENT_RETENTION_DAYS", "90"))

for setting_name, setting_value in {
    "PORT": PORT,
    "HELIUS_MAX_PAYLOAD_BYTES": HELIUS_MAX_PAYLOAD_BYTES,
    "ALERT_WORKER_POLL_SECONDS": ALERT_WORKER_POLL_SECONDS,
    "EVENT_CLEANUP_SECONDS": EVENT_CLEANUP_SECONDS,
    "EVENT_RETENTION_DAYS": EVENT_RETENTION_DAYS,
    "DEAD_EVENT_RETENTION_DAYS": DEAD_EVENT_RETENTION_DAYS,
}.items():
    if setting_value <= 0 or (
        isinstance(setting_value, float) and not math.isfinite(setting_value)
    ):
        raise ValueError(f"{setting_name} must be a positive finite number")

if PORT > 65535:
    raise ValueError("PORT must be between 1 and 65535")
if ALERT_CHAT_ID is not None and (
    ALERT_CHAT_ID == 0 or not -(2**63) < ALERT_CHAT_ID < 2**63
):
    raise ValueError("ALERT_CHAT_ID must be a non-zero Telegram chat ID")

# Local welcome animation sent on first join; file_id is cached afterwards.
WELCOME_GIF_PATH: str = os.path.join(os.path.dirname(__file__), "..", "welcome.mp4")

# ─── Timing (seconds) ────────────────────────────────────────────────────────

KICK_TIMEOUT: int = 300  # 5 minutes to solve the CAPTCHA
DELETE_DELAY: int = 5  # delete success message after this many seconds
AUTO_DELETE_DELAY: int = 10  # delete CAPTCHA if user doesn't interact within this time

# ─── Logging ──────────────────────────────────────────────────────────────────

LOG_FORMAT: str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"


def setup_logging() -> None:
    """Configure root logger once at startup."""
    logging.basicConfig(format=LOG_FORMAT, level=logging.INFO)
    # httpx logs complete request URLs at INFO. Those URLs contain the Helius
    # API key and Telegram bot token, so retain only transport warnings/errors.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
