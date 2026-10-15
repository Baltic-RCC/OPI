import sys
import json
import logging
import opi.configs as config
from opi.shared.opi_functions import get_opi_timestamps, get_input_dir, get_uap_offset_time, get_opi_year
from opi.shared.data_functions import set_task_version, set_outage_corrections
from opi.shared.config_parser import parse_app_properties
from opi.core.opi_handler import HandlerRunOpi
from brcc_apis import elk_logger
from contextvars import ContextVar

parse_app_properties(caller_globals=globals(), path=config.paths.task_generator.task)
parse_app_properties(caller_globals=globals(), path=config.paths.integrations.elastic)

# Configure global logging metadata
log_context = ContextVar("log_context", default={})
old_factory = logging.getLogRecordFactory()

def record_factory(*args, **kwargs):
    record = old_factory(*args, **kwargs)
    ctx = log_context.get()
    if ctx:
        for k, v in ctx.items():
            record.__dict__[k] = v

    return record

logging.setLogRecordFactory(record_factory)

# Setup logging
root_logger = logging.getLogger()
if not any(isinstance(h, logging.StreamHandler) for h in root_logger.handlers):
    ch = logging.StreamHandler(sys.stdout)
    format = "%(levelname) -10s %(asctime) -20s %(name) -30s %(funcName) -35s %(lineno) -5d: %(message)s"
    date_format = "%Y-%m-%d %H:%M:%S"
    ch.setFormatter(logging.Formatter(fmt=format, datefmt=date_format))
    root_logger.addHandler(ch)

elk_log_handler = elk_logger.ElkLoggingHandler(elk_server=ELK_SERVER, index='opi-logs', api_key=ELK_API_KEY)
if elk_log_handler.connected:
    root_logger.addHandler(elk_log_handler)

logger = logging.getLogger(__name__)


tasks = []
timestamps = get_opi_timestamps(iso_offset=DATE_OFFSET, timestamps_per_day=HOURS_PER_TIMESTAMP.split(","),
                                time_horizon=TIME_HORIZON, set_timestamps=SET_CRITICAL_TIMESTAMPS)

opi_year = get_opi_year(timestamps)
data_import_folder = get_input_dir(time_horizon=TIME_HORIZON)
uap_offset_time = get_uap_offset_time(time_offset=UAP_QUERY_OFFSET, time_horizon=TIME_HORIZON)
task_version = set_task_version(timestamps=timestamps, time_horizon=TIME_HORIZON, set_version=TASK_VERSION)
outage_corrections = set_outage_corrections(OUTAGE_ENABLE, OUTAGE_DISABLE)

for timestamp in timestamps:
    task = {
        "time_horizon": TIME_HORIZON,
        "data_import_folder": data_import_folder,
        "run_time_offset": DATE_OFFSET,
        "uap_query_offset": uap_offset_time,
        "opi_year": opi_year,
        "task_version": task_version,
        "model_version": MODEL_VERSION,
        "hours_per_timestamp": HOURS_PER_TIMESTAMP.split(","),
        "scenario_timestamp": timestamp.isoformat(),
        "validate_models": json.loads(VALIDATE_MODELS.lower()),
        "send_to_elk": json.loads(SEND_TO_ELK.lower()),
        "use_default_inputs": json.loads(USE_DEFAULT_INPUTS.lower()),
        "lf_parameters_key": SET_LF_PARAMETERS,
        "import_minio_models": IMPORT_MINIO_MODELS,
        "export_models": json.loads(EXPORT_MODELS.lower()),
        "inertia_enabled": json.loads(CALCULATE_INERTIA.lower()),
        "outage_corrections": outage_corrections
    }
    tasks.append(task)

if tasks:
    for task in tasks:
        try:
            HandlerRunOpi().handle(task)
        except Exception as e:
            logger.error(e)
            continue
    raise SystemExit()
else:
    logger.info("No tasks generated at current timeframe, exiting worker.")
    raise SystemError("OPI worker failed")
