from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = (
    Path(__file__).parents[1]
    / "skills"
    / "meituan-safe"
    / "scripts"
    / "meituan_cli.py"
)
SPEC = importlib.util.spec_from_file_location("meituan_cli", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class MeituanCliTest(unittest.TestCase):
    def test_distance_parser(self) -> None:
        self.assertEqual(850, MODULE._distance_meters("850m"))
        self.assertEqual(1200, MODULE._distance_meters("1.2km"))
        self.assertEqual(900, MODULE._distance_meters("900米"))
        self.assertIsNone(MODULE._distance_meters("附近"))

    def test_location_query_variants(self) -> None:
        core, variants = MODULE._clean_location_query("北京望京地铁站")
        self.assertEqual("望京", core)
        self.assertIn("望京 北京", variants)

    def test_coordinate_conversion_is_deterministic(self) -> None:
        lat, lng = MODULE._wgs84_to_gcj02(39.9981119, 116.4647526)
        self.assertAlmostEqual(40.000, lat, places=2)
        self.assertAlmostEqual(116.471, lng, places=2)

    def test_parser_supports_nearby_search(self) -> None:
        args = MODULE._parser().parse_args(
            [
                "nearby-search",
                "--location",
                "北京望京地铁站",
                "--keyword",
                "烧烤",
                "--radius",
                "1000",
                "--pages",
                "3",
                "--min-rating",
                "4.6",
                "--sort-by",
                "recommended",
            ]
        )
        self.assertEqual("nearby-search", args.command)
        self.assertEqual(1000, args.radius)
        self.assertEqual(3, args.pages)
        self.assertEqual(4.6, args.min_rating)
        self.assertEqual("recommended", args.sort_by)

    def test_numeric_metrics_and_recommendation_score(self) -> None:
        self.assertEqual(0, MODULE._numeric_value("免配送费", free_is_zero=True))
        self.assertEqual(12000, MODULE._numeric_value("月售1.2万"))
        self.assertEqual(45, MODULE._delivery_minutes("预计35-45分钟"))
        near = MODULE._enrich_restaurant(
            {"rating": 4.8, "distance": "300m", "shippingFee": "免配送费", "monthSales": "月售2000"}
        )
        far = MODULE._enrich_restaurant(
            {"rating": 4.8, "distance": "3.5km", "shippingFee": "配送费8元", "monthSales": "月售2000"}
        )
        self.assertGreater(near["recommendation_score"], far["recommendation_score"])

    def test_menu_search_filters_and_sorts(self) -> None:
        args = MODULE._parser().parse_args(
            [
                "menu-search",
                "--restaurant-id",
                "r1",
                "--keyword",
                "羊肉",
                "--max-price",
                "30",
                "--in-stock",
            ]
        )
        response = {
            "items": [
                {"id": "1", "name": "羊肉串", "min_price": 8, "month_saled": 30, "skus": [{"stock": 10}]},
                {"id": "2", "name": "羊肉锅", "min_price": 58, "month_saled": 80, "skus": [{"stock": 10}]},
                {"id": "3", "name": "羊肉饼", "min_price": 12, "month_saled": 10, "skus": [{"stock": 0}]},
            ]
        }
        with mock.patch.object(MODULE, "_run_mt", return_value=response):
            result = MODULE._run_menu_search(args)
        self.assertEqual(["1"], [item["id"] for item in result["items"]])
        self.assertEqual(3, result["scanned_count"])

    def test_schema_supports_progressive_discovery(self) -> None:
        args = MODULE._parser().parse_args(["schema", "--method", "menu-search"])
        self.assertEqual("menu-search", args.schema_method)
        self.assertIn("order-detail", MODULE._schemas())

    def test_windows_login_starts_visible_without_activation(self) -> None:
        arguments = MODULE._login_browser_args()
        self.assertIn("--no-first-run", arguments)
        if MODULE.os.name == "nt":
            self.assertIn("--start-minimized", arguments)
            self.assertNotIn("--window-position=-32000,-32000", arguments)

    def test_login_uses_explicit_official_login_route(self) -> None:
        self.assertEqual("https://h5.waimai.meituan.com/login?force=true", MODULE.LOGIN_URL)

    def test_login_compacts_cookie_for_windows_keyring(self) -> None:
        cookies = [
            {"name": "token", "value": "t" * 152},
            {"name": "userId", "value": "1234567890"},
            {"name": "wm_order_channel", "value": "default"},
            {"name": "openh5_uuid", "value": "u" * 64},
            {"name": "w_token", "value": "w" * 152},
            {"name": "unrelated_large_cookie", "value": "x" * 3000},
        ]
        compact, names = MODULE._compact_auth_cookie(cookies)
        self.assertNotIn("unrelated_large_cookie", names)
        self.assertNotIn("x" * 3000, compact)
        self.assertLessEqual(
            len(compact.encode("utf-16-le")),
            MODULE.WINDOWS_CREDENTIAL_UTF16_LIMIT,
        )

    def test_login_cookie_requires_user_identity(self) -> None:
        with self.assertRaises(MODULE.MeituanError) as raised:
            MODULE._compact_auth_cookie([{"name": "token", "value": "token-only"}])
        self.assertEqual("login_cookie_incomplete", raised.exception.code)

    def test_upstream_timeout_is_bounded_and_structured(self) -> None:
        expired = MODULE.subprocess.TimeoutExpired(
            cmd=["node", "mt"],
            timeout=90,
            output="",
            stderr="browser proxy stalled",
        )
        with mock.patch.object(MODULE, "_require_runtime"), mock.patch.object(
            MODULE.subprocess, "run", side_effect=expired
        ):
            with self.assertRaises(MODULE.MeituanError) as raised:
                MODULE._run_mt(["waimai", "search", "烧烤"])
        self.assertEqual("upstream_timeout", raised.exception.code)
        self.assertTrue(raised.exception.retryable)

    def test_non_json_403_is_retryable_not_login_failure(self) -> None:
        completed = MODULE.subprocess.CompletedProcess(
            args=["node", "mt"],
            returncode=1,
            stdout=json.dumps(
                {
                    "ok": False,
                    "error": "Meituan returned non-JSON response (HTTP 403, text/html)",
                }
            ),
            stderr="",
        )
        with mock.patch.object(MODULE, "_require_runtime"), mock.patch.object(
            MODULE.subprocess, "run", return_value=completed
        ):
            with self.assertRaises(MODULE.MeituanError) as raised:
                MODULE._run_mt(["waimai", "search", "烧烤"])
        self.assertEqual("upstream_error", raised.exception.code)
        self.assertTrue(raised.exception.retryable)

    def test_nearby_search_uses_single_upstream_batch(self) -> None:
        args = MODULE._parser().parse_args(
            [
                "nearby-search",
                "--lat",
                "40.0",
                "--lng",
                "116.47",
                "--keyword",
                "烧烤",
                "--pages",
                "3",
            ]
        )
        with mock.patch.object(MODULE, "_run_mt", return_value=[]) as run_mt:
            result = MODULE._run_nearby_search(args)
        self.assertEqual(1, run_mt.call_count)
        invocation = run_mt.call_args.args[0]
        self.assertEqual(["waimai", "search-at", "烧烤"], invocation[:3])
        self.assertEqual(1, result["performance"]["browser_sessions"])

    def test_geocode_cache_round_trip(self) -> None:
        candidates = [{"display_name": "望京站", "score": 10}]
        with mock.patch.object(MODULE, "_geocode_cache_path") as cache_path:
            from tempfile import TemporaryDirectory

            with TemporaryDirectory() as temporary:
                cache_path.return_value = Path(temporary) / "geocode.json"
                MODULE._store_geocode_cache("北京望京地铁站", candidates)
                self.assertEqual(candidates, MODULE._load_geocode_cache("北京望京地铁站", 5))

    def test_deal_normalization_uses_known_group_buying_fields(self) -> None:
        item = MODULE._normalize_deal(
            {
                "productId": 123,
                "poiId": 456,
                "poiName": "测试烧烤店",
                "productName": "双人套餐",
                "salePrice": "200",
                "distanceText": "358m",
                "poiDpFiveScore": "4.7",
            }
        )
        self.assertEqual("123", item["product_id"])
        self.assertEqual("测试烧烤店", item["restaurant"])
        self.assertEqual(200, item["sale_price"])
        self.assertEqual(358, item["distance_meters"])
        self.assertEqual(4.7, item["rating"])
        self.assertEqual(2, item["party_size_min"])
        self.assertEqual(100, item["average_price"])
        self.assertEqual("deal_price_divided_by_min_party_size", item["average_price_source"])

    def test_deal_average_price_prefers_official_value(self) -> None:
        item = MODULE._normalize_deal(
            {"productName": "双人餐", "salePrice": "200", "avgPrice": "88"}
        )
        self.assertEqual(88, item["average_price"])
        self.assertEqual(100, item["estimated_average_price"])
        self.assertEqual("meituan_official", item["average_price_source"])

    def test_deal_party_range_uses_minimum_size_conservatively(self) -> None:
        item = MODULE._normalize_deal(
            {"productName": "招牌烧烤3-4人餐|2店通用", "salePrice": "198"}
        )
        self.assertEqual(3, item["party_size_min"])
        self.assertEqual(4, item["party_size_max"])
        self.assertEqual(66, item["estimated_average_price"])

    def test_deal_party_parser_does_not_treat_store_count_as_people(self) -> None:
        item = MODULE._normalize_deal(
            {"productName": "招牌烤肉套餐|2店通用", "salePrice": "198"}
        )
        self.assertIsNone(item["party_size_min"])
        self.assertIsNone(item["estimated_average_price"])

    def test_parser_supports_deals_search(self) -> None:
        args = MODULE._parser().parse_args(
            [
                "deals-search",
                "--location",
                "北京望京地铁站",
                "--keyword",
                "烧烤",
                "--radius",
                "1000",
                "--min-rating",
                "4.0",
            ]
        )
        self.assertEqual("deals-search", args.command)
        self.assertEqual(4.0, args.min_rating)
        self.assertEqual(1000, args.radius)

    def test_parser_supports_distinct_food_search(self) -> None:
        args = MODULE._parser().parse_args(
            [
                "food-search",
                "--location",
                "北京望京地铁站",
                "--keyword",
                "烧烤",
                "--max-official-average-price",
                "120",
            ]
        )
        self.assertEqual("food-search", args.command)
        self.assertEqual(120, args.max_official_average_price)

    def test_food_projection_does_not_use_package_average_as_store_average(self) -> None:
        deal_result = {
            "query": {"filters": {"min_rating": 4.0}},
            "deals": [
                {
                    "poi_id": "p1",
                    "restaurant": "测试烧烤店",
                    "product_id": "d1",
                    "title": "双人餐",
                    "sale_price": 200,
                    "average_price": 100,
                    "average_price_source": "deal_price_divided_by_min_party_size",
                    "official_average_price": None,
                    "rating": 4.8,
                    "distance": "300m",
                    "distance_meters": 300,
                    "image": None,
                }
            ],
            "scanned_count": 1,
        }
        unfiltered = MODULE._food_projection_from_deals(
            deal_result,
            min_official_average_price=None,
            max_official_average_price=None,
            limit=20,
        )
        self.assertIsNone(unfiltered["restaurants"][0]["official_average_price"])
        self.assertTrue(unfiltered["deal_estimates_excluded_from_store_average"])
        filtered = MODULE._food_projection_from_deals(
            deal_result,
            min_official_average_price=70,
            max_official_average_price=130,
            limit=20,
        )
        self.assertEqual([], filtered["restaurants"])

    def test_deal_page_size_is_capped_to_avoid_price_corruption(self) -> None:
        with self.assertRaises(SystemExit):
            MODULE._parser().parse_args(
                ["deals-search", "--keyword", "烧烤", "--page-size", "20"]
            )

    def test_window_snapshot_is_safe(self) -> None:
        handles, foreground = MODULE._windows_snapshot()
        self.assertIsInstance(handles, set)
        self.assertTrue(foreground is None or isinstance(foreground, int))

    def test_capabilities_exclude_orders_and_payment(self) -> None:
        capabilities = MODULE._capabilities()
        self.assertIn("nearby-search", capabilities["read"])
        self.assertIn("food-search", capabilities["read"])
        self.assertIn("compare", capabilities["read"])
        self.assertIn("order-status", capabilities["read"])
        self.assertIn("order_submission", capabilities["unsupported"])
        self.assertIn("payment", capabilities["unsupported"])


if __name__ == "__main__":
    unittest.main()
