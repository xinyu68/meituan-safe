# Codex 美团安全助手

`meituan-safe` 是一个面向 Codex 的本地 Skill，用于登录美团、解析地点，并按指定半径分别搜索到店美食门店、到店团购或外卖餐厅、查看餐厅详情和菜单。

当前支持：

- 直达官方登录页、可见但不主动抢焦点且非置顶的浏览器登录，不要求复制 Cookie
- 登录状态和保存地址查询
- 中文地点解析与候选选择
- WGS-84 到 GCJ-02 坐标转换
- 指定地点、关键词、半径和数量搜索餐厅
- 单独搜索到店美食门店；门店官方人均与套餐折算人均严格分开
- 搜索到店团购套餐，返回门店、套餐、团购价、评分和距离
- 优先使用官方人均；缺失时按套餐人数估算并标明来源，例如双人餐 200 元按 100 元/人计算
- 多页抓取；按评分、距离、配送费、起送价、配送时间、月售和优惠筛选
- 按距离、评分、费用、月售或本地综合推荐分排序
- 餐厅详情、带评分说明的餐厅对比和菜单查询
- 按菜名、价格、月售和库存状态搜索菜品
- 展示接口实际返回的优惠标签
- 按准确订单 ID 只读查询订单状态和订单详情
- 稳定 JSON 响应、错误码、能力发现与参数 Schema
- 搜索时复用单个后台浏览器完成坐标设置和多页抓取，并缓存地点解析结果

示例：

```powershell
python skills/meituan-safe/scripts/bootstrap.py
skills/meituan-safe/.runtime/python/Scripts/python.exe skills/meituan-safe/scripts/meituan_cli.py login
skills/meituan-safe/.runtime/python/Scripts/python.exe skills/meituan-safe/scripts/meituan_cli.py food-search --location "北京望京地铁站" --keyword "烧烤" --radius 1000 --min-rating 4.0 --limit 20
skills/meituan-safe/.runtime/python/Scripts/python.exe skills/meituan-safe/scripts/meituan_cli.py nearby-search --location "北京望京地铁站" --keyword "烧烤" --radius 1000 --pages 3 --min-rating 4.6 --max-delivery-fee 5 --sort-by recommended --limit 20
skills/meituan-safe/.runtime/python/Scripts/python.exe skills/meituan-safe/scripts/meituan_cli.py deals-search --location "北京望京地铁站" --keyword "烧烤" --radius 1000 --min-rating 4.0 --limit 20
skills/meituan-safe/.runtime/python/Scripts/python.exe skills/meituan-safe/scripts/meituan_cli.py menu-search --restaurant-id <餐厅ID> --keyword "羊肉串" --max-price 30 --in-stock --sort-by sales
skills/meituan-safe/.runtime/python/Scripts/python.exe skills/meituan-safe/scripts/meituan_cli.py menus --restaurant-ids <餐厅ID1> <餐厅ID2>
skills/meituan-safe/.runtime/python/Scripts/python.exe skills/meituan-safe/scripts/meituan_cli.py order-status --order-id <订单ID>
```

当前不支持领券、购物车变更、下单、支付、验证码绕过或无人值守购买。附近搜索是对美团排序结果进行距离过滤，不等同于完整地图普查。

实现参考：

- https://github.com/juntaochi/meituan-cli
- https://github.com/LewisChen1219/Meituan-Mcp-Server-WIP
- https://github.com/xinyu68/xianyu-safe
