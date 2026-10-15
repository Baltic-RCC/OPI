import openpyxl.styles
import pandas as pd
import openpyxl
from openpyxl.worksheet.worksheet import Worksheet
from openpyxl.utils.dataframe import dataframe_to_rows
import datetime
import logging
from pathlib import Path
import pypowsybl as pp
from io import BytesIO
from opi.shared.loadflow import get_network_elements, get_power_losses, get_xborder_flows

# Start logger
logger = logging.getLogger(__name__)


def get_system_balance(service, border_flows):
    result_df = pd.DataFrame(index=service.present_igms,
                             columns=['P Load MW', 'P Gen MW', 'P Losses MW', 'HVDC flow', 'ACNP'])

    generation = get_network_elements(service.network, element_type=pp.network.ElementType.GENERATOR)
    load = get_network_elements(service.network, element_type=pp.network.ElementType.LOAD)
    p_losses = get_power_losses(service.network, present_igms=service.present_igms,
                                by_region=True).drop('Tie_lines', axis=1).T

    result_df['P Losses MW'] = p_losses
    for tso in service.present_igms:
        result_df.loc[tso, 'P Load MW'] = load[load['CGMES.regionName'] == tso]['p'].sum()
        result_df.loc[tso, 'P Gen MW'] = abs(generation[generation['CGMES.regionName'] == tso]['p'].sum())
        if tso == 'EE':
            result_df.loc[tso, 'HVDC flow'] = (border_flows[border_flows['name'].str.contains('ESTLINK1')].iloc[0]['P_MW']
            + border_flows[border_flows['name'].str.contains('ESTLINK2')].iloc[0]['P_MW'])
        elif tso == 'LT':
            result_df.loc[tso, 'HVDC flow'] = border_flows[border_flows['name'].str.contains('NORDBALT')].iloc[0]['P_MW']
        else:
            result_df.loc[tso, 'HVDC flow'] = float(0)

        result_df.loc[tso, 'ACNP'] = (result_df.loc[tso, 'P Gen MW'] - result_df.loc[tso, 'P Load MW']
                                      - result_df.loc[tso, 'P Losses MW'] - result_df.loc[tso, 'HVDC flow'])

    return round(result_df, 2).reset_index(names=['area'])


def get_ba_cross_border_flows(service):
    result_df = pd.DataFrame()
    cross_borders = []
    if 'LT' in service.present_igms:
        cross_borders.extend(['LT-PL', 'LT-LV', 'LT-SE'])
    if 'LV' in service.present_igms:
        cross_borders.extend(['LV-LT', 'LV-EE'])
    if 'Estonia' or 'EE' or 'EESTI' in service.present_igms:
        cross_borders.extend(['EE-LV', 'EE-FI'])

    for border in cross_borders:
        flow, limit, bf_df = get_xborder_flows(service.network, cross_border=border, logger_output=False)
        bf_df['Cross_border'] = border
        result_df = pd.concat([result_df, bf_df])

    return result_df.reset_index(names=['name']).drop_duplicates(subset='name')


def get_opi_week_number(timestamp: pd.Timestamp) -> str:
    if timestamp.weekday() == 4:
        last_friday = timestamp - pd.Timedelta(days=7)
    else:
        days_ago = (timestamp.weekday() - 4) % 7
        last_friday = timestamp - pd.Timedelta(days=days_ago)
    week_number = last_friday.isocalendar().week

    return str(week_number)


def get_result_opi_year(timestamp: datetime.datetime, time_horizon: str, task_opi_year: str) -> str:
    """opi_year for results and logs; W-1 weeks 52/53 dated in January stay in the previous year."""
    if time_horizon == "WK":
        if get_opi_week_number(timestamp) in ['52', '53'] and timestamp.month == 1:
            return str(timestamp.year - 1)
        return str(timestamp.year)
    return task_opi_year


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

    format_numbers(worksheet=workbook[f"{timestamp} Base Case Data"])
    format_numbers(worksheet=workbook[f"{timestamp} Violations"])
    if export_locally:
        workbook.save(out_path)
        logger.info(f"Results exported -> {out_path}")

    excel_buffer = BytesIO()
    excel_buffer.name = f'{file_name}'
    workbook.save(excel_buffer)
    excel_buffer.seek(0)

    return excel_buffer
