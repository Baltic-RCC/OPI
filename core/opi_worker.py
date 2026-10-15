import sys
import json
import logging
import opi.configs as config
from opi.shared.opi_functions import get_opi_timestamps, get_input_dir, get_uap_offset_time, get_opi_year
from opi.shared.data_functions import set_task_version, set_outage_corrections
from opi.shared.config_parser import parse_app_properties
from opi.core.opi_handler import HandlerRunOpi
from opi.shared.logging_setup import setup_logging, add_elk_handler
import pypowsybl as pp

setup_logging()
parse_app_properties(caller_globals=globals(), path=config.paths.task_generator.task)
parse_app_properties(caller_globals=globals(), path=config.paths.integrations.elastic)
add_elk_handler(elk_server=ELK_SERVER, elk_api_key=ELK_API_KEY, index='opi-logs')

logger = logging.getLogger(__name__)
logger.info(f"pypowsybl {pp.__version__}")


tasks = []
timestamps = get_opi_timestamps(iso_offset=DATE_OFFSET, timestamps_per_day=HOURS_PER_TIMESTAMP.split(","),
                                time_horizon=TIME_HORIZON, set_timestamps=SET_CRITICAL_TIMESTAMPS)
if not timestamps:
    logger.error(f"No OPI timestamps generated (TIME_HORIZON={TIME_HORIZON}, "
                 f"SET_CRITICAL_TIMESTAMPS={SET_CRITICAL_TIMESTAMPS!r}), exiting worker")
    sys.exit(1)

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

logger.info(f"OPI run: {TIME_HORIZON}, version {task_version}, {len(tasks)} timestamp(s): "
            f"{[task['scenario_timestamp'] for task in tasks]}")
failed = []
for number, task in enumerate(tasks, start=1):
    logger.info(f"OPI task {number}/{len(tasks)} starting: {TIME_HORIZON} {task['scenario_timestamp']} v{task_version}")
    try:
        HandlerRunOpi().handle(task)
    except Exception as e:
        logger.exception(f"OPI task {number}/{len(tasks)} failed: {task['scenario_timestamp']}: {e}")
        failed.append(task['scenario_timestamp'])

logger.info(f"OPI run finished: {len(tasks) - len(failed)}/{len(tasks)} timestamp(s) succeeded"
            + (f", failed: {failed}" if failed else ""))
# Job fails only when no timestamp succeeded
sys.exit(1 if len(failed) == len(tasks) else 0)
