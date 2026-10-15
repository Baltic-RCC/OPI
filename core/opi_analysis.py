from datetime import datetime
import uuid
import pandas as pd
from pathlib import Path
from opi.shared.opi_functions import assessed_elements_from_dataframe, contingencies_from_dataframe
from brcc_apis import  minio_api
import opi.core.config as conf
import logging
import isodate
import inspect
from opi.shared.outage_class import Outage
from io import BytesIO
from datetime import timezone
from opi.shared.loadflow import load_network
from loadflow_engine.security_analysis import SecurityAnalysis
from opi.configs.opi import opi_lf_parameters
from opi.shared.opi_functions import get_yr_model_timestamp
import opi.shared.sa_config as shared_config
from opi.shared.opi_functions import run_outage_crosscheck
from opi.shared.outage_crosscheck import get_relevant_outages
from opi.shared.process_results import get_system_balance, get_ba_cross_border_flows
import pypowsybl as pp
import zipfile
from opi.shared.integrations import (get_all_minio_objects, query_data, download_models_from_minio, get_docs_by_query,
                                     get_lf_settings_manager, minio_service)

# Loading configuration
sa_config = shared_config.SecurityAnalysis(suspend_stdout=True)
config = conf.get_config(logging=conf.ElkLogging(),
                         elastic=conf.Elk(),
                         rmq=conf.RMQ(),
                         minio=conf.Minio(suspend_stdout=True),
                         process=conf.OperationalSecurityAssessment(),
                         )

logger = logging.getLogger(__name__)


class OutagePlanIncompatibilityAssessment:

    def __init__(self,  task: dict = {}, debug: bool = False):

        # Set task attributes
        for key, value in task.items():
            setattr(self, key, value)

        self.debug = debug
        # Private properties
        self._source_module = 'opi'
        self._content_reference = ""
        self._expected_tsos = ['AST', 'LITGRID', 'ELERING']
        self._expected_areas = ['LV', 'LT', 'EE']

        self.uap_outages = None
        self.model_is_valid = None

    def calculate_inertia(self) -> None:
        """Kinetic energy (Ek, MWs) of rated units per area: total fleet, scheduled in model, missing due to UAP outages."""
        ratings_path = Path(__file__).resolve().parent.parent / "inputs" / "general" / "Inertia_ratings.xlsx"
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
        if self.uap_outages:
            scenario_time = datetime.strptime(self.scenario_timestamp, '%Y-%m-%dT%H:%M:%S').replace(tzinfo=timezone.utc)
            relevant = get_relevant_outages(pd.DataFrame([vars(o) for o in self.uap_outages]), scenario_time)
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

    def get_loadflow_settings(self, key: str | None = None, elk_server=None):
        try:
            lf_settings_manager = get_lf_settings_manager(elk_server, self.lf_parameters_key)
            self.lf_parameters = lf_settings_manager.build_pypowsybl_parameters()
            self.sa_parameters = getattr(opi_lf_parameters, "SA_PARAMETERS")
            logger.info(f"LF settings loaded from Elasticsearch settings manager: {self.lf_parameters_key}")
        except Exception as error:
            logger.error(f"LF settings retrieving from Elasticsearch failed: {error}")
            logger.info("Using LF settings defined in repository")
            self.lf_parameters = getattr(opi_lf_parameters, self.lf_parameters_key)
            self.sa_parameters = getattr(opi_lf_parameters, "SA_PARAMETERS")


    def get_input_data(self, minio_bucket, use_local_inputs=True):

        logger.info("Retrieving input data")
        if use_local_inputs:
            inputs_path = Path(__file__).parent.parent.parent.joinpath(f'opi/inputs/{self.time_horizon}')
            assessed_elements_path = inputs_path.joinpath("assessed_element_list.xlsx")
            contingencies_path = inputs_path.joinpath("contingency_list.xlsx")
            assessed_element_df = pd.read_excel(assessed_elements_path)
            contingency_df = pd.read_excel(contingencies_path)
        else:
            minio_service = minio_api.ObjectStorage(server=OBJECTSTORAGE_URL, username=OBJECTSTORAGE_USERNAME,
                                                    password=OBJECTSTORAGE_PASSWORD)
            assessed_element_df = pd.read_excel(
                BytesIO(minio_service.download_object(minio_bucket, f'{self.data_import_folder}/assessed_element_list.xlsx')))
            contingency_df = pd.read_excel(
                BytesIO(minio_service.download_object(minio_bucket, f'{self.data_import_folder}/contingency_list.xlsx')))

        assessed_elements = assessed_elements_from_dataframe(data=assessed_element_df)
        contingencies = contingencies_from_dataframe(data=contingency_df)

        self.assessed_elements = assessed_elements
        self.contingencies = contingencies
        self.assessed_elements_df = assessed_element_df


    def get_elk_uap(self, index):
        logger.info(f"Retrieving outages from ELK index: '{index}'")

        # now represents the time of the run, in P0W case it should be current time
        now = datetime.now() + isodate.parse_duration(self.run_time_offset)
        filter_time = (now + isodate.parse_duration(self.uap_query_offset)).strftime("%Y-%m-%dT%H:%M") + "Z"
        now = now.strftime("%Y-%m-%dT%H:%M") + "Z"

        if self.time_horizon == 'WK':
            merge_type_list = ['week']
        elif self.time_horizon == 'MO':
            merge_type_list = ['week', 'month']
        elif self.time_horizon == 'YR':
            merge_type_list = ['year']

        query = {
            "bool": {
                "must": [
                    {"exists": {"field": "name"}},
                    {"terms": {"Merge": merge_type_list}},
                ],
                "filter": [{"range": {"reportParsedDate": {"lte": now, "gte": filter_time}}}],
            }
        }
        response = get_docs_by_query(index=index, query=query, size=10000, return_df=True)

        if not response.empty:
            # Get only latest report data
            response['reportParsedDate'] = pd.to_datetime(response['reportParsedDate'])
            response = response[response['reportParsedDate'] == response['reportParsedDate'].max()]
            response = response[response['outage_type'].isin(['OUT', 'SSS'])]
            response = response[response['asset_type'] != 'PROD']

            params = inspect.signature(Outage).parameters
            outage_list = [
                Outage(**{k: v for k,v in row.items() if k in params}) for row in response.to_dict(orient="records")
            ]
        else:
            outage_list = None

        self.uap_outages = outage_list


    def get_opi_model(self):
        logger.info("Retrieving model")

        # Get all models in the Y-1 folder then filter out the closest one to scenario timestamp
        if self.import_minio_models and self.time_horizon == 'YR':
            minio_models = get_all_minio_objects(
                bucket=f'opde-confidential-analyses',
                folder=f'OPI/inputs/Y-1/YR_models/{self.opi_year}/')
            model_timestamp = datetime.strptime(get_yr_model_timestamp(self.scenario_timestamp), "%Y-%m-%d").date()
            model = next((item for item in minio_models if item["@scenario_timestamp"].date() == model_timestamp), None)
            model_data = [model.pop('DATA')]

        else:
            query = {
                "merge_type": "BA",
                "@time_horizon": self.time_horizon,
                "@scenario_timestamp": self.scenario_timestamp,
            }

            if self.time_horizon == 'YR':
                model_timestamp = get_yr_model_timestamp(self.scenario_timestamp)
                query['@scenario_timestamp'] = model_timestamp

            model_meta = query_data(query, query_filter=None, return_payload=False, scroll=None)

            if model_meta:
                version = self.model_version.lower()
                if version == 'latest':
                    version = max(int(x["@version"]) for x in model_meta)
                else:
                    version = int(version)
            else:
                raise ValueError(f"Model was not found for timestamp: {self.scenario_timestamp}")

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
                raise ValueError(f"Model was not found for timestamp: {self.scenario_timestamp}")

        self.model_meta = model
        self.model_data = model_data[0]


    def load_model_data(self):

        self.pp_network = load_network(self.model_data, parameters=sa_config.cgmes_import)
        self.security_service = SecurityAnalysis(network=self.pp_network, contingencies=self.contingencies,
                                                 assessed_elements=self.assessed_elements,
                                                 lf_parameters=self.lf_parameters, sa_parameters=self.sa_parameters,
                                                 debug=False)
        self.security_service.present_igms = self.security_service.network.get_areas()['name'].to_list()


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

        if 'PL' not in self.security_service.present_igms:
            logger.warning(f"Missing PSE model in {self.model_meta['name']}")

        # Perform base case security analysis
        self.security_service.present_igms = [x.replace('Estonia', 'EE').replace("10YLV", "LV") for x in self.security_service.present_igms]
        if set(self._expected_areas).issubset(self.security_service.present_igms):
            self.security_service.run_security_analysis()
            if not self.security_service.results:
                self.model_is_valid = False
                logger.info(f"Model {self.model_meta['name']} missing results")

            base_case_violation = self.security_service.processed_limit_violations.query(
                "contingency_id == '' and limit_type == 'CURRENT'")

            # Check for base case violations
            if not base_case_violation.empty:
                self.model_is_valid = False
                logger.info(f"Base case violations found before applied UAP in {self.model_meta['name']}")
            else:
                self.model_is_valid = True
            end_time = datetime.now(timezone.utc)
            logger.info(f"Validation took {(end_time - start_time).total_seconds()} s")
        else:
            self.model_is_valid = False
            logger.info(f"Validation failed, IGMs -> {self.security_service.present_igms} for {str(self.model_meta['name'])} ")


    def calculate_opi(self):

        outage_configuration_id = str(uuid.uuid4())
        self.outage_configuration_id = outage_configuration_id

        self.crosschecked_element_df = run_outage_crosscheck(self.security_service.network,
                                                             self.assessed_elements_df,
                                                             self.contingencies,
                                                             set_timestamp=self.scenario_timestamp,
                                                             outages=self.uap_outages)

        self.confirmed_outages = self.crosschecked_element_df[self.crosschecked_element_df['outage_in_uap']]
        self.model_outages = self.crosschecked_element_df[self.crosschecked_element_df['outage_in_cgm']]
        self.outages_to_reconnect = self.crosschecked_element_df[~self.crosschecked_element_df['outage_in_uap']]

        # Update outage lists according to manual inputs
        mask1 = self.crosschecked_element_df[list(self.outage_corrections['to_enable'])].isin(self.outage_corrections['to_enable']).any(axis=1)
        self.outages_to_reconnect = pd.concat([self.outages_to_reconnect, self.crosschecked_element_df[mask1]])
        mask2 = self.crosschecked_element_df[list(self.outage_corrections['to_disable'])].isin(self.outage_corrections['to_disable']).any(axis=1)
        self.confirmed_outages = pd.concat([self.confirmed_outages, self.crosschecked_element_df[mask2]])

        # Clone initial network state and apply outage to run N-1
        self.security_service.network.clone_variant(src='InitialState', target=outage_configuration_id)
        self.security_service.network.set_working_variant(outage_configuration_id)
        logger.info(f"Network model working variant initialized and activated: {outage_configuration_id}")

        changed_outage_list = []
        # Reconnecting relevant outages
        # TODO add reconnect flag to manual run config
        reconnect = True
        if reconnect:
            logger.info(f"Reconnecting outages to network instance: '{outage_configuration_id}'")
            for index, outage in self.outages_to_reconnect.iterrows():
                if outage.type != 'transformer' and outage.area != 'PL' and pd.notna(outage.equipment):
                    try:
                        if self.security_service.network.connect(outage.equipment):
                            logger.info(f" {outage['name']} {outage.equipment} reconnected")
                            # Reconnect switches at the end of lines to ensure it's not disconnected
                            if outage.type == 'line':
                                line = self.security_service.network.get_lines().loc[outage.equipment]
                                vl1 = line.voltage_level1_id
                                vl2 = line.voltage_level2_id
                                for vl in [vl1, vl2]:
                                    topo = self.security_service.network.get_bus_breaker_topology(vl)
                                    sw_df = topo.switches
                                    for sw_id, sw in sw_df.iterrows():
                                        if sw['open']:
                                            self.security_service.network.close_switch(sw_id)
                            elif outage.type == 'tieline':
                                line = self.security_service.network.get_dangling_lines().loc[outage.equipment]
                                vl = line.voltage_level_id
                                topo = self.security_service.network.get_bus_breaker_topology(vl)
                                sw_df = topo.switches
                                for sw_id, sw in sw_df.iterrows():
                                    if sw['open']:
                                        self.security_service.network.close_switch(sw_id)
                            changed_outage_list.append(outage)

                    except Exception as e:
                        logger.error((e, outage.name))
                        continue

        # Disconnect confirmed outages
        logger.info(f"Applying outages to network instance: {outage_configuration_id}")
        for index, outage in self.confirmed_outages.iterrows():
            try:
                if self.security_service.network.disconnect(outage.equipment):
                    logger.info(f"{outage['name']} {outage.equipment} disconnected")
                    if outage.type == 'line':
                        line = self.security_service.network.get_lines().loc[outage.equipment]
                        vl1 = line.voltage_level1_id
                        vl2 = line.voltage_level2_id
                        for vl in [vl1, vl2]:
                            topo = self.security_service.network.get_bus_breaker_topology(vl)
                            sw_df = topo.switches
                            for sw_id, sw in sw_df.iterrows():
                                if not sw['open']:
                                    self.security_service.network.open_switch(sw_id)
                    elif outage.type == 'tieline':
                        line = self.security_service.network.get_dangling_lines().loc[outage.equipment]
                        vl = line.voltage_level_id
                        topo = self.security_service.network.get_bus_breaker_topology(vl)
                        sw_df = topo.switches
                        for sw_id, sw in sw_df.iterrows():
                            if not sw['open']:
                                self.security_service.network.open_switch(sw_id)
                    changed_outage_list.append(outage)

                else:
                    if self.model_outages.equipment.str.contains(outage.equipment).any():
                        logger.debug(f"{outage['name']} {outage.equipment} is already in outage")
                    else:
                        logger.error(f"Failed to disconnect: {outage['name']} {outage.equipment}")
            except Exception as e:
                logger.error(e)
                continue

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
            opi_result_diverged_contingencies = pd.DataFrame(
                [{"mrid": key, "status": value.status} for key, value in self.security_service.results.post_contingency_results.items() if
                 value.status.name != 'CONVERGED'])
            if not opi_result_diverged_contingencies.empty:
                opi_result_diverged_contingencies["status"] = opi_result_diverged_contingencies["status"].astype(str).str.split(".").str[-1]

            border_flows = get_ba_cross_border_flows(self.security_service).sort_values('Cross_border')
            system_balance = get_system_balance(self.security_service, border_flows)
            system_balance = system_balance[~system_balance['area'].str.contains('PL')]

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
            lf_result = pp.loadflow.run_ac(network, parameters=self.lf_parameters)
            if lf_result[0].status.name != 'CONVERGED':
                logger.warning(f"Exported OPI model load flow status: {lf_result[0].status.name}")
            exported = zipfile.ZipFile(network.save_to_binary_buffer(format="CGMES", parameters={
                "iidm.export.cgmes.cgm_export": "True",
                "iidm.export.cgmes.topology-kind": "NODE_BREAKER",
                "iidm.export.cgmes.export-sv-injections-for-slacks": "False",
            }))
            original = zipfile.ZipFile(BytesIO(self.model_data))
            tso_names = {"LT": "LITGRID", "LV": "AST", "EE": "ELERING", "PL": "PSE"}
            model_buffer = BytesIO()
            with zipfile.ZipFile(model_buffer, "w", zipfile.ZIP_DEFLATED) as model_zip:
                for name in original.namelist():
                    if "_SSH_" not in name and "_SV_" not in name:
                        model_zip.writestr(name, original.read(name))
                for name in exported.namelist():
                    if "_SSH" in name:
                        original_part = f"-{tso_names[name.split('_')[-2]]}_SSH_"
                    else:
                        original_part = "_SV_"
                    new_name = next(n for n in original.namelist() if original_part in n)
                    new_name = new_name.replace("BALTICRCC-BA", "BRCC-OPI")
                    profile_buffer = BytesIO()
                    with zipfile.ZipFile(profile_buffer, "w", zipfile.ZIP_DEFLATED) as profile_zip:
                        profile_zip.writestr(new_name.replace(".zip", ".xml"), exported.read(name))
                    model_zip.writestr(new_name, profile_buffer.getvalue())
            model_buffer.seek(0)
            model_buffer.name = (f"{folder}/{self.time_horizon}/"
                                 f"{pd.Timestamp(self.scenario_timestamp):%Y%m%d}/"
                                 f"{self.model_meta['name']}_v{self.task_version}.zip")
            minio_service.upload_object(file_path_or_file_object=model_buffer, bucket_name=bucket)
            logger.info(f"OPI model saved to MinIO: {bucket}/{model_buffer.name}")
        except Exception as error:
            logger.error(f"OPI model export to MinIO failed: {error}")
        finally:
            network.set_working_variant('InitialState')


    def update_result_metadata(self):

        from opi.shared.process_results import get_opi_week_number, get_result_opi_year

        logger.info("Parsing finalized results")

        run_id = str(uuid.uuid4())
        timestamp = datetime.strptime(self.scenario_timestamp, '%Y-%m-%dT%H:%M:%S')
        result_list = []
        for name, result_df in self.opi_results.items():
            if not result_df.empty:
                result_df['@scenario_timestamp'] = self.scenario_timestamp
                result_df['@time_horizon'] = self.time_horizon
                result_df['@run_id'] = run_id
                result_df['@version'] = self.task_version
                result_df['week_day'] = timestamp.weekday()
                opi_week = get_opi_week_number(timestamp)
                result_df['opi_week'] = opi_week
                result_df['opi_month'] = str(timestamp.month)
                result_df['opi_year'] = get_result_opi_year(timestamp, self.time_horizon, self.opi_year)


    def send_results_to_elk(self):

        from opi.shared.data_functions import send_opi_results_to_elk

        response = send_opi_results_to_elk(self.opi_results, self.time_horizon, self.scenario_timestamp)

        return all(item is not False for item in response)
