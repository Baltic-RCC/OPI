from datetime import datetime
import uuid
import re
import pandas as pd
from pathlib import Path
import logging
from dataclasses import dataclass
from io import BytesIO
from datetime import timezone
from opi.network import load_network
from loadflow_engine.security_analysis import SecurityAnalysis
from config.opi import opi_lf_parameters
from opi.timeframes import select_yr_models
from opi.outages import run_outage_crosscheck
from opi.outages import get_relevant_outages, get_uap_outages
from opi.network import get_system_balance, get_cross_border_flows
import pypowsybl as pp
import zipfile
from opi.integrations import (get_all_minio_objects, query_data, download_models_from_minio,
                              get_lf_settings_manager, minio_service, OPI_MODEL_BUCKET, OPI_MODEL_FOLDER,
                              ELK_SERVER, ELK_UAP_INDEX, DATA_IMPORT_BUCKET, DATA_EXPORT_BUCKET, OPI_MODEL_EXPORT_FOLDER)
from opi.timeframes import get_opi_week_number, get_result_opi_year
from opi.integrations import send_opi_results_to_elk
from opi.common import log_context
from opi.region import (MERGE_TYPE, ASSESSED_AREAS, EXTERNAL_AREAS, AREA_TSO_NAMES, EXPORT_NAME_REPLACE,
                        normalize_area)
from pydantic import BaseModel, Field, AliasChoices
from typing import Optional, List, Any

logger = logging.getLogger(__name__)


class AssessedElement(BaseModel):
    mRID: Optional[str] = Field(default_factory=lambda: f"{uuid.uuid4()}")
    name: Optional[str] = ""
    type: Optional[str] = Field(default="AssessedElement", validation_alias=AliasChoices("type", "Type"))
    equipment: str = Field(validation_alias=AliasChoices("equipment", "ConductingEquipment"))
    area: Optional[str] = ""
    contingency: Optional[List[str]] = None
    remedial_action: Optional[List[str]] = None


class Contingency(BaseModel):
    mRID: Optional[str] = Field(default_factory=lambda: f"{uuid.uuid4()}")
    name: Optional[str] = ""
    type: Optional[str] = Field(default="Ordinary", validation_alias=AliasChoices("type", "Type"))
    equipment: List[str]
    eq_eic: Optional[str] = None
    #
    # @field_validator('mRID', 'equipment')
    # def validate_mrids(cls, value):
    #     return remove_leading_underscore(value)

    def model_post_init(self, __context: Any) -> None:
        if len(self.equipment) > 1 and self.type != 'OutOfRange':
            self.type = "Exceptional"


def assessed_elements_from_dataframe(data: str | pd.DataFrame) -> List[AssessedElement]:
    """
    Converts Assessed elements Excel or Dataframe file to assessed elements format for security analysis input

    Dataframe should have columns:
    - mRID or registered_resource
    - equipment or grid_element_id
    """

    # Load Excel file if input was path to file
    if isinstance(data, str):
        logger.info(f"Parsing file: {data}")
        assessed_elements = pd.read_excel(data)
    else:
        assessed_elements = data

    logger.info(f"Converting Assessed elements from dataframe")

    # Rename the columns if they are present
    column_name_map = {'registered_resource': 'mRID', 'grid_element_id': 'equipment', 'secured_for_region': 'area', 'eq_type': 'type'}
    relevant_mapping = {k: v for k, v in column_name_map.items() if k in assessed_elements.columns}
    assessed_elements = assessed_elements.rename(columns=relevant_mapping)

    # Filter out assessed elements where equipment is missing
    assessed_elements = assessed_elements.dropna(subset='equipment')

    # Filter out assessed elements where attribute normal_enabled == 'false' if columns is in dataframe
    if 'normal_enabled' in assessed_elements.columns:
        assessed_elements = assessed_elements[assessed_elements.normal_enabled == True]

    # Build AssessedElement instances
    # Blank cells (NaN) are dropped so model defaults apply
    converted_assessed_elements = [AssessedElement(**{k: v for k, v in assessed_element.items() if pd.notna(v)})
                                   for assessed_element in assessed_elements.to_dict('records')]

    return converted_assessed_elements


def contingencies_from_dataframe(data: str | pd.DataFrame) -> List[Contingency]:
    """
    Converts Contingency Excel or Dataframe file to contingencies format for security analysis input

    Dataframe should have columns:
    - mRID or registered_resource
    - equipment or grid_element_id
    - name or co_name
    """

    # Load Excel file if input was path to file
    if isinstance(data, str):
        logger.info(f"Parsing file: {data}")
        contingencies = pd.read_excel(data)
    else:
        contingencies = data

    logger.info(f"Converting Contingencies from dataframe")

    # Rename the columns if they are present
    column_name_map = {'registered_resource': 'mRID', 'grid_element_id': 'equipment', 'co_name': 'name'}
    relevant_mapping = {k: v for k, v in column_name_map.items() if k in contingencies.columns}
    contingencies = contingencies.rename(columns=relevant_mapping)

    # Filter out contingencies where grid_element_id is missing
    contingencies = contingencies.dropna(subset='equipment')

    # Filter out contingencies where attribute normalMustStudy == 'false' if columns is in dataframe
    if 'normal_must_study' in contingencies.columns:
        contingencies = contingencies[contingencies.normal_must_study == True]

    # Combine contingency equipments grid_element_id to lists
    aggregated_dict = contingencies.groupby('mRID').agg({'equipment': list,
                                                         'type': 'first',
                                                         'name': 'first',
                                                         'eq_eic': 'first'}).reset_index().to_dict('records')

    # Build Contingency instances
    converted_contingencies = [Contingency(**{k: v for k, v in contingency.items() if isinstance(v, list) or pd.notna(v)})
                               for contingency in aggregated_dict]

    return converted_contingencies


def get_loadflow_settings(lf_parameters_key: str, elk_server: str) -> tuple[pp.loadflow.Parameters, pp.security.Parameters]:
    """LF parameters from the ELK settings manager, the repository set of the same name as fallback, and SA parameters."""
    try:
        lf_settings_manager = get_lf_settings_manager(elk_server, lf_parameters_key)
        lf_parameters = lf_settings_manager.build_pypowsybl_parameters()
        logger.info(f"LF settings loaded from Elasticsearch settings manager: {lf_parameters_key}")
    except Exception as error:
        logger.error(f"LF settings retrieving from Elasticsearch failed: {error}")
        logger.info(f"Using LF settings defined in repository: {lf_parameters_key}")
        lf_parameters = getattr(opi_lf_parameters, lf_parameters_key)

    sa_parameters = pp.security.Parameters(load_flow_parameters=lf_parameters,
                                           provider_parameters=opi_lf_parameters.SA_PROVIDER)
    return lf_parameters, sa_parameters


def get_input_data(time_horizon: str, minio_bucket: str, data_import_folder: str,
                   use_local_inputs: bool = True) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Assessed element and contingency lists of the horizon, from the repository inputs or MinIO."""
    logger.info("Retrieving input data")
    if use_local_inputs:
        inputs_path = Path(__file__).resolve().parent.parent.joinpath(f'inputs/{time_horizon}')
        assessed_elements_path = inputs_path.joinpath("assessed_element_list.xlsx")
        contingencies_path = inputs_path.joinpath("contingency_list.xlsx")
        assessed_element_df = pd.read_excel(assessed_elements_path)
        contingency_df = pd.read_excel(contingencies_path)
    else:
        assessed_element_df = pd.read_excel(
            BytesIO(minio_service.download_object(minio_bucket, f'{data_import_folder}/assessed_element_list.xlsx')))
        contingency_df = pd.read_excel(
            BytesIO(minio_service.download_object(minio_bucket, f'{data_import_folder}/contingency_list.xlsx')))

    return assessed_element_df, contingency_df


@dataclass(frozen=True)
class OpiTask:
    """One timestamp of an OPI run."""
    time_horizon: str
    scenario_timestamp: str
    opi_year: str
    task_version: int
    model_version: str
    validate_models: bool
    send_to_elk: bool
    import_minio_models: bool
    reconnect_outages: bool
    export_models: bool
    inertia_enabled: bool
    outage_corrections: dict

    @property
    def timestamp(self) -> datetime:
        return datetime.fromisoformat(self.scenario_timestamp)


@dataclass(frozen=True)
class RunInputs:
    """Inputs shared by all timestamps of a run, loaded once."""
    lf_parameters: pp.loadflow.Parameters
    sa_parameters: pp.security.Parameters
    assessed_elements_df: pd.DataFrame
    contingencies_df: pd.DataFrame
    uap_outages: pd.DataFrame

    @classmethod
    def load(cls, time_horizon: str, lf_parameters_key: str, use_local_inputs: bool, data_import_folder: str,
             run_time_offset: str, uap_query_offset: str) -> "RunInputs":
        lf_parameters, sa_parameters = get_loadflow_settings(lf_parameters_key, elk_server=ELK_SERVER)
        assessed_elements_df, contingencies_df = get_input_data(time_horizon, DATA_IMPORT_BUCKET, data_import_folder,
                                                                use_local_inputs=use_local_inputs)
        uap_outages = get_uap_outages(time_horizon=time_horizon, run_time_offset=run_time_offset,
                                      uap_query_offset=uap_query_offset, index=ELK_UAP_INDEX)
        return cls(lf_parameters, sa_parameters, assessed_elements_df, contingencies_df, uap_outages)


class OutagePlanIncompatibilityAssessment:
    """OPI of one timestamp: model, outages applied, N-1, results and model export."""

    def __init__(self, task: OpiTask, inputs: RunInputs):
        self.task = task
        self.inputs = inputs

        self.assessed_elements: List[AssessedElement] = []
        self.contingencies: List[Contingency] = []
        self.model_meta = None
        self.model_data = None
        self.pp_network = None
        self.security_service = None
        self.areas: List[str] = []
        self.model_is_valid = None
        self.base_case_violations = None
        self.outage_configuration_id = None
        self.outage_lf_status = None
        self.crosschecked_element_df = None
        self.confirmed_outages = None
        self.model_outages = None
        self.outages_to_reconnect = None
        self.opi_results = None

    def run(self) -> None:
        """Runs the timestamp; raises when it failed, after the export and results it could still produce."""
        task = self.task
        # Set logging metadata for OPI task run
        log_context.set({
            "@time_horizon": task.time_horizon,
            "@scenario_timestamp": task.scenario_timestamp,
            "@version": task.task_version,
            "opi_year": get_result_opi_year(task.timestamp, task.time_horizon, task.opi_year),
            "opi_month": str(task.timestamp.month),
            "opi_week": get_opi_week_number(task.timestamp),
        })

        logger.info("Starting new OPI task")
        # Built per timestamp: loadflow_engine gets its own objects
        self.assessed_elements = assessed_elements_from_dataframe(data=self.inputs.assessed_elements_df)
        self.contingencies = contingencies_from_dataframe(data=self.inputs.contingencies_df)
        self.get_opi_model()
        self.load_model_data()
        if task.validate_models:
            self.validate_base_case()
        if self.model_is_valid is False:
            raise ValueError(f"Model failed validation, timestamp {task.scenario_timestamp}")

        # Perform OPI calculation
        self.calculate_opi()
        sa_diverged = self.opi_results is None
        if task.export_models:
            self.export_model_to_minio(bucket=DATA_EXPORT_BUCKET, folder=OPI_MODEL_EXPORT_FOLDER)
        # After calculate_opi: adds to opi_results, also when security analysis diverged
        if task.inertia_enabled:
            try:
                self.calculate_inertia()
            except Exception as error:
                logger.error(f"Inertia calculation failed: {error}")
        if self.opi_results:
            self.update_result_metadata()

            if task.send_to_elk is True:
                if not self.send_results_to_elk():
                    raise RuntimeError(f"Sending results to ELK failed, timestamp {task.scenario_timestamp}")

            if not sa_diverged:
                logger.info(f"{task.scenario_timestamp} OPI calculation finished")

        if self.model_is_valid is False:
            raise ValueError(f"Model with outages applied does not solve, timestamp {task.scenario_timestamp}")
        if sa_diverged:
            raise RuntimeError(f"Security analysis diverged, no OPI results for timestamp {task.scenario_timestamp}")

    def calculate_inertia(self) -> None:
        """Kinetic energy (Ek, MWs) of rated units per area: total fleet, scheduled in model, missing due to UAP outages."""
        ratings_path = Path(__file__).resolve().parent.parent / "inputs" / "general" / "inertia_ratings.xlsx"
        ratings = pd.concat(pd.read_excel(ratings_path, sheet_name=None).values(), ignore_index=True)

        units = pd.DataFrame({
            "area": ratings["Area"].fillna("").astype(str).str.strip(),
            "plant": ratings["Plant"].fillna("").astype(str).str.strip(),
            "unit": ratings["Unit ID"].fillna("").astype(str).str.strip(),
            "mrid": ratings["ID"].fillna("").astype(str).str.strip(),
            "ek_mws": pd.to_numeric(ratings["Ek MWs"], errors="coerce"),
        })
        unparsed = units.loc[ratings["Ek MWs"].notna() & units["ek_mws"].isna(), "plant"].tolist()
        if unparsed:
            logger.warning(f"Ek not numeric for {unparsed}, counted as 0")

        # Ids are compared without the leading underscore on all sides
        connected = self.pp_network.get_generators()["connected"]
        connected.index = connected.index.str.lstrip("_")
        unit_ids = units["mrid"].str.lstrip("_")
        units["model_status"] = unit_ids.map(connected).map({True: "connected", False: "offline"}).fillna("not_in_model")

        active = pd.DataFrame(columns=["mrid", "name", "Area"])
        if not self.inputs.uap_outages.empty:
            scenario_time = self.task.timestamp.replace(tzinfo=timezone.utc)
            relevant = get_relevant_outages(self.inputs.uap_outages.copy(), scenario_time)
            if not relevant.empty:
                active = relevant[relevant["asset_type"].isin(["GEN", "SCON"])]
        active_ids = active["mrid"].fillna("").astype(str).str.lstrip("_")

        units["in_outage"] = unit_ids.isin(active_ids[active_ids != ""])
        units["conflict"] = units["in_outage"] & (units["model_status"] == "connected")
        if units["conflict"].any():
            logger.warning(f"UAP says OUT but model says ON: {units.loc[units['conflict'], 'plant'].tolist()}")

        # Active outages without a ratings row stay visible in results with unknown Ek
        unmatched = active[~active_ids.isin(unit_ids[unit_ids != ""])].drop_duplicates(subset=["mrid", "name"])
        if not unmatched.empty:
            logger.warning(f"{len(unmatched)} active GEN/SCON outage(s) not in inertia ratings: {unmatched['name'].tolist()}")
            units = pd.concat([units, pd.DataFrame({
                "area": unmatched["Area"].fillna("").astype(str),
                "plant": unmatched["name"].fillna("").astype(str),
                "unit": "",
                "mrid": unmatched["mrid"].fillna("").astype(str),
                "model_status": "not_in_ratings",
                "in_outage": True,
                "conflict": False,
            })], ignore_index=True)

        # Available = full fleet minus planned outages
        ek = units["ek_mws"].fillna(0)
        areas = pd.DataFrame({
            "area": units["area"],
            "total_mws": ek,
            "scheduled_mws": ek.where(units["model_status"] == "connected", 0),
            "missing_mws": ek.where(units["in_outage"], 0),
            "n_in_outage": units["in_outage"],
            "n_conflicts": units["conflict"],
        }).groupby("area").sum()
        areas["available_mws"] = areas["total_mws"] - areas["missing_mws"]
        areas["delta_mws"] = areas["available_mws"] - areas["scheduled_mws"]
        status_counts = pd.crosstab(units["area"], units["model_status"]).reindex(
            columns=["connected", "offline", "not_in_model", "not_in_ratings"], fill_value=0)
        areas = areas.join(status_counts.add_prefix("n_")).reset_index()

        totals = areas[["total_mws", "missing_mws", "available_mws", "scheduled_mws"]].sum()
        logger.info(f"Inertia MWs - total {totals['total_mws']:.1f} | missing {totals['missing_mws']:.1f} "
                    f"| available {totals['available_mws']:.1f} | scheduled {totals['scheduled_mws']:.1f}")

        self.opi_results = {**(self.opi_results or {}), "inertia-areas": areas, "inertia-units": units}

    def get_opi_model(self):
        logger.info("Retrieving model")

        # Y-1 fallback: model of the timestamp's season from the MinIO folder
        if self.task.import_minio_models and self.task.time_horizon == 'YR':
            bucket, folder = OPI_MODEL_BUCKET, f'{OPI_MODEL_FOLDER}/{self.task.opi_year}/'
            matches = select_yr_models(get_all_minio_objects(bucket=bucket, folder=folder), self.task.scenario_timestamp)
            if not matches:
                raise ValueError(f"Y-1 model for {self.task.scenario_timestamp} not found in {bucket}/{folder}")
            model = max(matches, key=lambda item: item['last_modified'])
            if len(matches) > 1:
                logger.warning(f"{len(matches)} Y-1 models for {model['@scenario_timestamp']}, "
                               f"using latest uploaded: {model['name']}")
            model_data = download_models_from_minio(minio_bucket=bucket, minio_folder=[model['object_name']])
            logger.info(f"Model retrieved: {model['name']}")

        else:
            query = {
                "merge_type": MERGE_TYPE,
                "@time_horizon": self.task.time_horizon,
                "@scenario_timestamp": self.task.scenario_timestamp,
            }

            # Y-1: BA YR merges of the timestamp's season, the one nearest to the timestamp
            if self.task.time_horizon == 'YR':
                del query['@scenario_timestamp']

            model_meta = query_data(query, query_filter=None, scroll=None)
            if self.task.time_horizon == 'YR':
                model_meta = select_yr_models(model_meta, self.task.scenario_timestamp)

            if model_meta:
                version = self.task.model_version.lower()
                if version == 'latest':
                    version = max(int(x["@version"]) for x in model_meta)
                else:
                    version = int(version)
            else:
                raise ValueError(f"Model was not found for timestamp: {self.task.scenario_timestamp}")

            # get max version
            selected_models = []
            for model in model_meta:
                if version == int(model['@version']):
                    selected_models.append(model)
            # If versions match, latest timestamp gets chosen
            if selected_models:
                if len(selected_models) > 1:
                    model = max(selected_models, key=lambda d: d['@timestamp'], default=None)
                else:
                    model = selected_models[0]
                try:
                    model_data = download_models_from_minio(minio_folder=[model['content_reference']])
                    if model_data:
                        logger.info(f"Model retrieved: {model['name']}")
                except Exception as error:
                    raise ValueError(f"Model download failed for {model['name']}: {error}") from error
            else:
                raise ValueError(f"Model was not found for timestamp: {self.task.scenario_timestamp}")

        self.model_meta = model
        self.model_data = model_data[0]


    def load_model_data(self):

        self.pp_network = load_network(self.model_data, parameters=opi_lf_parameters.CGMES_IMPORT_PARAMETERS)
        self.security_service = SecurityAnalysis(network=self.pp_network, contingencies=self.contingencies,
                                                 assessed_elements=self.assessed_elements,
                                                 lf_parameters=self.inputs.lf_parameters, sa_parameters=self.inputs.sa_parameters,
                                                 debug=False)
        self.areas = [normalize_area(x) for x in self.security_service.network.get_areas()['name'].to_list()]
        # Kept on the service as before, loadflow_engine may read it
        self.security_service.present_igms = self.areas


    def validate_base_case(self):

        logger.info("Validating model")
        start_time = datetime.now(timezone.utc)

        # Check if model is a single island
        islands = self.security_service.network.get_buses().connected_component.value_counts().to_list()
        if len(islands) > 1:
            main_island = islands[0]
            sub_islands = sum(islands[1:])
            island_diff = sub_islands / main_island
        else:
            island_diff = 0
        if island_diff > 0.15:
            self.model_is_valid = False
            logger.info(f"Network components: {islands}")
            logger.info(f"Model {str(self.model_meta['name'])} has separated islands")
            return

        missing_external = [area for area in EXTERNAL_AREAS if area not in self.areas]
        if missing_external:
            logger.warning(f"Missing {missing_external} model in {self.model_meta['name']}")

        if not set(ASSESSED_AREAS).issubset(self.areas):
            self.model_is_valid = False
            logger.info(f"Validation failed, IGMs -> {self.areas} for {str(self.model_meta['name'])} ")
            return

        # Model as is must solve: base case without contingencies, one load flow with the N-1 settings
        base_case = pp.security.create_analysis().run_ac(self.security_service.network, parameters=self.inputs.sa_parameters)
        base_case_status = base_case.pre_contingency_result.status.name
        if base_case_status != 'CONVERGED':
            self.model_is_valid = False
            logger.info(f"Base case load flow {base_case_status} in {self.model_meta['name']}")
            return

        # Base case violations do not invalidate the model; they are flagged in the OPI violations
        violations = base_case.limit_violations.reset_index()
        self.base_case_violations = violations[violations['limit_type'] == 'CURRENT']
        if not self.base_case_violations.empty:
            logger.warning(f"{len(self.base_case_violations)} base case CURRENT violation(s) before UAP outages in "
                           f"{self.model_meta['name']}: {self.base_case_violations['subject_id'].unique().tolist()}")
        self.model_is_valid = True
        end_time = datetime.now(timezone.utc)
        logger.info(f"Validation took {(end_time - start_time).total_seconds()} s")


    def calculate_opi(self):

        outage_configuration_id = str(uuid.uuid4())
        self.outage_configuration_id = outage_configuration_id

        self.crosschecked_element_df = run_outage_crosscheck(self.security_service.network,
                                                             self.inputs.assessed_elements_df,
                                                             self.contingencies,
                                                             set_timestamp=self.task.scenario_timestamp,
                                                             outages=self.inputs.uap_outages)

        self.confirmed_outages = self.crosschecked_element_df[self.crosschecked_element_df['outage_in_uap']]
        self.model_outages = self.crosschecked_element_df[self.crosschecked_element_df['outage_in_cgm']]
        self.outages_to_reconnect = self.crosschecked_element_df[~self.crosschecked_element_df['outage_in_uap']]
        if not self.task.reconnect_outages:
            logger.info("RECONNECT_OUTAGES is off, reconnecting only OUTAGE_ENABLE elements")
            self.outages_to_reconnect = self.outages_to_reconnect.iloc[0:0]

        # Update outage lists according to manual inputs
        mask1 = self.crosschecked_element_df[list(self.task.outage_corrections['to_enable'])].isin(self.task.outage_corrections['to_enable']).any(axis=1)
        self.outages_to_reconnect = pd.concat([self.outages_to_reconnect, self.crosschecked_element_df[mask1]])
        mask2 = self.crosschecked_element_df[list(self.task.outage_corrections['to_disable'])].isin(self.task.outage_corrections['to_disable']).any(axis=1)
        self.confirmed_outages = pd.concat([self.confirmed_outages, self.crosschecked_element_df[mask2]]).drop_duplicates('equipment')
        # Manual corrections override UAP and model state
        enabled = self.crosschecked_element_df.loc[mask1, 'equipment']
        disabled = self.crosschecked_element_df.loc[mask2, 'equipment']
        self.confirmed_outages = self.confirmed_outages[~self.confirmed_outages['equipment'].isin(enabled)]
        self.outages_to_reconnect = self.outages_to_reconnect[~self.outages_to_reconnect['equipment'].isin(disabled)]

        # Clone initial network state and apply outage to run N-1
        self.security_service.network.clone_variant(src='InitialState', target=outage_configuration_id)
        self.security_service.network.set_working_variant(outage_configuration_id)
        logger.info(f"Network model working variant initialized and activated: {outage_configuration_id}")

        changed_outage_list = []
        # Reconnecting relevant outages
        if not self.outages_to_reconnect.empty:
            logger.info(f"Reconnecting outages to network instance: '{outage_configuration_id}'")
            for index, outage in self.outages_to_reconnect.drop_duplicates('equipment').iterrows():
                if outage.type != 'transformer' and outage.area not in EXTERNAL_AREAS and pd.notna(outage.equipment):
                    try:
                        # Closes the open switches on the shortest path to a busbar section (needs node-breaker import)
                        if self.security_service.network.connect(outage.equipment, operate_disconnectors=True,
                                                                 operate_fictitious=True):
                            logger.info(f" {outage['name']} {outage.equipment} reconnected")
                            changed_outage_list.append(outage)
                        elif outage.outage_in_cgm:
                            logger.warning(f"Failed to reconnect {outage['name']} {outage.equipment}: no switch path to a busbar section")

                    except Exception as e:
                        logger.error(f"Failed to reconnect {outage['name']} {outage.equipment}: {e}")
                        continue

        # Disconnect confirmed outages
        logger.info(f"Applying outages to network instance: {outage_configuration_id}")
        for index, outage in self.confirmed_outages.iterrows():
            try:
                if self.security_service.network.disconnect(outage.equipment):
                    logger.info(f"{outage['name']} {outage.equipment} disconnected")
                    changed_outage_list.append(outage)

                else:
                    if self.model_outages.equipment.str.contains(outage.equipment).any():
                        logger.debug(f"{outage['name']} {outage.equipment} is already in outage")
                    else:
                        logger.error(f"Failed to disconnect: {outage['name']} {outage.equipment}")
            except Exception as e:
                logger.error(f"Failed to disconnect {outage['name']} {outage.equipment}: {e}")
                continue

        # Model with outages applied must solve; border flows, system balance and the export use this solved state
        try:
            lf_result = pp.loadflow.run_ac(self.security_service.network, parameters=self.inputs.lf_parameters)
            self.outage_lf_status = lf_result[0].status.name
        except Exception as error:
            logger.error(f"Load flow with outages applied failed: {error}")
            self.outage_lf_status = 'FAILED'
        if self.outage_lf_status != 'CONVERGED':
            if self.task.validate_models:
                self.model_is_valid = False
                logger.error(f"Model with outages applied does not solve ({self.outage_lf_status}), N-1 not run")
                self.opi_results = None
                self.security_service.network.set_working_variant('InitialState')
                return
            logger.warning(f"Model with outages applied does not solve ({self.outage_lf_status}), "
                           f"border flows and system balance may show pre-outage values")

        # Run security assessment scenario
        sa_status = self.security_service.run_security_analysis()

        # Process OPI results
        if sa_status == "DIVERGED":
            self.opi_results = None
        else:
            opi_result_bus = self.security_service.processed_bus_results
            opi_result_branch = self.security_service.processed_branch_results
            opi_result_violations = self.security_service.processed_limit_violations
            opi_result_violations = opi_result_violations[opi_result_violations['limit_type'] == 'CURRENT']
            if self.base_case_violations is not None and 'subject_id' in opi_result_violations.columns:
                # Elements already violated in the base case before the outages were applied
                opi_result_violations = opi_result_violations.assign(
                    pre_outage_violation=opi_result_violations['subject_id'].isin(self.base_case_violations['subject_id']))
            opi_result_diverged_contingencies = pd.DataFrame(
                [{"mrid": key, "status": value.status} for key, value in self.security_service.results.post_contingency_results.items() if
                 value.status.name not in ('CONVERGED', 'NO_IMPACT')])
            if not opi_result_diverged_contingencies.empty:
                opi_result_diverged_contingencies["status"] = opi_result_diverged_contingencies["status"].astype(str).str.split(".").str[-1]

            # Security analysis does not update the network state: read from the solved outage-applied model
            self.security_service.network.set_working_variant(outage_configuration_id)
            border_flows = get_cross_border_flows(self.security_service.network, self.model_data).sort_values('Cross_border')
            system_balance = get_system_balance(self.security_service.network, self.areas, border_flows)
            system_balance = system_balance[~system_balance['area'].isin(EXTERNAL_AREAS)]

            self.opi_results = {"confirmed-outages" : self.confirmed_outages[['area', 'name', 'description']].copy(),
                                "border-flows": border_flows[['Cross_border', 'name', 'P_MW']].copy(),
                                "bus-results": opi_result_bus,
                                "system-balance": system_balance,
                                "branch-results": opi_result_branch,
                                "violations": opi_result_violations,
                                "diverged-contingencies": opi_result_diverged_contingencies
                                }
        self.security_service.network.set_working_variant('InitialState')
        logger.info(f"OPI calculation completed")

    def export_model_to_minio(self, bucket: str, folder: str) -> None:
        """Save OPI outage variant to MinIO: original EQ, TP and boundary files with updated SSH per TSO and SV."""
        network = self.security_service.network
        try:
            network.set_working_variant(self.outage_configuration_id)
            if self.outage_lf_status != 'CONVERGED':
                logger.warning(f"Exported OPI model does not solve ({self.outage_lf_status}), its SV is not valid")
            exported = zipfile.ZipFile(network.save_to_binary_buffer(format="CGMES", parameters={
                "iidm.export.cgmes.cgm_export": "True",
                "iidm.export.cgmes.topology-kind": "NODE_BREAKER",
                "iidm.export.cgmes.export-sv-injections-for-slacks": "False",
            }))
            original = zipfile.ZipFile(BytesIO(self.model_data))
            model_buffer = BytesIO()
            with zipfile.ZipFile(model_buffer, "w", zipfile.ZIP_DEFLATED) as model_zip:
                for name in original.namelist():
                    if "_SSH_" not in name and "_SV_" not in name:
                        model_zip.writestr(name, original.read(name))
                for name in exported.namelist():
                    if "_SSH" in name:
                        original_part = f"-{AREA_TSO_NAMES[name.split('_')[-2]]}_SSH_"
                    else:
                        original_part = "_SV_"
                    new_name = next(n for n in original.namelist() if original_part in n)
                    for old, new in EXPORT_NAME_REPLACE.items():
                        new_name = new_name.replace(old, new)
                    profile_buffer = BytesIO()
                    with zipfile.ZipFile(profile_buffer, "w", zipfile.ZIP_DEFLATED) as profile_zip:
                        profile_zip.writestr(new_name.replace(".zip", ".xml"), exported.read(name))
                    model_zip.writestr(new_name, profile_buffer.getvalue())
            model_buffer.seek(0)
            # <folder>/<TH>/ as EMFOS models: OPI_ + source model name, its trailing uuid replaced by a new one
            model_name = re.sub(r"_[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$", "",
                                self.model_meta['name'].removesuffix('.zip'))
            model_buffer.name = f"{folder}/{self.task.time_horizon}/OPI_{model_name}_{uuid.uuid4()}.zip"
            minio_service.upload_object(file_path_or_file_object=model_buffer, bucket_name=bucket)
            logger.info(f"OPI model saved to MinIO: {bucket}/{model_buffer.name}")
        except Exception as error:
            logger.exception(f"OPI model export to MinIO failed: {error!r}")
        finally:
            network.set_working_variant('InitialState')


    def update_result_metadata(self):

        logger.info("Parsing finalized results")

        run_id = str(uuid.uuid4())
        timestamp = self.task.timestamp
        result_list = []
        for name, result_df in self.opi_results.items():
            if not result_df.empty:
                result_df['@scenario_timestamp'] = self.task.scenario_timestamp
                result_df['@time_horizon'] = self.task.time_horizon
                result_df['@run_id'] = run_id
                result_df['@version'] = self.task.task_version
                result_df['week_day'] = timestamp.weekday()
                opi_week = get_opi_week_number(timestamp)
                result_df['opi_week'] = opi_week
                result_df['opi_month'] = str(timestamp.month)
                result_df['opi_year'] = get_result_opi_year(timestamp, self.task.time_horizon, self.task.opi_year)


    def send_results_to_elk(self):

        response = send_opi_results_to_elk(self.opi_results, self.task.time_horizon, self.task.scenario_timestamp)

        return all(item is not False for item in response)
