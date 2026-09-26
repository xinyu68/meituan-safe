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

Version 0.6 added an optional Passport authorization using standard HTTPS and PKCE
after a shared-credential rejection. That observation did not establish that
the endpoint always requires a dedicated token. The authorization uses:
`/api/account/userauth/code` creates a user authorization link and
`/api/account/userauth/check` performs a single status check. The wrapper does
not bundle the reference project's private tarball, obfuscated CLIGuard,
updater, coupon, ordering, or payment code. The user must explicitly confirm the
authorization in the Meituan app. `deal-login` writes a local PNG QR code for
the returned HTTPS link and returns its absolute `qr_image_path`; desktop agents
must render that image instead of treating the link as a browser login page.

Passport tokens never appear in CLI arguments or JSON output. On Windows the
token and pending PKCE session are encrypted for the current OS user with DPAPI;
on other platforms the local files use mode `0600`. The token is delegated to
the pinned Node runtime only through `MEITUAN_SAFE_DEAL_TOKEN`. A rejected token
is removed and reported as `deal_auth_rejected`, after which callers use
`deal-login --force` once instead of repeating the H5 login.
The deal command uses this delegated Passport token directly, so a separate
H5/waimai login is not required for in-store search.

Successful results are normalized to product ID, POI ID, restaurant, package
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

`status` reports local H5 and Passport credential presence separately, not
end-to-end validation of every Meituan business API. `login --force` clears H5
cookies only. `deal-login --force` starts a fresh Passport PKCE authorization;
`deal-login-status` performs one non-blocking status check, and `deal-logout`
removes the local Passport token and pending session.

Version 0.7 removes the wrapper's mandatory Passport gate. `auto` uses a cached
Passport token when present and otherwise lets the upstream `requireAuth()`
reuse the shared login, as it did before version 0.6. Explicit `shared` clears
the delegated Passport environment variable and does not read or delete the
Passport cache; explicit `passport` still requires that authorization. Search
results identify the credential source and the endpoint actually verified.

`auth-check --location ...` compares the two businesses using the same shared
credential source. It first checks local presence without exposing credential
previews, then performs one waimai search and one deal search, without credential
writes, retries, or Passport fallback. A valid empty result still verifies API
acceptance; malformed output, timeouts, and HTTP failures do not. Diagnostic
envelope success only means the comparison completed; callers must inspect
`shared_login_verified` and each `checks` entry. A valid H5 login followed by a
deal rejection makes Passport an option, not proof that all users need two logins.

HTTP 403/429 and explicit risk/captcha failures are `access_restricted`, not
automatic re-login triggers. Upstream `Session expired`/401 errors are reported
as `auth_rejected`; they establish rejection but do not independently prove its
cause. A rejected Passport is invalidated only when Passport was the selected
source. A shared rejection does not remove Passport or force its authorization.

The upstream patch also keeps the shared credential internally consistent:
when a full cookie contains a token, that same sanitized token is used in the
deal request instead of a leftover standalone token. A successful cookie import
removes the old standalone credential, and a successful standalone-token import
removes the old cookie. This does not touch the optional Passport cache. It fixes
a possible mixed-credential failure; it does not establish the cause of any
particular live 401 or 403 response.

Desktop Passport QR experiments are not part of the supported H5 login. They
confirmed website authorization but did not pass waimai search verification;
mapping web cookie names or retaining cookies is not itself an end-to-end test.
No such experimental conversion is shipped. `login` and `status` explicitly
report stored-only verification, and existing `logged_in` means local credential
presence, not successful authentication against both services.

A live shared-source check after official H5 re-login returned both waimai and
deal results without Passport fallback. This verifies that one shared login can
work for both tested endpoints; it is not a guarantee for every account or future
request. Login cleanup ignores only Playwright's already-closed-target error so
it cannot turn a saved login into failure or mask an earlier import failure.

Version 0.7.1 launches the interactive H5 login with Playwright's Pixel 7 mobile
viewport, user agent, device scale factor, and touch emulation. These context
options are applied before the first navigation, so a desktop user does not
need to open DevTools and switch to mobile mode. Only the dedicated login
context changes; the normal browser, stored credentials, and headless business
query runtime are untouched. This is page-rendering configuration, not a way to
bypass verification or risk controls.

## Orders

Order status and order detail are read-only wrappers and require an exact order
ID. The skill intentionally does not expose upstream cart, reorder, coupon-apply,
order-place, or payment flows.
