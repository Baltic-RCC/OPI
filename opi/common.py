import configparser
import logging
import os
import ast
import sys
from contextvars import ContextVar
from typing import Dict, Any

logger = logging.getLogger(__name__)


def parse_app_properties(caller_globals: Dict[str, Any],
                         path: str,
                         section: str = "MAIN",
                         sanitize_mask: str = "****",
                         eval_types: bool = False) -> None:
    """Parse application properties and assign values to caller_globals dictionary.

    Args:
        caller_globals (dict): The dictionary of caller's globals to assign the parsed properties.
        path (str): The path to the .properties file.
        section (str, optional): The section name in the properties file to parse. Defaults to "MAIN".
        sanitize_mask (str, optional): The mask to use for sanitizing sensitive values. Defaults to "****".
        eval_types (bool, optional): Flag to convert strings to native datatypes. Defaults to False.
    """

    # Configure settings parser
    raw_settings = configparser.RawConfigParser()
    raw_settings.optionxform = str

    # Load settings
    raw_settings.read(path)

    for setting in raw_settings.items(section):

        # Get parameter name and value from settings
        parameter_name, parameter_config_value = setting

        # Force parameter name to upper, to follow python PEP and also force that all settings are defined with cappital letters
        parameter_name = parameter_name.upper()

        # Get parameter value from ENV, if available
        parameter_env_value = os.getenv(parameter_name)

        # Check if secret needs to be sanitized
        sanitize = any(word in parameter_name for word in ("PASSWORD", "KEY", "TOKEN", "SECRET"))

        # If parameter is defined in ENV
        if parameter_env_value:
            defined_in = "ENVIRONMENT"
            parameter_value = parameter_env_value

        # If not, take the default value form configs
        else:
            defined_in = "PROPERTIES"
            parameter_value = parameter_config_value

        # Sanitize parameter value for logging
        sanitized_parameter_value = sanitize_mask if sanitize else parameter_value

        logger.info(f"[{defined_in}] {parameter_name} = {sanitized_parameter_value}",
                    extra={"parameter_defined_in": defined_in,
                           "parameter_name": parameter_name,
                           "parameter_value": sanitized_parameter_value})

        # Convert string parameters to native datatypes
        if eval_types:
            try:
                parameter_value = ast.literal_eval(parameter_value)
            except ValueError:
                pass

        # Assign value to globals with upper letters
        caller_globals[parameter_name] = parameter_value

        # TODO - maybe return globals, so any dict could be used. Also create dict in the beginning if no globals were provided


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
    else:
        logging.getLogger(__name__).warning(f"ELK log handler not connected to '{elk_server}', logs not sent to '{index}'")
