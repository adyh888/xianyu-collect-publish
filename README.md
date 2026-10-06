# 闲鱼采集核心包（xianyu-collect-core）

对标"闲鱼搬运助手"类产品的采集核心，服务器版与本地桌面版共用。
2026-10-06 已完成端到端 POC 验证：**搜索 + 详情纯 HTTP 直连可用**。

## 架构

```
xianyu_collect/
├── mtop.py        # mtop 直连客户端（纯标准库）：签名、令牌自动刷新、频控、Cookie 回填
├── account.py     # 账号会话：cookie + UA 绑定持久化（data/accounts/<name>/session.json）
├── risk.py        # 风控熔断：命中即冷却（指数退避 5min→1h），成功归零
├── store.py       # SQLite：items / item_details / collect_log
├── collection.py  # 采集编排：search / detail / chain
└── login.py       # 自包含扫码登录（可选依赖 httpx+qrcode）
cli.py             # 命令行入口
poc/               # POC 验证脚本（含 x5sec 解题流程）
```

## 快速开始

```bash
uv venv --python 3.12
uv pip install --python .venv/bin/python httpx qrcode

# 1) 登录（二选一）
.venv/bin/python cli.py login                          # 扫码
.venv/bin/python cli.py import-session                 # 迁移 poc 已验证会话

# 2) 采集
.venv/bin/python cli.py search "iPhone 13" --pages 2
.venv/bin/python cli.py chain "iPhone 13" --top 3
.venv/bin/python cli.py items
```

## 铁律（实测教训，违反即被风控）

1. **UA 与账号精确绑定**：登录后用浏览器真实 UA（`navigator.userAgent`），存档永久复用，版本号不能猜写。
2. **频控**：mtop 客户端内置最小间隔 + 抖动；批量采集必须经 `RiskGuard`。
3. **风控不硬刚**：命中 RGV587/punish → 熔断冷却 → 用 `poc/x5sec_flow2.py`（patchright + 系统 Chrome）解题 → 整套 cookie 回填（不是只摘 x5sec）→ 立刻重试。
4. 数据目录（`data/`）含登录态，严禁入库/外传。

## 已验证接口

| 接口 | 用途 | 状态 |
|---|---|---|
| `mtop.taobao.idlemtopsearch.pc.search`/1.0 | 关键词搜索（30条/页） | ✅ 稳定 |
| `mtop.taobao.idle.pc.detail`/1.0 | 商品详情（标题/价格/描述/图片/quantity） | ✅ 稳定（偶发风控，走解题流程） |
| `mtop.idle.pc.backend.idleitem.publish` | 直连发布（上游已实现，P3 接入） | ⏳ 待移植 |
| `mtop.common.getTimestamp` | 免登录连通性检查 | ✅ |

## 待办（P1 → P2）

- [ ] 多 SKU 商品详情结构确认（`itemDO.simpleItem` / skuList）
- [ ] 店铺全店采集接口（`mtop.taobao.idle.shop.search`? 待探）
- [ ] 增量更新（按 publish_time/gmtCreate 去重增量）
- [ ] 发布通道：移植上游 `xianyu_direct_publisher` + 载荷构造器
- [ ] 服务器版壳：FastAPI 包一层 + 多账号调度
