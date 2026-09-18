---
name: meituan-safe
description: "Safely search Meituan in-store food restaurants, group-buying deals, or food-delivery restaurants around a named place, inspect menus, compare results, and read exact order details through delegated login. Use for 美团登录、到店美食、到店团购、附近餐厅、外卖、菜品、菜单或订单只读查询；do not use for coupon claiming, cart mutation, ordering, payment, captcha bypass, or unattended purchases."
---

# Meituan Safe

Use the bundled CLI. It combines a pinned `meituan-cli` runtime with a safety
wrapper that owns login delegation, geocoding, radius filtering, structured
output, and capability boundaries. Never ask the user to paste cookies.

Resolve this skill directory as `SKILL_DIR` before constructing commands.

## Runtime

If `.runtime` is absent, run:

```text
python <SKILL_DIR>/scripts/bootstrap.py
```

Use the Python path printed by bootstrap. On Windows it is normally
`<SKILL_DIR>/.runtime/python/Scripts/python.exe`.

## Commands

```text
<runtime-python> <SKILL_DIR>/scripts/meituan_cli.py capabilities
<runtime-python> <SKILL_DIR>/scripts/meituan_cli.py schema
<runtime-python> <SKILL_DIR>/scripts/meituan_cli.py login
<runtime-python> <SKILL_DIR>/scripts/meituan_cli.py status
<runtime-python> <SKILL_DIR>/scripts/meituan_cli.py geocode --location "北京望京地铁站"
<runtime-python> <SKILL_DIR>/scripts/meituan_cli.py food-search --location "北京望京地铁站" --keyword "烧烤" --radius 1000 --min-rating 4.0 --limit 20
<runtime-python> <SKILL_DIR>/scripts/meituan_cli.py nearby-search --location "北京望京地铁站" --keyword "烧烤" --radius 1000 --limit 20
<runtime-python> <SKILL_DIR>/scripts/meituan_cli.py deals-search --location "北京望京地铁站" --keyword "烧烤" --radius 1000 --min-rating 4.0 --limit 20
<runtime-python> <SKILL_DIR>/scripts/meituan_cli.py nearby-search --location "北京望京地铁站" --keyword "烧烤" --radius 1000 --pages 3 --min-rating 4.6 --max-delivery-fee 5 --sort-by recommended
<runtime-python> <SKILL_DIR>/scripts/meituan_cli.py addresses
<runtime-python> <SKILL_DIR>/scripts/meituan_cli.py restaurant --id <restaurant-id>
<runtime-python> <SKILL_DIR>/scripts/meituan_cli.py compare --ids <restaurant-id-1> <restaurant-id-2>
<runtime-python> <SKILL_DIR>/scripts/meituan_cli.py menu --restaurant-id <restaurant-id>
<runtime-python> <SKILL_DIR>/scripts/meituan_cli.py menus --restaurant-ids <restaurant-id-1> <restaurant-id-2>
<runtime-python> <SKILL_DIR>/scripts/meituan_cli.py menu-search --restaurant-id <restaurant-id> --keyword "羊肉串" --max-price 30 --in-stock --sort-by sales
<runtime-python> <SKILL_DIR>/scripts/meituan_cli.py order-status --order-id <order-id>
<runtime-python> <SKILL_DIR>/scripts/meituan_cli.py order-detail --order-id <order-id>
```

## Workflow

- Run `login` only when authentication is missing or expired. The visible Chrome
  window belongs to the user; wait for them to complete login or verification.
  It opens the official forced-login route directly so the phone/SMS and any
  official third-party login controls are visible instead of relying on a home-
  page login link.
  On Windows it opens visible without activation and is explicitly marked
  non-topmost, so the user can select it from the taskbar without losing focus
  repeatedly. The CLI delegates the resulting session internally and never
  prints cookies.
- For named-place searches, run `geocode` when ambiguity matters. If the first
  candidate is wrong, rerun `nearby-search` with `--location-index` from the
  returned candidate list, or pass trusted GCJ-02 `--lat` and `--lng` values.
- Route 到店美食、门店、餐厅评分 and 门店官方人均 to `food-search`.
  Route 团购、套餐、代金券 and 套餐折算人均 to `deals-search`. Route 外卖、
  配送、起送价 and 菜品 delivery discovery to `nearby-search`. When the user
  says 美食/团购 or asks for both, run `food-search` and `deals-search` and
  present two separately labeled sections; never merge their average-price
  fields into one ranking.
- `food-search` returns unique restaurant cards and uses only Meituan's official
  store average for average-price filtering. Its current source is a restaurant
  projection from Meituan's deal catalog, so coverage is limited to restaurants
  represented there; disclose this instead of calling it a complete food
  directory. Never substitute a package-derived estimate for a missing official
  store average.
  Deal results contain actual returned package prices. Prefer Meituan's official
  average spend; otherwise accept the clearly labeled estimate derived from a
  recognized package size, such as ¥200 / 2 for a 双人餐. For ranges such as
  3-4人餐, divide by the minimum party size so the estimate does not understate
  cost. Filters fail closed when neither official nor estimated spend is known.
- Treat radius results as a filter over Meituan's ranked search response, not an
  exhaustive geographic directory. State this limitation when reporting results.
- Promotion labels are best effort: report only labels returned by Meituan and
  never infer that a promotion applies at checkout. The recommendation score is
  a documented local heuristic, not Meituan's ranking.
- Order reads require an exact order ID. Never route an order request to place,
  reorder, cart, coupon-apply, or payment commands.
- Restaurant search uses a temporary coordinate locally and does not change the
  user's saved delivery addresses. Delivery eligibility still depends on the
  actual checkout address.
- Nearby search batches coordinate selection and all requested pages into one
  headless browser session. Use `menus` instead of separate `menu` calls when
  reading more than one restaurant so the signing browser is reused.
- Login and human verification are the only visible-browser flows. All other
  commands keep their signing browser headless. Do not bypass captchas, risk
  controls, or device confirmation.

## Boundary

This version supports login delegation, login status, saved-address lookup,
named-place geocoding, separate read-only in-store restaurant and deal views, paginated and filtered
food-delivery restaurant search, restaurant comparison, menu and dish search,
promotion-label display, and exact-ID order status/detail reads. It does not
claim coupons, mutate carts, submit or reorder orders, select payment methods,
pay, or operate unattended.

Read [implementation notes](references/implementation.md) only when maintaining
or diagnosing this skill.
