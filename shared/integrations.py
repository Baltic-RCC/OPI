import logging
from opi import configs
from datetime import datetime
from brcc_apis.elk_api import Elastic
from rdflib.plugins.sparql.operators import datetime
from opi.shared.config_parser import parse_app_properties
from brcc_apis.minio_api import ObjectStorage
from brcc_apis import settings_manager

logger = logging.getLogger(__name__)
parse_app_properties(caller_globals=globals(), path=configs.paths.integrations.elastic)
parse_app_properties(caller_globals=globals(), path=configs.paths.integrations.minio)


minio_service = ObjectStorage(server=OBJECTSTORAGE_URL, username=OBJECTSTORAGE_USERNAME, password=OBJECTSTORAGE_PASSWORD)
elastic_service = Elastic(server=ELK_SERVER, api_key=ELK_API_KEY)


def query_data(metadata_query: dict, query_filter: str = None, index: str = ELASTIC_QUERY_INDEX, return_payload: bool = False, size: str = '10000', sort: dict=None, scroll='1m'):
    """
    Queries Elasticsearch based on provided metadata queries.

    Args:
        metadata_query (dict): A dictionary containing metadata fields and their values to be queried.
        query_filter (dict): Optional. A dictionary specifying parameters by which to filter the query.
        index (str): The index to query data from. Defaults to ELASTIC_QUERY_INDEX from configs variables.
        return_payload (bool): Optional. If True, retrieves the full content for each hit.
            Defaults to False.

    Returns:
        list: A list of dictionaries containing the retrieved content from Elasticsearch.

    Note:
        The function constructs an Elasticsearch query based on the provided metadata_query.
        It retrieves data from the specified index and processes the response to extract content.

    Example:
        To query data with metadata fields 'TSO' and 'timeHorizon' and return payload:
        >>> metadata_query = {"pmd:TSO": "TERNA", "pmd:timeHorizon": "2D"}
        ... response = query_data(metadata_query, return_payload=True)
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

    if return_payload:
        for num, item in enumerate(content_list):
            content_list[num] = get_content(item)

    return content_list


def get_content(metadata: dict, bucket_name=OBJECTSTORAGE_BUCKET):
    """
    Retrieves content data from MinIO based on metadata information.

    Args:
        metadata (dict): A dictionary containing metadata information.
        bucket_name (str): The name of the MinIO bucket to fetch data from.
            Defaults to MINIO_BUCKET_NAME from configs variables.

    Returns:
        list: A list of dictionaries representing content components with updated 'DATA' field.

    Note:
        It expects metadata to contain 'opde:Component' information.
        For each component, it downloads data from MinIO and updates the 'DATA' field in the component dictionary.
    """

    logger.info(f"Getting data from MinIO")
    for component in metadata["opde:Component"]:
        content_reference = component.get("opdm:Profile").get("pmd:content-reference")
        logger.info(f"Downloading {content_reference}")
        component["opdm:Profile"]["DATA"] = minio_service.download_object(bucket_name, content_reference)

    return metadata


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
    response = minio_service.list_objects(bucket_name=bucket, prefix=folder)
    file_list = [item.object_name for item in response]
    object_data = download_models_from_minio(minio_bucket=bucket, minio_folder=file_list)
    ts_list = [datetime.strptime(t.split('/')[-1].split('.')[0][11:].split('Z')[0], "%Y%m%dT%H%M") for t in file_list]
    name_list = [t.split('/')[-1] for t in file_list]
    objects = [{"@scenario_timestamp": ts, "DATA": data, "name": name} for ts, data, name in zip(ts_list, object_data, name_list)]
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
