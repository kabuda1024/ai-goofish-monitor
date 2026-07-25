"""Mercari(日本站)JSON 解析器。

产出的字典键名与闲鱼一致(中文 key),下游 result_storage / notification / AI 层零改动。
Mercari 价格单位为日元整数,前面加 `¥` 符号即可(通用 UI 显示保持)。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from src.scrapers.mercari.constants import (
    ITEM_CONDITION_LABELS,
    ITEM_PAGE_URL_TEMPLATE,
    SHIPPING_PAYER_SELLER,
)


def _get(source: Any, *keys: str, default: Any = None) -> Any:
    """安全访问嵌套字典。"""
    current = source
    for key in keys:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
        if current is None:
            return default
    return current


def _format_price(value: Any) -> str:
    """Mercari 价格是日元整数,格式化为 '¥12,345' 字符串。"""
    try:
        price = int(value)
    except (TypeError, ValueError):
        return "价格异常"
    return f"¥{price:,}"


def _format_publish_time(value: Any) -> str:
    """Mercari 的 `created` / `updated` 字段是 UNIX 秒。"""
    try:
        return datetime.fromtimestamp(int(value)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return "未知时间"


def _extract_photos(item: dict) -> list[str]:
    """Mercari 商品图片:优先 photos[].url,回退 thumbnails。"""
    photos = item.get("photos") or []
    urls: list[str] = []
    for entry in photos:
        if isinstance(entry, dict):
            url = entry.get("url") or entry.get("uri")
            if url:
                urls.append(url)
        elif isinstance(entry, str):
            urls.append(entry)
    if urls:
        return urls
    thumbnails = item.get("thumbnails") or []
    return [t for t in thumbnails if isinstance(t, str)]


def _build_item_link(item_id: str) -> str:
    return ITEM_PAGE_URL_TEMPLATE.format(item_id=item_id)


def parse_search_results_json(json_data: dict, source: str) -> list[dict]:
    """解析搜索 API 响应,返回商品基础信息列表(中文 key)。

    Mercari `v2/entities:search` 响应结构(通用字段,可能因版本略异):
      {
        "items": [
          { "id": "...", "name": "...", "price": 12345,
            "thumbnails": [...], "photos": [...],
            "item_condition_id": 3, "shipping_payer_id": 2,
            "seller": {"id": "...", "name": "..."},
            "item_shipping_from_area": "東京都",
            "num_likes": 5, "created": 1700000000, "status": "on_sale" }, ...
        ],
        "meta": {...}
      }
    """
    items = json_data.get("items")
    if not items:
        # 有些接口把商品放在 data.items 或 result 里,做一次回退查找
        items = _get(json_data, "data", "items") or _get(json_data, "result", "items") or []
    if not items:
        print(f"LOG: ({source}) Mercari API 响应中未找到商品列表。")
        return []

    parsed: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id") or "").strip()
        if not item_id:
            continue

        photos = _extract_photos(item)
        seller = item.get("seller") or {}
        tags: list[str] = []
        if item.get("shipping_payer_id") == SHIPPING_PAYER_SELLER:
            tags.append("送料込み")
        condition_label = ITEM_CONDITION_LABELS.get(item.get("item_condition_id"))
        if condition_label:
            tags.append(condition_label)

        parsed.append({
            "商品标题": item.get("name") or "未知标题",
            "当前售价": _format_price(item.get("price")),
            "商品原价": "暂无",
            "“想要”人数": item.get("num_likes", "NaN"),
            "商品标签": tags,
            "发货地区": item.get("item_shipping_from_area") or "地区未知",
            "卖家昵称": seller.get("name") or "匿名卖家",
            "商品链接": _build_item_link(item_id),
            "发布时间": _format_publish_time(item.get("created")),
            "商品ID": item_id,
            "商品图片列表": photos,
            "商品主图链接": photos[0] if photos else None,
        })

    print(f"LOG: ({source}) Mercari 成功解析到 {len(parsed)} 条商品基础信息。")
    return parsed


def parse_detail_json(detail_json: dict, item_data: dict) -> tuple[dict, str | None, dict]:
    """解析商品详情 API 响应,更新 item_data 并抽取卖家 ID。

    Mercari 详情响应结构(常见字段):
      {
        "data": {
          "id": "...", "name": "...", "description": "...",
          "price": 12345, "num_likes": 5, "views": 123,
          "photos": [...], "thumbnails": [...],
          "seller": {"id": "...", "name": "...", "num_sell_items": 45,
                     "num_ratings": 200, "ratings": {"good": 195, "normal": 3, "bad": 2},
                     "star_rating_score": 4.9, "created": 1600000000},
          "item_shipping_from_area": "東京都",
          "shipping_payer_id": 2, "item_condition": {"id": 3, "name": "..."}
        }
      }
    某些接口把商品在顶层,做一次回退。
    """
    data = detail_json.get("data") if isinstance(detail_json, dict) else None
    if not isinstance(data, dict):
        data = detail_json if isinstance(detail_json, dict) else {}

    # 更新图片列表
    photos = _extract_photos(data)
    if photos:
        item_data["商品图片列表"] = photos
        item_data["商品主图链接"] = photos[0]

    # 更新想要人数 / 浏览量
    if data.get("num_likes") is not None:
        item_data["“想要”人数"] = data.get("num_likes")
    if data.get("views") is not None:
        item_data["浏览量"] = data.get("views")

    # 更新描述
    description = data.get("description")
    if description:
        item_data["商品描述"] = description

    # 更新更精确的 tag(送料込み 覆盖搜索页 tag)
    if data.get("shipping_payer_id") == SHIPPING_PAYER_SELLER:
        tags = item_data.get("商品标签") or []
        if "送料込み" not in tags:
            tags.append("送料込み")
        item_data["商品标签"] = tags

    # 详情级 condition
    condition_id = _get(data, "item_condition", "id") or data.get("item_condition_id")
    if condition_id:
        label = ITEM_CONDITION_LABELS.get(condition_id)
        tags = item_data.get("商品标签") or []
        if label and label not in tags:
            tags.append(label)
        item_data["商品标签"] = tags

    # 卖家 ID + 卖家画像信息
    seller = data.get("seller") or {}
    seller_id = seller.get("id")
    seller_id_str = str(seller_id) if seller_id else None

    # 卖家评分(取代 zhima 信用)
    rating_score = seller.get("star_rating_score")
    num_ratings = seller.get("num_ratings")
    if rating_score is not None and num_ratings is not None:
        zhima_credit_text = f"メルカリ評価 {rating_score} ({num_ratings}件)"
    elif rating_score is not None:
        zhima_credit_text = f"メルカリ評価 {rating_score}"
    else:
        zhima_credit_text = None

    # 卖家注册时长(from seller.created UNIX 秒 → 天数)
    seller_created = seller.get("created")
    registration_duration_text = "未知"
    if seller_created:
        try:
            created_dt = datetime.fromtimestamp(int(seller_created))
            days = (datetime.now() - created_dt).days
            registration_duration_text = _format_mercari_registration(days)
        except (TypeError, ValueError, OSError):
            registration_duration_text = "未知"

    extras = {
        "zhima_credit_text": zhima_credit_text,
        "registration_duration_text": registration_duration_text,
        "seller_summary": {
            "num_sell_items": seller.get("num_sell_items"),
            "num_ratings": num_ratings,
            "star_rating_score": rating_score,
        },
    }
    return item_data, seller_id_str, extras


def _format_mercari_registration(total_days: int) -> str:
    """把注册天数格式化为 'メルカリ登録 X年Y个月'(措辞平台化)。"""
    if not isinstance(total_days, int) or total_days <= 0:
        return "未知"
    DAYS_IN_YEAR = 365.25
    DAYS_IN_MONTH = DAYS_IN_YEAR / 12
    years = int(total_days // DAYS_IN_YEAR)
    remaining_days = total_days - int(years * DAYS_IN_YEAR)
    months = int(round(remaining_days / DAYS_IN_MONTH))
    if months == 12:
        years += 1
        months = 0
    if years > 0 and months > 0:
        return f"メルカリ登録{years}年{months}ヶ月"
    if years > 0:
        return f"メルカリ登録{years}年"
    if months > 0:
        return f"メルカリ登録{months}ヶ月"
    return "メルカリ登録1ヶ月未満"


def format_registration_days(total_days: int) -> str:
    """公开给 scraper 使用。"""
    return _format_mercari_registration(total_days)


def parse_seller_profile_json(profile_json: dict) -> dict:
    """解析卖家资料 API 响应,产出与闲鱼同名的中文 key。"""
    data = profile_json.get("data") if isinstance(profile_json, dict) else None
    if not isinstance(data, dict):
        data = profile_json if isinstance(profile_json, dict) else {}

    rating_score = data.get("star_rating_score")
    num_ratings = data.get("num_ratings")
    ratings_dict = data.get("ratings") or {}
    good = ratings_dict.get("good") or 0
    normal = ratings_dict.get("normal") or 0
    bad = ratings_dict.get("bad") or 0
    total_ratings = good + normal + bad
    good_rate = f"{(good / total_ratings * 100):.2f}%" if total_ratings else "N/A"

    seller_credit_text = "暂无"
    if rating_score is not None:
        seller_credit_text = f"メルカリ評価 {rating_score}"

    return {
        "卖家昵称": data.get("name"),
        "卖家头像链接": data.get("photo_url") or data.get("photo_thumbnail_url"),
        "卖家个性签名": data.get("introduction") or "",
        "卖家在售/已售商品数": data.get("num_sell_items"),
        "卖家收到的评价总数": num_ratings,
        "卖家信用等级": seller_credit_text,
        "买家信用等级": "N/A",
        "作为卖家的好评数": f"{good}/{total_ratings}" if total_ratings else "0/0",
        "作为卖家的好评率": good_rate,
        "作为买家的好评数": "N/A",
        "作为买家的好评率": "N/A",
    }
