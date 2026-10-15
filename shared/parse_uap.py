import xmltodict
import logging
from json import dumps, loads
import pandas as pd
import numpy as np
from pathlib import Path
import datetime
import copy
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


def parse_uap_xml(input_document: bytes | str) -> Optional[pd.DataFrame]:
    """
    parses uap xml file and returns only the latest outages
    """
    # Parse XML to dict
    try:
        if isinstance(input_document, str):
            with open(input_document, "rb") as xml_file:
                xml_content = xml_file.read()
        elif isinstance(input_document, bytes):
            xml_content = input_document
        else:
            logger.error("Invalid input type for parse_uap_xml. Expected str or bytes")
            return None

        data_dict = xmltodict.parse(xml_content, xml_attribs=True, dict_constructor=dict, process_namespaces=True)
        time_series_list = data_dict.get('OutageSchedule_MarketDocument', {}).get('TimeSeries', [])

        if not time_series_list:
            logger.warning("No 'TimeSeries' data found in the UAP document.")
            return pd.DataFrame() #Return empty DataFrame

        # Flatten data and handle list of RegisteredResources
        flat_outage_list = []
        for ts in time_series_list:
            resources = ts.get('RegisteredResource')
            # Ensure resources is always a list
            if resources is None:
                continue # Skip TimeSeries with no resource
            if not isinstance(resources, list):
                resources = [resources]

            for resource in resources:
                #Create a flat dict for each outage record
                flat_outage = {}
                flat_outage['ts_mRID'] = ts.get('mRID')

                # EIC
                eic_val = resource.get('mRID')
                if isinstance(eic_val, dict):
                    flat_outage['eic'] = eic_val.get('#text')
                else:
                    flat_outage['eic'] = eic_val

                flat_outage['name'] = resource.get('name')

                # Time interval data
                time_interval = ts.get('outage_Period', {}).get('timeInterval', {})
                flat_outage['start_time_str'] = time_interval.get('start')
                flat_outage['end_time_str'] = time_interval.get('end')

                # Creation datetime
                flat_outage['created_dt_str'] = ts.get('lastChange_MarketAgreement', {}).get('createdDateTime')

                # Add region mapping info
                flat_outage['area_mapping_mRID'] = ts.get('mRID')

                #other fields if necessary
                # flat_outage['reason_code'] = ts.get('Reason', {}).get('code')

                if flat_outage.get('eic'):
                    flat_outage_list.append(flat_outage)

        if not flat_outage_list:
            logger.warning("Could not extract any valid outage records.")
            return pd.DataFrame()

        # Create DataFrame
        df = pd.DataFrame(flat_outage_list)

        # Convert datetime columns
        df['start_time_utc'] = pd.to_datetime(df['start_time_str'], errors='coerce', utc=True)
        df['end_time_utc'] = pd.to_datetime(df['end_time_str'], errors='coerce', utc=True)
        df['created_dt_utc'] = pd.to_datetime(df['created_dt_str'], errors='coerce', utc=True)

        # Drop rows where essential dates failed to parse
        df = df.dropna(subset=['eic', 'start_time_utc', 'end_time_utc', 'created_dt_utc'])

        # Keep only the latest entry per EIC
        df = df.sort_values('created_dt_utc', ascending=False)
        df = df.drop_duplicates(subset=['eic'], keep='first')

        # Apply region mapping using set_outage_area logic
        area_region_info = df.apply(lambda row: pd.Series(set_outage_area({'mRID': row['area_mapping_mRID']})), axis=1)[['region', 'area']]
        df['region'] = area_region_info['region']
        df['area'] = area_region_info['area']

        # Reorder final columns
        final_columns = [
            'eic',
            'name',
            'start_time_utc',
            'end_time_utc',
            'created_dt_utc',
            'region',
            'area',
            'ts_mRID'
            # Add other columns if needed
        ]
        # Filter columns
        final_columns = [col for col in final_columns if col in df.columns]
        df_final = df[final_columns]

        logger.info(f"Successfully parsed UAP into DataFrame with {len(df_final)} unique outages.")
        return df_final.reset_index(drop=True)

    except Exception as e:
        logger.error(f"Error parsing UAP XML: {e}", exc_info=True)
        return None

def parse_uap_xlsx(input_document):
    outages = []

    header = pd.read_excel(input_document, header=1, nrows=1).iloc[:, 0:8].to_dict('records')[0]
    body = pd.read_excel(input_document, header=3).iloc[:, 0:34].to_dict(orient='records')

    opc_message = {}
    opc_message['RegisteredResource'] = {}
    opc_message['outage_Period.timeInterval'] = {}
    opc_message['createdDateTime'] = header['DATE-TIME-CREATED']

    for num, TimeSeries in enumerate(body):
        opc_message['mRID'] = body[num]['CASE_ID']
        opc_message['RegisteredResource']['name'] = body[num]['LONG-NAME']
        opc_message['RegisteredResource']['mRID'] = str(body[num]['ELEMENT-ID-EIC'])
        opc_message['lastChange_MarketAgreement.createdDateTime'] = body[num]['DATE-OF-LAST-CHANGE']

        opc_message['outage_Period.timeInterval']['start'] = datetime.datetime.strptime(
            body[num]['DATE-FROM'] + '.' + body[num]['TIME-FROM'], '%d.%m.%Y.%H:%M').isoformat()
        opc_message['outage_Period.timeInterval']['end'] = datetime.datetime.strptime(
            body[num]['DATE-TO'] + '.' + body[num]['TIME-TO'], '%d.%m.%Y.%H:%M').isoformat()

        outages.append(copy.deepcopy(opc_message))

    outages.sort(key=lambda x: x.get('lastChange_MarketAgreement.createdDateTime'), reverse=True)
    seen = set()
    outage_list = []
    for outage in outages:
        # Split multiple resources in single time series
        if type(outage['RegisteredResource']) == list:
            resources = outage.pop('RegisteredResource')
            for resource in resources:
                new_outage = outage
                new_outage['RegisteredResource'] = resource
                outage_list.append(new_outage.copy())
            continue
        if outage['RegisteredResource']['name']:
            if outage['RegisteredResource']['name'] not in seen:
                seen.add(outage['RegisteredResource']['name'])
                outage = set_outage_area(outage)
                outage_list.append(outage)

    return outage_list


def get_relevant_outages(outage_data, scenario_time):
    relevant_outages = []
    for outage in outage_data:
        outage_start = datetime.datetime.fromisoformat(outage['outage_Period.timeInterval']['start'])
        outage_end = datetime.datetime.fromisoformat(outage['outage_Period.timeInterval']['end'])
        if outage_start < scenario_time < outage_end:
            relevant_outages.append(outage)

    return relevant_outages


def get_grid_id(outages, eic_path):
    eic_df = pd.read_excel(eic_path)
    new_list = [{'mRID': outage['RegisteredResource']['mRID']['#text'], 'name': outage['RegisteredResource']['name']}
                for outage in outages]
    outage_df = pd.DataFrame(new_list).sort_values('name')
    eic_set = set(outage_df['mRID'])
    grid_id_df = eic_df[eic_df['eic'].isin(eic_set)]

    # To check what lines got mapped
    # outage_df['grid_id'] = outage_df['mRID'].apply(
    #     lambda x: 'Found' #eic_df[eic_df['eic'].fillna('').str.startswith(x)]['grid_element_id']
    #     if eic_df['eic'].str.startswith(x).any() else 'Not Found')

    return grid_id_df['grid_element_id'].reset_index(drop=True)


def set_outage_area(uap_outage: dict):
    uap_outage['region'] = 'Undefined'
    uap_outage['area'] = 'Undefined'

    if uap_outage['mRID'].startswith('LT') or uap_outage['mRID'].startswith('TP_LT') or uap_outage['mRID'].startswith('10T-LT-PL') or uap_outage['mRID'].startswith('10T-LT-SE'):
        uap_outage['region'] = 'Baltic'
        uap_outage['area'] = 'Lithuania'
    elif uap_outage['mRID'].startswith('LV') or uap_outage['mRID'].startswith('TP_LV'):
        uap_outage['region'] = 'Baltic'
        uap_outage['area'] = 'Latvia'
    elif uap_outage['mRID'].startswith('EE') or uap_outage['mRID'].startswith('TP_EE') or uap_outage['mRID'].startswith('10T-FI-EE'):
        uap_outage['region'] = 'Baltic'
        uap_outage['area'] = 'Estonia'

    return uap_outage


def parse_outages(file):
    if file[:5] == b'<?xml':
        outages = parse_uap_xml(file)
    elif file[:4] == b'\x50\x4B\x03\x04':
        outages = parse_uap_xlsx(file)
    else:
        outages = None

    return outages


if __name__ == "__main__":
    # Testing
    uap_path = Path(__file__).parent.parent.joinpath('inputs/OpcUnavailabilityPlanManualExport.xml')
    eic_path = Path(__file__).parent.parent.joinpath('inputs/eic_to_mrid_map.xlsx')
    timeframe = datetime.datetime(year=2024, month=1, day=2, hour=7, minute=30, tzinfo=datetime.timezone.utc)

    opc_data = parse_uap_xml(uap_path)

    test_relevant_outages = get_relevant_outages(opc_data, timeframe)

    result = get_grid_id(opc_data, eic_path)

    print("")
