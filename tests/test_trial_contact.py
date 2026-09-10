"""Проверка присланного контакта — та часть защиты, что живёт в хендлере.

Вынесена из :mod:`tests.test_trial`: этим проверкам не нужна база, и
маркер ``db`` исключал бы их из быстрого прогона без PostgreSQL.
"""

from __future__ import annotations

import pytest

from services.trial import ForeignContactError
from tg_bot.handlers.trial import _own_phone
from tests.conftest import TELEGRAM_ID

PHONE = "+79001234567"


def test_own_phone_accepts_contact_shared_by_its_owner(make_message, make_contact) -> None:
    message = make_message(text=None)
    contact = make_contact(PHONE, user_id=TELEGRAM_ID)

    assert _own_phone(contact, message) == PHONE, "Собственный контакт должен приниматься"


def test_own_phone_rejects_contact_of_another_person(make_message, make_contact) -> None:
    # Контакт, выбранный из адресной книги, приходит с чужим user_id —
    # именно так обходилась бы защита по номеру телефона.
    message = make_message(text=None)
    contact = make_contact("+79007654321", user_id=TELEGRAM_ID + 1)

    with pytest.raises(ForeignContactError):
        _own_phone(contact, message)


def test_own_phone_rejects_contact_without_user_id(make_message, make_contact) -> None:
    message = make_message(text=None)
    contact = make_contact("+79007654321", user_id=None)

    with pytest.raises(ForeignContactError):
        _own_phone(contact, message)


def test_own_phone_rejects_contact_without_number(make_message, make_contact) -> None:
    message = make_message(text=None)
    contact = make_contact("", user_id=TELEGRAM_ID)

    with pytest.raises(ForeignContactError):
        _own_phone(contact, message)
