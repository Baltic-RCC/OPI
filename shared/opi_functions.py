import calendar
import uuid
import logging
import pandas as pd
import isodate
import opi.shared.sa_config as conf
from datetime import datetime, timedelta, time, timezone
from opi.shared.process_results import get_system_balance, get_ba_cross_border_flows
from opi.shared.outage_crosscheck import run_outage_crosscheck
from opi.shared.loadflow import load_network, replace_hvdc_reactive_generation
from loadflow_engine.security_analysis import SecurityAnalysis
from opi.shared.utilities import AssessedElement, Contingency
from typing import List

logger = logging.getLogger(__name__)
sa_config = conf.SecurityAnalysis(suspend_stdout=True)


# TODO DEPRECATED
def run_opi(network, contingencies, assessed_elements, timestamp_list: list, element_df: pd.DataFrame,
            outages_df: pd.DataFrame = None, time_horizon=None):

    pp_network = load_network(network, parameters=sa_config.cgmes_import)

    # HVDC generator workaround
    try:
        pp_network = replace_hvdc_reactive_generation(pp_network, replace_nordbalt=True, replace_estlink1=True)
    except Exception as e:
        logger.error(f"HVDC workaround failed: {e}")

    security_service = SecurityAnalysis(network=pp_network, contingencies=contingencies, assessed_elements=assessed_elements, debug=False)

    total_results = []

    for num, timestamp in enumerate(timestamp_list):
        outage_configuration_id = str(uuid.uuid4())

        crosschecked_element_df = run_outage_crosscheck(security_service.network, element_df, set_timestamp=timestamp, outages_df=outages_df)

        crosschecked_element_df = crosschecked_element_df.rename(columns={'secured_for_region': 'area'})
        confirmed_outages = crosschecked_element_df[crosschecked_element_df['outage_in_uap']]
        existing_outages = crosschecked_element_df[crosschecked_element_df['outage_in_cgm']]

        # Clone initial network state and apply outage to run N-1
        security_service.network.clone_variant(src='InitialState', target=outage_configuration_id)
        security_service.network.set_working_variant(outage_configuration_id)
        logger.info(f"Network model working variant initialized and activated: {outage_configuration_id}")

        changed_outage_list = []
        # Reconnecting relevant outages
        logger.info(f"Reconnecting outages to network instance: '{outage_configuration_id}'")
        for outage in security_service.assessed_elements:
            if outage.type != 'transformer':
                try:
                    if security_service.network.connect(outage.equipment):
                        changed_outage_list.append(outage)
                        logger.info(f" {outage.name} {outage.equipment} successfully reconnected")
                    else:
                        continue
                except Exception as e:
                    logger.error((e, outage.name))
                    continue
        logger.info(f"Done reconnected all assessed elements")

        # Disconnect confirmed outages
        logger.info(f"Applying outages to network instance: {outage_configuration_id}")
        for index, outage in confirmed_outages.iterrows():
            try:
                if security_service.network.disconnect(outage['grid_element_id']):
                    logger.info(f"Line {outage['name']} {outage['grid_element_id']} successfully disconnected")
                else:
                    if existing_outages['grid_element_id'].str.contains(outage['grid_element_id']).any():
                        logger.info(f"Line {outage['name']} {outage['grid_element_id']} is already in outage")
                    else:
                        logger.error(f"Failed to disconnect line: {outage['name']} {outage['grid_element_id']}")
            except Exception as e:
                logger.error(e)
                continue

        # Run post outage scenario
        security_service.run_security_analysis()

        # Process OPI results
        if not security_service.results:
            logger.error(f"Model {str(timestamp)} did not finish calculation")
            opi_results = None
        else:
            opi_result_bus = security_service.processed_bus_results
            opi_result_branch = security_service.processed_branch_results
            opi_result_violations = security_service.processed_limit_violations
            opi_result_diverged_contingencies = pd.DataFrame([key for key, value in security_service.results.post_contingency_results.items() if value.status.name != 'CONVERGED'])

            border_flows = get_ba_cross_border_flows(security_service).sort_values('Cross_border')
            system_balance = get_system_balance(security_service, border_flows)
            system_balance = system_balance[~system_balance['area'].str.contains('PL')]

            # TODO fix output to fit existing structure
            opi_results = [
                timestamp,
                confirmed_outages[['area', 'name', 'description']],
                border_flows[['Cross_border', 'name', 'P_MW']],
                opi_result_bus,
                system_balance,
                opi_result_branch,
                opi_result_violations,
                opi_result_diverged_contingencies
            ]

        total_results.append(opi_results)

        security_service.network.set_working_variant('InitialState')
        logger.info(f"Network model working variant reset to initial state ")

    return total_results


def calculate_critical_timestamps(uap_df, properties):

    logger.info("Calculating critical timestamps")
    # TODO check if this statement still needed or remove it
    pd.set_option('future.no_silent_downcasting', True)

    year = properties['OPI_Year']
    critical_timestamps = []

    calendar = pd.date_range(start=pd.Timestamp(f'{year}-01-01 12:00'),
                             end=pd.Timestamp(f'{year}-12-31 12:00'), freq='D')
    date_outages_list = []
    add_timestamp = True
    for date in calendar:
        outages = ((pd.to_datetime(uap_df['start_date']) <= date) & (pd.to_datetime(uap_df['end_date']) >= date))
        date_outages = uap_df[outages]

        area_tieline_count = date_outages[date_outages['asset_type'].isin(['TIE'])].groupby('Area').size().max()
        area_line_count = date_outages[date_outages['asset_type'].isin(['LINE'])].groupby('Area').size().max()
        region_total_line_count = (date_outages['asset_type'].isin(['TIE', 'LINE'])).sum()
        area_outage_count = date_outages.groupby('Area').size().max()
        region_total_outage_count = outages.sum()
        unavailable_generation = pd.to_numeric(date_outages[(date_outages['asset_type'].isin(['GEN', 'PROD']))][
                'Unavailable_capacity']).sum()
        # transformer_count = (date_outages['pSRType.psrType'] == 'B24').sum()

        # Remove duplicate outage combinations
        if not date_outages_list:
            date_outages_list.append(date_outages)
        else:
            for item in date_outages_list:
                add_timestamp = True
                if date_outages.equals(item):
                    add_timestamp = False
            if add_timestamp:
                date_outages_list.append(date_outages)
                add_timestamp = False

                # Check if new outage set can be considered critical timestamp
                if area_tieline_count >= int(properties['Area_tieline_limit']):
                    critical_timestamps.append(date)
                elif area_line_count >= int(properties['Area_line_limit']):
                    critical_timestamps.append(date)
                elif region_total_line_count >= int(properties['Region_total_line_limit']):
                    critical_timestamps.append(date)
                elif area_outage_count >= int(properties['Area_outages_limit']):
                    critical_timestamps.append(date)
                elif region_total_outage_count >= int(properties['Region_total_outages_limit']):
                    critical_timestamps.append(date)
                elif unavailable_generation >= float(properties['Unavailable_generation_limit']):
                    critical_timestamps.append(date)

    logger.info(f"OPI Y-1 timestamps: {critical_timestamps}")

    return critical_timestamps


def parse_set_timestamps(timestamp_str: str, timestamps_per_day: list):

    timestamps = []
    seen = set()

    def parse_hour(h):
        parts = h.strip().split(":")
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
        return time(hour, minute)

    for r in timestamp_str.split(','):
        r = r.strip()
        if not r:
            continue
        if '-' in r:
            from_str, to_str = r.strip().split('-')
        else:
            from_str = to_str = r

        from_date = datetime.strptime(from_str.strip(), "%Y_%m_%d").date()
        to_date = datetime.strptime(to_str.strip(), "%Y_%m_%d").date()

        for day in pd.date_range(start=from_date, end=to_date, freq='D'):
            day_date = day.date()
            for hour in timestamps_per_day:
                hr = parse_hour(hour)
                timestamp = datetime.combine(day_date, hr)
                if day not in seen:
                    timestamps.append(timestamp)
                    seen.add(timestamp)

    return [pd.Timestamp(dt) for dt in timestamps]

#TODO Delete after refactoring
def validate_base_case(opi_models, contingencies: list, assessed_elements: list):
    valid_models = []
    for num, model in enumerate(opi_models):
        # Check model data type to define naming and other parameters
        if dict == type(model):
            network = model.get("DATA")
            timestamp = model.get("@scenario_timestamp")
            model_name = model['name'][:28]
        else:
            network = model
            model_name = str(num+1)
            timestamp = None
        # Load model
        logger.info(f"Validating model {num+1}/{len(opi_models)}: {model_name}")
        start_time = datetime.now(timezone.utc)
        pp_network = load_network(network, parameters=sa_config.cgmes_import)
        security_service = SecurityAnalysis(network=pp_network, contingencies=contingencies, assessed_elements=assessed_elements, debug=False)
        if not timestamp:
            timestamp = str(security_service.network.case_date.isoformat())
        logger.info(f"Network scenario timestamp: {timestamp}")
        security_service.present_igms = ['EE' if tso == 'Estonia' else tso for tso in security_service.present_igms]

        # Check if model is a single island
        islands = security_service.network.get_buses().connected_component.value_counts().to_list()
        if len(islands) > 1:
            main_island = islands[0]
            sub_islands = sum(islands[1:])
            island_diff = sub_islands / main_island
        else:
            island_diff = 0
        if island_diff > 0.15:
            logger.info(f"Network components: {islands}")
            logger.error(f"Model {str(model_name)} has separated islands, skipping validation")
            continue

        # TODO potentially move to configs, but not necessary yet
        if 'PL' not in security_service.present_igms:
            logger.warning("Missing PSE model")
        if all(tso in security_service.present_igms for tso in ['LT', 'LV', 'EE']):
            security_service.run_security_analysis()
            if not security_service.results:
                logger.error(f"Model {model_name} missing results, skipping validation")
                continue
            base_case_violation = security_service.processed_limit_violations.query("contingency_id == '' and limit_type == 'CURRENT'")

            # Check for base case violations
            if not base_case_violation.empty:
                logger.error(f"Base case violations found in outage free model : {model_name}")
            else:
                if bytes == type(model):
                    valid_models.append({"DATA": model, "@scenario_timestamp": timestamp})
                else:
                    valid_models.append(model)
            end_time = datetime.now(timezone.utc)
            logger.info(f"Validation {num+1}/{len(opi_models)} took {(end_time-start_time).total_seconds()} s")
        else:
            logger.error(f"Model {str(model_name)} missing TSO(s), skipping validation")

    return valid_models

# TODO DEPRECATED
def generate_opi_runs(model_list: list, critical_timestamps: list, time_horizon: str):
    runs = []
    for model in model_list:
        model_ts = model["@scenario_timestamp"]

        # Defined month seasonality of YR models by CGM requirements
        # TODO year needs to be dynamic, taken from opi config
        if time_horizon == 'YR':
            intervals = [
                (pd.Timestamp('2025-01-01 12:30'), pd.Timestamp('2025-02-28 12:30')),
                (pd.Timestamp('2025-03-01 12:30'), pd.Timestamp('2025-05-31 12:30')),
                (pd.Timestamp('2025-06-01 12:30'), pd.Timestamp('2025-08-31 12:30')),
                (pd.Timestamp('2025-09-01 12:30'), pd.Timestamp('2025-11-30 12:30')),
                (pd.Timestamp('2025-12-01 12:30'), pd.Timestamp('2025-12-31 12:30')),
            ]
            matching_interval = [(start, end) for start, end in intervals if start <= model_ts <= end]
            start = matching_interval[0][0]
            end = matching_interval[0][1]
            ts = [ts for ts in critical_timestamps if start <= ts <= end]
        else:
            ts = [ts for ts in critical_timestamps if datetime.fromisoformat(model_ts).date() == ts.date()]

        if ts:
            runs.append((model["DATA"], ts))
        else:
            logger.warning(f"No run created for model timestamp: {str(model_ts)}")

    return runs


def get_opi_timestamps(iso_offset: str, timestamps_per_day: list, time_horizon: str, set_timestamps=None) -> List[pd.Timestamp]:
    # TODO update logic to be fool proof
    if set_timestamps:
        critical_timestamps = parse_set_timestamps(set_timestamps, timestamps_per_day)
    elif time_horizon == 'WK':
        critical_timestamps = get_opi_1w_timestamps(iso_offset, timestamps_per_day)
    elif time_horizon == 'MO':
        critical_timestamps = get_opi_1m_timestamps(iso_offset, timestamps_per_day)
    elif time_horizon == 'YR':
        from opi.configs.opi.opi_yr_criteria import opi_yr_ct_properties
        from opi.shared.data_functions import query_elk_uap
        outage_df = query_elk_uap(time_horizon=time_horizon, run_time_offset=iso_offset, filter_range='-P10W')
        critical_timestamps = calculate_critical_timestamps(outage_df, opi_yr_ct_properties)
    else:
        critical_timestamps = None

    return critical_timestamps


def get_opi_1w_timestamps(iso_duration: str, timestamps_per_day: list):

    # Get run week
    timehorizon_offset = isodate.parse_duration(iso_duration)
    now = datetime.now()

    # OLD Added 7 day time delta to not change existing code that calculated for run day for WK
    run_time = now + timehorizon_offset

    # Find upcoming Saturday
    days_until_saturday = (5 - run_time.weekday() + 7) % 7
    if days_until_saturday == 0:
        days_until_saturday = 7
    next_saturday = run_time + timedelta(days=days_until_saturday)

    # Generate dates from next Saturday to the following Friday
    next_week_dates = [datetime.combine(next_saturday + timedelta(days=i), time(hour=0, minute=0)) for i in range(7)]

    set_hours = [time(hour=int(hour.split(':')[0]), minute=int(hour.split(':')[1])) for hour in timestamps_per_day]
    timestamps = [pd.Timestamp(datetime.combine(dt, h)) for h in set_hours for dt in next_week_dates]
    logger.info(f"OPI W-1 timestamps: {str(next_week_dates[0].date())} - {str(next_week_dates[-1].date())}")

    return timestamps


def get_opi_1m_timestamps(iso_duration: str, hours_per_day: list):

    # Get current date
    timehorizon_offset = isodate.parse_duration(iso_duration)
    now = datetime.now()
    run_time = now + timehorizon_offset

    # Find first day of upcoming month
    next_month = run_time.month % 12 + 1
    year = run_time.year + (1 if next_month == 1 else 0)
    first_day_next_month = datetime(year, next_month, 1)

    # Find last day of upcoming month
    _, last_day = calendar.monthrange(year, next_month)
    last_day_next_month =  datetime(year, next_month, last_day)

    timestamps = []
    current_day = first_day_next_month
    while current_day <= last_day_next_month:
        timestamps.append(current_day)
        current_day += timedelta(days=1)

    set_hours = [time(hour=int(hour.split(':')[0]), minute=int(hour.split(':')[1])) for hour in hours_per_day]
    timestamps = [pd.Timestamp(datetime.combine(dt, h)) for h in set_hours for dt in timestamps]

    return timestamps


def get_input_dir(time_horizon: str):

    mapping_dict = {
        "WK" : "OPI/inputs/W-1",
        "MO" : "OPI/inputs/M-1",
        "YR" : "OPI/inputs/Y-1",
    }
    return mapping_dict.get(time_horizon)


def get_uap_offset_time(time_offset: str, time_horizon: str):
    mapping_dict = {
        "WK": "-P2W",
        "MO": "-P2M",
        "YR": "-P6M",
    }
    if time_offset:
        uap_offset_time = time_offset
    else:
        uap_offset_time = mapping_dict.get(time_horizon)

    return uap_offset_time


def get_opi_year(timestamps: list):

    opi_year = str(min(ts.year for ts in timestamps))

    return opi_year

def get_yr_model_timestamp(scenario_timestamp):

    from opi.configs.opi.opi_yr_criteria import intervals

    input_date = datetime.strptime(scenario_timestamp, "%Y-%m-%dT%H:%M:%S").date()

    for interval in intervals:
        sorted_dates = [datetime.strptime(date, "%Y-%m-%d").date() for date in interval]
        start, end, timestamp = sorted_dates[0], sorted_dates[1], sorted_dates[2]

        if start <= input_date <= end:
            model_timestamp = timestamp

    return datetime.strftime(model_timestamp, "%Y-%m-%d")


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
    converted_assessed_elements = [AssessedElement(**assessed_element) for assessed_element in assessed_elements.to_dict('records')]

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
    converted_contingencies = [Contingency(**contingency) for contingency in aggregated_dict]

    return converted_contingencies


if __name__ == "__main__":
    # Testing
    from opi.shared.parse_uap import parse_uap_xml

    critical_timestamp_properties = {
        "OPI_Year": '2026',
        'Area_tieline_limit': '6',
        'Area_line_limit': '7',
        'Region_total_line_limit': '13',
        'Area_outages_limit': '10',
        'Region_total_outages_limit': '15',
        'Unavailable_generation_limit': '800',
    }

    uap_path = r'/opi/inputs\OpcUnavailabilityPlanManualExport.xml'
    opc_data = parse_uap_xml(uap_path)

    critical_ts = calculate_critical_timestamps(opc_data, critical_timestamp_properties)

    print("")
