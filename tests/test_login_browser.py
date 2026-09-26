"""Opt-in rendering checks with isolated profiles; never submits a login form."""

from __future__ import annotations

import os
import tempfile
import unittest

from test_meituan_cli import MODULE


class LoginBrowserTest(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(os.environ.get("MEITUAN_SAFE_BROWSER_TESTS") == "1", "Opt-in browser test")
    async def test_mobile_configuration_applies_in_real_browser(self):
        from playwright.async_api import async_playwright

        with tempfile.TemporaryDirectory(prefix="meituan-login-render-") as profile:
            async with async_playwright() as playwright:
                context = await playwright.chromium.launch_persistent_context(
                    profile, channel="chrome", headless=True,
                    **MODULE._login_mobile_options(playwright),
                )
                try:
                    page = context.pages[0]
                    await page.set_content(
                        '<meta name="viewport" content="width=device-width, initial-scale=1">'
                        '<style>input {display:none} @media(max-width:600px) {input {display:block}}</style>'
                        '<input type="tel" placeholder="手机号">'
                    )
                    state = await page.evaluate("""() => ({
                        mobile: /Mobile/.test(navigator.userAgent),
                        touch: navigator.maxTouchPoints > 0,
                        width: window.innerWidth,
                        scale: window.devicePixelRatio
                    })""")
                    self.assertTrue(state["mobile"])
                    self.assertTrue(state["touch"])
                    self.assertEqual(412, state["width"])
                    self.assertEqual(2.625, state["scale"])
                    self.assertTrue(await page.locator('input[type="tel"]').is_visible())
                finally:
                    await context.close()

    @unittest.skipUnless(os.environ.get("MEITUAN_SAFE_LIVE_LOGIN_TESTS") == "1", "Opt-in read-only live test")
    async def test_official_mobile_login_has_visible_phone_input(self):
        from playwright.async_api import async_playwright

        with tempfile.TemporaryDirectory(prefix="meituan-login-live-") as profile:
            async with async_playwright() as playwright:
                context = await playwright.chromium.launch_persistent_context(
                    profile, channel="chrome", headless=True, locale="zh-CN",
                    **MODULE._login_mobile_options(playwright),
                )
                try:
                    page = context.pages[0]
                    response = await page.goto(MODULE.LOGIN_URL, wait_until="domcontentloaded", timeout=45_000)
                    self.assertIsNotNone(response)
                    self.assertLess(response.status, 400)
                    phone = page.locator(
                        'input[placeholder*="手机号"]:visible, input[type="tel"]:visible, '
                        'input[autocomplete="tel"]:visible'
                    ).first
                    await phone.wait_for(state="visible", timeout=20_000)
                    self.assertTrue(await phone.is_visible())
                finally:
                    await context.close()


if __name__ == "__main__":
    unittest.main()
