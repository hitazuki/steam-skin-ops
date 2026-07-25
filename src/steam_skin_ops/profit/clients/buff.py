"""
BUFF API 客户端
拉取 CS2 / DOTA2 的买单历史记录
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger(__name__)

# BUFF 游戏标识映射
GAME_IDS = {
    "csgo": "csgo",
    "dota2": "dota2",
}

# BUFF 订单类型
ORDER_TYPE_BUY = "buy"   # 买单（我方买入）
ORDER_TYPE_SELL = "sell" # 卖单（我方卖出，不统计）


class BuffClient:
    """
    BUFF 交易记录客户端
    使用账号 Cookie 拉取个人买单历史
    """

    BASE_URL = "https://buff.163.com"

    def __init__(self, cookie: str, page_size: int = 20, max_pages: int = 100):
        self.cookie = cookie
        self.page_size = min(page_size, 20)  # BUFF 最大 20
        self.max_pages = max_pages
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Referer": "https://buff.163.com/market/bill_order",
            "Cookie": self.cookie,
        })

    # ------------------------------------------------------------------
    # 公共接口
    # ------------------------------------------------------------------

    def fetch_buy_orders(
        self,
        game: str,
        cache_path: Path | None = None,
        force_refresh: bool = False,
        incremental: bool = False,
    ) -> list[dict]:
        """
        拉取指定游戏的全部买单历史

        Args:
            game: 游戏标识，csgo 或 dota2
            cache_path: 本地缓存文件路径（若存在且未过期则直接读取）

        Returns:
            买单列表，每条记录格式见 `_parse_order`
        """
        game = GAME_IDS.get(game, game)

        cached_orders: list[dict] = []
        if cache_path and cache_path.exists() and not force_refresh:
            logger.info("[BUFF] 读取缓存: %s", cache_path)
            with open(cache_path, encoding="utf-8") as f:
                cached_orders = json.load(f)
            if not force_refresh and not incremental:
                return cached_orders

        cached_ids = {
            str(order.get("id", ""))
            for order in cached_orders
            if order.get("id")
        }

        logger.info("[BUFF] 开始拉取 %s 买单历史...", game)
        orders: list[dict] = []

        for page in range(1, self.max_pages + 1):
            page_orders, has_more = self._fetch_page(game, page)
            reached_cache = False
            for order in page_orders:
                if incremental and str(order.get("id", "")) in cached_ids:
                    reached_cache = True
                    break
                orders.append(order)
            logger.info("[BUFF] %s 第 %d 页，获取 %d 条，累计 %d 条",
                        game, page, len(page_orders), len(orders))

            if reached_cache or not has_more:
                break
            time.sleep(1.0)  # 礼貌性延迟，避免触发频率限制

        if incremental and cached_orders:
            new_count = len(orders)
            orders = self._merge_orders(orders, cached_orders)
            logger.info(
                "[BUFF] %s 增量拉取完成，新增 %d 条，合并后 %d 条",
                game,
                new_count,
                len(orders),
            )
        else:
            logger.info("[BUFF] %s 买单历史拉取完成，共 %d 条", game, len(orders))

        if cache_path:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(orders, f, ensure_ascii=False, indent=2)
            logger.info("[BUFF] 已缓存到 %s", cache_path)

        return orders

    @staticmethod
    def _merge_orders(new_orders: list[dict], cached_orders: list[dict]) -> list[dict]:
        """按记录 ID 合并，保留接口返回的从新到旧顺序。"""
        merged: list[dict] = []
        seen: set[str] = set()
        for order in [*new_orders, *cached_orders]:
            record_id = str(order.get("id", ""))
            if record_id and record_id in seen:
                continue
            if record_id:
                seen.add(record_id)
            merged.append(order)
        return merged

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _fetch_page(self, game: str, page_num: int) -> tuple[list[dict], bool]:
        """
        拉取单页买单记录
 
        Returns:
            (当前页订单列表, 是否还有下一页)
        """
        url = f"{self.BASE_URL}/api/market/buy_order/history"
        params = {
            "game": game,
            "page_num": page_num,
            "page_size": self.page_size,
        }

        try:
            resp = self.session.get(url, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            logger.error("[BUFF] 请求失败 (page=%d): %s", page_num, e)
            return [], False
        except json.JSONDecodeError as e:
            logger.error("[BUFF] JSON 解析失败 (page=%d): %s", page_num, e)
            return [], False

        if data.get("code") != "OK":
            logger.error("[BUFF] API 返回错误: %s", data.get("error", data))
            return [], False

        payload = data.get("data", {})
        raw_items: list[dict] = payload.get("items", [])
        total_count: int = payload.get("total_count", 0)
        goods_infos: dict = payload.get("goods_infos", {})

        # 仅保留已成交的买单 (SUCCESS)
        buy_orders = [
            self._parse_order(item, game, goods_infos)
            for item in raw_items
            if item.get("state") == "SUCCESS"
        ]

        fetched_so_far = page_num * self.page_size
        has_more = fetched_so_far < total_count and len(raw_items) == self.page_size

        return buy_orders, has_more

    @staticmethod
    def _parse_order(item: dict, game: str, goods_infos: dict) -> dict:
        """
        将 BUFF 原始订单字段标准化
 
        标准化字段：
            id          : 订单ID
            game        : 游戏（csgo/dota2）
            name        : 物品名称（英文）
            name_zh     : 物品名称（中文，若有）
            price_cny   : 买入价（CNY，float）
            quantity    : 数量
            created_at  : 成交时间（datetime）
            order_no    : BUFF 订单号
            source      : 数据来源标记 "buff"
        """
        # 成交时间优先用 transact_time，其次用 created_at 或者是 updated_at
        ts = item.get("transact_time") or item.get("created_at") or item.get("updated_at", 0)
        try:
            created_at = datetime.fromtimestamp(int(ts))
        except (ValueError, TypeError):
            created_at = datetime.min

        # 价格：优先使用 real_price 或 price，单位为元
        try:
            price_cny = float(item.get("real_price") or item.get("price", 0))
        except (ValueError, TypeError):
            price_cny = 0.0

        goods_id = str(item.get("goods_id", ""))
        goods_info = goods_infos.get(goods_id, {})

        return {
            "id": str(item.get("id", "")),
            "game": game,
            "name": goods_info.get("market_hash_name") or item.get("market_hash_name", ""),
            "name_zh": goods_info.get("name") or item.get("name", ""),
            "price_cny": price_cny,
            "quantity": int(item.get("num", 1)),
            "created_at": created_at.isoformat(),
            "order_no": str(item.get("id", "")),
            "source": "buff",
            "buyer_steamid": str(item.get("buyer_steamid", "")),
        }



    def check_login(self) -> bool:
        """检查 Cookie 是否有效（访问用户信息接口）"""
        try:
            resp = self.session.get(
                f"{self.BASE_URL}/account/api/user/info",
                timeout=10
            )
            data = resp.json()
            if data.get("code") == "OK":
                username = data.get("data", {}).get("nickname", "未知")
                logger.info("[BUFF] 登录成功，用户：%s", username)
                return True
            logger.error(
                "[BUFF] Cookie 无效或已过期，请更新 config/profit.yaml 中的 buff.cookie"
            )
            return False
        except Exception as e:
            logger.error("[BUFF] 登录检查失败: %s", e)
            return False
