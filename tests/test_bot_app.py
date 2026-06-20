import asyncio
import logging

from telegram.error import Conflict

from app import bot_app


def test_application_error_handler_logs_polling_conflict_as_warning(caplog):
    class Context:
        error = Conflict("terminated by other getUpdates request")

    with caplog.at_level(logging.WARNING, logger="bot"):
        asyncio.run(bot_app._on_application_error(None, Context()))

    assert "telegram.update.conflict" in caplog.text
    assert "Traceback" not in caplog.text
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]
