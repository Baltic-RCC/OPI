import json
import logging
import opi.configs
from opi.shared.config_parser import parse_app_properties
import opi.shared.sa_config as sa_conf
from opi.core.opi_analysis import OutagePlanIncompatibilityAssessment

parse_app_properties(caller_globals=globals(), path=opi.configs.paths.integrations.elastic)
parse_app_properties(caller_globals=globals(), path=opi.configs.paths.integrations.minio)

sa_config = sa_conf.SecurityAnalysis(suspend_stdout=True)

logger = logging.getLogger(__name__)


def convert_dict_str_to_bool(data_dict: dict):
    for key, value in data_dict.items():
        if isinstance(value, str):
            if value in ['True', 'true', 'TRUE']:
                data_dict[key] = True
            elif value in ['False', 'false', 'FALSE']:
                data_dict[key] = False
        elif isinstance(value, dict):
            # Recursively converter nested dictionaries
            data_dict[key] = convert_dict_str_to_bool(value)

    return data_dict


class HandlerRunOpi:

    def __init__(self):
        self.name = 'opi_handler'

    def handle(self, task_object):
        from opi.shared.logging_setup import log_context
        from opi.shared.process_results import get_opi_week_number, get_result_opi_year
        from datetime import datetime

        task = task_object
        if not isinstance(task_object, dict):
            task = json.loads(task_object)
        task = convert_dict_str_to_bool(task)
        timestamp = datetime.strptime(task['scenario_timestamp'], '%Y-%m-%dT%H:%M:%S')

        # Set logging metadata for OPI task run
        token = log_context.set({
            "@time_horizon": task['time_horizon'],
            "@scenario_timestamp": task['scenario_timestamp'],
            "@version": task['task_version'],
            "opi_year": get_result_opi_year(timestamp, task['time_horizon'], task['opi_year']),
            "opi_month": str(timestamp.month),
            "opi_week": get_opi_week_number(timestamp),

        })

        logger.info("Starting new OPI task")

        # Create OPI class object
        opi_service = OutagePlanIncompatibilityAssessment(task=task)
        opi_service.get_loadflow_settings(elk_server=ELK_SERVER)
        opi_service.get_input_data(minio_bucket=DATA_IMPORT_BUCKET)
        opi_service.get_elk_uap(index=ELK_UAP_INDEX)
        opi_service.get_opi_model()
        opi_service.load_model_data()
        if opi_service.validate_models:
            opi_service.validate_base_case()
        if opi_service.model_is_valid is False:
            raise ValueError(f"Model failed validation, timestamp {task['scenario_timestamp']}")

        # Perform OPI calculation
        opi_service.calculate_opi()
        sa_diverged = opi_service.opi_results is None
        if opi_service.export_models:
            opi_service.export_model_to_minio(bucket=DATA_EXPORT_BUCKET, folder=OPI_MODEL_EXPORT_FOLDER)
        # After calculate_opi: adds to opi_results, also when security analysis diverged
        if opi_service.inertia_enabled:
            try:
                opi_service.calculate_inertia()
            except Exception as error:
                logger.error(f"Inertia calculation failed: {error}")
        if opi_service.opi_results:
            opi_service.update_result_metadata()

            if opi_service.send_to_elk is True:
                if not opi_service.send_results_to_elk():
                    raise RuntimeError(f"Sending results to ELK failed, timestamp {task['scenario_timestamp']}")

            logger.info(f"{opi_service.scenario_timestamp} OPI calculation finished")

        if sa_diverged:
            raise RuntimeError(f"Security analysis diverged, no OPI results for timestamp {task['scenario_timestamp']}")

        return task_object


if __name__ == "__main__":
    import logging
    logger = logging.getLogger(__name__)

    sample_task = {'data_import_folder': 'OPI/inputs/W-1',
                   'export_to_minio': True,
                   'hours_per_timestamp': ['09:30'],
                   'run_time_offset': 'P0W',
                   'scenario_timestamp': '2025-10-03T09:30:00',
                   'send_to_elk': False,
                   'time_horizon': 'WK',
                   'uap_query_offset': '-P2W',
                   'use_default_inputs': True,
                   'validate_models': False,
                   'task_version': 0,
                   'model_version': 'LATEST',
                   "lf_parameters": "LF_PARAMETERS"
                   }

    worker = HandlerRunOpi()
    finished_task = worker.handle(sample_task)
    print('Done')
