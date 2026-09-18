# Implementation notes

## Upstream

The runtime pins `juntaochi/meituan-cli` commit
`251522f157e14c444e5afe311f2e9562d2f71011`. Bootstrap applies a narrow patch
that adds environment-delegated cookie import and temporary coordinate selection.
The wrapper never accepts or prints a cookie argument.

The upstream project performs Meituan H5 requests through a Playwright signing
proxy. Page APIs and signing behavior are not stable public contracts, so an
upstream or Meituan change may require a new pinned commit and patch review.
The local patch closes the signing browser after every CLI command. The wrapper
also enforces a 90-second subprocess timeout and can recover a complete JSON
envelope emitted before a delayed browser shutdown.

Version 0.3 adds bounded batch commands for coordinate selection plus paginated
search, and for multi-restaurant menu reads. Each batch reuses one headless
browser and H5guard context, then closes it normally. This avoids a persistent
background daemon while removing repeated browser cold starts inside one user
operation.

## Location

Named places are resolved through OpenStreetMap Nominatim. The query text is sent
to that service; account data and cookies are not. Nominatim returns WGS-84
coordinates, which the wrapper converts to GCJ-02 before calling Meituan.
Successful geocoding results are cached locally for seven days under the
meituan-safe application cache directory.

Search results are filtered using Meituan's returned distance field. Results with
missing or unparseable distance are excluded and counted separately. The result
set is ranked and may not contain every business physically inside the radius.

Version 0.2 adds bounded multi-page fetching followed by local filtering and
sorting. Numeric metrics are parsed from the display strings returned by Meituan;
missing metrics fail closed when a corresponding filter is requested. Promotion
labels are extracted only from known promotion containers in the search response.
The local recommendation score combines rating, distance, delivery fee, delivery
time, monthly sales, minimum order, and returned promotion count. It is not a
checkout quote or Meituan ranking.

## In-store deals

Version 0.4 adds a separate read-only in-store group-buying search path based on
the Meituan agent product search endpoint documented by the referenced
`ysansan98/meituan-living` project. Only the location and search calls were
reimplemented. Its bundled passport package, obfuscated CLIGuard, updater,
coupon-claiming, ordering, and payment-related code are not included.

The existing delegated Meituan cookie token is sent only to the Meituan-owned
deal endpoint. Results are normalized to product ID, POI ID, restaurant, package
title, returned prices, rating, distance, image, sales label, and URL when those
fields are present. Official average spend takes precedence. When it is absent,
the wrapper recognizes explicit package sizes such as 单人餐, 双人餐, 2人餐, or
3-4人餐 and estimates per-person cost as deal price divided by the minimum party
size. The result exposes the source, official and estimated values, parsed party
range, and matched title text. A requested average-price filter fails closed when
neither value is available. Deal results are ranked results rather than an
exhaustive map inventory.

## In-store food restaurants

Version 0.5 separates restaurant discovery from package discovery. `food-search`
deduplicates restaurant cards by POI ID and exposes only the official store
average returned by Meituan; it never promotes a package-price estimate into the
store-average field. The current upstream source is Meituan's deal catalog, so
this view covers restaurants represented by returned deals rather than the
complete nearby food directory. Responses expose this provenance in
`data_origin` and `coverage`.

The deal endpoint is capped at a page size of 10. Larger page sizes have been
observed to return corrupted or truncated numeric values, so the wrapper rejects
them instead of silently emitting unreliable prices.

## Authentication

The user logs in within a dedicated persistent Chrome profile under
`%LOCALAPPDATA%/meituan-safe/chrome-profile` unless overridden with
`MEITUAN_SAFE_PROFILE_DIR`. Cookies are transferred directly to the pinned
runtime through a short-lived environment variable and stored through its OS
credential mechanism.

On Windows, interactive login launches Chrome minimized, then shows only the new
dedicated-profile window with `SW_SHOWNOACTIVATE` and clears any topmost state via
`SetWindowPos(HWND_NOTOPMOST)`. It then attempts to restore the window that was
foreground before launch. Non-login upstream requests use headless Playwright.

The interactive flow navigates directly to the official consumer login route
`https://h5.waimai.meituan.com/login?force=true`. After JavaScript renders, it
checks for phone, verification-code, third-party, or login-button controls. If
none appear and no authenticated cookie exists, it returns `login_entry_missing`
instead of waiting until the login timeout.

The browser may hold more cookie data than Windows Credential Manager accepts as
a UTF-16 password. Login delegates only an allowlisted authentication subset,
requires `token` and `userId`, and keeps the serialized UTF-16 payload below a
conservative 2400-byte limit. Cookie values are never logged.

## Orders

Order status and order detail are read-only wrappers and require an exact order
ID. The skill intentionally does not expose upstream cart, reorder, coupon-apply,
order-place, or payment flows.
