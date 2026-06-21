import logging
import sys

def setup_logger():
    """
    Настраивает единый формат логирования для всего приложения.
    Вывод направляется в stdout для корректной работы Docker логов.
    """
    logger = logging.getLogger("bot")
    logger.setLevel(logging.INFO)

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # Отключаем лишний спам от библиотек
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("aiogram").setLevel(logging.INFO)

    return logger

logger = setup_logger()