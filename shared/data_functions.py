import logging
import isodate
import pandas as pd
from opi.shared.integrations import get_docs_by_query, elastic_service, ELK_SERVER, ELK_API_KEY, ELK_UAP_INDEX
from datetime import datetime

logger = logging.getLogger(__name__)


def query_elk_uap(index=ELK_UAP_INDEX, run_time_offset='P0D', time_horizon=None, filter_range=None):

    logger.info(f"Retrieving outages from ELK index: '{ELK_UAP_INDEX}'")

    # now represents the time of the run, in P0W case it should be current time
    now = datetime.now() + isodate.parse_duration(run_time_offset)
    filter_time = (now + isodate.parse_duration(filter_range)).strftime("%Y-%m-%dT%H:%M") + "Z"
    now = now.strftime("%Y-%m-%dT%H:%M") + "Z"

    if time_horizon == 'WK':
        merge_type_list = ['week']
    elif time_horizon == 'MO':
        merge_type_list = ['week', 'month']
    elif time_horizon == 'YR':
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
    result = pd.DataFrame()

    if not response.empty:

        # Get only latest report data
        response['reportParsedDate'] = pd.to_datetime(response['reportParsedDate'])
        response = response[response['reportParsedDate'] == response['reportParsedDate'].max()]
        # Only keep latest outages
        duplicated_outages = response[response.duplicated('eic', keep=False)]
        latest_duplicate = duplicated_outages.groupby('eic')['date_of_last_change'].idxmax()
        response = response.loc[response.index.isin(latest_duplicate) | ~response['eic'].duplicated(keep=False)]

        response = response[response['outage_type'].isin(['OUT'])]

        response = response.sort_values(by=['eic', 'start_date', 'end_date']).reset_index(drop=True)
        last_end_time = {}

        # Remove outage duplicate if there is time overlap
        for _, row in response.iterrows():
            eic = row['eic']
            start_time = row['start_date']
            end_time = row['end_date']

            if eic not in last_end_time or start_time > last_end_time[eic]:
                result = pd.concat([result, pd.DataFrame([row])], ignore_index=True)
                last_end_time[eic] = end_time

    return result


def send_opi_results_to_elk(results: dict, time_horizon: str, timestamp: str):
    logger.info(f"Sending results to Elastic")
    response_list = []

    # system-balance marks the timestamp as complete (report, task version): send it last, only if all others succeeded
    for name, df in sorted(results.items(), key=lambda item: item[0] == 'system-balance'):
        df = df.fillna(0)
        if df.empty:
            continue
        if name == 'system-balance' and any(item is False for item in response_list):
            logger.error("Not sending 'opi-system-balance' because other result indices failed")
            continue
        data = df.to_dict('records')
        logging.getLogger('brcc_apis.elk_api').disabled = True
        response = elastic_service.send_to_elastic_bulk(index=f"opi-{name}", json_message_list=data, server=ELK_SERVER, api_key=ELK_API_KEY)
        response_list.append(response)
        if response is False:
            logger.error(f"Failed sending data to index 'opi-{name}'")
    if all(response_list):
        logger.info("Results successfully sent to Elastic")

    return response_list


def set_task_version(timestamps, time_horizon: str, set_version: str = None):

    set_version = str(set_version or "").strip()
    if set_version.upper() in ("", "LATEST"):
        timestamp_list = [ts.isoformat() for ts in timestamps]
        query = {"bool": {"must": [{"exists": {"field": "@version"}},
                                   {"terms": {"@scenario_timestamp": timestamp_list}},
                                   {"match": {"@time_horizon": time_horizon}}
                                   ]}}
        elk_df = get_docs_by_query(index=f"opi-system-balance*", query=query, size=10000)

        if not elk_df.empty:
            task_version = max(elk_df['@version']) + 1
        else:
            task_version = 1
    else:
        task_version = int(set_version)

    return task_version


def set_outage_corrections(outage_enable, outage_disable):

    outage_corrections = {
        "to_enable": {"equipment": [], "eic": [], "name": []},
        "to_disable": {"equipment": [], "eic": [], "name": []}
    }

    # Run if strings are not empty
    if not (not outage_enable.strip() and not outage_disable.strip()):

        # Sanitize inputs
        outage_enable = outage_enable.replace(" ", "")
        outage_enable = outage_enable.strip().rstrip(",")
        outage_enable = [item for item in outage_enable.split(",") if item]

        outage_disable = outage_disable.replace(" ", "")
        outage_disable = outage_disable.strip().rstrip(",")
        outage_disable = [item for item in outage_disable.split(",") if item]

        # Split outages either into mrid or name
        for outage in outage_enable:
            if outage.startswith("_"):
                outage_corrections["to_enable"]["equipment"].append(outage)
            elif outage.startswith("*"):
                outage = outage.lstrip("*")
                outage_corrections["to_enable"]["eic"].append(outage)
            else:
                outage_corrections["to_enable"]["name"].append(outage)

        for outage in outage_disable:
            if outage.startswith("_"):
                outage_corrections["to_disable"]["equipment"].append(outage)
            elif outage.startswith("*"):
                outage = outage.lstrip("*")
                outage_corrections["to_disable"]["eic"].append(outage)
            else:
                outage_corrections["to_disable"]["name"].append(outage)

    return outage_corrections
