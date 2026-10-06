"""闲鱼采集核心包。

设计约定（2026-10-06 POC 实测结论，勿凭感觉改）：
1. mtop 客户端纯标准库实现（urllib），服务器版零重依赖；
2. UA 必须与账号会话精确绑定：登录/首次成功采集时确定并永久存档，版本号都不能猜；
3. 风控策略：命中即熔断冷却，不做无谓重试；滑块走 patchright + 系统 Chrome 解题，
   解完整套 cookie 回填（不是只摘 x5sec）；
4. 详情数据结构：data.itemDO{title, desc, soldPrice, imageInfos[], quantity, simpleItem}。
"""
from .account import Account, AccountStore
from .mtop import MtopClient, MtopError
from .risk import RiskGuard, RiskBlocked
from .store import Store

__all__ = ["Account", "AccountStore", "MtopClient", "MtopError", "RiskGuard", "RiskBlocked", "Store"]
