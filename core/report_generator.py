from opi.shared.integrations import send_excel_to_minio
from opi.shared.process_results import opi_results_to_excel
from opi.shared.integrations import get_docs_by_query
from opi.shared.opi_functions import get_opi_timestamps, get_opi_year
from opi.shared.config_parser import parse_app_properties
from opi.shared.data_functions import set_task_version
import opi.configs as config
import pandas as pd

parse_app_properties(caller_globals=globals(), path=config.paths.opi.report_generator)

index_list = ["system-balance", "confirmed-outages", "border-flows",
              "bus-results", "branch-results", "violations",
              "diverged-contingencies"]

ignore_columns = {'_index', '_id', '_score', '@timestamp', }
ignore_columns2 = {'low_voltage_limit', 'high_voltage_limit', 'nominal_v', 'limit_type', 'subject_type', 'limit_type3',
                   'limit_type2', 'is_violation', '@run_id', 'is_violation1', 'is_violation2', 'is_violation3',
                   'substation_name3', 'loading_percent3', 's3', 'country3', 'flow_transfer', 'p3', 'q3', 'i3',
                   'limit3', 'limit_type3' 'subject_type', 'operator_strategy_id', 'subject_id', 'contingency_id',
                   '@run_id' 'operator_strategy_id', 'contingency_name', 'bus_id', 'CGMES.RatioTapChanger1',
                   'CGMES.RatioTapChanger2', 'CGMES.Terminal1', 'CGMES.Terminal2', 'CGMES.Terminal3',
                   'CGMES.Terminal_Boundary', 'CGMES.TransformerEnd1', 'CGMES.TransformerEnd2', 'CGMES.TransformerEnd3'}
ignore_columns3 = {'subject_id', 'contingency_id', '@run_id', 'limit_reduction', 'acceptable_duration'}
time_horizon = TIME_HORIZON
timestamps = get_opi_timestamps(iso_offset=DATE_OFFSET, timestamps_per_day=HOURS_PER_TIMESTAMP.split(","),
                                time_horizon=TIME_HORIZON, set_timestamps=SET_CRITICAL_TIMESTAMPS)
opi_year = get_opi_year(timestamps)
if RESULT_VERSION.lower() != 'latest':
    version = int(RESULT_VERSION)

timestamp_list = [ts.isoformat() for ts in timestamps]

full_results = []
for timestamp in timestamp_list:
    # Latest version per timestamp, so a partial re-run does not hide the other timestamps
    if RESULT_VERSION.lower() == 'latest':
        version = set_task_version(timestamps=[pd.Timestamp(timestamp)], time_horizon=TIME_HORIZON) - 1
    result_list = []
    for index in index_list:
        if index in ["bus-results", "branch-results"]:
            query = {"bool": {"filter": [
                {"match": {"@scenario_timestamp": timestamp}},
                {"match": {"@time_horizon": time_horizon}},
                {"match": {"@version": version}},
                {"term": {"contingency_id.keyword": ""}}
            ]}}
        else:
            query = {"bool": {"filter": [
                {"match": {"@scenario_timestamp": timestamp}},
                {"match": {"@time_horizon": time_horizon}},
                {"match": {"@version": version}},
            ]}}
        elk_df = get_docs_by_query(index=f"opi-{index}*", query=query, size=10000)
        if elk_df.empty and index == 'system-balance':
            break
        elk_df.drop(columns=list(ignore_columns), errors='ignore', inplace=True)
        if index != "violations":
            elk_df.drop(columns=list(ignore_columns2), errors='ignore', inplace=True)
        if index == "violations":
            if elk_df.empty:
                elk_df = pd.DataFrame(
                    columns=['subject_name', 'limit_type', 'limit_name', 'limit', 'value', 'side', 'subject_type',
                             'contingency_name', 'area', 'substation_name', 'nominal_v', 'base_case_value',
                             'value_percent', 'base_case_value_percent', 'base_case_violation', '@scenario_timestamp',
                             '@time_horizon', '@version', 'week_day', 'opi_week', 'opi_month', 'opi_year'])
            else:
                elk_df.drop(columns=list(ignore_columns3), errors='ignore', inplace=True)
                elk_df = elk_df[elk_df['limit_type'] == "CURRENT"]
                # Keep only values that are considered over OPI violation deadbands
                elk_df = elk_df[
                    (
                            (elk_df['subject_type'] == "LINE") &
                            (elk_df['value_percent'] >= 105)
                    ) |
                    (
                            (
                                    (elk_df['subject_type'] == "TWO_WINDINGS_TRANSFORMER") |
                                    (elk_df['subject_type'] == "THREE_WINDINGS_TRANSFORMER")
                            ) &
                            (elk_df['value_percent'] >= 110)
                    )]

        if index == "branch-results":
            if not elk_df.empty:
                elk_df = elk_df[elk_df['country1'] != 'PL']
                elk_df = elk_df[['subject_name'] + [c for c in elk_df.columns if c != 'subject_name']]
        if index == "bus-results":
            if not elk_df.empty:
                elk_df = elk_df[
                    ['substation_name'] + [c for c in elk_df.columns if c != 'subject_name' and c != 'substation_name']]
        num_cols = elk_df.select_dtypes(include='number').columns
        elk_df[num_cols] = elk_df[num_cols].round(2)

        result_list.append(elk_df)
    full_results.append(result_list)

full_results = [x for x in full_results if x != []]
result_tuple = (full_results, index_list)

excel_data = opi_results_to_excel(results=result_tuple, time_horizon=time_horizon, year=opi_year, export_locally=False)
send_excel_to_minio(excel_data, time_horizon)

SystemExit()
