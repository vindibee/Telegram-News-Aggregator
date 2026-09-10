"""Middleware ограничения частоты обращений."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.dispatcher.flags import get_flag
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, Message, TelegramObject, User

from core.logger import get_logger
from services.ratelimit.base import RateLimitRule
from services.ratelimit.policy import AntiFloodPolicy, FloodAction, FloodVerdict
from services.i18n import Translator
from tg_bot.flags import RATE_LIMIT_FLAG, SKIP_THROTTLING_FLAG
from tg_bot.middlewares.i18n import I18N_KEY

logger = get_logger(__name__)



class ThrottlingMiddleware(BaseMiddleware):
    """Ограничивает частоту обращений пользователя.

    Регистрируется как **внутренний** middleware наблюдателей ``message`` и
    ``callback_query``: только на этом этапе известен выбранный хендлер, а
    значит и его флаги с индивидуальным лимитом.

    Правило берётся из флага хендлера, иначе применяется значение по
    умолчанию для типа события. Сообщения и нажатия на кнопки считаются
    раздельно: активная переписка не должна лишать пользователя
    возможности нажать кнопку.
    """

    def __init__(
        self,
        policy: AntiFloodPolicy,
        default_rule: RateLimitRule,
    ) -> None:
        self._policy = policy
        self._default_rule = default_rule

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if get_flag(data, SKIP_THROTTLING_FLAG, default=False):
            return await handler(event, data)

        user: User | None = data.get("event_from_user")
        if user is None:
            # Служебные обновления без автора ограничивать не по чему.
            return await handler(event, data)

        rule = get_flag(data, RATE_LIMIT_FLAG, default=None) or self._default_rule
        if not isinstance(rule, RateLimitRule):
            logger.error("Флаг %s содержит неверный тип %s, применяю правило по умолчанию",
                         RATE_LIMIT_FLAG, type(rule).__name__)
            rule = self._default_rule

        verdict = await self._policy.check(user.id, rule)
        if not verdict.blocked:
            return await handler(event, data)

        await self._notify(event, verdict, data.get(I18N_KEY))
        # Возврат None вместо вызова хендлера: обработка обновления
        # прекращается, но апдейт считается обработанным, и aiogram не
        # передаёт его дальше по цепочке роутеров.
        return None

    async def _notify(
        self,
        event: TelegramObject,
        verdict: FloodVerdict,
        i18n: Translator | None,
    ) -> None:
        """Сообщает пользователю о срабатывании ограничения.

        Для нажатий на кнопки ответ отправляется всегда, даже без текста:
        неотвеченный callback оставляет на кнопке «часики» примерно на
        полминуты, и пользователь считает, что бот завис.

        Сообщения в чат, наоборот, отправляются только когда политика
        разрешила предупреждение: иначе бот отвечал бы на каждое сообщение
        флудера и удваивал нагрузку.

        Ошибки доставки не прерывают обработку — они лишь логируются.
        """
        if i18n is None:
            # Локализатор кладёт middleware, который стоит выше по цепочке;
            # его отсутствие означает ошибку сборки диспетчера, а не
            # штатную ситуацию, — но молчать в ответ на флуд нельзя.
            logger.error("ThrottlingMiddleware вызван без локализатора")
            return

        key = (
            "throttle.muted" if verdict.action is FloodAction.MUTE else "throttle.too_often"
        )
        text = i18n(key, seconds=i18n.plural("units.seconds", verdict.retry_after))

        try:
            if isinstance(event, CallbackQuery):
                await event.answer(
                    text if verdict.notify else "",
                    show_alert=verdict.notify and verdict.action is FloodAction.MUTE,
                )
            elif isinstance(event, Message) and verdict.notify:
                await event.answer(text)
        except TelegramAPIError as exc:
            logger.warning("Не удалось уведомить об ограничении: %s", exc)
