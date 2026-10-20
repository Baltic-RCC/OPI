import logging
import pandas as pd
from io import BytesIO
import pypowsybl as pp
from zipfile import ZipFile, ZIP_DEFLATED
from pypowsybl.network import ElementType
from loadflow_engine.helper import get_network_elements
from opi.constants import CROSS_BORDER_DATA
from numpy import nan, sign, isnan


logger = logging.getLogger(__name__)


def unzip_zipped_profiles(path_or_buffer: str):
    """
    Extracts zipped profiles of a model.
    Detects automatically if the zipped folder is read from file bath or BytesIO buffer.
    :param path_or_buffer: path of zipped model or BytesIO buffer
    :return: output_zip_buffer
    """
    # Checks if path_or_buffer is string
    if isinstance(path_or_buffer, str):
        with open(path_or_buffer, 'rb') as original_zip_file:
            original_zip_buffer = BytesIO(original_zip_file.read())

    # Checks if path_or_buffer is BytesIO class
    elif isinstance(path_or_buffer, BytesIO):
        original_zip_buffer = path_or_buffer
    else:
        logger.error(f"Provided variable is nor string nor BytesIO object")

    output_zip_buffer = BytesIO()

    # Read the original zipped folder from provided buffer
    with ZipFile(original_zip_buffer, 'r') as original_zip:
        # Create a new zip file where we will store the unzipped XMLs
        with ZipFile(output_zip_buffer, 'w', ZIP_DEFLATED) as new_zip:
            # Iterate trough each file in the original zip
            for file_name in original_zip.namelist():
                # Read the zipped xml file
                with original_zip.open(file_name) as zipped_xml_file:
                    with ZipFile(zipped_xml_file) as zipped_xml:
                        # Extract each file and add it to the new zip
                        for xml_file_name in zipped_xml.namelist():
                            xml_data = zipped_xml.read(xml_file_name)
                            # Add unzipped XML file to the new zip
                            new_zip.writestr(xml_file_name, xml_data)

    output_zip_buffer.seek(0)

    return output_zip_buffer


def load_network(network, parameters):

    logger.info("Loading model into pypowsybl")

    if bytes == type(network):
        try:
            network = unzip_zipped_profiles(BytesIO(network))
        except Exception as error:
            logger.error(error)
        network = pp.network.load_from_binary_buffer(network, parameters=parameters)
    if str == type(network):
        try:
            network = unzip_zipped_profiles(BytesIO(network))
            network = pp.network.load_from_binary_buffer(network, parameters=parameters)
        except Exception as error:
            logger.error(error)
            network = pp.network.load(network, parameters=parameters)

    return network


def get_power_losses(network: pp.network, by_region: bool = False, present_igms: list = None):
    """
    Gets power losses in MegaWatts from the model
    :param network: loadflow_engine2.network.load()
        Network created in PyPowSyBl using pp.network.load()
    :param by_region: bool, default False
        True: calculates power losses to each area and for tie lie lines separately
        False: calculates the power losses for whole model
    :param present_igms: list, default None
        Cross-borders that are available in the model.
        Note: Check from loadflow_engine2.capacity.py get_present_igms()
    :returns:
        DataFrame
            by_region = True -> a 1xn DataFrame where n represents the areas + tie-lines and the rows consist of power
            losses
        float
            by_region = False -> a float value of the whole area's power losses
    """

    # Reads and calculates losses on lines
    lines = get_network_elements(network=network, element_type=pp.network.ElementType.LINE)
    lines['P_loss'] = abs(abs(lines.p1) - abs(lines.p2))
    P_loss_line = lines['P_loss'].sum()

    tie_lines = get_network_elements(network=network, element_type=pp.network.ElementType.TIE_LINE)
    dang = get_network_elements(network=network, element_type=pp.network.ElementType.BOUNDARY_LINE)
    tie_lines_and_dang1 = tie_lines.merge(dang[['p']], left_on='dangling_line1_id', right_index=True).rename(
        columns={'p': 'p1'})
    tie_lines_and_dang1_and_dang2 = tie_lines_and_dang1.merge(dang[['p']], left_on='dangling_line2_id',
                                                              right_index=True).rename(columns={'p': 'p2'})
    tie_lines_and_dang1_and_dang2['P_losses'] = tie_lines_and_dang1_and_dang2[['p1', 'p2']].abs().max(axis=1) - \
                                                tie_lines_and_dang1_and_dang2[['p1', 'p2']].abs().min(axis=1)
    P_loss_tie_line = tie_lines_and_dang1_and_dang2['P_losses'].sum()

    # Reads and calculates losses on two winding transformers
    transformers_two = get_network_elements(network=network,
                                            element_type=pp.network.ElementType.TWO_WINDINGS_TRANSFORMER)
    transformers_two['P_loss'] = abs(abs(transformers_two.p1) - abs(transformers_two.p2))
    P_loss_trans_two = transformers_two['P_loss'].sum()

    # Reads and calculates losses on three winding transformers
    transformers_three = get_network_elements(network=network,
                                              element_type=pp.network.ElementType.THREE_WINDINGS_TRANSFORMER)
    transformers_three['P_loss'] = transformers_three.p1 + transformers_three.p2 + transformers_three.p3
    P_loss_trans_three = transformers_three['P_loss'].sum()

    system_power_losses = (P_loss_line + P_loss_tie_line + P_loss_trans_two + P_loss_trans_three)

    if by_region:
        all_elements = pd.concat([lines, transformers_two, transformers_three])
        all_region_losses_dict = {}

        for region in present_igms:
            region_losses = all_elements[all_elements["CGMES.regionName1"] == region]['P_loss'].sum()
            all_region_losses_dict[region] = pd.DataFrame({region: [region_losses]})

        all_region_losses_dict['Tie_lines'] = pd.DataFrame({'Tie_lines': [P_loss_tie_line]})
        df_losses = pd.concat(all_region_losses_dict.values(), axis=1)

        return round(df_losses, 2)

    return round(system_power_losses, 2)


def get_xborder_flows(network, cross_border: str, logger_output: bool = True):
    """
    Returns a specific cross-border's lines flows values.
    :param network, loadflow_engine2 network instance
    :param cross_border: str
        A string of the cross-border.
    :param logger_output: bool, default True.
        True: shows a summary of the calculation in logger
        False: does not show any logger
    :return:
        Pandas dataframe
    """
    # Extract the lists based on the cross border
    xnodes = CROSS_BORDER_DATA[cross_border]['XNODES']
    line_names = CROSS_BORDER_DATA[cross_border]['LINE_NAMES']

    # Reading a line. Xborder lines are under dangling lines
    dangling_no_voltage = get_network_elements(network=network, element_type=ElementType.BOUNDARY_LINE).reset_index()
    # Merges buses to determine the voltage
    buses = network.get_buses()
    dangling_lines_no_limits = dangling_no_voltage.merge(buses,
                                                         how='inner',
                                                         left_on='voltage_level_id',
                                                         right_on='voltage_level_id')
    # Merges operational limits to get the current limit
    # Index is element_id only in older pypowsybl, (element_id, side, type, acceptable_duration, group_name) in 1.16
    op_limits = network.get_operational_limits().reset_index()
    op_limits = op_limits[(op_limits['name'] == 'permanent_limit') & (op_limits['type'] == 'CURRENT')]
    op_limits = op_limits.drop_duplicates(subset='element_id', keep='first').set_index('element_id')

    dangling_lines = dangling_lines_no_limits.merge(op_limits['value'], how='left', left_on='id', right_index=True)

    # Determines up and down regions
    if 'Estonia' in network.get_substations(all_attributes=True)['CGMES.regionName'].unique():
        up, down = cross_border_region_name_check(cross_border, ee_cgmes_region_name='Estonia')
    else:
        up, down = cross_border_region_name_check(cross_border)

    # List for later appending
    p_list = []
    i_list = []
    i_limit_list = []

    if logger_output:
        logger.info(f"Reading {cross_border} cross-border results:")

    for node, line_name in zip(xnodes, line_names):
        # sorts dangling lines for specific nodes
        df = dangling_lines.loc[dangling_lines['ucte_xnode_code'] == node]

        # Check for duplicates
        duplicates = df[df.duplicated(subset='CGMES.regionName', keep=False)]
        if not duplicates.empty:
            duplicates_groups = duplicates.groupby('CGMES.regionName').size()
            for region, count in duplicates_groups.items():
                if logger_output:
                    logger.warning(f"For {line_name}, {count} duplicates were found in region {region}")
            df = df.drop_duplicates(subset='CGMES.regionName')

        # Checks if line is disconnected
        try:
            # Determines flow direction. Minus sign means flows towards the node
            p_up = df[(df['CGMES.regionName'] == up)].iloc[0]['p']
            p_down = df[(df['CGMES.regionName'] == down)].iloc[0]['p']

            # Determines current limit
            i = min(df[(df['CGMES.regionName'] == up)].iloc[0]['i'],
                    df[(df['CGMES.regionName'] == down)].iloc[0]['i'])
            i_limit = min(df[(df['CGMES.regionName'] == up)].iloc[0]['value'],
                          df[(df['CGMES.regionName'] == down)].iloc[0]['value'])

            # If sign of p_up is positive, then flow is according to the direction of cross_border(e.g. LT-LV)
            direction = sign(p_up)
            p = min(abs(p_up), abs(p_down)) * direction
            i_loading = i / i_limit

            # Gets voltage and it's limits
            v = max(df['v_mag'])  # gets maximum V from both nodes (for limits)
            v_min = min(df['low_voltage_limit'])  # gets minimum voltage value
            v_max = max(df['high_voltage_limit'])  # gets maximum voltage value

            if logger_output:
                logger.info(
                    f"\t\tThe CGMES.regionName taken for p_up is "
                    f"{df[(df['CGMES.regionName'] == up)].iloc[0]['CGMES.regionName']}")

        except IndexError:
            p = nan
            i = nan
            i_limit = nan
            i_loading = nan
            v = nan
            v_min = nan
            v_max = nan
            logger.warning(f"\tLine {line_name} was not found in region {down}")

        status = dangling_lines.loc[dangling_lines['ucte_xnode_code'] == node, 'connected']

        # checks if filtered dataframe has nan
        if not isnan(p):
            p_list.append(p)
            if not (isnan(i) or isnan(i_limit)):
                i_list.append(i)
                i_limit_list.append(i_limit)

            if logger_output:
                logger.info(f"\t\tLine {line_name} "
                            f"P = {round(p, 2)} MW | "
                            f"i_loading = {round(i_loading, 2)} % "
                            f"V = {round(v, 2)} kV | "
                            f"V_max_limit = {round(v_max,2)} kV | "
                            f"V_min_limit = {round(v_min, 2)} kV")
        else:
            p_list.append(0)
            if logger_output:
                try:
                    logger.error(f"\t\tLine {line_name} was not computed. Connection status of line nodes: "
                                 f"{status.iat[0], status.iat[1]}")
                except IndexError:
                    logger.error(f" One of the line nodes is missing")
                    logger.error(f"\t\tLine {line_name} was not computed")

    # Gets sum of all Xborder lines
    x_border_flow = sum(p_list)
    x_border_flow = direction_check_hvdc(cross_border=cross_border, xborder_flow=x_border_flow)

    sum_i = sum(i_list)
    sum_i_limit = sum(i_limit_list)
    try:
        x_border_limit = sum_i / sum_i_limit
    except ZeroDivisionError:
        x_border_limit = nan

    if logger_output:
        logger.info(f"\t\tCross-border flow is {round(x_border_flow, 2)} MW")
        logger.info(f"\t\tCross-border capacity limit is {round(x_border_limit, 2)} %")

        direction = sign(x_border_flow)
        up_dir, down_dir = cross_border.split('-')
        if direction >= 0:
            logger.info(f"\t\tFlow direction is {up_dir}-{down_dir}")
        else:
            logger.info(f"\t\tFlow direction is {down_dir}-{up_dir}")

    border_flows = pd.DataFrame(p_list, index=line_names, columns=['P_MW'])

    return x_border_flow, x_border_limit, border_flows


def cross_border_region_name_check(border: str, ee_cgmes_region_name: str = 'EE'):
    """
    Redefines the cross-border areas which names are defined differently in the model e.g. EE is defined as Estonia.
    Also used for reading results from HVDC links due to CIM modeling.
    :param border: str
        A cross-border e.g. 'EE-FI'
    :param ee_cgmes_region_name: str
        Sometimes cgmes region name can be 'EESTI'
    :returns:
        tuple
            Strings where the import and export regions are separated from the cross-border string and defined to areas
            definitions found in the model e.g. cross-border = 'EE-FI' -> up = 'Estonia', down = 'Estonia'
            up: first area of the cross-border
            down: second area of the cross-border
    """
    up, down = border.split('-')

    up_map = {
        'EE': ee_cgmes_region_name,
        'FI': ee_cgmes_region_name,
        'SE': 'LT'}

    down_map = {
        'EE': ee_cgmes_region_name,
        'FI': ee_cgmes_region_name,
        'SE': 'LT'}

    if up in up_map.keys():
        up = up_map[up]

    if down in down_map.keys():
        down = down_map[down]

    return up, down


def direction_check_hvdc(cross_border, xborder_flow):
    """
    Function to adjust the sign according to the naming and flow direction convention for HVDCs (if positive, flows
    according to described cross_border.
    E.g. If FI-EE flow is 100 MW, it flows from FI to EE, if flow is -100 MW, flows from EE to FI
    :param cross_border: cross border
    :param xborder_flow: calculated flow on the described cross border
    :return:
    """
    border_map = {
        'EE-FI': xborder_flow,
        'FI-EE': xborder_flow * -1,
        'LT-SE': xborder_flow,
        'SE-LT': xborder_flow * -1}

    if cross_border in border_map.keys():
        flow = border_map[cross_border]
    else:
        flow = xborder_flow

    return flow


def get_system_balance(network: pp.network.Network, areas: list, border_flows: pd.DataFrame) -> pd.DataFrame:
    result_df = pd.DataFrame(index=areas,
                             columns=['P Load MW', 'P Gen MW', 'P Losses MW', 'HVDC flow', 'ACNP'])

    generation = get_network_elements(network, element_type=pp.network.ElementType.GENERATOR)
    load = get_network_elements(network, element_type=pp.network.ElementType.LOAD)
    p_losses = get_power_losses(network, present_igms=areas,
                                by_region=True).drop('Tie_lines', axis=1).T

    result_df['P Losses MW'] = p_losses
    for tso in areas:
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


def get_ba_cross_border_flows(network: pp.network.Network, areas: list) -> pd.DataFrame:
    result_df = pd.DataFrame()
    cross_borders = []
    if 'LT' in areas:
        cross_borders.extend(['LT-PL', 'LT-LV', 'LT-SE'])
    if 'LV' in areas:
        cross_borders.extend(['LV-LT', 'LV-EE'])
    if any(area in areas for area in ('Estonia', 'EE', 'EESTI')):
        cross_borders.extend(['EE-LV', 'EE-FI'])

    for border in cross_borders:
        flow, limit, bf_df = get_xborder_flows(network, cross_border=border, logger_output=False)
        bf_df['Cross_border'] = border
        result_df = pd.concat([result_df, bf_df])

    return result_df.reset_index(names=['name']).drop_duplicates(subset='name')
