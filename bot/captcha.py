"""
Math CAPTCHA generation and inline keyboard builder.

Pure functions — no Telegram API calls, no side effects.
"""

import random

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def generate_captcha() -> tuple[str, int, list[int]]:
    """Create a random addition CAPTCHA.

    Returns:
        (question_text, correct_answer, shuffled_choices)
        where choices is always length 4 with exactly one correct value.
    """
    a = random.randint(1, 9)
    b = random.randint(1, 9)
    correct = a + b

    wrong: set[int] = set()
    while len(wrong) < 3:
        candidate = random.randint(2, 18)
        if candidate != correct:
            wrong.add(candidate)

    choices = list(wrong) + [correct]
    random.shuffle(choices)
    return f"{a} + {b} = ?", correct, choices


def build_keyboard(
    user_id: int, correct: int, choices: list[int]
) -> InlineKeyboardMarkup:
    """Build a single-row inline keyboard with 4 answer buttons.

    callback_data format: ``verify:{user_id}:{chosen}:{correct}``
    Embedding the values keeps state in the message itself so we never
    need a separate lookup for wrong-answer alerts.
    """
    buttons = [
        InlineKeyboardButton(
            text=str(c),
            callback_data=f"verify:{user_id}:{c}:{correct}",
        )
        for c in choices
    ]
    return InlineKeyboardMarkup([buttons])
