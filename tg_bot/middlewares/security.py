"""Защитный контур бота: частота, гонки и целостность транзакции.

Модуль собирает защиту в один слой и добавляет то, чего не покрывают
остальные middleware проекта. Здесь намеренно **не** переписаны заново три
уже работающих компонента — второй экземпляр каждого из них был бы не
дублированием, а поломкой:

* :class:`~tg_bot.middlewares.throttling.ThrottlingMiddleware` — бюджет
  обращений с эскалацией наказаний. Второй троттлер на тех же событиях
  списывал бы жетоны дважды, и заявленный лимит оказался бы вдвое строже
  настроенного.
* :class:`~tg_bot.middlewares.dependencies.DependenciesMiddleware` —
  сессия SQLAlchemy и граница транзакции: фиксация после успешного
  возврата из хендлера, откат при любом исключении. Второй такой
  middleware открыл бы на апдейт вторую сессию и вторую транзакцию,
  которые не видели бы изменений друг друга и конкурировали за блокировки
  строк.
* :class:`~tg_bot.middlewares.single_flight.SingleFlightMiddleware` —
  блокировка на время обработки нажатия.

Добавляются два уровня.

**Жёсткий интервал** (:class:`CooldownMiddleware`). Ведро с жетонами
позволяет всплеск: при лимите «20 за минуту» все двадцать сообщений можно
отправить за одну секунду. Обычно это удобно, но у дорогих обработчиков
такой всплеск успевает наделать дел до того, как бюджет закончится.
Скользящее окно даёт строгий пол: не чаще одного обращения в полсекунды,
сколько бы бюджета ни оставалось.

**Критические действия** (:class:`CriticalActionMiddleware`). Защита от
двойного нажатия на «Оплатить» или «Активировать триал» отличается от
обычной тем, что опасен не только одновременный повтор, но и повтор
сразу после успеха: хендлер отработал за секунду, пользователь нажал
второй раз через две — и получил второй счёт. Поэтому блокировка держится
ещё некоторое время после успешного завершения.

Про redlock. Классический алгоритм рассчитан на несколько независимых
мастеров Redis и кворум между ними; здесь мастер один, и переносить сюда
redlock значило бы имитировать его гарантии, не имея их. При одном
мастере правильный примитив — ``SET NX PX`` с проверкой владельца при
снятии, и он уже реализован в
:class:`~services.ratelimit.redis_limiter.RedisRateLimiter`. Важно
понимать его границу: это блокировка «на честном слове», без fencing
token, и при остановке процесса дольше TTL блокировку может перехватить
другой. Единственной защитой критических операций она поэтому не служит —
за окончательную однократность отвечают ограничения БД (уникальный ключ
идемпотентности платежа, лимит незавершённых счетов), а этот слой снимает
подавляющее большинство случаев дёшево и до похода в базу.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Final

from aiogram import BaseMiddleware, Dispatcher
from aiogram.dispatcher.flags import get_flag
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, Message, TelegramObject, User

from core.logger import get_logger
from services.i18n import Translator
from services.ratelimit.base import KeyGuard, RateLimiter, RateLimitRule
from services.ratelimit.policy import AntiFloodPolicy
from services.ratelimit.sliding import describe
from tg_bot.flags import CRITICAL_FLAG, SKIP_THROTTLING_FLAG, CriticalActionFlag
from tg_bot.middlewares.dependencies import DependenciesMiddleware
from tg_bot.middlewares.i18n import I18N_KEY
from tg_bot.middlewares.single_flight import SingleFlightMiddleware
from tg_bot.middlewares.throttling import ThrottlingMiddleware

logger = get_logger(__name__)

#: Минимальный интервал между обращениями одного пользователя, секунды.
DEFAULT_COOLDOWN: Final[float] = 0.5

#: Сколько живёт блокировка критического действия, секунды.
#:
#: Это страховка на случай, если процесс умрёт, не сняв её. Значение с
#: запасом больше самого долгого критического хендлера (выставление счёта
#: ходит во внешний API), но заметно меньше времени, за которое человек
#: успеет пожаловаться на «кнопка не работает».
DEFAULT_LOCK_TTL: Final[float] = 30.0

#: Сколько не принимать повтор после успешного выполнения, секунды.
DEFAULT_ACTION_COOLDOWN: Final[float] = 5.0

#: Области скользящего окна: сообщения и нажатия считаются раздельно.
#:
#: Активная переписка не должна лишать человека возможности нажать кнопку —
#: это разные каналы взаимодействия, и общий счётчик связал бы их.
COOLDOWN_SCOPE_MESSAGE: Final[str] = "cooldown-msg"
COOLDOWN_SCOPE_CALLBACK: Final[str] = "cooldown-cb"

#: Псевдотокен «блокировка не бралась из-за отказа хранилища».
#:
#: Настоящие токены выдаёт ``secrets.token_hex``, поэтому совпасть с
#: этим значением они не могут. Отдельный признак нужен, чтобы в
#: ``finally`` не пытаться снять блокировку, которой нет.
_DEGRADED: Final[str] = "\x00degraded"


@dataclass(frozen=True, slots=True)
class SecurityConfig:
    """Настройки защитного контура."""

    #: Минимальный интервал между обращениями, секунды.
    cooldown: float = DEFAULT_COOLDOWN
    #: Время жизни блокировки критического действия, секунды.
    lock_ttl: float = DEFAULT_LOCK_TTL
    #: Пауза после успешного критического действия, секунды.
    action_cooldown: float = DEFAULT_ACTION_COOLDOWN
    #: Держать ли блокировку критического действия при отказе хранилища.
    #:
    #: ``False`` — пропускать. Выбор в пользу доступности осознан: при
    #: недоступном Redis отказ означал бы, что оплатить нельзя вовсе,
    #: тогда как повторный счёт отсекается уникальным ключом
    #: идемпотентности уже в базе.
    fail_closed: bool = False

    def __post_init__(self) -> None:
        if self.cooldown <= 0:
            raise ValueError(f"Интервал должен быть положительным, получено: {self.cooldown}")
        if self.lock_ttl <= 0:
            raise ValueError(f"TTL блокировки должен быть положительным, получено: {self.lock_ttl}")
        if self.action_cooldown <= 0:
            raise ValueError(
                f"Пауза после действия должна быть положительной, получено: {self.action_cooldown}"
            )
        if self.action_cooldown > self.lock_ttl:
            # Пауза длиннее блокировки означает, что повтор отсекался бы
            # уже не блокировкой, а паузой, и TTL перестал бы страховать
            # от зависшего хендлера.
            raise ValueError(
                "Пауза после действия не должна превышать TTL блокировки: "
                f"{self.action_cooldown} > {self.lock_ttl}"
            )


class CooldownMiddleware(BaseMiddleware):
    """Держит минимальный интервал между обращениями пользователя.

    Ставится **после** троттлинга, а не перед ним, и это принципиально.
    Троттлинг считает нарушения и наращивает наказания; если бы жёсткий
    интервал отбивал обращение раньше, политика анти-флуда не увидела бы
    ни одного нарушения и никогда не дошла бы до заглушки — систематический
    флудер получал бы вечное «слишком часто» вместо мьюта.

    Отсюда же следует, что своих нарушений этот слой не регистрирует:
    попытка уже учтена троттлингом выше по цепочке.
    """

    def __init__(
        self,
        limiter: RateLimiter,
        *,
        interval: float = DEFAULT_COOLDOWN,
    ) -> None:
        """
        :param limiter: Ограничитель со скользящим окном.
        :param interval: Минимальный интервал в секундах.
        :raises ValueError: Некорректный интервал.
        """
        if interval <= 0:
            raise ValueError(f"Интервал должен быть положительным, получено: {interval}")

        self._limiter = limiter
        self._message_rule = RateLimitRule(
            limit=1, window=interval, scope=COOLDOWN_SCOPE_MESSAGE
        )
        self._callback_rule = RateLimitRule(
            limit=1, window=interval, scope=COOLDOWN_SCOPE_CALLBACK
        )
        logger.info("Жёсткий интервал обращений: %s", describe(self._message_rule))

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        """Пропускает обращение, если с предыдущего прошёл интервал."""
        if get_flag(data, SKIP_THROTTLING_FLAG, default=False):
            # Тот же флаг, что и у троттлинга: служебные обработчики вроде
            # ответа на PreCheckoutQuery обязаны отвечать немедленно, и
            # придерживать их нельзя ни одним из слоёв.
            return await handler(event, data)

        user: User | None = data.get("event_from_user")
        if user is None:
            return await handler(event, data)

        rule = self._callback_rule if isinstance(event, CallbackQuery) else self._message_rule

        try:
            decision = await self._limiter.acquire(str(user.id), rule)
        except Exception:
            # Отказ хранилища не повод перестать обслуживать людей:
            # бюджетный лимит выше по цепочке продолжает действовать.
            logger.exception("Сбой проверки интервала, обращение пропущено")
            return await handler(event, data)

        if decision.allowed:
            return await handler(event, data)

        logger.debug(
            "Слишком частое обращение: user_id=%s, область %s, повтор через %.2f с",
            user.id, rule.scope, decision.retry_after,
        )
        await self._reject(event, data.get(I18N_KEY))
        # None вместо вызова хендлера: обновление считается обработанным и
        # дальше по цепочке роутеров не пойдёт.
        return None

    @staticmethod
    async def _reject(event: TelegramObject, i18n: Translator | None) -> None:
        """Сообщает об отказе, не создавая шума в чате.

        Нажатию отвечать обязательно: неотвеченный callback оставляет на
        кнопке «часики» примерно на полминуты, и человек считает, что бот
        завис. Сообщениям, наоборот, не отвечаем вовсе — на таком коротком
        интервале это превратило бы бота во второго флудера, а внятное
        предупреждение всё равно даёт троттлинг на своём лимите.
        """
        if not isinstance(event, CallbackQuery):
            return

        try:
            await event.answer(i18n("common.busy") if i18n is not None else "")
        except TelegramAPIError as exc:
            logger.warning("Не удалось ответить на частое нажатие: %s", exc)


class CriticalActionMiddleware(BaseMiddleware):
    """Не даёт выполнить критическое действие дважды.

    Работает только с хендлерами, помеченными флагом
    :func:`~tg_bot.flags.critical`; остальные проходят без единого
    обращения к хранилищу.

    Используются два ключа, и разделение между ними существенно.

    *Блокировка* берётся на время работы хендлера и снимается в
    ``finally``. Её TTL — страховка от смерти процесса, а не рабочий
    параметр.

    *Пауза* ставится только после успешного завершения и никогда не
    снимается — она истекает сама. Именно она отсекает второй тап, который
    приходит уже после того, как хендлер отработал: одной блокировки для
    этого мало, потому что к моменту второго нажатия она снята.

    После исключения пауза не ставится: неудачную попытку человек должен
    иметь возможность повторить сразу.
    """

    def __init__(
        self,
        guard: KeyGuard,
        config: SecurityConfig | None = None,
    ) -> None:
        """
        :param guard: Хранилище коротких блокировок.
        :param config: Настройки; по умолчанию — стандартные.
        """
        self._guard = guard
        self._config = config or SecurityConfig()

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        """Выполняет хендлер под распределённой блокировкой."""
        flag = get_flag(data, CRITICAL_FLAG, default=None)
        if flag is None:
            return await handler(event, data)

        if not isinstance(flag, CriticalActionFlag):
            # Некорректно собранный флаг — ошибка разработчика. Пропускать
            # критическое действие без защиты нельзя, но и молча ронять
            # обработку тоже: чиним до значения по умолчанию и кричим в лог.
            logger.error(
                "Флаг %s содержит неверный тип %s, действие защищено под общим именем",
                CRITICAL_FLAG, type(flag).__name__,
            )
            flag = CriticalActionFlag(name="unknown")

        user: User | None = data.get("event_from_user")
        if user is None:
            return await handler(event, data)

        cooldown = flag.cooldown or self._config.action_cooldown
        lock_key = f"critical:{flag.name}:{user.id}"
        cooldown_key = f"critical-cooldown:{flag.name}:{user.id}"

        try:
            waiting = await self._guard.ttl(cooldown_key)
        except Exception:
            waiting = 0.0
            logger.exception("Сбой проверки паузы критического действия %s", flag.name)

        if waiting > 0:
            logger.info(
                "Повтор критического действия %s отклонён: user_id=%s, осталось %.1f с",
                flag.name, user.id, waiting,
            )
            await self._reject(event, data.get(I18N_KEY))
            return None

        token = await self._acquire(lock_key, flag.name)
        if token is None:
            logger.info(
                "Критическое действие %s уже выполняется: user_id=%s", flag.name, user.id
            )
            await self._reject(event, data.get(I18N_KEY))
            return None

        try:
            result = await handler(event, data)
        except Exception:
            # Паузу не ставим: неудачу нужно дать повторить сразу.
            await self._release(lock_key, token, flag.name)
            raise

        await self._start_cooldown(cooldown_key, cooldown, flag.name)
        await self._release(lock_key, token, flag.name)
        return result

    async def _acquire(self, key: str, action: str) -> str | None:
        """Берёт блокировку, переживая отказ хранилища.

        :return: Токен владения, ``None`` — занято либо отказано.
        """
        try:
            return await self._guard.acquire_once(key, self._config.lock_ttl)
        except Exception:
            logger.exception("Сбой блокировки критического действия %s", action)
            if self._config.fail_closed:
                return None
            # Пропускаем: за однократность в этом случае отвечают
            # ограничения БД, а отказ означал бы неработающую оплату.
            return _DEGRADED

    async def _release(self, key: str, token: str, action: str) -> None:
        """Снимает блокировку, не мешая основному потоку при сбое."""
        if token is _DEGRADED:
            return
        try:
            await self._guard.release(key, token)
        except Exception:
            # Не страшно: блокировка истечёт по TTL сама.
            logger.exception("Сбой снятия блокировки критического действия %s", action)

    async def _start_cooldown(self, key: str, cooldown: float, action: str) -> None:
        """Ставит паузу, в течение которой повтор не принимается."""
        try:
            await self._guard.acquire_once(key, cooldown)
        except Exception:
            logger.exception("Сбой установки паузы после действия %s", action)

    @staticmethod
    async def _reject(event: TelegramObject, i18n: Translator | None) -> None:
        """Объясняет отказ, гася «часики» на кнопке."""
        text = i18n("security.in_progress") if i18n is not None else ""
        try:
            if isinstance(event, CallbackQuery):
                await event.answer(text, show_alert=bool(text))
            elif isinstance(event, Message) and text:
                await event.answer(text)
        except TelegramAPIError as exc:
            logger.warning("Не удалось сообщить об отклонённом повторе: %s", exc)


def setup_security(
    dispatcher: Dispatcher,
    *,
    policy: AntiFloodPolicy,
    guard: KeyGuard,
    cooldown_limiter: RateLimiter,
    message_rule: RateLimitRule,
    callback_rule: RateLimitRule,
    single_flight_ttl: float,
    config: SecurityConfig | None = None,
) -> None:
    """Регистрирует защитный контур на диспетчере.

    Порядок регистрации — это и есть порядок исполнения, поэтому он собран
    в одном месте, а не разбросан по точке входа. Слои идут от дешёвых и
    общих к дорогим и точечным:

    1. **Троттлинг** — бюджет обращений и эскалация наказаний. Первым,
       чтобы каждая попытка была учтена политикой: слой, отбивающий
       обращение раньше, лишил бы её возможности когда-либо дойти до
       заглушки.
    2. **Жёсткий интервал** — строгий пол поверх бюджета: гасит всплеск,
       который ведро с жетонами пропускает целиком.
    3. **Одиночный запуск** — не даёт одному нажатию обрабатываться
       дважды одновременно.
    4. **Критические действия** — держит блокировку и после успеха.

    Middleware сессии здесь не регистрируется: он внешний (``outer``) и
    должен охватывать в том числе фильтры, которым тоже нужна открытая
    транзакция. Его место — рядом с остальной сборкой зависимостей в точке
    входа; :class:`DependenciesMiddleware` реэкспортирован этим модулем,
    чтобы место импорта оставалось одним.

    :param dispatcher: Диспетчер aiogram.
    :param policy: Политика анти-флуда.
    :param guard: Хранилище коротких блокировок.
    :param cooldown_limiter: Ограничитель со скользящим окном.
    :param message_rule: Бюджетное правило для сообщений.
    :param callback_rule: Бюджетное правило для нажатий.
    :param single_flight_ttl: TTL блокировки одиночного запуска.
    :param config: Настройки защитного контура.
    """
    settings = config or SecurityConfig()

    dispatcher.message.middleware(ThrottlingMiddleware(policy, message_rule))
    dispatcher.callback_query.middleware(ThrottlingMiddleware(policy, callback_rule))

    cooldown = CooldownMiddleware(cooldown_limiter, interval=settings.cooldown)
    dispatcher.message.middleware(cooldown)
    dispatcher.callback_query.middleware(cooldown)

    dispatcher.callback_query.middleware(
        SingleFlightMiddleware(guard, ttl=single_flight_ttl)
    )

    critical = CriticalActionMiddleware(guard, settings)
    dispatcher.message.middleware(critical)
    dispatcher.callback_query.middleware(critical)

    logger.info(
        "Защитный контур собран: интервал %.1f с, блокировка %.0f с, пауза после действия %.0f с",
        settings.cooldown, settings.lock_ttl, settings.action_cooldown,
    )


__all__ = [
    "COOLDOWN_SCOPE_CALLBACK",
    "COOLDOWN_SCOPE_MESSAGE",
    "DEFAULT_ACTION_COOLDOWN",
    "DEFAULT_COOLDOWN",
    "DEFAULT_LOCK_TTL",
    "CooldownMiddleware",
    "CriticalActionMiddleware",
    "DependenciesMiddleware",
    "SecurityConfig",
    "SingleFlightMiddleware",
    "ThrottlingMiddleware",
    "setup_security",
]
