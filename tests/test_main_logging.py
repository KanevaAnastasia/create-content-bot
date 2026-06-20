import logging
from pathlib import Path

import main

from app.llm_logging import get_llm_io_logger


def test_configure_logging_creates_file_handlers(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("BOT_LOG_DIR", str(tmp_path))

    main._configure_logging()
    logger = logging.getLogger("test.logging")
    logger.info("hello info")
    logger.error("hello error")

    all_log = tmp_path / "bot.log"
    err_log = tmp_path / "errors.log"
    assert all_log.exists()
    assert err_log.exists()

    all_text = all_log.read_text(encoding="utf-8")
    err_text = err_log.read_text(encoding="utf-8")
    assert "hello info" in all_text
    assert "hello error" in all_text
    assert "hello error" in err_text


def test_configure_logging_writes_console_to_stdout(monkeypatch, tmp_path: Path, capsys):
    monkeypatch.setenv("BOT_LOG_DIR", str(tmp_path))

    main._configure_logging()
    logging.getLogger("test.console").info("hello console")

    captured = capsys.readouterr()
    assert "hello console" in captured.out


def test_configure_logging_llm_io_file(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("BOT_LOG_DIR", str(tmp_path))
    main._configure_logging()
    get_llm_io_logger().info("llm full response line")
    llm_log = tmp_path / "llm_responses.log"
    assert llm_log.exists()
    assert "llm full response line" in llm_log.read_text(encoding="utf-8")
    assert "llm full response line" in (tmp_path / "bot.log").read_text(encoding="utf-8")


def test_upgrade_database_to_head_preserves_app_logging(monkeypatch):
    seen = {}

    def fake_upgrade(config, target):
        seen["target"] = target
        seen["skip_logging_config"] = config.attributes.get("skip_logging_config")

    monkeypatch.setattr(main.alembic_command, "upgrade", fake_upgrade)

    main.upgrade_database_to_head()

    assert seen == {"target": "head", "skip_logging_config": True}
