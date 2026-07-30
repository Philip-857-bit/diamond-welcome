"""
Configuration — environment variables and constants.

All tunables live here so handlers never touch os.environ directly.
"""

import logging
import os

from dotenv import load_dotenv

load_dotenv()

# ─── Telegram ─────────────────────────────────────────────────────────────────

BOT_TOKEN: str = os.environ["BOT_TOKEN"]

# Local welcome animation sent on first join; file_id is cached afterwards.
WELCOME_GIF_PATH: str = os.path.join(os.path.dirname(__file__), "..", "welcome.mp4")

# ─── Timing (seconds) ────────────────────────────────────────────────────────

KICK_TIMEOUT: int = 300   # 5 minutes to solve the CAPTCHA
DELETE_DELAY: int = 5     # delete success message after this many seconds
AUTO_DELETE_DELAY: int = 10  # delete CAPTCHA if user doesn't interact within this time

# ─── Logging ──────────────────────────────────────────────────────────────────

LOG_FORMAT: str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"


def setup_logging() -> None:
    """Configure root logger once at startup."""
    logging.basicConfig(format=LOG_FORMAT, level=logging.INFO)
