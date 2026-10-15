import logging
import sys
from contextvars import ContextVar

# Task metadata added to every log record (set per task in the handler)
log_context = ContextVar("log_context", default={})

LOG_FORMAT = "%(levelname) -10s %(asctime) -20s %(name) -30s %(funcName) -35s %(lineno) -5d: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def _install_record_factory() -> None:
    old_factory = logging.getLogRecordFactory()
    if getattr(old_factory, "opi_context", False):
        return

    def record_factory(*args, **kwargs):
        record = old_factory(*args, **kwargs)
        for k, v in log_context.get().items():
            record.__dict__[k] = v
        return record

    record_factory.opi_context = True
    logging.setLogRecordFactory(record_factory)


def setup_logging(level: int = logging.INFO) -> None:
    """Configure root logging to stdout, replacing handlers set by imported modules."""
    _install_record_factory()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(fmt=LOG_FORMAT, datefmt=DATE_FORMAT))
    logging.basicConfig(level=level, handlers=[handler], force=True)


def add_elk_handler(elk_server: str, elk_api_key: str, index: str = "opi-logs") -> None:
    """Send root logs to ELK if the server is reachable."""
    from brcc_apis import elk_logger
    elk_handler = elk_logger.ElkLoggingHandler(elk_server=elk_server, index=index, api_key=elk_api_key)
    if elk_handler.connected:
        logging.getLogger().addHandler(elk_handler)
