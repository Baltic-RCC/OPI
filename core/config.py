import inspect
import re
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, field_validator
import os
import sys
import logging
import uuid
from pathlib import Path
from typing import List, Any, Dict, Literal
import logging

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

    log_format: str = (
        "<green>{time:YYYY-MM-DD HH:mm:ss:SSS}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{name: <40}</cyan> | "
        "<cyan>{function: <40}</cyan> | "
        "<cyan>{line: <4}</cyan> | "
        "<level>{message}</level>"
    )
    level: int = 20
    alerts: bool = False


class ElkLogging(CustomBaseSettings):
    model_config = SettingsConfigDict(env_prefix='ELK_')

    handler: bool = False
    server: str | None = None
    level: int | None = None
    enqueue: bool = False
    catch: bool = True
    logs_rollover: bool = False

    def model_post_init(self, __context: Any) -> None:
        if not self.server and self.handler:
            self.server = Elk(suspend_stdout=True).server
        if not self.level:
            self.level = log_conf.level

    # def initialize(self, index: str, extra: dict | None = None, fields: list | None = None):
    #     if self.handler:
    #         from brcc_apis import elk_logger
    #         root = logging.getLogger()
    #         elk_handler = elk_logger.ElkLoggingHandler(elk_server=self.server,
    #                                                    index=index,
    #                                                    extra=extra,
    #                                                    fields_filter=fields,
    #                                                    index_rollover=self.logs_rollover)
    #         if elk_handler.connected:
    #             root.addHandler(elk_handler)
    #     else:
    #         elk_handler = None
    #
    #     return elk_handler


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

    # def model_post_init(self, __context: Any) -> None:
    #     if self.scenario_time_end == 'EOD':
    #         self.scenario_time_end = re.sub(r'(\d+)H', '23H', self.scenario_time_start)


# PROCESS
class OperationalSecurityAssessment(CustomBaseSettings):
    """Full security assessment of one timestamp with pypowsybl"""
    # Input data
    merge_type: str = 'BA'
    merge_model_version: int | None = None
    opde_included_igms: str | None = 'PSE,AST,LITGRID,ELERING'
    external_included_igms: str | None = None
    input_data_from_bms: bool = True  # general contingency and assessed element lists

    # Analysis
    cgmes_import: Dict[str, str] = {"iidm.import.cgmes.source-for-iidm-id": "rdfID"}
    enable_mfrr_case_analysis: bool = False

    # Result provision
    results_to_bms: bool = False
    results_to_elk: bool = False
    sar_profile_to_storage: bool = False
    network_models_to_storage: bool = False
    loading_limit: int = 80
    high_voltage_limit: float = 1.09
    low_voltage_limit: float = 0.91
    rmq_exchange: str = "service-csa.output"

    # Elastic
    elk_index_logs: str = 'csa-logs'
    elk_index_results: str = 'csa-results'
    elk_index_branch_results: str = 'csa-branch-results'
    elk_index_bus_results: str = 'csa-bus-results'
    elk_index_mfrr: str = 'csa-mfrr-results'
    # elk_fields_filter: List[str] = ['msg', 'levelname', 'name', 'process', 'numeric_value']


class ConsistencyCheck(CustomBaseSettings):
    opde_included_igms: str | None = 'PSE,AST,LITGRID,ELERING'
    external_included_igms: str | None = None
    elk_index_logs: str = 'csa-logs'
    upload_to_bms: bool = False


class RemedialActionImpactAssessment(CustomBaseSettings):
    # Input data
    opde_included_igms: str | None = 'PSE,AST,LITGRID,ELERING'
    external_included_igms: str | None = None
    cgmes_import: Dict[str, str] = {"iidm.import.cgmes.source-for-iidm-id": "rdfID",
                                    "iidm.import.cgmes.import-node-breaker-as-bus-breaker": "True"}
    # Result provision
    results_to_elk: bool = False

    # Elastic
    elk_index_results: str = 'csa-remedial-action-impact'


class CROSAInputData(CustomBaseSettings):
    version: str | int = '1'
    upload_to_bms: bool = False
    profiles: str = "AE,CO,RA"
    operators: str = "LITGRID,AST,ELERING,PSE"
    publisher: str = "38X-BALTIC-RSC-H"


class RemedialActionSchedule(CustomBaseSettings):
    publisher: str = "38X-BALTIC-RSC-H"
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
    model_config = SettingsConfigDict(env_file=Path(__file__).parent.joinpath(".env"), extra='ignore')

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


class RMQ(Integration):
    model_config = SettingsConfigDict(env_prefix='RMQ_')
    port: int = 5670
    vhost: str = "/"


# Initialize logging configuration
class InterceptHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        # try:
        #     level = logger.level(record.levelname).name
        # except ValueError:
        #     level = record.levelno

        # Find caller from where originated the logged message
        frame, depth = inspect.currentframe(), 0
        while frame:
            filename = frame.f_code.co_filename
            is_logging = filename == logging.__file__
            is_frozen = "importlib" in filename and "_bootstrap" in filename
            if depth > 0 and not (is_logging or is_frozen):
                break
            frame = frame.f_back
            depth += 1

        # logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


log_conf = Logging()
logging.basicConfig(handlers=[InterceptHandler()], level=log_conf.level, force=True)
# logger.remove()
# logger.add(sys.stderr, level=log_conf.level, format=log_conf.log_format)


if __name__ == '__main__':
    # Testing
    # BMS()
    config = ElkLogging()
    BMS(server='test', suspend_stdout=True)


