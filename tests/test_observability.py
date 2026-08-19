import io
import json
import logging

from wlc_manager.observability import JsonFormatter, process_run, process_step


def _test_logger(stream: io.StringIO) -> logging.Logger:
    logger = logging.getLogger("wlc_manager.tests.observability")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    return logger


def test_process_and_step_logs_share_run_id() -> None:
    stream = io.StringIO()
    logger = _test_logger(stream)

    with (
        process_run("test_process", trigger="cli", run_id="fixed-run", logger=logger) as run,
        process_step(run, "test_step"),
    ):
        pass

    records = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert len(records) == 4
    assert {record["run_id"] for record in records} == {"fixed-run"}
    assert [record["event"] for record in records] == [
        "process_started",
        "step_started",
        "step_finished",
        "process_finished",
    ]
    assert records[-1]["status"] == "succeeded"


def test_sensitive_extra_fields_are_redacted() -> None:
    formatter = JsonFormatter()
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "safe", (), None)
    record.wlc_password = "must-not-appear"

    payload = json.loads(formatter.format(record))

    assert payload["wlc_password"] == "[REDACTED]"
    assert "must-not-appear" not in formatter.format(record)
