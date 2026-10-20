import logging
import config
from datetime import datetime
from brcc_apis.elk_api import Elastic
from opi.common import parse_app_properties
from brcc_apis.minio_api import ObjectStorage
from brcc_apis import settings_manager

logger = logging.getLogger(__name__)
parse_app_properties(caller_globals=globals(), path=config.paths.integrations.elastic)
parse_app_properties(caller_globals=globals(), path=config.paths.integrations.minio)


minio_service = ObjectStorage(server=OBJECTSTORAGE_URL, username=OBJECTSTORAGE_USERNAME, password=OBJECTSTORAGE_PASSWORD)
elastic_service = Elastic(server=ELK_SERVER, api_key=ELK_API_KEY)


def query_data(metadata_query: dict, query_filter: str = None, index: str = ELASTIC_QUERY_INDEX, size: str = '10000', sort: dict=None, scroll='1m'):
    """
    Queries Elasticsearch based on provided metadata queries.

    Args:
        metadata_query (dict): A dictionary containing metadata fields and their values to be queried.
        query_filter (dict): Optional. A dictionary specifying parameters by which to filter the query.
        index (str): The index to query data from. Defaults to ELASTIC_QUERY_INDEX from configs variables.

    Returns:
        list: A list of dictionaries containing the retrieved content from Elasticsearch.

    Note:
        The function constructs an Elasticsearch query based on the provided metadata_query.
        It retrieves data from the specified index and processes the response to extract content.

    Example:
        To query data with metadata fields 'TSO' and 'timeHorizon':
        >>> metadata_query = {"pmd:TSO": "TERNA", "pmd:timeHorizon": "2D"}
        ... response = query_data(metadata_query)
    """

    # Create elastic query syntax
    # {
    #     "bool": {
    #         "must": [
    #             {"match": {"pmd:TSO": "TERNA"}},
    #             {"terms": {"pmd:timeHorizon": ["01", "02"]}}
    #         ]
    #     }
    # }


    match_and_term_list = []
    for key, value in metadata_query.items():
        if isinstance(value, list):
            match_and_term_list.append({"terms": {key: value}})
        else:
            match_and_term_list.append({"match": {key: value}})

    if query_filter:
        query = {"bool": {"must": match_and_term_list, "filter": {"range": {"pmd:creationDate": {"gte": query_filter}}}}}
    else:
        query = {"bool": {"must": match_and_term_list}}

    # Return query results
    response = elastic_service.client.search(index=index, query=query, size=size, sort=sort, scroll=scroll)
    hits = response["hits"]["hits"]
    content_list = [content["_source"] for content in hits]
    if scroll:
        scroll_id = response['_scroll_id']
        while len(hits) > 0:
            response = elastic_service.client.scroll(scroll_id=scroll_id, scroll=scroll)
            hits = response["hits"]["hits"]
            if hits:
                content_list.extend([content["_source"] for content in hits])
        elastic_service.client.clear_scroll(scroll_id=response['_scroll_id'])

    return content_list


def download_models_from_minio(minio_bucket=OBJECTSTORAGE_BUCKET, minio_folder=OBJECTSTORAGE_FOLDER, minio_file_list: list = None):

    content_list = []
    if minio_file_list:
        for file in minio_file_list:
            content = minio_service.download_object(minio_bucket, f'{minio_folder}/{file}')
            content_list.append(content)
    else:
        for reference in minio_folder:
            content = minio_service.download_object(minio_bucket, reference)
            content_list.append(content)

    return content_list


def send_excel_to_minio(excel_file, time_horizon):
    excel_file.name = f"OPI/results/{time_horizon}/{excel_file.name}"
    minio_service.upload_object(file_path_or_file_object=excel_file, bucket_name=DATA_EXPORT_BUCKET)
    logger.info(f"Sent results to Minio -> {DATA_EXPORT_BUCKET}/{excel_file.name}")


def get_all_minio_objects(bucket, folder):
    """Lists objects in folder with the scenario time from their name, without downloading; other names are skipped."""
    objects = []
    for item in minio_service.list_objects(bucket_name=bucket, prefix=folder):
        name = item.object_name.split('/')[-1]
        try:
            ts = datetime.strptime(name.split('.')[0][11:].split('Z')[0], "%Y%m%dT%H%M")
        except ValueError:
            logger.warning(f"Skipping MinIO object with unexpected name: {item.object_name}")
            continue
        objects.append({"@scenario_timestamp": ts, "name": name, "object_name": item.object_name,
                        "last_modified": item.last_modified})
    return objects


def get_docs_by_query(index, query, size=10000, return_df=True):
    logging.getLogger('elastic_transport.transport').disabled = True
    return elastic_service.get_docs_by_query(index=index, query=query, size=size, return_df=return_df)


def get_lf_settings_manager(elk_server, lf_parameters_key):

    lf_settings_manager = settings_manager.LoadflowSettingsManager(
        elastic_server=elk_server,
        settings_keyword=lf_parameters_key,
        elastic_api_key=ELK_API_KEY
    )

    return lf_settings_manager


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
