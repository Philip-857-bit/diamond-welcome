"""
In-memory runtime state.

Two pieces of mutable state are shared across handlers:
  1. pending_users  — tracks users who haven't solved the CAPTCHA yet
  2. gif_file_id    — cached Telegram file_id for the welcome GIF
"""

from __future__ import annotations

from dataclasses import dataclass


# ─── Pending user record ──────────────────────────────────────────────────────


@dataclass(slots=True)
class PendingUser:
    correct_answer: int
    message_id: int
    first_name: str


# ─── Pending-users registry ───────────────────────────────────────────────────


class PendingUsers:
    """Thread-safe-ish dict keyed by (chat_id, user_id)."""

    def __init__(self) -> None:
        self._data: dict[tuple[int, int], PendingUser] = {}

    def set(
        self, chat_id: int, user_id: int, *, correct_answer: int,
        message_id: int, first_name: str,
    ) -> None:
        self._data[(chat_id, user_id)] = PendingUser(
            correct_answer=correct_answer,
            message_id=message_id,
            first_name=first_name,
        )

    def pop(self, chat_id: int, user_id: int) -> PendingUser | None:
        return self._data.pop((chat_id, user_id), None)

    def has(self, chat_id: int, user_id: int) -> bool:
        return (chat_id, user_id) in self._data


# ─── Module-level singletons ──────────────────────────────────────────────────

pending_users = PendingUsers()

# Cached Telegram file_id for the welcome GIF (persists across joins).
gif_file_id: str | None = None


def get_gif_file_id() -> str | None:
    return gif_file_id


def set_gif_file_id(value: str) -> None:
    global gif_file_id
    gif_file_id = value
