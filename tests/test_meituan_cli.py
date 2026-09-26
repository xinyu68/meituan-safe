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

    def test_non_json_403_is_access_restriction_without_automatic_retry(self) -> None:
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
        self.assertEqual("access_restricted", raised.exception.code)
        self.assertFalse(raised.exception.retryable)

    def test_deal_token_rejection_does_not_assume_passport_was_used(self) -> None:
        completed = MODULE.subprocess.CompletedProcess(
            args=["node", "mt"],
            returncode=1,
            stdout=json.dumps(
                {
                    "ok": False,
                    "error": "Deal search failed: token校验异常,请稍后重试!",
                },
                ensure_ascii=False,
            ),
            stderr="",
        )
        with mock.patch.object(MODULE, "_require_runtime"), mock.patch.object(
            MODULE.subprocess, "run", return_value=completed
        ):
            with self.assertRaises(MODULE.MeituanError) as raised:
                MODULE._run_mt(["deal", "search", "--keyword", "烧烤"])
        self.assertEqual("deal_auth_rejected", raised.exception.code)
        self.assertFalse(raised.exception.retryable)
        self.assertEqual("auth-check", raised.exception.details["action"])
        self.assertFalse(raised.exception.details["login_refresh_recommended"])

    def test_status_reports_separate_passport_authorization(self) -> None:
        with mock.patch.object(MODULE, "_passport_cached_token", return_value="passport-token"), mock.patch.object(
            MODULE, "_run_mt", return_value={"loggedIn": True, "authMode": "cookie"}
        ):
            status = MODULE._run_status()
        self.assertTrue(status["logged_in"])
        self.assertEqual("stored_credentials_only", status["verification_scope"])
        self.assertEqual("stored_unverified", status["in_store_deal_auth"])
        self.assertEqual("passport_pkce", status["in_store_deal_auth_mode"])
        self.assertEqual("stored_unverified", status["shared_auth_state"])
        self.assertFalse(status["api_verified"])

    def test_login_parser_supports_force_refresh(self) -> None:
        args = MODULE._parser().parse_args(["login", "--force"])
        self.assertTrue(args.force)

    def test_deal_login_parser_supports_force_refresh(self) -> None:
        args = MODULE._parser().parse_args(["deal-login", "--force"])
        self.assertTrue(args.force)

    def test_passport_authorization_link_is_restricted_to_meituan(self) -> None:
        self.assertTrue(MODULE._trusted_passport_link("https://dpurl.cn/example"))
        self.assertTrue(MODULE._trusted_passport_link("https://passport.meituan.com/example"))
        self.assertFalse(MODULE._trusted_passport_link("https://meituan.com.example.org/steal"))
        self.assertFalse(MODULE._trusted_passport_link("http://passport.meituan.com/example"))

    def test_passport_qr_is_written_as_png(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temporary, mock.patch.object(
            MODULE, "_cache_dir", return_value=Path(temporary) / "cache"
        ):
            output = MODULE._write_passport_qr("https://dpurl.cn/example")
            self.assertTrue(output.is_file())
            self.assertEqual(b"\x89PNG\r\n\x1a\n", output.read_bytes()[:8])

    def test_deal_login_stores_session_without_exposing_secret(self) -> None:
        args = MODULE._parser().parse_args(["deal-login"])
        response = {
            "authCode": "authorization-code",
            "shortLink": "https://passport.meituan.com/example",
        }
        with mock.patch.object(MODULE, "_passport_cached_token", return_value=None), mock.patch.object(
            MODULE, "_passport_request", return_value=response
        ) as request, mock.patch.object(MODULE, "_write_private_json") as write_private, mock.patch.object(
            MODULE, "_write_passport_qr", return_value=Path("C:/tmp/passport-auth-qr.png")
        ):
            result = MODULE._run_deal_login(args)
        self.assertFalse(result["authorized"])
        self.assertFalse(result["token_exposed"])
        self.assertEqual(response["shortLink"], result["auth_link"])
        self.assertEqual(str(Path("C:/tmp/passport-auth-qr.png")), result["qr_image_path"])
        self.assertEqual("/api/account/userauth/code", request.call_args.args[0])
        stored = write_private.call_args.args[1]
        self.assertEqual("authorization-code", stored["auth_code"])
        self.assertNotIn("token", stored)

    def test_forced_deal_login_discards_cached_token_before_new_session(self) -> None:
        args = MODULE._parser().parse_args(["deal-login", "--force"])
        response = {
            "authCode": "authorization-code",
            "shortLink": "https://passport.meituan.com/example",
        }
        with mock.patch.object(MODULE, "_passport_cached_token", return_value="old-token"), mock.patch.object(
            MODULE, "_passport_request", return_value=response
        ), mock.patch.object(MODULE, "_write_private_json"), mock.patch.object(
            MODULE, "_write_passport_qr", return_value=Path("C:/tmp/passport-auth-qr.png")
        ), mock.patch.object(
            MODULE, "_delete_private_file"
        ) as delete_private:
            result = MODULE._run_deal_login(args)
        self.assertEqual("authorization_required", result["status"])
        delete_private.assert_called_once_with(MODULE._passport_auth_path())

    def test_deal_login_status_stores_token_without_returning_it(self) -> None:
        session = {"auth_code": "authorization-code", "code_verifier": "verifier"}
        with mock.patch.object(MODULE, "_passport_cached_token", return_value=None), mock.patch.object(
            MODULE, "_read_private_json", return_value=session
        ), mock.patch.object(
            MODULE, "_passport_request", return_value={"authStatus": 4, "token": "secret-token"}
        ), mock.patch.object(MODULE, "_write_private_json") as write_private, mock.patch.object(
            MODULE, "_delete_private_file"
        ):
            result = MODULE._run_deal_login_status()
        self.assertTrue(result["authorized"])
        self.assertFalse(result["token_exposed"])
        self.assertNotIn("token", result)
        self.assertEqual("secret-token", write_private.call_args.args[1]["token"])

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

    def test_deals_search_delegates_passport_token_only_through_environment(self) -> None:
        args = MODULE._parser().parse_args(
            [
                "deals-search",
                "--lat",
                "40.0",
                "--lng",
                "116.47",
                "--city-id",
                "1",
                "--keyword",
                "烧烤",
            ]
        )
        with mock.patch.object(MODULE, "_passport_cached_token", return_value="secret-passport-token"), mock.patch.object(
            MODULE, "_run_mt", return_value={"products": [], "cityId": "1"}
        ) as run_mt:
            result = MODULE._run_deals_search(args)
        self.assertEqual([], result["deals"])
        invocation = run_mt.call_args.args[0]
        delegated = run_mt.call_args.kwargs["env"]
        self.assertNotIn("secret-passport-token", invocation)
        self.assertEqual("secret-passport-token", delegated["MEITUAN_SAFE_DEAL_TOKEN"])

    def test_deals_search_can_reuse_shared_login_without_passport(self) -> None:
        args = MODULE._parser().parse_args(
            [
                "deals-search",
                "--lat",
                "40.0",
                "--lng",
                "116.47",
                "--city-id",
                "1",
                "--keyword",
                "烧烤",
            ]
        )
        with mock.patch.object(MODULE, "_passport_cached_token", return_value=None), mock.patch.object(
            MODULE, "_run_mt", return_value={"products": [], "cityId": "1"}
        ) as run_mt:
            result = MODULE._run_deals_search(args)
        self.assertNotIn("MEITUAN_SAFE_DEAL_TOKEN", run_mt.call_args.kwargs["env"])
        self.assertEqual("shared", result["authentication"]["source"])
        self.assertTrue(result["authentication"]["api_verified"])

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


class SharedAuthRegressionTest(unittest.TestCase):
    def deal_args(self, source="auto", command="deals-search"):
        return MODULE._parser().parse_args([
            command, "--lat", "40", "--lng", "116.47", "--city-id", "1",
            "--keyword", "烧烤", "--auth-source", source,
        ])

    def run_check(self, responses):
        args = MODULE._parser().parse_args(["auth-check", "--location", "北京望京地铁站", "--keyword", "汉堡"])
        with mock.patch.dict(MODULE.os.environ, {"MEITUAN_SAFE_DEAL_TOKEN": "ambient-secret"}), mock.patch.object(
            MODULE, "_geocode", return_value=[{"latitude_wgs84": 40, "longitude_wgs84": 116.47}]
        ), mock.patch.object(MODULE, "_run_mt", side_effect=responses) as run_mt, mock.patch.object(
            MODULE, "_delete_private_file"
        ) as delete, mock.patch.object(MODULE, "_write_private_json") as write:
            result = MODULE._run_auth_check(args)
            calls = run_mt.call_args_list
        delete.assert_not_called()
        write.assert_not_called()
        for call in calls:
            self.assertNotIn("MEITUAN_SAFE_DEAL_TOKEN", call.kwargs["env"])
        self.assertNotIn("ambient-secret", json.dumps(result))
        return result, calls

    def test_explicit_shared_ignores_cached_and_ambient_passport(self):
        with mock.patch.dict(MODULE.os.environ, {"MEITUAN_SAFE_DEAL_TOKEN": "ambient-secret"}), mock.patch.object(
            MODULE, "_passport_cached_token"
        ) as cached, mock.patch.object(MODULE, "_run_mt", return_value={"products": []}) as run_mt:
            result = MODULE._run_deals_search(self.deal_args("shared"))
        cached.assert_not_called()
        self.assertEqual("shared", result["authentication"]["source"])
        self.assertNotIn("MEITUAN_SAFE_DEAL_TOKEN", run_mt.call_args.kwargs["env"])

    def test_missing_passport_only_blocks_explicit_passport_mode(self):
        with mock.patch.object(MODULE, "_passport_cached_token", return_value=None), mock.patch.object(
            MODULE, "_run_mt"
        ) as run_mt:
            with self.assertRaises(MODULE.MeituanError) as raised:
                MODULE._run_deals_search(self.deal_args("passport"))
        self.assertEqual("deal_login_required", raised.exception.code)
        run_mt.assert_not_called()

    def test_shared_rejection_does_not_delete_passport_or_force_separate_login(self):
        error = MODULE._upstream_error(["deal", "search"], "token校验异常")
        with mock.patch.object(MODULE, "_run_mt", side_effect=error) as run_mt, mock.patch.object(
            MODULE, "_delete_private_file"
        ) as delete:
            with self.assertRaises(MODULE.MeituanError) as raised:
                MODULE._run_deals_search(self.deal_args("shared"))
        self.assertEqual("auth-check", raised.exception.details["action"])
        self.assertEqual("shared", raised.exception.details["auth_source"])
        delete.assert_not_called()
        self.assertEqual(1, run_mt.call_count)

    def test_rejected_passport_is_scoped_to_passport_cache(self):
        for code in ("deal_auth_rejected", "auth_rejected"):
            with self.subTest(code=code), mock.patch.object(MODULE, "_passport_cached_token", return_value="secret"), mock.patch.object(
                MODULE, "_run_mt", side_effect=MODULE.MeituanError(code, "rejected")
            ), mock.patch.object(MODULE, "_delete_private_file") as delete:
                with self.assertRaises(MODULE.MeituanError) as raised:
                    MODULE._run_deals_search(self.deal_args())
            self.assertEqual("deal-login", raised.exception.details["action"])
            delete.assert_called_once_with(MODULE._passport_auth_path())

    def test_403_does_not_delete_passport_or_trigger_reauthorization(self):
        error = MODULE._upstream_error(["deal", "search"], "Meituan deal API returned HTTP 403")
        with mock.patch.object(MODULE, "_passport_cached_token", return_value="secret"), mock.patch.object(
            MODULE, "_run_mt", side_effect=error
        ), mock.patch.object(MODULE, "_delete_private_file") as delete:
            with self.assertRaises(MODULE.MeituanError) as raised:
                MODULE._run_deals_search(self.deal_args())
        self.assertEqual("access_restricted", raised.exception.code)
        self.assertEqual("investigate_access_or_request", raised.exception.details["action"])
        delete.assert_not_called()

    def test_food_search_preserves_shared_auth_source(self):
        with mock.patch.object(MODULE, "_run_mt", return_value={"products": []}):
            result = MODULE._run_food_search(self.deal_args("shared", "food-search"))
        self.assertEqual("shared", result["authentication"]["source"])

    def test_invalid_deal_response_does_not_report_verified(self):
        with mock.patch.object(MODULE, "_run_mt", return_value={}):
            with self.assertRaises(MODULE.MeituanError) as raised:
                MODULE._run_deals_search(self.deal_args("shared"))
        self.assertEqual("upstream_invalid_output", raised.exception.code)

    def test_401_is_rejected_not_proven_expired_and_missing_is_distinct(self):
        error = MODULE._upstream_error(["waimai", "search-at"], "Session expired (code: 401). Run: mt auth login")
        self.assertEqual("auth_rejected", error.code)
        missing = MODULE._upstream_error(["waimai", "search-at"], "Not logged in. Run: mt auth login")
        self.assertEqual("not_logged_in", missing.code)

    def test_auth_check_both_pass_with_empty_search_results(self):
        result, calls = self.run_check([{"loggedIn": True}, [], {"products": []}])
        self.assertTrue(result["shared_login_verified"])
        self.assertEqual("none", result["next_action"])
        self.assertEqual(3, len(calls))
        self.assertEqual(["waimai", "search-at"], calls[1].args[0][:2])
        self.assertEqual(["deal", "search"], calls[2].args[0][:2])
        self.assertEqual("1", calls[1].args[0][calls[1].args[0].index("--pages") + 1])
        self.assertEqual("1", calls[2].args[0][calls[2].args[0].index("--page-size") + 1])

    def test_auth_check_missing_credentials_does_not_call_business_apis(self):
        result, calls = self.run_check([{"loggedIn": False}])
        self.assertFalse(result["shared_login_verified"])
        self.assertEqual("login_then_recheck", result["next_action"])
        self.assertEqual(1, len(calls))

    def test_auth_check_rejected_shared_credentials_does_not_prove_split_login(self):
        result, _ = self.run_check([
            {"loggedIn": True}, MODULE.MeituanError("auth_rejected", "rejected"),
            MODULE.MeituanError("deal_auth_rejected", "rejected"),
        ])
        self.assertIsNone(result["separate_login_required"])
        self.assertFalse(result["shared_login_verified"])
        self.assertEqual("login_then_recheck", result["next_action"])

    def test_auth_check_offers_passport_only_after_waimai_verified(self):
        result, _ = self.run_check([
            {"loggedIn": True}, [], MODULE.MeituanError("deal_auth_rejected", "rejected"),
        ])
        self.assertEqual("offer_passport_authorization", result["next_action"])
        self.assertIsNone(result["separate_login_required"])

    def test_auth_check_403_or_timeout_stops_auth_inference(self):
        for error_code in ("access_restricted", "upstream_timeout"):
            with self.subTest(error_code=error_code):
                result, calls = self.run_check([
                    {"loggedIn": True}, MODULE.MeituanError(error_code, "failed"), {"products": []},
                ])
            self.assertEqual("investigate_access_or_request", result["next_action"])
            self.assertFalse(result["shared_login_verified"])
            self.assertEqual(3, len(calls))

    def test_auth_check_malformed_results_fail_closed(self):
        result, _ = self.run_check([{"loggedIn": True}, {}, {}])
        self.assertFalse(result["shared_login_verified"])
        self.assertEqual("unavailable", result["checks"]["waimai"]["state"])
        self.assertEqual("unavailable", result["checks"]["deals"]["state"])

    def test_auth_check_output_does_not_include_credential_previews(self):
        result, _ = self.run_check([
            {"loggedIn": True, "cookiePreview": "private-cookie", "tokenPreview": "private-token"},
            [], {"products": []},
        ])
        self.assertNotIn("private-", json.dumps(result))

    def test_schema_and_capabilities_expose_auth_comparison(self):
        self.assertIn("auth-check", MODULE._capabilities()["read"])
        for command in ("deals-search", "food-search"):
            self.assertEqual("auto", MODULE._schemas()[command]["params"]["auth_source"]["default"])


class LoginCleanupTest(unittest.IsolatedAsyncioTestCase):
    async def run_login(self, close_error=None, import_error=None):
        import tempfile
        from playwright.async_api import Error as PlaywrightError

        page = mock.MagicMock()
        page.goto = mock.AsyncMock()
        page.wait_for_timeout = mock.AsyncMock()
        page.evaluate = mock.AsyncMock(return_value=True)
        context = mock.MagicMock()
        context.pages = [page]
        context.cookies = mock.AsyncMock(return_value=[
            {"name": "token", "value": "test-token"},
            {"name": "userId", "value": "test-user"},
        ])
        context.close = mock.AsyncMock(
            side_effect=PlaywrightError(close_error) if close_error else None,
        )
        playwright = mock.MagicMock()
        playwright.devices = {"Pixel 7": {
            "user_agent": "Mozilla/5.0 (Linux; Android 14) Mobile Safari/537.36",
            "viewport": {"width": 412, "height": 839},
            "device_scale_factor": 2.625,
            "is_mobile": True,
            "has_touch": True,
            "default_browser_type": "chromium",
        }}
        playwright.chromium.launch_persistent_context = mock.AsyncMock(return_value=context)
        self.browser_launch = playwright.chromium.launch_persistent_context
        manager = mock.MagicMock()
        manager.__aenter__ = mock.AsyncMock(return_value=playwright)
        manager.__aexit__ = mock.AsyncMock(return_value=False)
        responses = [import_error, None] if import_error else [{}, {"loggedIn": True, "authMode": "cookie"}]
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            MODULE, "_require_runtime"
        ), mock.patch.object(MODULE, "_windows_snapshot", return_value=(set(), None)), mock.patch.object(
            MODULE, "_show_new_browser_without_activation", return_value=0
        ), mock.patch("playwright.async_api.async_playwright", return_value=manager), mock.patch.object(
            MODULE, "_run_mt", side_effect=responses
        ):
            args = MODULE._parser().parse_args(["--profile-dir", directory, "login"])
            return await MODULE._run_login(args)

    async def test_normal_close_preserves_stored_login_result(self):
        result = await self.run_login()
        self.assertTrue(result["logged_in"])
        self.assertFalse(result["api_verified"])

    async def test_already_closed_browser_does_not_turn_saved_login_into_failure(self):
        result = await self.run_login("BrowserContext.close: Target page, context or browser has been closed")
        self.assertTrue(result["logged_in"])
        self.assertFalse(result["api_verified"])

    async def test_already_closed_browser_does_not_hide_import_failure(self):
        failure = MODULE.MeituanError("credential_store_failed", "Cannot save credentials")
        with self.assertRaises(MODULE.MeituanError) as raised:
            await self.run_login("Target page, context or browser has been closed", failure)
        self.assertIs(failure, raised.exception)

    async def test_unrelated_close_error_is_not_suppressed(self):
        from playwright.async_api import Error as PlaywrightError
        with self.assertRaises(PlaywrightError):
            await self.run_login("Unexpected transport error")

    async def test_login_launches_visible_mobile_context_without_devtools(self):
        result = await self.run_login()
        options = self.browser_launch.call_args.kwargs
        self.assertTrue(options["is_mobile"])
        self.assertTrue(options["has_touch"])
        self.assertIn("Mobile", options["user_agent"])
        self.assertEqual({"width": 412, "height": 839}, options["viewport"])
        self.assertEqual(2.625, options["device_scale_factor"])
        self.assertFalse(options["headless"])
        self.assertEqual("zh-CN", options["locale"])
        self.assertEqual("chrome", options["channel"])
        self.assertNotIn("default_browser_type", options)
        self.assertNotIn("--auto-open-devtools-for-tabs", options["args"])
        self.assertEqual("mobile", result["browser_mode"])


if __name__ == "__main__":
    unittest.main()
