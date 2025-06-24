# -*- coding: utf-8 -*-
import logging
import requests
import json
import os
import azure.functions as func
from azure.storage.blob import BlobServiceClient

HSREPLAY_API_URL = "https://hsreplay.net/api/v1/battlegrounds/trinkets/?BattlegroundsMMRPercentile=TOP_50_PERCENT&BattlegroundsTimeRange=LAST_7_DAYS"
CARD_DATA_URL = "https://api.hearthstonejson.com/v1/latest/zhCN/cards.json"

CONNECTION_STRING = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
CONTAINER_NAME = os.environ.get("STORAGE_CONTAINER_NAME", "hearthstone-data")
CARD_DATA_BLOB_NAME = "cards_zhCN.json"
PROCESSED_DATA_BLOB_NAME = "latest_trinket_stats.json"

def get_card_data_and_map(blob_service_client: BlobServiceClient, force_update=False):
    """
    从 Blob Storage 或 API 获取卡牌数据，并返回 dbfId 映射表。
    如果数据是从 API 新获取的，则会上传到 Blob Storage。
    """
    container_client = blob_service_client.get_container_client(CONTAINER_NAME)
    blob_client = container_client.get_blob_client(CARD_DATA_BLOB_NAME)
    
    if not force_update and blob_client.exists():
        logging.info(f"从 Blob Storage '{CONTAINER_NAME}/{CARD_DATA_BLOB_NAME}' 加载卡牌数据。")
        try:
            downloader = blob_client.download_blob(max_concurrency=1, encoding='UTF-8')
            card_data = json.loads(downloader.readall())
            card_map = {card['dbfId']: card for card in card_data if 'dbfId' in card}
            return card_map, False # False 表示数据未从API更新
        except Exception as e:
            logging.warning(f"从 Blob 读取失败: {e}。将尝试从 API 重新下载。")

    logging.info("从 API 下载最新的卡牌数据...")
    try:
        response = requests.get(CARD_DATA_URL)
        response.raise_for_status()
        card_data = response.json()
        
        logging.info(f"正在将新卡牌数据上传到 Blob Storage...")
        blob_client.upload_blob(json.dumps(card_data, ensure_ascii=False), overwrite=True)

        card_map = {card['dbfId']: card for card in card_data if 'dbfId' in card}
        return card_map, True
    except requests.exceptions.RequestException as e:
        logging.error(f"无法从 API 下载卡牌数据: {e}")
        return None, False

def get_trinket_stats():
    """从 HSReplay API 获取饰品统计数据。"""
    headers = {
        'Accept': 'application/json, text/plain, */*',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36'
    }
    logging.info("正在从 HSReplay API 获取饰品统计数据...")
    try:
        response = requests.get(HSREPLAY_API_URL, headers=headers)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        logging.error(f"无法从 HSReplay API 获取数据: {e}")
        return None

def main(mytimer: func.TimerRequest) -> None:
    if mytimer.past_due:
        logging.info('The timer is past due!')
    logging.info('Python timer trigger function executed.')

    if not CONNECTION_STRING:
        logging.error("AZURE_STORAGE_CONNECTION_STRING 环境变量未设置。")
        return func.HttpResponse("服务器配置错误。", status_code=500)

    try:
        blob_service_client = BlobServiceClient.from_connection_string(CONNECTION_STRING)
    except Exception as e:
        logging.error(f"无法连接到 Blob Storage: {e}")
        return func.HttpResponse("无法连接到存储服务。", status_code=500)
        
    card_map, was_updated = get_card_data_and_map(blob_service_client)
    if not card_map:
        return func.HttpResponse("无法获取卡牌数据。", status_code=500)

    trinket_stats = get_trinket_stats()
    if not trinket_stats:
        return func.HttpResponse("无法获取饰品统计数据。", status_code=500)
        
    missing_trinkets_exist = any(t.get("trinket_dbf_id") not in card_map for t in trinket_stats)
    if missing_trinkets_exist and not was_updated:
        logging.info("检测到缺失的饰品，正在强制更新卡牌数据...")
        card_map, _ = get_card_data_and_map(blob_service_client, force_update=True)
        if not card_map:
             return func.HttpResponse("尝试更新卡牌数据失败。", status_code=500)

    processed_trinkets = []
    for trinket in trinket_stats:
        dbf_id = trinket.get("trinket_dbf_id")
        card_info = card_map.get(dbf_id)
        
        if card_info:
            placement_dist = {f"{i+1}st": val for i, val in enumerate(trinket.get("final_placement_distribution", []))}
            combined_data = {
                "name": card_info.get("name"),
                "avg_placement": trinket.get("avg_final_placement"),
                "pick_rate": f"{trinket.get('pick_rate', 0):.2f}%",
                "tier": trinket.get("tier", "N/A").upper(),
                "group": trinket.get("group"),
                "text": card_info.get("text", "").replace("\\n", " ").replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", ""),
                "placement_dist": placement_dist,
                "dbf_id": dbf_id,
            }
            processed_trinkets.append(combined_data)
        else:
            logging.warning(f"即使更新后，仍未能在卡牌数据中找到 ID 为 {dbf_id} 的饰品。")
            
    sorted_trinkets = sorted(processed_trinkets, key=lambda x: x["avg_placement"])
    
    try:
        container_client = blob_service_client.get_container_client(CONTAINER_NAME)
        container_client.upload_blob(
            name=PROCESSED_DATA_BLOB_NAME,
            data=json.dumps(sorted_trinkets, ensure_ascii=False, indent=2),
            overwrite=True
        )
        logging.info(f"成功将处理后的数据保存到 '{PROCESSED_DATA_BLOB_NAME}'。")
    except Exception as e:
        logging.error(f"上传处理结果到 Blob Storage 失败: {e}")
