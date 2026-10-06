#!/usr/bin/env python3
"""闲鱼采集 CLI。

用法：
    python3 cli.py import-session --cookie poc/cookie.txt --ua poc/ua.txt   # 迁移已验证会话
    python3 cli.py login [--name default]                                  # 扫码登录
    python3 cli.py search "iPhone 13" [--pages 1] [--account default]      # 搜索入库
    python3 cli.py detail <item_id> [--account default]                    # 详情入库
    python3 cli.py chain "iPhone 13" [--top 3]                             # 搜索→详情
    python3 cli.py items [--limit 20]                                      # 查看已采集
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from xianyu_collect.account import Account, AccountStore
from xianyu_collect.collection import Collector
from xianyu_collect.mtop import MtopClient, MtopError
from xianyu_collect.risk import RiskBlocked, RiskGuard
from xianyu_collect.store import Store

DATA = ROOT / "data"


def build(account_name: str):
    accounts = AccountStore(DATA)
    account = accounts.load(account_name)
    if not account:
        print(f"❌ 账号 {account_name} 不存在，先 login 或 import-session")
        sys.exit(1)
    client = MtopClient(account.cookies, ua=account.ua)
    return accounts, account, Collector(client, Store(DATA / "collect.db"), RiskGuard(account_name))


def main() -> int:
    parser = argparse.ArgumentParser(description="闲鱼采集 CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("import-session", help="从 poc 文件迁移已验证会话")
    p.add_argument("--cookie", default=str(ROOT / "poc/cookie.txt"))
    p.add_argument("--ua", default=str(ROOT / "poc/ua.txt"))
    p.add_argument("--name", default="default")

    p = sub.add_parser("login", help="扫码登录")
    p.add_argument("--name", default="default")

    p = sub.add_parser("search", help="关键词搜索入库")
    p.add_argument("keyword")
    p.add_argument("--pages", type=int, default=1)
    p.add_argument("--account", default="default")

    p = sub.add_parser("detail", help="商品详情入库")
    p.add_argument("item_id")
    p.add_argument("--account", default="default")

    p = sub.add_parser("chain", help="搜索→详情完整链路")
    p.add_argument("keyword")
    p.add_argument("--top", type=int, default=1)
    p.add_argument("--account", default="default")

    p = sub.add_parser("shop", help="店铺全店采集（卖家 userId）")
    p.add_argument("user_id")
    p.add_argument("--pages", type=int, default=5)
    p.add_argument("--account", default="default")

    p = sub.add_parser("items", help="查看已采集商品")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--keyword", default="")

    args = parser.parse_args()

    if args.cmd == "import-session":
        cookie_str = Path(args.cookie).read_text(encoding="utf-8").strip()
        ua = Path(args.ua).read_text(encoding="utf-8").strip() if Path(args.ua).exists() else ""
        account = Account.from_cookie_string(args.name, cookie_str, ua=ua or None)
        # 从 cookie 里补 unb
        account.unb = account.cookies.get("unb", "")
        path = AccountStore(DATA).save(account)
        print(f"✅ 会话已导入: {path}（unb={account.unb}, ua 绑定: {account.ua[:60]}...）")
        return 0

    if args.cmd == "login":
        from xianyu_collect.login import qr_login
        result = asyncio.run(qr_login(qr_png_path=ROOT / "login_qr.png"))
        if not result.cookies:
            print("❌ 登录未完成")
            return 2
        accounts = AccountStore(DATA)
        existing = accounts.load(args.name)
        account = Account(
            name=args.name,
            cookies=result.cookies,
            ua=(existing.ua if existing else ""),
            unb=result.unb,
            updated_at=__import__("time").time(),
        )
        if not account.ua:
            print("⚠️ 首次采集后请把浏览器真实 UA 写入 session.json（UA 纪律）")
        path = accounts.save(account)
        print(f"✅ 登录成功 unb={result.unb}，会话已保存: {path}")
        return 0

    if args.cmd == "search":
        accounts, account, collector = build(args.account)
        total = 0
        for page in range(1, args.pages + 1):
            total += collector.search(args.keyword, page=page)
        print(f"✅ 搜索「{args.keyword}」共入库 {total} 条（{args.pages} 页）")
        return 0

    if args.cmd == "detail":
        accounts, account, collector = build(args.account)
        detail = collector.detail(args.item_id)
        print(f"✅ 详情入库: {detail['title'][:40]} | 价格 {detail['price']} | 图片 {len(detail['images'])} 张")
        return 0

    if args.cmd == "chain":
        accounts, account, collector = build(args.account)
        details = collector.chain(args.keyword, top=args.top)
        for d in details:
            print(f"  - {d['item_id']} {d['title'][:36]} ¥{d['price']} 图片{len(d['images'])}张")
        print(f"✅ 链路完成：{len(details)}/{args.top} 条详情入库")
        return 0 if len(details) == args.top else 1

    if args.cmd == "shop":
        accounts, account, collector = build(args.account)
        total = collector.seller_items(args.user_id, max_pages=args.pages)
        print(f"✅ 店铺 {args.user_id} 入库 {total} 条（keyword=seller:{args.user_id}，用 items --keyword seller:{args.user_id} 查看）")
        return 0

    if args.cmd == "items":
        store = Store(DATA / "collect.db")
        for row in store.list_items(limit=args.limit, keyword=args.keyword):
            print(f"  {row['item_id']}  ¥{str(row['price']):>8}  想要{row['want_count']:>4}  {row['title'][:40]}")
        return 0

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RiskBlocked as exc:
        print(f"⛔ {exc}")
        sys.exit(3)
    except MtopError as exc:
        print(f"❌ 采集失败: {exc}")
        if exc.kind == "risk":
            print("   命中风控：等待冷却后重试，或运行 x5sec 解题流程（poc/x5sec_flow2.py）")
        sys.exit(2)
