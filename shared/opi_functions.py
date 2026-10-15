import calendar
import logging
import pandas as pd
import isodate
from datetime import datetime, timedelta, time
from opi.shared.utilities import AssessedElement, Contingency
from typing import List

logger = logging.getLogger(__name__)


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
