# Codex 美团安全助手

这是一个面向 Codex 的美团 Skill，用于登录美团，并按地点、距离、评分和价格等条件查询到店美食、团购套餐与外卖餐厅。登录时显示浏览器，其他查询默认在后台运行。

## 让智能体一句话安装

直接对 Codex 说：

> 从 https://github.com/xinyu68/meituan-safe/tree/main/skills/meituan-safe 安装这个技能

也可以使用标准安装器：

```powershell
python "$env:USERPROFILE\.codex\skills\.system\skill-installer\scripts\install-skill-from-github.py" --repo xinyu68/meituan-safe --path skills/meituan-safe
```

首次运行会在 Skill 目录的 `.runtime` 中创建隔离运行环境，拉取固定版本的 `meituan-cli` 并应用安全补丁，不会污染系统 Python。

## 支持的功能

- 以手机模式打开美团官方登录页，无需手动 F12 切换，由用户完成手机号、验证码或页面提供的登录方式
- 检查本地凭证状态、对照验证共享登录能否访问外卖与团购，以及读取当前账号保存的地址
- 解析中文地点，并在同名地点之间选择正确候选
- 通过美团 Passport 的标准 PKCE 授权获取独立团购令牌，并在电脑端生成二维码供美团 App 扫码确认
- 按地点、关键词、半径、评分和价格搜索到店餐厅或团购；支持共享登录或可选 Passport 授权，不强制两次登录
- 单独查询到店美食门店，返回门店官方人均、评分和距离
- 单独查询团购套餐，返回套餐价格、适用人数、评分、距离和套餐折算人均
- 官方人均缺失时，可按明确标注人数的套餐估算人均，例如双人餐 200 元按 100 元/人计算
- 查询外卖餐厅，并按配送费、起送价、配送时间、月售和优惠等条件筛选、排序
- 查看餐厅详情、比较多家餐厅、读取菜单，以及按菜名、价格、销量和库存搜索菜品
- 展示美团接口实际返回的优惠标签
- 按准确订单 ID 只读查询订单状态和订单详情
- 输出稳定的 JSON 结果、错误码、能力清单和参数 Schema
- 在一次搜索中复用后台浏览器，并缓存地点解析结果以减少重复等待

## 三种查询方式

| 用户需求 | 查询方式 | 价格口径 |
|---|---|---|
| 到店美食、附近餐厅、门店评分 | `food-search` | 仅使用美团返回的门店官方人均 |
| 团购、套餐、代金券 | `deals-search` | 优先官方人均，否则明确标注套餐折算人均 |
| 外卖、配送、起送价、菜品 | `nearby-search` | 使用外卖门店和菜单返回的价格 |

当用户同时要求“美食/团购”时，Skill 会分别查询并分区展示，不会把门店官方人均和团购套餐折算人均合并排名。

## 使用示例

安装后可以直接对 Codex 说：

> 登录美团

> 美团到店美食，找望京地铁站两公里内、评分 4.0 以上、人均 100 左右的烧烤

> 美团团购，找望京地铁站两公里内适合三个人的套餐，人均不超过 130

> 找附近配送费不超过 5 元、评分 4.6 以上的外卖餐厅

> 对比这两家餐厅的评分、距离、价格和菜单

## 更新技能

已安装的 Skill 不会自动跟随 GitHub 更新。更新时删除旧目录并重新安装：

```powershell
$SkillPath = Join-Path $env:USERPROFILE '.codex\skills\meituan-safe'
if (Test-Path -LiteralPath $SkillPath) {
    Remove-Item -LiteralPath $SkillPath -Recurse -Force
}
python "$env:USERPROFILE\.codex\skills\.system\skill-installer\scripts\install-skill-from-github.py" --repo xinyu68/meituan-safe --path skills/meituan-safe
```

## 开发与测试

```powershell
python skills/meituan-safe/scripts/bootstrap.py
skills/meituan-safe/.runtime/python/Scripts/python.exe -m unittest discover -s tests -v
skills/meituan-safe/.runtime/python/Scripts/python.exe skills/meituan-safe/scripts/meituan_cli.py capabilities
skills/meituan-safe/.runtime/python/Scripts/python.exe skills/meituan-safe/scripts/meituan_cli.py auth-check --location "北京望京地铁站" --keyword "汉堡"
skills/meituan-safe/.runtime/python/Scripts/python.exe skills/meituan-safe/scripts/meituan_cli.py deal-login
skills/meituan-safe/.runtime/python/Scripts/python.exe skills/meituan-safe/scripts/meituan_cli.py deal-login-status
```

主要命令及参数可以通过以下方式查看：

```powershell
skills/meituan-safe/.runtime/python/Scripts/python.exe skills/meituan-safe/scripts/meituan_cli.py schema
```

## 安全边界

本项目只提供餐厅、团购、菜单和订单的只读查询，不支持领券、购物车变更、下单、再次购买、选择支付方式、支付、验证码绕过或无人值守购买。

登录态保存在用户本机的独立 Chrome Profile 中，只向固定版本的本地运行时临时委派必要的认证信息。程序不会输出 Cookie、Token 或 storage state。除登录和人工验证外，浏览器均在后台无界面运行。

外卖使用普通登录凭证；到店查询默认保留并使用已有的 Passport 授权，没有 Passport 时尝试共享登录。可以使用 `--auth-source shared` 明确测试共享凭证，或 `--auth-source passport` 明确使用 Passport。它们是可选择的凭证来源，不代表美团必须分别登录。只使用已授权的 Passport 查团购时，无需先登录外卖 H5。

`status` 和 `login` 仅表示本地保存了凭证，不等于业务接口可用。`auth-check --location "北京望京地铁站" --keyword "汉堡"` 用同一共享凭证来源对两个业务各做一次只读搜索；要检查 `shared_login_verified` 和各项 `checks`，不能只看顶层 `ok`。旧凭证被拒绝时应先确认共享登录是否有效，再判断是否需要额外授权。403/429、风控或请求失败不应直接归为“登录过期”，也不会触发自动换凭证重试。

可选团购授权使用可审计的标准 HTTPS + PKCE 两阶段流程，电脑端为美团 App 授权链接生成本地二维码，不打包参考项目的私有依赖、混淆签名组件及下单能力。Passport token 不进入命令行或 JSON 输出；Windows 上使用当前用户的 DPAPI 加密保存在本机，搜索时仅通过子进程环境临时委派。仅在使用该来源被拒绝后，才考虑 `deal-login --force`。电脑版网页登录二维码到外卖登录态的转换尚未通过实测，不作为正式能力发布。

地点搜索结果是对美团返回的排序结果进行距离过滤，并不等同于完整地图普查；团购内容、价格和优惠是否可用应以实际门店页面及结算时展示为准。美团页面和同源接口可能变化，上游发生变化时可能需要更新固定版本与补丁。

## 实现参考

- [juntaochi/meituan-cli](https://github.com/juntaochi/meituan-cli)
- [ysansan98/meituan-living](https://github.com/ysansan98/meituan-living)
- [LewisChen1219/Meituan-Mcp-Server-WIP](https://github.com/LewisChen1219/Meituan-Mcp-Server-WIP)
- [xinyu68/xianyu-safe](https://github.com/xinyu68/xianyu-safe)
