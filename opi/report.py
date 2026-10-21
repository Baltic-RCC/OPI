from opi.integrations import send_excel_to_minio
from opi.integrations import get_docs_by_query
from opi.timeframes import get_opi_timestamps, get_opi_year, get_opi_period
from opi.common import parse_app_properties
from opi.integrations import set_task_version
from opi.common import setup_logging
from opi.region import EXTERNAL_AREAS, LINE_LOADING_LIMIT, TRANSFORMER_LOADING_LIMIT
import config
import pandas as pd
import logging
import sys
import datetime
import openpyxl
import openpyxl.styles
from io import BytesIO
from pathlib import Path
from openpyxl.worksheet.worksheet import Worksheet
from openpyxl.utils.dataframe import dataframe_to_rows

logger = logging.getLogger(__name__)


def format_numbers(worksheet: Worksheet):
    for row in worksheet.iter_rows():
        for cell in row:
            if isinstance(cell.value, (int, float)):
                cell.number_format = '0.00'


def set_headers(worksheet: Worksheet, row: int, col: int, val: str):
    """Sets the value of a cell specified by row & col, center aligned and in bold text"""
    worksheet.cell(row=row, column=col, value=val.upper())
    worksheet.cell(row=row, column=col, value=val.upper()).alignment = openpyxl.styles.Alignment(horizontal='center', vertical='center')
    worksheet.cell(row=row, column=col, value=val.upper()).font = openpyxl.styles.Font(bold=True)


def opi_results_to_excel(results: tuple, time_horizon: str = '', year='', export_locally=False):
    now = datetime.datetime.now().strftime(format='%Y-%m-%d_%H:%M:%S')
    sign_col = {"WK": "opi_week", "MO": "opi_month", "YR": "opi_year"}[time_horizon]
    signature = f'{time_horizon[0]}{results[0][0][0][sign_col][0]}'
    file_name = f'OPI_results_{time_horizon}_{signature}_{now}.xlsx'
    out_path = Path(__file__).parent.parent.joinpath(file_name)
    # template = openpyxl.load_workbook(Path(__file__).parent.parent.joinpath('inputs/opi_template.xlsx'))
    merged_results, df_names = results
    # split_results = [item for sublist in merged_results for item in sublist]
    workbook = openpyxl.Workbook()

    # Add all the sheet timestamps
    for i, timestamp_results in enumerate(merged_results):
        timestamp = pd.to_datetime(timestamp_results[0]['@scenario_timestamp']).iloc[0].strftime("%m-%d")
        timestamp = f"{timestamp} v{int(timestamp_results[0]['@version'].iloc[0])}"

        if f"{timestamp} Base Case Data" not in workbook.sheetnames:
            logger.info(f"Creating result sheets for {timestamp}")
            workbook.create_sheet(title=f"{timestamp} Base Case Data")
        if f"{timestamp} Violations" not in workbook.sheetnames:
            # logger.info(f"Creating violation sheet for {timestamp}")
            workbook.create_sheet(title=f"{timestamp} Violations")

        # Add the data to the sheets
        for i, data in enumerate(timestamp_results):
            # TODO find better solution
            drop_list = ['@scenario_timestamp', '@time_horizon', '@version', '@run_id', 'opi_year', 'opi_week',
                         'week_day', 'opi_month']
            for column_name in drop_list:
                data.drop(column_name, axis=1, inplace=True, errors='ignore')

            if df_names[i] == "violations":
                worksheet = workbook[f"{timestamp} Violations"]
                worksheet.merge_cells(start_row=worksheet.max_row, start_column=1, end_row=worksheet.max_row, end_column=17)
                set_headers(worksheet, worksheet.max_row, col=1, val=df_names[i])
                rows = dataframe_to_rows(data, index=False)
                for row in rows:
                    worksheet.append(row)
            else:
                worksheet = workbook[f"{timestamp} Base Case Data"]
                # if df_names[i] == "bus-results" or df_names[i] == "branch-results":
                if df_names[i] == "system-balance":
                    set_headers(worksheet, row=1, col=worksheet.max_column+3, val=df_names[i])
                    worksheet.merge_cells(start_row=1, start_column=worksheet.max_column, end_row=1, end_column=worksheet.max_column+len(data.columns))

                    columns = data.columns.tolist()
                    START_COLUMN = worksheet.max_column-len(data.columns)
                    START_ROW = 2 #row 1 is the title of the table
                    data = data.reset_index()
                    #Set the title for columns
                    for i, col in enumerate(columns):
                        worksheet.cell(row=START_ROW, column=START_COLUMN + i, value=col)
                    START_ROW += 1
                    #Set value for each row of the dataframe
                    for i, row in data.iterrows():
                        for j, col in enumerate(columns):
                            worksheet.cell(row = START_ROW + i, column=START_COLUMN+j, value=row[col])

                elif df_names[i] == "branch-results":
                    set_headers(worksheet, row=10, col=10, val=df_names[i])
                    worksheet.merge_cells(start_row=10, start_column=10, end_row=10, end_column=10+len(data.columns))

                    columns = data.columns.tolist()
                    START_COLUMN = 10
                    START_ROW = 11 #row 1 is the title of the table
                    data = data.reset_index()
                    #Set the title for columns
                    for i, col in enumerate(columns):
                        worksheet.cell(row=START_ROW, column=START_COLUMN + i, value=col)
                    START_ROW += 1
                    #Set value for each row of the dataframe
                    for i, row in data.iterrows():
                        for j, col in enumerate(columns):
                            worksheet.cell(row = START_ROW + i, column=START_COLUMN+j, value=row[col])
                else:
                    rows = dataframe_to_rows(data, index=False)

                    worksheet.merge_cells(start_row=worksheet.max_row, start_column=1, end_row=worksheet.max_row, end_column=8)
                    set_headers(worksheet, worksheet.max_row, col=1, val=df_names[i])

                    # worksheet[worksheet.max_row] = openpyxl.styles.Font(bold=True)
                    for row in rows:
                        worksheet.append(row)
                    worksheet.cell(row=worksheet.max_row+2, column=1, value="")

    if 'Sheet' in workbook.sheetnames:
        workbook.remove(workbook["Sheet"])

    for worksheet in workbook.worksheets:
        format_numbers(worksheet=worksheet)
    if export_locally:
        workbook.save(out_path)
        logger.info(f"Results exported -> {out_path}")

    excel_buffer = BytesIO()
    excel_buffer.name = f'{file_name}'
    workbook.save(excel_buffer)
    excel_buffer.seek(0)

    return excel_buffer


index_list = ["system-balance", "confirmed-outages", "border-flows",
              "bus-results", "branch-results", "violations",
              "diverged-contingencies"]

ignore_columns = {'_index', '_id', '_score', '@timestamp', }
ignore_columns2 = {'low_voltage_limit', 'high_voltage_limit', 'nominal_v', 'limit_type', 'subject_type', 'limit_type3',
                   'limit_type2', 'is_violation', '@run_id', 'is_violation1', 'is_violation2', 'is_violation3',
                   'substation_name3', 'loading_percent3', 's3', 'country3', 'flow_transfer', 'p3', 'q3', 'i3',
                   'limit3', 'limit_type3', 'subject_type', 'operator_strategy_id', 'subject_id', 'contingency_id',
                   '@run_id', 'operator_strategy_id', 'contingency_name', 'bus_id', 'CGMES.RatioTapChanger1',
                   'CGMES.RatioTapChanger2', 'CGMES.Terminal1', 'CGMES.Terminal2', 'CGMES.Terminal3',
                   'CGMES.Terminal_Boundary', 'CGMES.TransformerEnd1', 'CGMES.TransformerEnd2', 'CGMES.TransformerEnd3'}
ignore_columns3 = {'subject_id', 'contingency_id', '@run_id', 'limit_reduction', 'acceptable_duration'}


def main() -> None:
    setup_logging()
    parse_app_properties(caller_globals=globals(), path=config.paths.opi.report_generator)

    time_horizon = TIME_HORIZON
    if SET_CRITICAL_TIMESTAMPS:
        timestamps = get_opi_timestamps(iso_offset=DATE_OFFSET, timestamps_per_day=HOURS_PER_TIMESTAMP.split(","),
                                        time_horizon=TIME_HORIZON, set_timestamps=SET_CRITICAL_TIMESTAMPS)
    else:
        # Timestamps with results in the assessed period, not recalculated (Y-1 ones depend on the UAP at run time)
        period_start, period_end = get_opi_period(iso_offset=DATE_OFFSET, time_horizon=TIME_HORIZON)
        query = {"bool": {"filter": [
            {"match": {"@time_horizon": time_horizon}},
            {"range": {"@scenario_timestamp": {"gte": f"{period_start:%Y-%m-%d}T00:00:00",
                                               "lte": f"{period_end:%Y-%m-%d}T23:59:59"}}}
        ]}}
        balance_df = get_docs_by_query(index="opi-system-balance*", query=query, size=10000)
        timestamps = [] if balance_df.empty else sorted(pd.Timestamp(ts) for ts in balance_df['@scenario_timestamp'].unique())
        logger.info(f"Report {time_horizon} {period_start:%Y-%m-%d} - {period_end:%Y-%m-%d}: "
                    f"{len(timestamps)} timestamp(s) with results")
    if not timestamps:
        logger.error(f"No OPI results to report for {time_horizon}, exiting")
        sys.exit(1)
    opi_year = get_opi_year(timestamps)

    timestamp_list = [ts.isoformat() for ts in timestamps]

    full_results = []
    for ts, timestamp in zip(timestamps, timestamp_list):
        # Latest version per timestamp, a rerun may cover only some timestamps
        if RESULT_VERSION.strip().lower() in ('', 'latest'):
            version = set_task_version(timestamps=[ts], time_horizon=TIME_HORIZON) - 1
        else:
            version = int(RESULT_VERSION)
        logger.info(f"Report {timestamp} ({time_horizon}): version {version}")
        result_list = []
        run_id = None
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
            if run_id:
                query["bool"]["filter"].append({"match_phrase": {"@run_id": run_id}})
            elk_df = get_docs_by_query(index=f"opi-{index}*", query=query, size=10000)
            if elk_df.empty and index == 'system-balance':
                logger.warning(f"No results for {timestamp} ({time_horizon}, version {version}), not in report")
                break
            if index == 'system-balance' and '@run_id' in elk_df.columns:
                # Report one run: a version can hold several (version reused after a failed send, TASK_VERSION set by hand)
                runs = elk_df.sort_values('@timestamp', kind='stable') if '@timestamp' in elk_df.columns else elk_df
                run_id = runs['@run_id'].iloc[-1]
                if elk_df['@run_id'].nunique() > 1:
                    logger.warning(f"{elk_df['@run_id'].nunique()} runs found for {timestamp} ({time_horizon}, "
                                   f"version {version}), using latest run {run_id}")
                elk_df = elk_df[elk_df['@run_id'] == run_id].reset_index(drop=True)
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
                                (elk_df['value_percent'] >= LINE_LOADING_LIMIT)
                        ) |
                        (
                                (
                                        (elk_df['subject_type'] == "TWO_WINDINGS_TRANSFORMER") |
                                        (elk_df['subject_type'] == "THREE_WINDINGS_TRANSFORMER")
                                ) &
                                (elk_df['value_percent'] >= TRANSFORMER_LOADING_LIMIT)
                        )]

            if index == "branch-results":
                if not elk_df.empty:
                    elk_df = elk_df[~elk_df['country1'].isin(EXTERNAL_AREAS)]
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
    if not full_results:
        logger.error(f"No OPI results found for {time_horizon} timestamps {timestamp_list}, report not created")
        sys.exit(1)
    result_tuple = (full_results, index_list)

    excel_data = opi_results_to_excel(results=result_tuple, time_horizon=time_horizon, year=opi_year, export_locally=False)
    send_excel_to_minio(excel_data, time_horizon)


if __name__ == "__main__":
    main()
