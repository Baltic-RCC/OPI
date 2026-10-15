from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, field_validator
import os
import sys
import logging
import uuid
from pathlib import Path
from typing import List, Any, Dict

# Start logger
logger = logging.getLogger(__name__)


def _print_parameter_source(classobj: object, sanitize_mask: str = '****') -> None:
    model_env_prefix = classobj.model_config.get('env_prefix')
    for parameter_name, parameter_value in classobj.model_dump(exclude_none=True).items():
        sanitize_flag = any(word in parameter_name.lower() for word in ('password', 'key', 'token', 'secret'))
        if parameter_name in classobj.model_fields_set:
            if os.environ.get(f"{model_env_prefix.lower()}{parameter_name}" if model_env_prefix else parameter_name):
                parameter_source = 'ENVIRONMENT'
            else:
                parameter_source = 'DOTENV'
        else:
            parameter_source = 'CONFIGURATION'

        # Add prefix to parameter name if it was used in model
        parameter_name = f"{model_env_prefix.lower()}{parameter_name}" if model_env_prefix else parameter_name

        # Sanitize parameter value
        parameter_value = sanitize_mask if sanitize_flag else parameter_value

        # Log message
        logger.info(f"[{parameter_source}] Parameter {parameter_name}: {parameter_value}")


def get_config(**kwargs):
    config = Config()
    for config_name, config_object in kwargs.items():
        setattr(config, config_name, config_object)

    return config


class Config:
    pass


class CustomBaseSettings(BaseSettings):
    """
    Modified class of BaseSettings with automatically printing parameters source to logger
    """
    # suspends the output to stdout
    suspend_stdout: bool = Field(default=False, exclude=True, repr=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.suspend_stdout:
            _print_parameter_source(self)


# LOGGING
class Logging(BaseSettings):
    """
    0 - NOTSET, 10 - DEBUG, 20 - INFO, 30 - WARNING, 40 - ERROR, 50 - CRITICAL
    """
    model_config = SettingsConfigDict(env_prefix='LOGGING_')

    stream_format: str = "%(levelname) -10s %(asctime) -10s %(name) -40s %(funcName) -40s %(lineno) -5d: %(message)s"
    date_format: str = "%Y-%m-%d %H:%M:%S"
    level: int = 20

    # Updating logger basic configs
    logging.basicConfig(stream=sys.stdout,
                        format=stream_format,
                        datefmt=date_format,
                        level=level,
                        )


class ElkLogging(CustomBaseSettings):
    model_config = SettingsConfigDict(env_prefix='ELK_')

    handler: bool = False
    server: str | None = None

    def model_post_init(self, __context: Any) -> None:
        if not self.server and self.handler:
            self.server = Elk(suspend_stdout=True).server

    def initialize(self, index: str, extra: dict | None = None, fields: list | None = None):
        if self.handler:
            from brcc_apis import elk_logger
            root = logging.getLogger()
            elk_handler = elk_logger.ElkLoggingHandler(elk_server=self.server,
                                                       api_key=ELK_API_KEY,
                                                       index=index,
                                                       extra=extra,
                                                       fields_filter=fields)
            if elk_handler.connected:
                root.addHandler(elk_handler)
        else:
            elk_handler = None

        return elk_handler


# TASK
class Task(CustomBaseSettings):
    time_horizon: str = '1D'
    scenario_time_start: str = 'P0DT0H30M'
    scenario_time_end: str = 'P0DT23H30M'  # can be used 'EOD'
    scenario_time_negative_offset: str = 'P0D'
    scenario_time_reference: str = 'day'
    time_zone: str = 'CET'
    trigger: str = 'MANUAL'
    debug: bool = False


# PROCESS
class OperationalSecurityAssessment(CustomBaseSettings):
    """Full security assessment of one timestamp with loadflow_engine2"""
    # Input data
    opde_included_igms: str | None = 'PSE,AST'
    external_included_igms: str | None = 'LITGRID,ELERING'
    input_data_from_bms: bool = True  # general contingency and assessed element lists

    # Analysis
    cgmes_import: Dict[str, str] = {"iidm.import.cgmes.source-for-iidm-id": "rdfID"}
    brell_xnodes: List[str] = ['XRE_VE81']
    process_deadline: str = 'P0DT8H'  # counting from relevant scenario timestamp day start

    # Result provision
    results_to_bms: bool = False
    results_to_elk: bool = False
    sar_profile_to_storage: bool = False
    network_models_to_storage: bool = False

    # Elastic
    elk_index_logs: str = 'loadflow_engine2-csa-log'
    elk_index_results: str = 'loadflow_engine2-csa-results'
    elk_fields_filter: List[str] = ['msg', 'levelname', 'name', 'process', 'numeric_value']


class SecurityAnalysis(CustomBaseSettings):
    """Generic security analysis with loadflow_engine2"""
    min_monitored_voltage_level: int = 330
    monitored_voltage_level_areas: str | List[str] = 'LT,LV,EE'
    cgmes_import: Dict[str, str] = {
                                    "iidm.import.cgmes.source-for-iidm-id": "rdfID",
                                    "iidm.import.cgmes.import-node-breaker-as-bus-breaker": "True",
                                    }

    def model_post_init(self, __context: Any) -> None:
        if isinstance(self.monitored_voltage_level_areas, str):
            self.monitored_voltage_level_areas = self.monitored_voltage_level_areas.split(',')


class ConsistencyCheck(CustomBaseSettings):
    opde_included_igms: str | None = 'PSE,AST'
    external_included_igms: str | None = 'LITGRID,ELERING'
    elk_index_logs: str = 'consistency-check'
    upload_to_bms: bool = False


class ImpactAssessment(BaseSettings):
    # Core
    ignore_emerged_islands: bool = True
    # Results thresholds
    threshold_voltage_percent: int | None = 5
    threshold_identification_percent: int | None = 15
    threshold_filtering: int | None = 3


class RemedialActionImpactAssessment(CustomBaseSettings):
    # Input data
    opde_included_igms: str | None = 'PSE,AST'
    external_included_igms: str | None = 'LITGRID,ELERING'
    cgmes_import: Dict[str, str] = {"iidm.import.cgmes.source-for-iidm-id": "rdfID",
                                    "iidm.import.cgmes.import-node-breaker-as-bus-breaker": "True",
                                    }
    # Result provision
    results_to_elk: bool = False

    # Elastic
    elk_index_results: str = 'remedial-action-impact'


class CommonInputDataProfile(CustomBaseSettings):
    version: str | int = '1'
    upload_to_bms: bool = False


class RemedialActionSchedule(CustomBaseSettings):
    run_id: str = '00'
    version: str | int = '1'
    name: str | None = None
    description: str | None = None
    schedule_type: str = 'GridStateIntensitySchedule'
    contingency: str | None = None
    remedial_action: str = Field(default_factory=lambda: f"{uuid.uuid4()}")
    grid_state_alteration: str = Field(default_factory=lambda: f"{uuid.uuid4()}")
    value: str | int | float = '0.0'
    assigned_region: str | None = None

    @field_validator('contingency', 'remedial_action', 'grid_state_alteration')
    @classmethod
    def remove_leading_underscore(cls, value):
        if value is not None:
            if value.startswith('_'):
                return value.lstrip('_')
        return value


# INTEGRATIONS
class Integration(CustomBaseSettings):
    model_config = SettingsConfigDict(env_file=Path(__file__).parent.joinpath(".env"))

    server: str | None = None
    username: str | None = None
    password: str | None = None


class Opdm(Integration):
    model_config = SettingsConfigDict(env_prefix='OPDM_')


class WebDav(Integration):
    model_config = SettingsConfigDict(env_prefix='WEBDAV_')


class Minio(Integration):
    model_config = SettingsConfigDict(env_prefix='MINIO_')

    models_bucket: str = 'opde-confidential-models'
    analyses_bucket: str = 'opde-confidential-analyses'


class BMS(Integration):
    model_config = SettingsConfigDict(env_prefix='BMS_')


class Elk(Integration):
    model_config = SettingsConfigDict(env_prefix='ELK_')


if __name__ == '__main__':
    # Testing
    # BMS()
    config = ElkLogging()
    BMS(server='test', suspend_stdout=True)


