"""Mercari(日本站)JSON 解析器测试。"""
from __future__ import annotations

from src.scrapers.mercari.parsers import (
    parse_detail_json,
    parse_search_results_json,
    parse_seller_profile_json,
)


def _sample_search_payload() -> dict:
    return {
        "items": [
            {
                "id": "m123456",
                "name": "iPhone 15 Pro 256GB ブラック",
                "price": 128000,
                "photos": [
                    {"url": "https://static.mercdn.net/item/detail/orig/1.jpg"},
                    {"url": "https://static.mercdn.net/item/detail/orig/2.jpg"},
                ],
                "thumbnails": ["https://static.mercdn.net/item/thumb/1.jpg"],
                "item_condition_id": 2,
                "shipping_payer_id": 2,
                "seller": {"id": "user_9999", "name": "田中太郎"},
                "item_shipping_from_area": "東京都",
                "num_likes": 12,
                "created": 1700000000,
                "status": "on_sale",
            },
            {
                "id": "m654321",
                "name": "MacBook Air M2",
                "price": 145000,
                "photos": [],
                "thumbnails": ["https://static.mercdn.net/item/thumb/x.jpg"],
                "item_condition_id": 3,
                "shipping_payer_id": 1,  # 着払い
                "seller": {"id": "user_1", "name": ""},
                "item_shipping_from_area": "大阪府",
                "num_likes": 3,
                "created": 1690000000,
            },
        ]
    }


class TestParseSearchResultsJson:
    def test_returns_expected_number_of_items(self):
        parsed = parse_search_results_json(_sample_search_payload(), "unit")
        assert len(parsed) == 2

    def test_uses_chinese_key_convention(self):
        parsed = parse_search_results_json(_sample_search_payload(), "unit")
        required_keys = {
            "商品标题", "当前售价", "商品原价", "“想要”人数", "商品标签",
            "发货地区", "卖家昵称", "商品链接", "发布时间", "商品ID",
            "商品图片列表", "商品主图链接",
        }
        for item in parsed:
            missing = required_keys - set(item.keys())
            assert not missing, f"missing keys: {missing}"

    def test_price_is_formatted_with_yen_and_thousand_separator(self):
        parsed = parse_search_results_json(_sample_search_payload(), "unit")
        assert parsed[0]["当前售价"] == "¥128,000"
        assert parsed[1]["当前售价"] == "¥145,000"

    def test_free_shipping_tag_appears_when_seller_pays(self):
        parsed = parse_search_results_json(_sample_search_payload(), "unit")
        assert "送料込み" in parsed[0]["商品标签"]
        assert "送料込み" not in parsed[1]["商品标签"]

    def test_condition_label_is_localized_japanese(self):
        parsed = parse_search_results_json(_sample_search_payload(), "unit")
        assert "未使用に近い" in parsed[0]["商品标签"]
        assert "目立った傷や汚れなし" in parsed[1]["商品标签"]

    def test_link_uses_jp_mercari_item_url(self):
        parsed = parse_search_results_json(_sample_search_payload(), "unit")
        assert parsed[0]["商品链接"] == "https://jp.mercari.com/item/m123456"

    def test_photos_fall_back_to_thumbnails_when_photos_empty(self):
        parsed = parse_search_results_json(_sample_search_payload(), "unit")
        assert parsed[1]["商品图片列表"] == [
            "https://static.mercdn.net/item/thumb/x.jpg"
        ]

    def test_empty_items_returns_empty_list(self):
        assert parse_search_results_json({"items": []}, "unit") == []
        assert parse_search_results_json({}, "unit") == []

    def test_alternate_data_key_supported(self):
        # 有些接口把商品放到 data.items 下
        payload = {"data": {"items": _sample_search_payload()["items"][:1]}}
        parsed = parse_search_results_json(payload, "unit-alt")
        assert len(parsed) == 1
        assert parsed[0]["商品ID"] == "m123456"


class TestParseDetailJson:
    def test_updates_item_data_with_richer_photos(self):
        detail = {
            "data": {
                "id": "m123456",
                "photos": [
                    {"url": "https://static.mercdn.net/big/1.jpg"},
                    {"url": "https://static.mercdn.net/big/2.jpg"},
                    {"url": "https://static.mercdn.net/big/3.jpg"},
                ],
                "shipping_payer_id": 2,
                "item_condition": {"id": 2, "name": "未使用に近い"},
                "seller": {
                    "id": "seller_abc",
                    "name": "田中",
                    "num_sell_items": 45,
                    "num_ratings": 200,
                    "star_rating_score": 4.9,
                    "created": 1600000000,
                },
                "num_likes": 15,
                "views": 300,
                "description": "状態良好",
            }
        }
        item_data = {"商品ID": "m123456", "商品标题": "iPhone 15 Pro"}
        updated, seller_id, extras = parse_detail_json(detail, item_data)

        assert seller_id == "seller_abc"
        assert updated["商品图片列表"] == [
            "https://static.mercdn.net/big/1.jpg",
            "https://static.mercdn.net/big/2.jpg",
            "https://static.mercdn.net/big/3.jpg",
        ]
        assert updated["浏览量"] == 300
        assert "送料込み" in updated["商品标签"]
        assert "未使用に近い" in updated["商品标签"]
        assert extras["zhima_credit_text"] == "メルカリ評価 4.9 (200件)"
        assert extras["registration_duration_text"].startswith("メルカリ登録")

    def test_handles_top_level_data_without_wrapper(self):
        detail = {"id": "x", "name": "y", "seller": {"id": "sid"}}
        item_data = {"商品ID": "x"}
        _, seller_id, _ = parse_detail_json(detail, item_data)
        assert seller_id == "sid"

    def test_no_seller_returns_none_id(self):
        detail = {"data": {"id": "x", "seller": {}}}
        item_data = {"商品ID": "x"}
        _, seller_id, _ = parse_detail_json(detail, item_data)
        assert seller_id is None


class TestParseSellerProfileJson:
    def test_produces_chinese_key_convention(self):
        profile = {
            "data": {
                "name": "田中太郎",
                "introduction": "よろしくお願いします",
                "num_sell_items": 45,
                "num_ratings": 200,
                "star_rating_score": 4.9,
                "ratings": {"good": 195, "normal": 3, "bad": 2},
                "photo_url": "https://x/y.jpg",
            }
        }
        parsed = parse_seller_profile_json(profile)
        required_keys = {
            "卖家昵称", "卖家头像链接", "卖家个性签名",
            "卖家在售/已售商品数", "卖家收到的评价总数", "卖家信用等级",
            "买家信用等级", "作为卖家的好评数", "作为卖家的好评率",
            "作为买家的好评数", "作为买家的好评率",
        }
        assert required_keys.issubset(set(parsed.keys()))
        assert parsed["卖家昵称"] == "田中太郎"
        assert parsed["作为卖家的好评数"] == "195/200"
        assert parsed["作为卖家的好评率"] == "97.50%"
        assert parsed["卖家信用等级"].startswith("メルカリ評価")

    def test_missing_ratings_falls_back_gracefully(self):
        profile = {"data": {"name": "test", "num_ratings": 0}}
        parsed = parse_seller_profile_json(profile)
        assert parsed["作为卖家的好评数"] == "0/0"
        assert parsed["作为卖家的好评率"] == "N/A"
