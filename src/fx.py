"""积分汇率与最优兑换算法。

核心观点（也是这个项目存在的理由）：
麦当劳积分不是「分」，是一种有汇率、有坏账、还会过期作废的货币。
同一批积分，换 100 分档的板烧两件套是 0.188 元/分，
换 800 分档的板烧四件套只有 0.036 元/分 —— 差 5 倍。
但商城的排序逻辑不会告诉你这件事。

本模块做三件事：
1. 把每个积分商品换算成「元/积分」，建立汇率表
2. 用 0/1 背包算出在过期前把积分花掉的最优组合
3. 把过期积分折算成已经蒸发掉的现金
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# 商品名里的金额，如「18.8元2份麦辣鸡翅」「5.9元香芋派/菠萝派任选」
_AMOUNT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*元")
# 名称里的份数，如「2份麦辣鸡翅」
_COUNT_RE = re.compile(r"(\d+)\s*份")

# 明显不是「餐饮兑换券」的商品类目，排除出汇率统计
_EXCLUDE_CATS = {"玩具", "生日类派对", "读书会"}


@dataclass
class RedeemItem:
    """一个可兑换的积分商品。"""

    spu_id: int
    name: str
    point: int
    amount: float          # 等效现金价值（元）
    category: str
    status: int
    count: int = 1         # 标价覆盖的份数
    note: str = ""

    @property
    def fx(self) -> float:
        """每 1 积分的等效现金价值（元/分）。"""
        return self.amount / self.point if self.point > 0 else 0.0

    @property
    def cash_per_point_pct(self) -> float:
        return self.fx * 100


@dataclass
class Plan:
    """背包求出的兑换方案。"""

    picks: list[tuple[RedeemItem, int]] = field(default_factory=list)
    cost: int = 0
    value: float = 0.0
    budget: int = 0

    @property
    def remaining(self) -> int:
        return max(self.budget - self.cost, 0)

    @property
    def efficiency(self) -> float:
        return self.value / self.cost if self.cost else 0.0


def parse_amount(name: str) -> tuple[float, int]:
    """从商品名里抠出标价金额与份数。抠不出返回 (0, 1)。"""
    am = _AMOUNT_RE.search(name)
    amount = float(am.group(1)) if am else 0.0
    cm = _COUNT_RE.search(name)
    count = int(cm.group(1)) if cm else 1
    return amount, max(count, 1)


def build_fx_table(products: list[dict]) -> list[RedeemItem]:
    """把 mall-points-products 的原始返回整理成可计算的汇率表。

    过滤规则（宁可少算，也不给错建议）：
    - 排除 point <= 0（纯现金商品）
    - 排除名称里抠不出金额的（比如 6666 分的罗技键鼠，价值无法从名称判定）
    - 排除玩具/派对/读书会类目（不是餐饮兑换券，价值维度不同）
    - 同 spuId 去重
    """
    seen: set[int] = set()
    items: list[RedeemItem] = []
    for p in products:
        try:
            spu_id = int(p.get("spuId") or 0)
            point = int(float(p.get("point") or 0))
        except (TypeError, ValueError):
            continue
        if spu_id in seen or point <= 0:
            continue
        name = (p.get("spuName") or "").strip()
        cat = (p.get("catName") or "").strip()
        if cat in _EXCLUDE_CATS:
            continue
        amount, count = parse_amount(name)
        if amount <= 0:
            continue
        seen.add(spu_id)
        items.append(RedeemItem(
            spu_id=spu_id,
            name=name,
            point=point,
            amount=amount,
            category=cat,
            status=int(p.get("status") or 0),
            count=count,
            note=(p.get("selling") or "").strip()[:40],
        ))
    items.sort(key=lambda x: x.fx, reverse=True)
    return items


def best_plan(items: list[RedeemItem], budget: int,
              unique: bool = True) -> Plan:
    """在预算内求等效价值最大的兑换组合。

    unique=True  → 0/1 背包（每个商品最多兑 1 次，符合券类商品的一般限制）
    unique=False → 完全背包（可重复兑换）

    预算通常不超过几千分，DP 规模很小，用经典一维滚动数组即可。
    """
    budget = max(int(budget), 0)
    pool = [it for it in items if 0 < it.point <= budget]
    if not pool or budget == 0:
        return Plan(budget=budget)

    # 把金额放大成整数分，避免浮点误差污染 DP 比较
    scale = 100
    n = len(pool)
    dp = [0] * (budget + 1)
    # choice[i][b] 记录第 i 个物品在预算 b 下是否被选
    choice = [[False] * (budget + 1) for _ in range(n)]

    for i, it in enumerate(pool):
        cost = it.point
        val = int(round(it.amount * scale))
        if unique:
            for b in range(budget, cost - 1, -1):
                cand = dp[b - cost] + val
                if cand > dp[b]:
                    dp[b] = cand
                    choice[i][b] = True
        else:
            for b in range(cost, budget + 1):
                cand = dp[b - cost] + val
                if cand > dp[b]:
                    dp[b] = cand
                    choice[i][b] = True

    # 回溯
    picks: list[tuple[RedeemItem, int]] = []
    b = budget
    for i in range(n - 1, -1, -1):
        if choice[i][b]:
            it = pool[i]
            picks.append((it, 1))
            b -= it.point
    picks.reverse()

    total_cost = sum(it.point * q for it, q in picks)
    total_val = sum(it.amount * q for it, q in picks)
    return Plan(picks=picks, cost=total_cost, value=total_val, budget=budget)


def analyze_account(account: dict) -> dict:
    """把积分账户换算成「资产 / 坏账」视角。"""
    def num(key: str) -> float:
        try:
            return float(account.get(key) or 0)
        except (TypeError, ValueError):
            return 0.0

    return {
        "available": num("availablePoint"),
        "accumulated": num("accumulativePoint"),
        "expiring_this_month": num("currentMouthExpirePoint"),
        "expiring_next_month": num("nextMouthExpirePoint"),
        "expired": num("expiredPoint"),
        "expired_last_month": num("lastMouthExpirePoint"),
        "used": num("usedPoint"),
        "frozen": num("frozenPoint"),
    }


def worst_plan(items: list[RedeemItem], budget: int) -> Plan:
    """同样预算下「最差」的兑换组合：优先装汇率最低的商品。

    存在的唯一目的，是给最优解一个公平、可执行的对照 ——
    不能拿「用户换不起的 800 分档」去吓人，那是不成立的比较。
    这里所有被选商品都在预算内，用户真的能照着换。
    """
    budget = max(int(budget), 0)
    pool = sorted((it for it in items if 0 < it.point <= budget), key=lambda x: x.fx)
    picks: list[tuple[RedeemItem, int]] = []
    remain = budget
    for it in pool:
        if it.point <= remain:
            picks.append((it, 1))
            remain -= it.point
    return Plan(
        picks=picks,
        cost=budget - remain,
        value=sum(it.amount for it, _ in picks),
        budget=budget,
    )


def cash_loss(expired_points: float, reference_fx: float) -> float:
    """按给定汇率把已过期积分折算成蒸发掉的钱。"""
    return expired_points * reference_fx


@dataclass
class TierStat:
    """同一积分档位下所有兑换商品的汇率统计。"""

    point: int
    count: int
    avg_fx: float
    max_fx: float
    min_fx: float

    @property
    def avg_amount(self) -> float:
        return self.avg_fx * self.point


def tier_curve(items: list[RedeemItem]) -> list[TierStat]:
    """按积分档位聚合汇率，得到「档位 → 汇率」曲线。

    这条曲线是本项目的核心结论来源：如果它随档位单调下降，
    说明商城在用大额商品稀释积分价值 —— 一个用户靠肉眼永远发现不了的规律。
    """
    buckets: dict[int, list[RedeemItem]] = {}
    for it in items:
        buckets.setdefault(it.point, []).append(it)
    stats: list[TierStat] = []
    for point in sorted(buckets):
        group = buckets[point]
        fxs = [g.fx for g in group]
        stats.append(TierStat(
            point=point,
            count=len(group),
            avg_fx=sum(fxs) / len(fxs),
            max_fx=max(fxs),
            min_fx=min(fxs),
        ))
    return stats


def tier_decay(stats: list[TierStat]) -> dict:
    """量化「档位越高、汇率越差」的衰减幅度。

    只统计样本数 >= 2 的档位，单样本档位容易受个别定价干扰。
    """
    valid = [s for s in stats if s.count >= 2]
    if len(valid) < 2:
        return {"available": False}
    low, high = valid[0], valid[-1]
    ratio = low.avg_fx / high.avg_fx if high.avg_fx else 0.0
    # 单调性检查：相邻档位是否逐级下降
    drops = [
        (valid[i].avg_fx - valid[i + 1].avg_fx) / valid[i].avg_fx
        for i in range(len(valid) - 1)
        if valid[i].avg_fx > 0
    ]
    monotonic = all(d > 0 for d in drops)
    return {
        "available": True,
        "low_tier": low.point,
        "high_tier": high.point,
        "low_fx": low.avg_fx,
        "high_fx": high.avg_fx,
        "ratio": ratio,
        "monotonic": monotonic,
        "drop_pct": (1 - 1 / ratio) * 100 if ratio else 0.0,
    }

