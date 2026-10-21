import sys
import json
import logging
import contextvars
import config
from opi.timeframes import get_opi_timestamps, get_input_dir, get_uap_offset_time, get_opi_year
from opi.integrations import set_task_version
from opi.outages import set_outage_corrections
from opi.common import parse_app_properties
from opi.assessment import OpiTask, RunInputs, OutagePlanIncompatibilityAssessment
from opi.common import setup_logging, add_elk_handler
from opi.region import MERGE_TYPE, ASSESSED_AREAS, EXTERNAL_AREAS
import pypowsybl as pp

logger = logging.getLogger(__name__)


def main() -> None:
    setup_logging()
    parse_app_properties(caller_globals=globals(), path=config.paths.integrations.elastic)
    add_elk_handler(elk_server=ELK_SERVER, elk_api_key=ELK_API_KEY, index='opi-logs')
    # Parsed after the ELK handler is added, so the run config echo reaches opi-logs
    parse_app_properties(caller_globals=globals(), path=config.paths.task_generator.task)

    # Uncaught errors, e.g. in the run preparation below, are logged (also to opi-logs); exit code stays 1
    sys.excepthook = lambda *exc_info: logger.critical("OPI run failed", exc_info=exc_info)
    logger.info(f"pypowsybl {pp.__version__}")
    # region.properties is parsed at import, before logging is set up
    logger.info(f"OPI region: {MERGE_TYPE} models, assessed areas {ASSESSED_AREAS}, external areas {EXTERNAL_AREAS}")

    uap_offset_time = get_uap_offset_time(time_offset=UAP_QUERY_OFFSET, time_horizon=TIME_HORIZON)
    timestamps = get_opi_timestamps(iso_offset=DATE_OFFSET, timestamps_per_day=HOURS_PER_TIMESTAMP.split(","),
                                    time_horizon=TIME_HORIZON, set_timestamps=SET_CRITICAL_TIMESTAMPS,
                                    uap_query_offset=uap_offset_time)
    if not timestamps:
        logger.error(f"No OPI timestamps generated (TIME_HORIZON={TIME_HORIZON}, "
                     f"SET_CRITICAL_TIMESTAMPS={SET_CRITICAL_TIMESTAMPS!r}), exiting worker")
        sys.exit(1)

    opi_year = get_opi_year(timestamps)
    task_version = set_task_version(timestamps=timestamps, time_horizon=TIME_HORIZON, set_version=TASK_VERSION)
    outage_corrections = set_outage_corrections(OUTAGE_ENABLE, OUTAGE_DISABLE)

    tasks = [OpiTask(time_horizon=TIME_HORIZON,
                     scenario_timestamp=timestamp.isoformat(),
                     opi_year=opi_year,
                     task_version=task_version,
                     model_version=MODEL_VERSION,
                     validate_models=json.loads(VALIDATE_MODELS.lower()),
                     send_to_elk=json.loads(SEND_TO_ELK.lower()),
                     import_minio_models=json.loads(IMPORT_MINIO_MODELS.lower()),
                     reconnect_outages=json.loads(RECONNECT_OUTAGES.lower()),
                     export_models=json.loads(EXPORT_MODELS.lower()),
                     inertia_enabled=json.loads(CALCULATE_INERTIA.lower()),
                     outage_corrections=outage_corrections)
             for timestamp in timestamps]

    logger.info(f"OPI run: {TIME_HORIZON}, version {task_version}, {len(tasks)} timestamp(s): "
                f"{[task.scenario_timestamp for task in tasks]}")
    # LF settings, input lists and the UAP batch are the same for every timestamp of the run
    inputs = RunInputs.load(time_horizon=TIME_HORIZON, lf_parameters_key=SET_LF_PARAMETERS,
                            use_local_inputs=json.loads(USE_DEFAULT_INPUTS.lower()),
                            data_import_folder=get_input_dir(time_horizon=TIME_HORIZON),
                            run_time_offset=DATE_OFFSET, uap_query_offset=uap_offset_time)
    failed = []
    for number, task in enumerate(tasks, start=1):
        logger.info(f"OPI task {number}/{len(tasks)} starting: {TIME_HORIZON} {task.scenario_timestamp} v{task_version}")
        # Own context per task: its log metadata does not leak into the next task or the run summary
        task_context = contextvars.copy_context()
        try:
            task_context.run(OutagePlanIncompatibilityAssessment(task, inputs).run)
        except Exception as e:
            task_context.run(logger.exception, f"OPI task {number}/{len(tasks)} failed: {task.scenario_timestamp}: {e}")
            failed.append(task.scenario_timestamp)

    logger.info(f"OPI run finished: {len(tasks) - len(failed)}/{len(tasks)} timestamp(s) succeeded"
                + (f", failed: {failed}" if failed else ""))
    # Job fails only when no timestamp succeeded
    sys.exit(1 if len(failed) == len(tasks) else 0)


if __name__ == "__main__":
    main()
