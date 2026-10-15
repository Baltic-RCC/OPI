from typing import Any
import uuid
import openpyxl.styles
import pandas as pd
import openpyxl
from openpyxl.worksheet.worksheet import Worksheet
from openpyxl.utils.dataframe import dataframe_to_rows
import datetime
import logging
import numpy as np
from word2number import w2n
from pathlib import Path
import pypowsybl as pp
from io import BytesIO
from collections import deque
import configparser
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


def process_base_case_results(service):
    # Load line limits from xlsx
    line_limits = pd.read_excel(Path(__file__).parent.parent.joinpath('inputs/general/line_limits.xlsx'))

    bc_branch_result = service.results.branch_results.reset_index().query("contingency_id == ''")
    # Get only line results
    assessed_lines = service.available_assessed_elements[service.available_assessed_elements['equipment_type'].isin(['LINE', 'DANGLING_LINE'])]
    bc_branch_result = bc_branch_result.merge(assessed_lines, left_on='branch_id', right_on='equipment', how='right')
    bc_branch_result = bc_branch_result[bc_branch_result['name'].isin(assessed_lines['name'])]

    # Get positive values for average value calculation
    bc_branch_result.loc[:, ['p1', 'p2', 'i1', 'i2']] = bc_branch_result.loc[:, ['p1', 'p2', 'i1', 'i2']].apply(lambda x: abs(x))
    bc_branch_result['P'] = bc_branch_result[['p1', 'p2']].mean(axis=1)
    bc_branch_result = bc_branch_result.drop(columns=['p1', 'p2'])
    bc_branch_result['I'] = bc_branch_result[['i1', 'i2']].mean(axis=1)
    bc_branch_result = bc_branch_result.drop(columns=['i1', 'i2'])
    current_limits = service.network.get_current_limits().reset_index()
    current_limits = current_limits[~current_limits['branch_id'].duplicated(keep='first')]

    bc_branch_result['I_max'] = bc_branch_result.merge(current_limits, on='branch_id', how='left')['value']
    bc_branch_result['Line limit'] = bc_branch_result.merge(line_limits, left_on='equipment', right_on='grid_id', how='left')['default_limit']
    bc_branch_result['I_max'] = bc_branch_result['I_max'].replace('', None)
    bc_branch_result['I_max'] = bc_branch_result['I_max'].fillna(bc_branch_result['Line limit']).astype(float)

    bc_branch_result['I_loading_%'] = bc_branch_result['I'] / bc_branch_result['I_max'] * 100
    bc_branch_result = bc_branch_result.sort_values('I_loading_%', ascending=False)

    substations = service.network.get_substations(all_attributes=True)
    substations.rename(columns={'name': 'substation_name', 'CGMES.regionName': 'area'}, inplace=True)
    voltage_levels = service.network.get_voltage_levels()
    voltage_levels = voltage_levels.merge(substations[['area', 'substation_name']], left_on='substation_id',
                                          right_index=True)

    # Mapping base case load flow results to bus results
    bc_bus_result = service.results.bus_results.reset_index().query("contingency_id == ''")
    bc_bus_result = bc_bus_result.sort_values(by='v_mag', ascending=False)
    bc_bus_result = bc_bus_result.drop_duplicates(subset='voltage_level_id', keep='first').set_index('voltage_level_id')
    bc_bus_result = bc_bus_result.merge(voltage_levels, left_on='voltage_level_id',
                                        right_index=True, how='left')
    bc_bus_result = bc_bus_result.sort_values('name', ascending=True)

    # Remove lines without flow
    bc_branch_result = bc_branch_result[~pd.isna(bc_branch_result['I'])]
    bc_branch_result = bc_branch_result.drop_duplicates(subset='name', keep='first')

    return bc_bus_result, bc_branch_result


def process_opi_violations(service):
    def _set_base_case_violation_flags(violation):
        if violation.limit_type in ['CURRENT', 'HIGH_VOLTAGE']:
            return True if violation.base_case_value_percent >= 100 else False
        elif violation.limit_type == 'LOW_VOLTAGE':
            return True if violation.base_case_value_percent <= 100 else False
        else:
            return None

    opi_violations: pd.DataFrame = service.results.limit_violations.reset_index()
    opi_violations['subject_name'] = opi_violations['subject_id'].map(service.mrid_name_map)

    # Mapping violated element types
    identifiables = service.network.get_identifiables()
    opi_violations = opi_violations.merge(identifiables.rename(columns={'type': 'subject_type'}),
                                          left_on='subject_id', right_index=True, how='left')

    # Mapping contingency element names
    _contingencies_map = service.contingencies_df.set_index('mRID')['name'].drop_duplicates()
    opi_violations['contingency_name'] = opi_violations['contingency_id'].map(_contingencies_map).fillna("")

    # Mapping substation/area to violations on nodes (voltage levels)
    substations = service.network.get_substations(all_attributes=True)
    substations.rename(columns={'name': 'substation_name', 'CGMES.regionName': 'area'}, inplace=True)
    voltage_levels = service.network.get_voltage_levels()
    voltage_levels = voltage_levels.merge(substations[['area', 'substation_name']], left_on='substation_id',
                                          right_index=True)
    opi_violations = opi_violations.merge(voltage_levels[['area', 'substation_name', 'nominal_v']],
                                          left_on='subject_id', right_index=True, how='left')

    # Mapping substation/area to violations on branches (current violations)
    _branches = get_network_elements(network=service.network, element_type=pp.network.ElementType.BRANCH)
    _dangling_lines = get_network_elements(network=service.network, element_type=pp.network.ElementType.DANGLING_LINE)
    _dangling_lines.rename(columns={'substation_name': 'substation_name1', 'CGMES.regionName': 'CGMES.regionName1'}, inplace=True)
    _3w_transformers = get_network_elements(network=service.network, element_type=pp.network.ElementType.THREE_WINDINGS_TRANSFORMER)
    _3w_transformers['type'] = '3W_TRANSFORMER'
    _dangling_lines['type'] = 'DANGLING_LINE'
    _elements = pd.concat([_branches, _dangling_lines, _3w_transformers])
    _relevant_columns = _elements.columns[_elements.columns.str.contains('substation_name|CGMES.regionName|nominal_v', regex=True)]
    for side_key in [x for x in opi_violations.side.unique() if x != ""]:
        side_key_integer = w2n.word_to_num(side_key)
        mask = (opi_violations['side'] == side_key)
        opi_violations.loc[mask, 'area'] = opi_violations[mask].merge(_elements[_relevant_columns], left_on='subject_id', right_index=True)[
                f"CGMES.regionName{side_key_integer}"]
        opi_violations.loc[mask, 'substation_name'] = opi_violations[mask].merge(_elements[_relevant_columns], left_on='subject_id', right_index=True)[
                f"substation_name{side_key_integer}"]
        opi_violations.loc[mask, 'nominal_v'] = opi_violations[mask].merge(_elements[_relevant_columns], left_on='subject_id', right_index=True)[
                f"nominal_v{side_key_integer}"]

    # Mapping base case load flow results to violations
    opi_violations['base_case_value'] = np.nan
    bc_bus_result = service.results.bus_results.reset_index().query("contingency_id == ''")
    bc_bus_result = bc_bus_result.sort_values(by='v_mag', ascending=False)
    bc_bus_result = bc_bus_result.drop_duplicates(subset='voltage_level_id', keep='first').set_index('voltage_level_id')
    opi_violations['base_case_value'] = opi_violations.merge(bc_bus_result['v_mag'], left_on='subject_id', right_index=True, how='left')['v_mag']
    bc_branch_result = service.results.branch_results.reset_index().query("contingency_id == ''").set_index('branch_id')
    bc_3wtr_result = service.results.three_windings_transformer_results.reset_index().query("contingency_id == ''").set_index('transformer_id')
    bc_branch_result = pd.concat([bc_branch_result, bc_3wtr_result])
    for side_key in [x for x in opi_violations.side.unique() if x != ""]:
        side_key_integer = w2n.word_to_num(side_key)
        mask = (opi_violations['side'] == side_key)
        opi_violations.loc[mask, 'base_case_value'] = opi_violations[mask].merge(bc_branch_result[f"i{side_key_integer}"],
                                                                                 left_on='subject_id',
                                                                                 right_index=True)[f"i{side_key_integer}"]

    # Calculate violation value in percentage
    opi_violations['value_percent'] = opi_violations['value'] / opi_violations['limit'] * 100
    opi_violations['base_case_value_percent'] = opi_violations['base_case_value'] / opi_violations['limit'] * 100

    # Setting base case violation flag
    opi_violations.loc[:, 'base_case_violation'] = opi_violations.apply(_set_base_case_violation_flags, axis=1)

    # Filter violations by monitored elements
    mask_monitored_branches = opi_violations['subject_id'].isin([item.equipment for item in service.assessed_elements])

    return opi_violations[mask_monitored_branches]


def create_dataframe_tuple_from_results(results: list, time_horizon: str) -> tuple[list[Any], list[str]]:

    merged_results = []
    run_id = str(uuid.uuid4())
    for result in results:
        timestamp = deque(result).popleft()
        result_list = []
        for df in result:
            new_df = df.copy()
            new_df['@scenario_timestamp'] = timestamp.isoformat()
            new_df['@time_horizon'] = time_horizon
            new_df['@run_id'] = run_id
            new_df['@version'] = 0
            new_df['week_day'] = timestamp.weekday()
            opi_week = get_opi_week_number(timestamp)
            new_df['opi_week'] = opi_week
            opi_month = str(timestamp.month)
            new_df['opi_month'] = opi_month
            if time_horizon == "WK":
                # Keep last week of the year in previous year if timestamp is after new year
                if opi_week in ['52','53'] and opi_month == "1":
                    new_df['opi_year'] = str(timestamp.year - 1)
                else:
                    new_df['opi_year'] = str(timestamp.year)
            else:
                new_df['opi_year'] = str(timestamp.year)
            result_list.append(new_df)
        merged_results.append(result_list)
    config = configparser.RawConfigParser()
    config.read("results.ini")
    DATAFRAME_NAMES = [config['names']['dataframe_names']]

    return merged_results, DATAFRAME_NAMES


def get_opi_week_number(timestamp: pd.Timestamp) -> str:
    if timestamp.weekday() == 4:
        last_friday = timestamp - pd.Timedelta(days=7)
    else:
        days_ago = (timestamp.weekday() - 4) % 7
        last_friday = timestamp - pd.Timedelta(days=days_ago)
    week_number = last_friday.isocalendar().week

    return str(week_number)


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
