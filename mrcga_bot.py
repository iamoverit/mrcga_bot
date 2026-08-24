#!/usr/bin/env -S uv run --env-file .env --script

# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "python-telegram-bot==22.8",
# ]
# ///

from __future__ import annotations

import csv
import html
import io
import os
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PicklePersistence,
    filters,
)

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
PROXY = os.environ.get("TELEGRAM_PROXY")

STATE_FILE = Path(
    os.environ.get(
        "BOT_STATE_FILE",
        "split-bot.pickle",
    )
)

REPORT_ITEM_NAME_LEN = int(
    os.environ.get(
        "REPORT_ITEM_NAME_LEN",
        "24",
    )
)

CSV_FIELDS = [
    "name",
    "quantity",
    "unit_price",
    "total",
]

ITEMS_PER_PAGE = 8


# ---------------------------------------------------------------------------
# Callback prefixes
# ---------------------------------------------------------------------------

CB_OPEN = "open"
CB_SHARED_PARTICIPANTS = "sps"
CB_SHARED_FINISH = "sf"
CB_SHARED_CANCEL = "sc"

CB_ITEM = "i"
CB_PAGE = "p"
CB_PARTICIPANTS = "ps"
CB_BACK = "b"

CB_ADD_KNOWN = "ak"
CB_ADD_MANUAL = "am"
CB_REMOVE = "rm"

CB_ACT_AS = "aa"
CB_ACT_SELF = "as"

CB_SET_PAYER = "pay"

CB_FINISH = "f"
CB_CANCEL = "c"

CB_PERCENT = "pct"
CB_PERCENT_CUSTOM = "pc"
CB_PERCENT_DELETE = "pd"

CB_NOOP = "noop"


# ---------------------------------------------------------------------------
# Basic helpers
# ---------------------------------------------------------------------------


def money(cents: int) -> str:
    return f"{cents / 100:.2f}"


def user_name(user) -> str:
    if user is None:
        return "Unknown"

    if user.full_name:
        return user.full_name

    if user.username:
        return f"@{user.username}"

    return str(user.id)


def short(
    text: str,
    limit: int = 28,
) -> str:
    text = " ".join(text.split())

    if len(text) <= limit:
        return text

    return text[: limit - 1] + "…"


def format_percent(
    value: Decimal,
) -> str:
    value = value.normalize()

    if value == value.to_integral():
        return str(int(value))

    return format(value, "f").rstrip("0").rstrip(".")


def active_receipt(
    context: ContextTypes.DEFAULT_TYPE,
) -> dict | None:
    return context.chat_data.get("receipt")


def receipt_total(
    receipt: dict,
) -> int:
    return sum(item["total"] for item in receipt["items"])


def clear_percentage_pending(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
) -> None:
    pending = context.chat_data.get("awaiting_percentage")

    if pending:
        pending.pop(
            user_id,
            None,
        )


# ---------------------------------------------------------------------------
# Users / participants
# ---------------------------------------------------------------------------


def remember_user(
    context: ContextTypes.DEFAULT_TYPE,
    user,
) -> None:
    """
    Telegram Bot API не предоставляет полный список
    обычных участников группы.

    Поэтому бот запоминает пользователей, которых
    уже видел в группе.
    """
    if user is None or user.is_bot:
        return

    known_users = context.chat_data.setdefault(
        "known_users",
        {},
    )

    known_users[user.id] = {
        "name": user_name(user),
        "username": user.username,
    }


def participant_name(
    receipt: dict,
    participant_id: int,
) -> str:
    participant = receipt["participants"].get(participant_id)

    if participant is None:
        return str(participant_id)

    return participant["name"]


def payer_name(
    receipt: dict,
) -> str:
    return participant_name(
        receipt,
        receipt["payer_id"],
    )


def add_telegram_participant(
    receipt: dict,
    user,
) -> None:
    receipt["participants"][user.id] = {
        "name": user_name(user),
        "telegram": True,
    }


def next_manual_id(
    receipt: dict,
) -> int:
    """
    Реальные Telegram user_id положительные.

    Для вручную добавленных участников используем
    отрицательные внутренние ID.
    """
    value = receipt.get(
        "next_manual_id",
        -1,
    )

    receipt["next_manual_id"] = value - 1

    return value


def current_actor(
    receipt: dict,
    telegram_user_id: int,
) -> int:
    """
    Обычный пользователь выбирает за себя.

    Загрузивший чек может временно переключиться
    на другого участника.
    """
    if telegram_user_id != receipt["owner_id"]:
        return telegram_user_id

    return receipt.get(
        "owner_acts_as",
        receipt["owner_id"],
    )


def validate_personal_menu(
    query,
    menu_user_id: int,
) -> bool:
    return query.from_user.id == menu_user_id


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------


def parse_money(
    value: str,
) -> int:
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"Некорректная сумма: {value}") from exc

    if amount < 0:
        raise ValueError("Сумма не может быть отрицательной")

    return int((amount * 100).quantize(Decimal(1)))


def parse_csv(
    data: bytes,
) -> list[dict]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("CSV должен быть в UTF-8") from exc

    reader = csv.DictReader(io.StringIO(text))

    if reader.fieldnames != CSV_FIELDS:
        raise ValueError(
            "Неверный заголовок CSV.\n\nОжидается строго:\n" + ",".join(CSV_FIELDS)
        )

    items = []

    for row_number, row in enumerate(
        reader,
        start=2,
    ):
        name = row["name"].strip()

        if not name:
            raise ValueError(f"Строка {row_number}: пустое название позиции")

        try:
            quantity = Decimal(row["quantity"])

            unit_price = Decimal(row["unit_price"])

        except InvalidOperation as exc:
            raise ValueError(
                f"Строка {row_number}: quantity и unit_price должны быть числами"
            ) from exc

        if quantity <= 0:
            raise ValueError(f"Строка {row_number}: quantity должно быть > 0")

        if unit_price < 0:
            raise ValueError(f"Строка {row_number}: unit_price должно быть >= 0")

        total = parse_money(row["total"])

        items.append(
            {
                "name": name,
                "quantity": str(quantity),
                "unit_price": str(unit_price),
                "total": total,
            }
        )

    if not items:
        raise ValueError("CSV не содержит позиций")

    return items


# ---------------------------------------------------------------------------
# Money allocation
# ---------------------------------------------------------------------------


def allocate(
    total: int,
    weights: dict[Any, Decimal],
) -> dict[Any, int]:
    """
    Делит сумму в копейках пропорционально весам.

    Результат всегда строго равен исходной сумме,
    включая остаточные копейки.
    """
    weight_sum = sum(
        weights.values(),
        Decimal(0),
    )

    if weight_sum <= 0:
        raise ValueError("Сумма долей должна быть больше нуля")

    exact = {
        key: (Decimal(total) * weight / weight_sum) for key, weight in weights.items()
    }

    result = {key: int(value) for key, value in exact.items()}

    remainder = total - sum(result.values())

    order = sorted(
        exact,
        key=lambda key: exact[key] - Decimal(result[key]),
        reverse=True,
    )

    for key in order[:remainder]:
        result[key] += 1

    return result


# ---------------------------------------------------------------------------
# Percentage shares
# ---------------------------------------------------------------------------


def set_percentage(
    receipt: dict,
    item_index: int,
    participant_id: int,
    percent: Decimal,
) -> None:
    if percent < 0 or percent > 100:
        raise ValueError("Доля должна быть от 0 до 100%.")

    if participant_id not in receipt["participants"]:
        raise ValueError("Участник не найден.")

    shares = receipt["shares"].setdefault(
        item_index,
        {},
    )

    others = sum(
        (
            value
            for uid, value in shares.items()
            if (uid != participant_id and uid in receipt["participants"])
        ),
        Decimal(0),
    )

    if others + percent > 100:
        available = Decimal(100) - others

        raise ValueError(
            f"Нельзя указать "
            f"{format_percent(percent)}%.\n\n"
            f"Другими участниками уже занято "
            f"{format_percent(others)}%.\n"
            f"Доступно максимум "
            f"{format_percent(available)}%."
        )

    if percent == 0:
        shares.pop(
            participant_id,
            None,
        )
    else:
        shares[participant_id] = percent

    if not shares:
        receipt["shares"].pop(
            item_index,
            None,
        )


def item_assignees(
    receipt: dict,
    item_index: int,
) -> str:
    shares = receipt["shares"].get(
        item_index,
        {},
    )

    valid = {
        uid: percent
        for uid, percent in shares.items()
        if (uid in receipt["participants"] and percent > 0)
    }

    if not valid:
        return "100% общее"

    parts = [
        (f"{participant_name(receipt, uid)} {format_percent(percent)}%")
        for uid, percent in valid.items()
    ]

    assigned = sum(
        valid.values(),
        Decimal(0),
    )

    common = Decimal(100) - assigned

    if common > 0:
        parts.append(f"общее {format_percent(common)}%")

    return ", ".join(parts)


# ---------------------------------------------------------------------------
# Shared receipt message
# ---------------------------------------------------------------------------


def shared_receipt_text(
    receipt: dict,
) -> str:
    return "\n".join(
        [
            "🧾 <b>Совместная закупка</b>",
            "",
            (f"Всего: <b>{money(receipt_total(receipt))} ₽</b>"),
            (f"Загрузил чек: <b>{html.escape(receipt['owner_name'])}</b>"),
            (f"Оплатил чек: <b>{html.escape(payer_name(receipt))}</b>"),
            "",
            (f"Позиций: <b>{len(receipt['items'])}</b>"),
            "",
            "Каждый участник открывает своё меню.",
            "Навигация и выбор позиций независимы.",
            "",
            "Не назначенная персонально часть позиции считается общей.",
        ]
    )


def shared_receipt_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🛒 Открыть мои позиции",
                    callback_data=CB_OPEN,
                )
            ],
            [
                InlineKeyboardButton(
                    "👥 Участники",
                    callback_data=CB_SHARED_PARTICIPANTS,
                )
            ],
            [
                InlineKeyboardButton(
                    "✅ Завершить",
                    callback_data=CB_SHARED_FINISH,
                ),
                InlineKeyboardButton(
                    "🗑 Отменить",
                    callback_data=CB_SHARED_CANCEL,
                ),
            ],
        ]
    )


async def refresh_shared_message(
    context: ContextTypes.DEFAULT_TYPE,
    receipt: dict,
) -> None:
    chat_id = receipt.get("shared_chat_id")

    message_id = receipt.get("shared_message_id")

    if not chat_id or not message_id:
        return

    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=shared_receipt_text(receipt),
            parse_mode="HTML",
            reply_markup=shared_receipt_keyboard(),
        )
    except BadRequest:
        pass


# ---------------------------------------------------------------------------
# Personal receipt UI
# ---------------------------------------------------------------------------


def receipt_text(
    receipt: dict,
    page: int,
    menu_user_id: int,
) -> str:
    total_pages = max(
        1,
        (len(receipt["items"]) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE,
    )

    start = page * ITEMS_PER_PAGE

    end = min(
        start + ITEMS_PER_PAGE,
        len(receipt["items"]),
    )

    actor_id = current_actor(
        receipt,
        menu_user_id,
    )

    lines = [
        "🛒 <b>Ваше меню чека</b>",
        "",
        (f"Всего: <b>{money(receipt_total(receipt))} ₽</b>"),
        (f"Оплатил: <b>{html.escape(payer_name(receipt))}</b>"),
    ]

    if menu_user_id == receipt["owner_id"] and actor_id != receipt["owner_id"]:
        lines.extend(
            [
                "",
                (
                    "🎯 Вы выбираете позиции за: "
                    f"<b>{html.escape(participant_name(receipt, actor_id))}</b>"
                ),
            ]
        )

    lines.extend(
        [
            "",
            "Нажмите на позицию, чтобы указать долю.",
            "",
            (f"Позиции {start + 1}–{end} из {len(receipt['items'])}"),
            (f"Страница {page + 1}/{total_pages}"),
        ]
    )

    return "\n".join(lines)


def receipt_keyboard(
    receipt: dict,
    page: int,
    menu_user_id: int,
) -> InlineKeyboardMarkup:
    items = receipt["items"]

    total_pages = max(
        1,
        (len(items) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE,
    )

    page = max(
        0,
        min(
            page,
            total_pages - 1,
        ),
    )

    start = page * ITEMS_PER_PAGE

    end = min(
        start + ITEMS_PER_PAGE,
        len(items),
    )

    rows = []

    for index in range(
        start,
        end,
    ):
        item = items[index]

        display_number = index + 1

        assigned = item_assignees(
            receipt,
            display_number,
        )

        prefix = "⬜" if assigned == "100% общее" else "✅"

        label = (
            f"{prefix} "
            f"{display_number}. "
            f"{short(item['name'], 21)} "
            f"— {money(item['total'])} ₽"
        )

        if assigned != "100% общее":
            label += f" · {short(assigned, 28)}"

        rows.append(
            [
                InlineKeyboardButton(
                    label,
                    callback_data=(f"{CB_ITEM}:{menu_user_id}:{page}:{display_number}"),
                )
            ]
        )

    nav = []

    if page > 0:
        nav.append(
            InlineKeyboardButton(
                "←",
                callback_data=(f"{CB_PAGE}:{menu_user_id}:{page - 1}"),
            )
        )

    nav.append(
        InlineKeyboardButton(
            f"{page + 1}/{total_pages}",
            callback_data=CB_NOOP,
        )
    )

    if page < total_pages - 1:
        nav.append(
            InlineKeyboardButton(
                "→",
                callback_data=(f"{CB_PAGE}:{menu_user_id}:{page + 1}"),
            )
        )

    rows.append(nav)

    rows.append(
        [
            InlineKeyboardButton(
                "👥 Участники",
                callback_data=(f"{CB_PARTICIPANTS}:{menu_user_id}:{page}"),
            )
        ]
    )

    if menu_user_id == receipt["owner_id"]:
        rows.append(
            [
                InlineKeyboardButton(
                    "✅ Завершить",
                    callback_data=(f"{CB_FINISH}:{menu_user_id}"),
                ),
                InlineKeyboardButton(
                    "🗑 Отменить",
                    callback_data=(f"{CB_CANCEL}:{menu_user_id}"),
                ),
            ]
        )

    return InlineKeyboardMarkup(rows)


async def edit_personal_receipt(
    query,
    receipt: dict,
    page: int,
    menu_user_id: int,
) -> None:
    await query.edit_message_text(
        receipt_text(
            receipt,
            page,
            menu_user_id,
        ),
        parse_mode="HTML",
        reply_markup=receipt_keyboard(
            receipt,
            page,
            menu_user_id,
        ),
    )


async def edit_personal_receipt_by_id(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    message_id: int,
    receipt: dict,
    page: int,
    menu_user_id: int,
) -> None:
    await context.bot.edit_message_text(
        chat_id=chat_id,
        message_id=message_id,
        text=receipt_text(
            receipt,
            page,
            menu_user_id,
        ),
        parse_mode="HTML",
        reply_markup=receipt_keyboard(
            receipt,
            page,
            menu_user_id,
        ),
    )


# ---------------------------------------------------------------------------
# Percentage selection UI
# ---------------------------------------------------------------------------


def percentage_text(
    receipt: dict,
    item_index: int,
    actor_id: int,
) -> str:
    item = receipt["items"][item_index - 1]

    shares = receipt["shares"].get(
        item_index,
        {},
    )

    valid_shares = {
        uid: percent
        for uid, percent in shares.items()
        if (uid in receipt["participants"] and percent > 0)
    }

    assigned = sum(
        valid_shares.values(),
        Decimal(0),
    )

    common = Decimal(100) - assigned

    lines = [
        (f"🛒 <b>{html.escape(item['name'])}</b>"),
        "",
        (f"Стоимость: <b>{money(item['total'])} ₽</b>"),
        "",
        "<b>Персональные доли:</b>",
    ]

    if valid_shares:
        for uid, percent in valid_shares.items():
            lines.append(
                "• "
                f"{html.escape(participant_name(receipt, uid))}"
                " — "
                f"{format_percent(percent)}%"
            )
    else:
        lines.append("• нет")

    lines.extend(
        [
            "",
            (f"Общая часть: <b>{format_percent(common)}%</b>"),
            "",
            (
                "Вы выбираете за: "
                f"<b>{html.escape(participant_name(receipt, actor_id))}</b>"
            ),
            "",
            "После выбора процента вы автоматически вернётесь к списку позиций.",
        ]
    )

    return "\n".join(lines)


def percentage_keyboard(
    receipt: dict,
    item_index: int,
    page: int,
    menu_user_id: int,
    actor_id: int,
) -> InlineKeyboardMarkup:
    current = (
        receipt["shares"]
        .get(item_index, {})
        .get(
            actor_id,
            Decimal(0),
        )
    )

    def percent_button(
        percent: int,
    ) -> InlineKeyboardButton:
        marker = "✅ " if current == Decimal(percent) else ""

        return InlineKeyboardButton(
            f"{marker}{percent}%",
            callback_data=(
                f"{CB_PERCENT}:{menu_user_id}:{page}:{item_index}:{percent}"
            ),
        )

    rows = [
        [
            percent_button(25),
            percent_button(50),
        ],
        [
            percent_button(75),
            percent_button(100),
        ],
        [
            InlineKeyboardButton(
                "✍️ Другой %",
                callback_data=(
                    f"{CB_PERCENT_CUSTOM}:{menu_user_id}:{page}:{item_index}"
                ),
            )
        ],
    ]

    if current > 0:
        rows.append(
            [
                InlineKeyboardButton(
                    "❌ Убрать мою долю",
                    callback_data=(
                        f"{CB_PERCENT_DELETE}:{menu_user_id}:{page}:{item_index}"
                    ),
                )
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(
                "← Без изменений",
                callback_data=(f"{CB_BACK}:{menu_user_id}:{page}"),
            )
        ]
    )

    return InlineKeyboardMarkup(rows)


# ---------------------------------------------------------------------------
# Participant UI
# ---------------------------------------------------------------------------


def participants_text(
    receipt: dict,
) -> str:
    lines = [
        "👥 <b>Участники закупки</b>",
        "",
    ]

    for (
        participant_id,
        participant,
    ) in receipt["participants"].items():
        suffixes = []

        if participant_id == receipt["owner_id"]:
            suffixes.append("загрузил")

        if participant_id == receipt["payer_id"]:
            suffixes.append("💳 оплатил")

        if not participant["telegram"]:
            suffixes.append("вручную")

        suffix = ""

        if suffixes:
            suffix = " · " + ", ".join(suffixes)

        lines.append(f"• {html.escape(participant['name'])}{suffix}")

    return "\n".join(lines)


def participants_keyboard(
    receipt: dict,
    known_users: dict,
    page: int,
    menu_user_id: int,
) -> InlineKeyboardMarkup:
    rows = []

    is_owner = menu_user_id == receipt["owner_id"]

    if is_owner:
        candidates = [
            (
                uid,
                data,
            )
            for uid, data in known_users.items()
            if uid not in receipt["participants"]
        ]

        if candidates:
            rows.append(
                [
                    InlineKeyboardButton(
                        "── Добавить из группы ──",
                        callback_data=CB_NOOP,
                    )
                ]
            )

            for uid, data in candidates[:15]:
                rows.append(
                    [
                        InlineKeyboardButton(
                            (f"➕ {short(data['name'], 35)}"),
                            callback_data=(
                                f"{CB_ADD_KNOWN}:{menu_user_id}:{uid}:{page}"
                            ),
                        )
                    ]
                )

        rows.append(
            [
                InlineKeyboardButton(
                    "✍️ Вписать имя",
                    callback_data=(f"{CB_ADD_MANUAL}:{menu_user_id}:{page}"),
                )
            ]
        )

        # ---------------------------------------------------
        # Payer selection
        # ---------------------------------------------------

        rows.append(
            [
                InlineKeyboardButton(
                    "── Кто оплатил чек ──",
                    callback_data=CB_NOOP,
                )
            ]
        )

        for uid, participant in receipt["participants"].items():
            marker = "✅ " if uid == receipt["payer_id"] else "💳 "

            rows.append(
                [
                    InlineKeyboardButton(
                        (f"{marker}{short(participant['name'], 30)}"),
                        callback_data=(f"{CB_SET_PAYER}:{menu_user_id}:{uid}:{page}"),
                    )
                ]
            )

        # ---------------------------------------------------
        # Acting as participant
        # ---------------------------------------------------

        removable = [
            (
                uid,
                participant,
            )
            for uid, participant in receipt["participants"].items()
            if uid != receipt["owner_id"]
        ]

        if removable:
            rows.append(
                [
                    InlineKeyboardButton(
                        "── Выбирать за участника ──",
                        callback_data=CB_NOOP,
                    )
                ]
            )

        for uid, participant in removable:
            rows.append(
                [
                    InlineKeyboardButton(
                        (f"🎯 {short(participant['name'], 22)}"),
                        callback_data=(f"{CB_ACT_AS}:{menu_user_id}:{uid}:{page}"),
                    ),
                    InlineKeyboardButton(
                        "❌",
                        callback_data=(f"{CB_REMOVE}:{menu_user_id}:{uid}:{page}"),
                    ),
                ]
            )

        actor = current_actor(
            receipt,
            receipt["owner_id"],
        )

        if actor != receipt["owner_id"]:
            rows.append(
                [
                    InlineKeyboardButton(
                        (
                            "↩️ Выбирать за себя "
                            f"(сейчас "
                            f"{short(participant_name(receipt, actor), 18)})"
                        ),
                        callback_data=(f"{CB_ACT_SELF}:{menu_user_id}:{page}"),
                    )
                ]
            )

    rows.append(
        [
            InlineKeyboardButton(
                "← К позициям",
                callback_data=(f"{CB_BACK}:{menu_user_id}:{page}"),
            )
        ]
    )

    return InlineKeyboardMarkup(rows)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    remember_user(
        context,
        update.effective_user,
    )

    await update.message.reply_text(
        "🧾 Бот для разделения совместных покупок.\n\n"
        "1. Загрузите CSV.\n"
        "2. Каждый участник открывает своё меню.\n"
        "3. Для позиции можно указать персональный процент.\n"
        "4. Остаток позиции считается общим.\n"
        "5. Общая часть делится между всеми участниками.\n"
        "6. Загрузивший чек может изменить того, "
        "кто фактически оплатил чек.\n\n"
        "Формат CSV:\n"
        "name,quantity,unit_price,total\n\n"
        "Команды:\n"
        "/items — открыть своё меню\n"
        "/finish — завершить расчёт\n"
        "/cancel — отменить чек"
    )


async def items_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    user = update.effective_user

    remember_user(
        context,
        user,
    )

    receipt = active_receipt(context)

    if not receipt:
        await update.message.reply_text("Сейчас нет активного чека.")
        return

    add_telegram_participant(
        receipt,
        user,
    )

    clear_percentage_pending(
        context,
        user.id,
    )

    await update.message.reply_text(
        receipt_text(
            receipt,
            0,
            user.id,
        ),
        parse_mode="HTML",
        reply_markup=receipt_keyboard(
            receipt,
            0,
            user.id,
        ),
    )


async def finish_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    receipt = active_receipt(context)

    if not receipt:
        await update.message.reply_text("Сейчас нет активного чека.")
        return

    if update.effective_user.id != receipt["owner_id"]:
        await update.message.reply_text(
            "Завершить расчёт может только загрузивший чек."
        )
        return

    await send_final_result(
        update.effective_chat,
        context,
        receipt,
    )


async def cancel_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    receipt = active_receipt(context)

    if not receipt:
        await update.message.reply_text("Сейчас нет активного чека.")
        return

    if update.effective_user.id != receipt["owner_id"]:
        await update.message.reply_text(
            "Отменить чек может только загрузивший его пользователь."
        )
        return

    clear_receipt_state(context)

    await update.message.reply_text("🗑 Чек отменён.")


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------


async def upload_csv(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    user = update.effective_user

    remember_user(
        context,
        user,
    )

    message = update.message

    if active_receipt(context):
        await message.reply_text("В этой группе уже есть незавершённый чек.")
        return

    document = message.document

    if not document.file_name.lower().endswith(".csv"):
        return

    telegram_file = await document.get_file()

    buffer = io.BytesIO()

    await telegram_file.download_to_memory(buffer)

    try:
        items = parse_csv(buffer.getvalue())
    except ValueError as exc:
        await message.reply_text(f"Не удалось загрузить чек:\n{exc}")
        return

    receipt = {
        "owner_id": user.id,
        "owner_name": user_name(user),
        # Кто фактически оплатил.
        "payer_id": user.id,
        "items": items,
        "participants": {},
        "shares": {},
        "next_manual_id": -1,
        "owner_acts_as": user.id,
    }

    add_telegram_participant(
        receipt,
        user,
    )

    context.chat_data["receipt"] = receipt

    shared_message = await message.reply_text(
        shared_receipt_text(receipt),
        parse_mode="HTML",
        reply_markup=shared_receipt_keyboard(),
    )

    receipt["shared_chat_id"] = shared_message.chat_id

    receipt["shared_message_id"] = shared_message.message_id


# ---------------------------------------------------------------------------
# Shared callbacks
# ---------------------------------------------------------------------------


async def callback_open(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    user = query.from_user

    remember_user(
        context,
        user,
    )

    receipt = active_receipt(context)

    if not receipt:
        await query.answer(
            "Чек уже закрыт.",
            show_alert=True,
        )
        return

    add_telegram_participant(
        receipt,
        user,
    )

    clear_percentage_pending(
        context,
        user.id,
    )

    await query.answer()

    await query.message.reply_text(
        receipt_text(
            receipt,
            0,
            user.id,
        ),
        parse_mode="HTML",
        reply_markup=receipt_keyboard(
            receipt,
            0,
            user.id,
        ),
    )


async def callback_shared_participants(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    user = query.from_user

    remember_user(
        context,
        user,
    )

    receipt = active_receipt(context)

    if not receipt:
        await query.answer(
            "Чек уже закрыт.",
            show_alert=True,
        )
        return

    await query.answer()

    await query.message.reply_text(
        participants_text(receipt),
        parse_mode="HTML",
        reply_markup=participants_keyboard(
            receipt,
            context.chat_data.get(
                "known_users",
                {},
            ),
            0,
            user.id,
        ),
    )


async def callback_shared_finish(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query

    receipt = active_receipt(context)

    if not receipt:
        await query.answer(
            "Чек уже закрыт.",
            show_alert=True,
        )
        return

    if query.from_user.id != receipt["owner_id"]:
        await query.answer(
            "Завершить расчёт может только загрузивший чек.",
            show_alert=True,
        )
        return

    await query.answer("Завершаю расчёт.")

    await send_final_result(
        query.message.chat,
        context,
        receipt,
    )


async def callback_shared_cancel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query

    receipt = active_receipt(context)

    if not receipt:
        await query.answer(
            "Чек уже закрыт.",
            show_alert=True,
        )
        return

    if query.from_user.id != receipt["owner_id"]:
        await query.answer(
            "Отменить чек может только загрузивший чек.",
            show_alert=True,
        )
        return

    clear_receipt_state(context)

    await query.answer("Чек отменён.")

    await query.edit_message_text("🗑 Чек отменён.")


# ---------------------------------------------------------------------------
# Personal receipt callbacks
# ---------------------------------------------------------------------------


async def callback_item(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    user = query.from_user

    remember_user(
        context,
        user,
    )

    receipt = active_receipt(context)

    if not receipt:
        await query.answer(
            "Чек уже закрыт.",
            show_alert=True,
        )
        return

    (
        _,
        menu_user_text,
        page_text,
        item_text,
    ) = query.data.split(":")

    menu_user_id = int(menu_user_text)

    if not validate_personal_menu(
        query,
        menu_user_id,
    ):
        await query.answer(
            "Это меню другого участника.",
            show_alert=True,
        )
        return

    page = int(page_text)

    item_index = int(item_text)

    add_telegram_participant(
        receipt,
        user,
    )

    clear_percentage_pending(
        context,
        user.id,
    )

    actor_id = current_actor(
        receipt,
        menu_user_id,
    )

    if actor_id not in receipt["participants"]:
        actor_id = menu_user_id

    await query.answer()

    await query.edit_message_text(
        percentage_text(
            receipt,
            item_index,
            actor_id,
        ),
        parse_mode="HTML",
        reply_markup=percentage_keyboard(
            receipt,
            item_index,
            page,
            menu_user_id,
            actor_id,
        ),
    )


async def callback_page(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query

    (
        _,
        menu_user_text,
        page_text,
    ) = query.data.split(":")

    menu_user_id = int(menu_user_text)

    if not validate_personal_menu(
        query,
        menu_user_id,
    ):
        await query.answer(
            "Это меню другого участника.",
            show_alert=True,
        )
        return

    receipt = active_receipt(context)

    if not receipt:
        await query.answer(
            "Чек уже закрыт.",
            show_alert=True,
        )
        return

    page = int(page_text)

    clear_percentage_pending(
        context,
        menu_user_id,
    )

    await query.answer()

    await edit_personal_receipt(
        query,
        receipt,
        page,
        menu_user_id,
    )


async def callback_back(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query

    (
        _,
        menu_user_text,
        page_text,
    ) = query.data.split(":")

    menu_user_id = int(menu_user_text)

    if not validate_personal_menu(
        query,
        menu_user_id,
    ):
        await query.answer(
            "Это меню другого участника.",
            show_alert=True,
        )
        return

    receipt = active_receipt(context)

    if not receipt:
        await query.answer(
            "Чек уже закрыт.",
            show_alert=True,
        )
        return

    page = int(page_text)

    clear_percentage_pending(
        context,
        menu_user_id,
    )

    await query.answer()

    await edit_personal_receipt(
        query,
        receipt,
        page,
        menu_user_id,
    )


# ---------------------------------------------------------------------------
# Percentage callbacks
# ---------------------------------------------------------------------------


async def callback_percentage(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    user = query.from_user

    (
        _,
        menu_user_text,
        page_text,
        item_text,
        percent_text,
    ) = query.data.split(":")

    menu_user_id = int(menu_user_text)

    if not validate_personal_menu(
        query,
        menu_user_id,
    ):
        await query.answer(
            "Это меню другого участника.",
            show_alert=True,
        )
        return

    receipt = active_receipt(context)

    if not receipt:
        await query.answer(
            "Чек уже закрыт.",
            show_alert=True,
        )
        return

    page = int(page_text)

    item_index = int(item_text)

    percent = Decimal(percent_text)

    actor_id = current_actor(
        receipt,
        menu_user_id,
    )

    try:
        set_percentage(
            receipt,
            item_index,
            actor_id,
            percent,
        )
    except ValueError as exc:
        await query.answer(
            str(exc),
            show_alert=True,
        )
        return

    clear_percentage_pending(
        context,
        user.id,
    )

    await query.answer(f"Установлено {format_percent(percent)}%")

    await edit_personal_receipt(
        query,
        receipt,
        page,
        menu_user_id,
    )


async def callback_percentage_delete(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query

    (
        _,
        menu_user_text,
        page_text,
        item_text,
    ) = query.data.split(":")

    menu_user_id = int(menu_user_text)

    if not validate_personal_menu(
        query,
        menu_user_id,
    ):
        await query.answer(
            "Это меню другого участника.",
            show_alert=True,
        )
        return

    receipt = active_receipt(context)

    if not receipt:
        await query.answer(
            "Чек уже закрыт.",
            show_alert=True,
        )
        return

    page = int(page_text)

    item_index = int(item_text)

    actor_id = current_actor(
        receipt,
        menu_user_id,
    )

    try:
        set_percentage(
            receipt,
            item_index,
            actor_id,
            Decimal(0),
        )
    except ValueError as exc:
        await query.answer(
            str(exc),
            show_alert=True,
        )
        return

    await query.answer("Доля удалена.")

    await edit_personal_receipt(
        query,
        receipt,
        page,
        menu_user_id,
    )


async def callback_percentage_custom(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    user = query.from_user

    (
        _,
        menu_user_text,
        page_text,
        item_text,
    ) = query.data.split(":")

    menu_user_id = int(menu_user_text)

    if not validate_personal_menu(
        query,
        menu_user_id,
    ):
        await query.answer(
            "Это меню другого участника.",
            show_alert=True,
        )
        return

    receipt = active_receipt(context)

    if not receipt:
        await query.answer(
            "Чек уже закрыт.",
            show_alert=True,
        )
        return

    page = int(page_text)

    item_index = int(item_text)

    actor_id = current_actor(
        receipt,
        menu_user_id,
    )

    pending = context.chat_data.setdefault(
        "awaiting_percentage",
        {},
    )

    pending[user.id] = {
        "page": page,
        "item_index": item_index,
        "actor_id": actor_id,
        "menu_user_id": menu_user_id,
        "chat_id": query.message.chat_id,
        "message_id": query.message.message_id,
    }

    await query.answer()

    await query.message.reply_text(
        "✍️ Введите процент для "
        f"{participant_name(receipt, actor_id)}.\n\n"
        "Например:\n"
        "35\n\n"
        "Можно использовать дробное значение:\n"
        "12.5"
    )


# ---------------------------------------------------------------------------
# Participants callbacks
# ---------------------------------------------------------------------------


async def callback_participants(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query

    (
        _,
        menu_user_text,
        page_text,
    ) = query.data.split(":")

    menu_user_id = int(menu_user_text)

    if not validate_personal_menu(
        query,
        menu_user_id,
    ):
        await query.answer(
            "Это меню другого участника.",
            show_alert=True,
        )
        return

    receipt = active_receipt(context)

    if not receipt:
        await query.answer(
            "Чек уже закрыт.",
            show_alert=True,
        )
        return

    page = int(page_text)

    await query.answer()

    await query.edit_message_text(
        participants_text(receipt),
        parse_mode="HTML",
        reply_markup=participants_keyboard(
            receipt,
            context.chat_data.get(
                "known_users",
                {},
            ),
            page,
            menu_user_id,
        ),
    )


async def callback_add_known(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query

    (
        _,
        menu_user_text,
        uid_text,
        page_text,
    ) = query.data.split(":")

    menu_user_id = int(menu_user_text)

    if not validate_personal_menu(
        query,
        menu_user_id,
    ):
        await query.answer(
            "Это меню другого участника.",
            show_alert=True,
        )
        return

    receipt = active_receipt(context)

    if not receipt:
        await query.answer(
            "Чек уже закрыт.",
            show_alert=True,
        )
        return

    if menu_user_id != receipt["owner_id"]:
        await query.answer(
            "Добавлять участников может только загрузивший чек.",
            show_alert=True,
        )
        return

    uid = int(uid_text)

    page = int(page_text)

    known = context.chat_data.get(
        "known_users",
        {},
    )

    known_user = known.get(uid)

    if not known_user:
        await query.answer(
            "Пользователь не найден.",
            show_alert=True,
        )
        return

    receipt["participants"][uid] = {
        "name": known_user["name"],
        "telegram": True,
    }

    await query.answer(f"Добавлен: {known_user['name']}")

    await query.edit_message_text(
        participants_text(receipt),
        parse_mode="HTML",
        reply_markup=participants_keyboard(
            receipt,
            known,
            page,
            menu_user_id,
        ),
    )


async def callback_add_manual(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query

    (
        _,
        menu_user_text,
        page_text,
    ) = query.data.split(":")

    menu_user_id = int(menu_user_text)

    if not validate_personal_menu(
        query,
        menu_user_id,
    ):
        await query.answer(
            "Это меню другого участника.",
            show_alert=True,
        )
        return

    receipt = active_receipt(context)

    if not receipt:
        await query.answer(
            "Чек уже закрыт.",
            show_alert=True,
        )
        return

    if menu_user_id != receipt["owner_id"]:
        await query.answer(
            "Добавлять участников может только загрузивший чек.",
            show_alert=True,
        )
        return

    page = int(page_text)

    context.chat_data["awaiting_manual_participant"] = {
        "owner_id": menu_user_id,
        "page": page,
        "chat_id": query.message.chat_id,
        "message_id": query.message.message_id,
    }

    await query.answer()

    await query.message.reply_text(
        "✍️ Отправьте следующим сообщением имя участника.\n\nНапример:\nИван"
    )


async def callback_set_payer(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query

    (
        _,
        menu_user_text,
        payer_text,
        page_text,
    ) = query.data.split(":")

    menu_user_id = int(menu_user_text)

    new_payer_id = int(payer_text)

    page = int(page_text)

    if not validate_personal_menu(
        query,
        menu_user_id,
    ):
        await query.answer(
            "Это меню другого участника.",
            show_alert=True,
        )
        return

    receipt = active_receipt(context)

    if not receipt:
        await query.answer(
            "Чек уже закрыт.",
            show_alert=True,
        )
        return

    if menu_user_id != receipt["owner_id"]:
        await query.answer(
            "Изменить плательщика может только загрузивший чек.",
            show_alert=True,
        )
        return

    if new_payer_id not in receipt["participants"]:
        await query.answer(
            "Участник не найден.",
            show_alert=True,
        )
        return

    receipt["payer_id"] = new_payer_id

    await query.answer(f"Теперь чек оплатил: {participant_name(receipt, new_payer_id)}")

    await refresh_shared_message(
        context,
        receipt,
    )

    await query.edit_message_text(
        participants_text(receipt),
        parse_mode="HTML",
        reply_markup=participants_keyboard(
            receipt,
            context.chat_data.get(
                "known_users",
                {},
            ),
            page,
            menu_user_id,
        ),
    )


async def callback_remove_participant(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query

    (
        _,
        menu_user_text,
        uid_text,
        page_text,
    ) = query.data.split(":")

    menu_user_id = int(menu_user_text)

    if not validate_personal_menu(
        query,
        menu_user_id,
    ):
        await query.answer(
            "Это меню другого участника.",
            show_alert=True,
        )
        return

    receipt = active_receipt(context)

    if not receipt:
        await query.answer(
            "Чек уже закрыт.",
            show_alert=True,
        )
        return

    if menu_user_id != receipt["owner_id"]:
        await query.answer(
            "Удалять участников может только загрузивший чек.",
            show_alert=True,
        )
        return

    uid = int(uid_text)

    page = int(page_text)

    if uid == receipt["owner_id"]:
        await query.answer(
            "Нельзя удалить загрузившего чек.",
            show_alert=True,
        )
        return

    receipt["participants"].pop(
        uid,
        None,
    )

    for shares in receipt["shares"].values():
        shares.pop(
            uid,
            None,
        )

    for item_index in list(receipt["shares"]):
        if not receipt["shares"][item_index]:
            receipt["shares"].pop(item_index)

    if receipt.get("owner_acts_as") == uid:
        receipt["owner_acts_as"] = receipt["owner_id"]

    # Если удалили фактического плательщика,
    # возвращаем плательщика к загрузившему чек.
    if receipt["payer_id"] == uid:
        receipt["payer_id"] = receipt["owner_id"]

        await refresh_shared_message(
            context,
            receipt,
        )

    await query.answer("Участник удалён.")

    await query.edit_message_text(
        participants_text(receipt),
        parse_mode="HTML",
        reply_markup=participants_keyboard(
            receipt,
            context.chat_data.get(
                "known_users",
                {},
            ),
            page,
            menu_user_id,
        ),
    )


async def callback_act_as(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query

    (
        _,
        menu_user_text,
        uid_text,
        page_text,
    ) = query.data.split(":")

    menu_user_id = int(menu_user_text)

    if not validate_personal_menu(
        query,
        menu_user_id,
    ):
        await query.answer(
            "Это меню другого участника.",
            show_alert=True,
        )
        return

    receipt = active_receipt(context)

    if not receipt:
        await query.answer(
            "Чек уже закрыт.",
            show_alert=True,
        )
        return

    if menu_user_id != receipt["owner_id"]:
        await query.answer(
            "Недоступно.",
            show_alert=True,
        )
        return

    uid = int(uid_text)

    page = int(page_text)

    if uid not in receipt["participants"]:
        await query.answer(
            "Участник не найден.",
            show_alert=True,
        )
        return

    receipt["owner_acts_as"] = uid

    await query.answer(f"Теперь вы выбираете за {participant_name(receipt, uid)}.")

    await edit_personal_receipt(
        query,
        receipt,
        page,
        menu_user_id,
    )


async def callback_act_self(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query

    (
        _,
        menu_user_text,
        page_text,
    ) = query.data.split(":")

    menu_user_id = int(menu_user_text)

    if not validate_personal_menu(
        query,
        menu_user_id,
    ):
        await query.answer(
            "Это меню другого участника.",
            show_alert=True,
        )
        return

    receipt = active_receipt(context)

    if not receipt:
        await query.answer(
            "Чек уже закрыт.",
            show_alert=True,
        )
        return

    receipt["owner_acts_as"] = receipt["owner_id"]

    page = int(page_text)

    await query.answer("Теперь вы выбираете за себя.")

    await edit_personal_receipt(
        query,
        receipt,
        page,
        menu_user_id,
    )


# ---------------------------------------------------------------------------
# Text input
# ---------------------------------------------------------------------------


async def normal_text_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    user = update.effective_user

    remember_user(
        context,
        user,
    )

    # -----------------------------------------------------------------------
    # Custom percentage
    # -----------------------------------------------------------------------

    percentage_pending = context.chat_data.get(
        "awaiting_percentage",
        {},
    )

    pending_percent = percentage_pending.get(user.id)

    if pending_percent:
        receipt = active_receipt(context)

        if not receipt:
            percentage_pending.pop(
                user.id,
                None,
            )
            return

        value = update.message.text.strip().replace(",", ".").replace("%", "").strip()

        try:
            percent = Decimal(value)
        except InvalidOperation:
            await update.message.reply_text("Введите число от 0 до 100.\nНапример: 35")
            return

        actor_id = pending_percent["actor_id"]

        item_index = pending_percent["item_index"]

        page = pending_percent["page"]

        menu_user_id = pending_percent["menu_user_id"]

        try:
            set_percentage(
                receipt,
                item_index,
                actor_id,
                percent,
            )
        except ValueError as exc:
            await update.message.reply_text(str(exc))
            return

        percentage_pending.pop(
            user.id,
            None,
        )

        await update.message.reply_text(
            f"✅ {participant_name(receipt, actor_id)}: {format_percent(percent)}%"
        )

        try:
            await edit_personal_receipt_by_id(
                context,
                pending_percent["chat_id"],
                pending_percent["message_id"],
                receipt,
                page,
                menu_user_id,
            )

        except BadRequest:
            await update.message.reply_text(
                receipt_text(
                    receipt,
                    page,
                    menu_user_id,
                ),
                parse_mode="HTML",
                reply_markup=receipt_keyboard(
                    receipt,
                    page,
                    menu_user_id,
                ),
            )

        return

    # -----------------------------------------------------------------------
    # Manual participant
    # -----------------------------------------------------------------------

    pending = context.chat_data.get("awaiting_manual_participant")

    if not pending:
        return

    if user.id != pending["owner_id"]:
        return

    receipt = active_receipt(context)

    if not receipt:
        context.chat_data.pop(
            "awaiting_manual_participant",
            None,
        )
        return

    name = update.message.text.strip()

    if not name:
        await update.message.reply_text("Имя не должно быть пустым.")
        return

    if len(name) > 64:
        await update.message.reply_text("Имя слишком длинное. Максимум 64 символа.")
        return

    manual_id = next_manual_id(receipt)

    receipt["participants"][manual_id] = {
        "name": name,
        "telegram": False,
    }

    context.chat_data.pop(
        "awaiting_manual_participant",
        None,
    )

    await update.message.reply_text(f"✅ Участник «{name}» добавлен.")

    try:
        await context.bot.edit_message_text(
            chat_id=pending["chat_id"],
            message_id=pending["message_id"],
            text=participants_text(receipt),
            parse_mode="HTML",
            reply_markup=participants_keyboard(
                receipt,
                context.chat_data.get(
                    "known_users",
                    {},
                ),
                pending["page"],
                user.id,
            ),
        )
    except BadRequest:
        pass


# ---------------------------------------------------------------------------
# Passive discovery
# ---------------------------------------------------------------------------


async def remember_any_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    remember_user(
        context,
        update.effective_user,
    )


# ---------------------------------------------------------------------------
# Final calculation
# ---------------------------------------------------------------------------


def calculate_result(
    receipt: dict,
) -> list[dict[str, Any]]:
    participants = receipt["participants"]

    if not participants:
        raise ValueError("Нет участников")

    result = {
        uid: {
            "name": participant["name"],
            "common": 0,
            "personal": 0,
            # Детальный список персональных долей.
            "personal_items": [],
        }
        for uid, participant in participants.items()
    }

    for (
        item_index,
        item,
    ) in enumerate(
        receipt["items"],
        start=1,
    ):
        shares = {
            uid: percent
            for uid, percent in receipt["shares"]
            .get(
                item_index,
                {},
            )
            .items()
            if (uid in participants and percent > 0)
        }

        assigned_percent = sum(
            shares.values(),
            Decimal(0),
        )

        if assigned_percent > 100:
            raise ValueError(
                f"У позиции {item_index} сумма персональных долей превышает 100%."
            )

        common_percent = Decimal(100) - assigned_percent

        component_weights: dict[
            Any,
            Decimal,
        ] = {}

        for uid, percent in shares.items():
            component_weights[("personal", uid)] = percent

        if common_percent > 0:
            component_weights[("common", 0)] = common_percent

        component_amounts = allocate(
            item["total"],
            component_weights,
        )

        # ---------------------------------------------------
        # Personal parts
        # ---------------------------------------------------

        for uid, percent in shares.items():
            cents = component_amounts[("personal", uid)]

            result[uid]["personal"] += cents

            result[uid]["personal_items"].append(
                {
                    "item_index": item_index,
                    "name": item["name"],
                    "percent": percent,
                    "amount": cents,
                }
            )

        # ---------------------------------------------------
        # Common remainder
        # ---------------------------------------------------

        common_cents = component_amounts.get(
            ("common", 0),
            0,
        )

        if common_cents:
            common_weights = {uid: Decimal(1) for uid in participants}

            common_allocation = allocate(
                common_cents,
                common_weights,
            )

            for uid, cents in common_allocation.items():
                result[uid]["common"] += cents

    rows = []

    for uid, data in result.items():
        total = data["common"] + data["personal"]

        rows.append(
            {
                "id": uid,
                "name": data["name"],
                "common": data["common"],
                "personal": data["personal"],
                "personal_items": data["personal_items"],
                "total": total,
            }
        )

    return rows


# ---------------------------------------------------------------------------
# Final Telegram report
# ---------------------------------------------------------------------------


def result_table(
    rows: list[dict],
) -> str:
    name_width = max(
        len("Участник"),
        *(len(row["name"]) for row in rows),
    )

    name_width = min(
        name_width,
        20,
    )

    lines = [
        (f"{'Участник':<{name_width}} {'Общие':>9} {'Личные':>9} {'Итого':>9}"),
        "-" * (name_width + 30),
    ]

    for row in rows:
        name = short(
            row["name"],
            name_width,
        )

        lines.append(
            f"{name:<{name_width}} "
            f"{money(row['common']):>9} "
            f"{money(row['personal']):>9} "
            f"{money(row['total']):>9}"
        )

    return "\n".join(lines)


def personal_details_text(
    rows: list[dict],
) -> str:
    lines = ["<b>Персональные доли:</b>"]

    found = False

    for row in rows:
        items = row["personal_items"]

        if not items:
            continue

        found = True

        lines.extend(
            [
                "",
                (f"<b>{html.escape(row['name'])}:</b>"),
            ]
        )

        for item in items:
            item_name = short(
                item["name"],
                REPORT_ITEM_NAME_LEN,
            )

            lines.append(
                "• "
                f"{html.escape(item_name)}"
                " — "
                f"{format_percent(item['percent'])}%"
                " = "
                f"<b>{money(item['amount'])} ₽</b>"
            )

    if not found:
        lines.append("\nНет персонально назначенных долей.")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Result CSV
# ---------------------------------------------------------------------------


def personal_details_csv(
    row: dict,
) -> str:
    parts = []

    for item in row["personal_items"]:
        name = short(
            item["name"],
            REPORT_ITEM_NAME_LEN,
        )

        parts.append(
            f"{name}:{format_percent(item['percent'])}%={money(item['amount'])}"
        )

    return "; ".join(parts)


def make_result_csv(
    receipt: dict,
    rows: list[dict],
) -> io.BytesIO:

    payer_id = receipt["payer_id"]

    output = io.StringIO()

    writer = csv.writer(output)

    writer.writerow(
        [
            "participant",
            "common",
            "personal",
            "personal_details",
            "total",
            "transfer_to_payer",
            "payer",
        ]
    )

    for row in rows:
        transfer = 0 if row["id"] == payer_id else row["total"]

        writer.writerow(
            [
                row["name"],
                money(row["common"]),
                money(row["personal"]),
                personal_details_csv(row),
                money(row["total"]),
                money(transfer),
                ("yes" if row["id"] == payer_id else "no"),
            ]
        )

    result = io.BytesIO(output.getvalue().encode("utf-8-sig"))

    result.name = "split-result.csv"

    return result


# ---------------------------------------------------------------------------
# Send final result
# ---------------------------------------------------------------------------


async def send_final_result(
    chat,
    context: ContextTypes.DEFAULT_TYPE,
    receipt: dict,
) -> None:

    try:
        rows = calculate_result(receipt)

    except ValueError as exc:
        await chat.send_message(f"Не удалось завершить расчёт:\n{exc}")
        return

    payer_id = receipt["payer_id"]

    final_payer_name = payer_name(receipt)

    table = result_table(rows)

    # -----------------------------------------------------------------------
    # Summary
    # -----------------------------------------------------------------------

    summary = (
        "✅ <b>Расчёт завершён</b>\n\n"
        f"<pre>{html.escape(table)}</pre>\n\n"
        "Чек оплатил: "
        f"<b>{html.escape(final_payer_name)}</b>"
    )

    await chat.send_message(
        summary,
        parse_mode="HTML",
    )

    # -----------------------------------------------------------------------
    # Personal details
    # -----------------------------------------------------------------------

    details = personal_details_text(rows)

    await chat.send_message(
        details,
        parse_mode="HTML",
    )

    # -----------------------------------------------------------------------
    # Transfers
    # -----------------------------------------------------------------------

    transfers = []

    for row in rows:
        if row["id"] == payer_id:
            continue

        transfers.append(
            
                "• "
                f"{html.escape(row['name'])}"
                " → "
                f"{html.escape(final_payer_name)}: "
                f"<b>{money(row['total'])} ₽</b>"
            
        )

    if transfers:
        await chat.send_message(
            ("<b>Кто кому переводит:</b>\n" + "\n".join(transfers)),
            parse_mode="HTML",
        )

    # -----------------------------------------------------------------------
    # CSV
    # -----------------------------------------------------------------------

    result_file = make_result_csv(
        receipt,
        rows,
    )

    await chat.send_document(
        result_file,
        caption="Итоговый расчёт",
    )

    clear_receipt_state(context)


def clear_receipt_state(
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    context.chat_data.pop(
        "receipt",
        None,
    )

    context.chat_data.pop(
        "awaiting_manual_participant",
        None,
    )

    context.chat_data.pop(
        "awaiting_percentage",
        None,
    )


# ---------------------------------------------------------------------------
# Personal finish / cancel
# ---------------------------------------------------------------------------


async def callback_finish(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    query = update.callback_query

    _, menu_user_text = query.data.split(":")

    menu_user_id = int(menu_user_text)

    if not validate_personal_menu(
        query,
        menu_user_id,
    ):
        await query.answer(
            "Это меню другого участника.",
            show_alert=True,
        )
        return

    receipt = active_receipt(context)

    if not receipt:
        await query.answer(
            "Чек уже закрыт.",
            show_alert=True,
        )
        return

    if menu_user_id != receipt["owner_id"]:
        await query.answer(
            "Недоступно.",
            show_alert=True,
        )
        return

    await query.answer("Завершаю расчёт.")

    await send_final_result(
        query.message.chat,
        context,
        receipt,
    )


async def callback_cancel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    query = update.callback_query

    _, menu_user_text = query.data.split(":")

    menu_user_id = int(menu_user_text)

    if not validate_personal_menu(
        query,
        menu_user_id,
    ):
        await query.answer(
            "Это меню другого участника.",
            show_alert=True,
        )
        return

    receipt = active_receipt(context)

    if not receipt:
        await query.answer(
            "Чек уже закрыт.",
            show_alert=True,
        )
        return

    if menu_user_id != receipt["owner_id"]:
        await query.answer(
            "Недоступно.",
            show_alert=True,
        )
        return

    clear_receipt_state(context)

    await query.answer("Чек отменён.")

    await query.edit_message_text("🗑 Чек отменён.")


async def callback_noop(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    remember_user(
        context,
        update.callback_query.from_user,
    )

    await update.callback_query.answer()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:

    builder = (
        Application.builder()
        .token(TOKEN)
        .persistence(
            PicklePersistence(
                filepath=STATE_FILE,
            )
        )
    )

    if PROXY:
        builder = builder.proxy(PROXY).get_updates_proxy(PROXY)

    app = builder.build()

    # Commands

    app.add_handler(
        CommandHandler(
            [
                "start",
                "help",
            ],
            help_command,
        )
    )

    app.add_handler(
        CommandHandler(
            "items",
            items_command,
        )
    )

    app.add_handler(
        CommandHandler(
            "finish",
            finish_command,
        )
    )

    app.add_handler(
        CommandHandler(
            "cancel",
            cancel_command,
        )
    )

    # CSV

    app.add_handler(
        MessageHandler(
            filters.Document.FileExtension("csv"),
            upload_csv,
        )
    )

    # Shared menu

    app.add_handler(
        CallbackQueryHandler(
            callback_open,
            pattern=r"^open$",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            callback_shared_participants,
            pattern=r"^sps$",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            callback_shared_finish,
            pattern=r"^sf$",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            callback_shared_cancel,
            pattern=r"^sc$",
        )
    )

    # Personal items

    app.add_handler(
        CallbackQueryHandler(
            callback_item,
            pattern=r"^i:\d+:\d+:\d+$",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            callback_page,
            pattern=r"^p:\d+:\d+$",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            callback_back,
            pattern=r"^b:\d+:\d+$",
        )
    )

    # Percentages

    app.add_handler(
        CallbackQueryHandler(
            callback_percentage,
            pattern=(
                r"^pct:\d+:\d+:\d+:"
                r"(25|50|75|100)$"
            ),
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            callback_percentage_custom,
            pattern=r"^pc:\d+:\d+:\d+$",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            callback_percentage_delete,
            pattern=r"^pd:\d+:\d+:\d+$",
        )
    )

    # Participants

    app.add_handler(
        CallbackQueryHandler(
            callback_participants,
            pattern=r"^ps:\d+:\d+$",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            callback_add_known,
            pattern=r"^ak:\d+:\d+:\d+$",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            callback_add_manual,
            pattern=r"^am:\d+:\d+$",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            callback_set_payer,
            pattern=r"^pay:\d+:-?\d+:\d+$",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            callback_remove_participant,
            pattern=r"^rm:\d+:-?\d+:\d+$",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            callback_act_as,
            pattern=r"^aa:\d+:-?\d+:\d+$",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            callback_act_self,
            pattern=r"^as:\d+:\d+$",
        )
    )

    # Finish / cancel

    app.add_handler(
        CallbackQueryHandler(
            callback_finish,
            pattern=r"^f:\d+$",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            callback_cancel,
            pattern=r"^c:\d+$",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            callback_noop,
            pattern=r"^noop$",
        )
    )

    # Text input:
    # - произвольный процент
    # - имя вручную добавленного участника

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            normal_text_message,
        )
    )

    # Запоминаем пользователей группы.

    app.add_handler(
        MessageHandler(
            ~filters.TEXT & ~filters.Document.FileExtension("csv"),
            remember_any_message,
        )
    )

    app.run_polling()


if __name__ == "__main__":
    main()
