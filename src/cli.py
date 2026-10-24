"""麦门汇率局 CLI 入口。

用法::

    python -m src.cli                    # 终端报告
    python -m src.cli --html out/r.html  # 生成可分享的 HTML 卡片
    python -m src.cli --budget 154.6     # 只用即将过期的积分做预算
    python -m src.cli --city 上海 --keyword 五角场
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fx import (  # noqa: E402
    analyze_account, best_plan, build_fx_table, cash_loss, tier_curve, tier_decay,
    worst_plan,
)
from mcp_client import McdMcpClient, McpError  # noqa: E402


def load_dotenv() -> None:
    """极简 .env 读取，避免为了一个变量引入 python-dotenv。"""
    root = Path(__file__).resolve().parent.parent
    env_file = root / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def collect(city: str, keyword: str) -> dict:
    """一次性把报告需要的所有数据取齐。"""
    with McdMcpClient() as mcd:
        account_raw = mcd.account()
        products = mcd.mall_products()
        try:
            coupons = mcd.my_coupons()
        except McpError:
            coupons = []
        stores = []
        try:
            stores = mcd.nearby_stores(city=city, keyword=keyword)
        except McpError:
            pass

    items = build_fx_table(products)
    acct = analyze_account(account_raw)
    stats = tier_curve(items)
    decay = tier_decay(stats)

    return {
        "account": acct,
        "raw_account": account_raw,
        "items": items,
        "tiers": stats,
        "decay": decay,
        "coupons": coupons,
        "stores": stores,
    }


def build_report(data: dict, budget_override: float | None = None) -> dict:
    items = data["items"]
    acct = data["account"]
    decay = data["decay"]

    # 基准汇率：用「用户实际会执行的小额档平均汇率」，而不是全场中位数。
    # 中位数会被数量占优的大额商品拉低，导致「蒸发金额」被低估、
    # 让报告显得比实际温和 —— 那是自欺。
    if decay.get("available"):
        base_fx = decay["low_fx"]
    else:
        fxs = sorted(it.fx for it in items) or [0.0]
        base_fx = fxs[len(fxs) // 2]

    expiring = acct["expiring_this_month"]
    available = acct["available"]

    # 预算优先使用「即将过期」的部分：这才是真正有截止日期的钱
    if budget_override is not None:
        budget = int(budget_override)
        budget_label = f"指定预算 {budget} 分"
    elif expiring > 0 and expiring <= available:
        budget = int(expiring)
        budget_label = f"本月将过期 {expiring:g} 分"
    else:
        budget = int(available)
        budget_label = f"全部可用 {available:g} 分"

    plan = best_plan(items, budget, unique=True)
    plan_inf = best_plan(items, budget, unique=False)
    bad = worst_plan(items, budget)

    return {
        "budget": budget,
        "budget_label": budget_label,
        "plan": plan,
        "plan_inf": plan_inf,
        "worst": bad,
        "base_fx": base_fx,
        "best_item": items[0] if items else None,
        "expired_cash": cash_loss(acct["expired"], base_fx),
        "expiring_cash": cash_loss(expiring, base_fx),
        "gap": plan.value - bad.value,
        "gap_ratio": (plan.value / bad.value) if bad.value else 0.0,
        "decay": decay,
        "tiers": data["tiers"],
        "items": items,
        "account": acct,
        "coupons": data["coupons"],
        "stores": data["stores"],
    }


def render_text(r: dict) -> str:
    acct = r["account"]
    decay = r["decay"]
    plan = r["plan"]
    L: list[str] = []
    add = L.append

    add("=" * 62)
    add("  麦门汇率局 · 你的积分是一种货币")
    add("=" * 62)
    add("")
    add("【资产】")
    add(f"  可用积分      {acct['available']:>10.1f}")
    add(f"  其中本月将过期 {acct['expiring_this_month']:>9.1f}  <- 有截止日期的钱")
    add(f"  累计获得      {acct['accumulated']:>10.1f}")
    add(f"  已过期作废    {acct['expired']:>10.1f}  <- ≈ ¥{r['expired_cash']:.2f} 已经蒸发掉")
    add("")

    if decay.get("available"):
        add("【汇率衰减】积分面额越大，汇率越差")
        for t in r["tiers"]:
            bar = "█" * max(int(t.avg_fx * 100), 1)
            add(f"  {t.point:>4} 分档 ({t.count:>2}件)  {t.avg_fx:.4f} 元/分  {bar}")
        add("")
        add(f"  {decay['low_tier']}分档 → {decay['high_tier']}分档 汇率掉 "
            f"{(1 - decay['high_fx']/decay['low_fx'])*100:.0f}%"
            f"（{decay['low_fx']/decay['high_fx']:.1f} 倍差距）")
        add(f"  单调递减: {'是' if decay['monotonic'] else '否'}")
        add("")

    add("【兑换效率榜】")
    add(f"  {'商品':24} {'积分':>5} {'价值':>7} {'元/分':>8}")
    for it in r["items"][:8]:
        add(f"  {it.name[:22]:24} {it.point:>5} {it.amount:>7.1f} {it.fx:>8.4f}")
    add("")

    add(f"【最优花法】预算 = {r['budget_label']}")
    if plan.picks:
        for it, _ in plan.picks:
            add(f"  · {it.name}  ({it.point} 分 / 价值 ¥{it.amount:.1f})")
        add(f"  → 花掉 {plan.cost} 分，换回 ¥{plan.value:.1f}，剩 {plan.remaining} 分")
        add(f"  → 综合汇率 {plan.efficiency:.4f} 元/分")
        if r["gap"] > 0.5:
            add("")
            add(f"  对照：同样 {r['worst'].cost} 分，闭眼换能拿到的最差组合是")
            for it, _ in r["worst"].picks:
                add(f"    · {it.name}  ({it.point} 分 / ¥{it.amount:.1f})")
            add(f"  → 只有 ¥{r['worst'].value:.1f}，差 ¥{r['gap']:.1f}"
                f"（最优是最差的 {r['gap_ratio']:.1f} 倍）")
    else:
        add("  当前积分不足任何档位，建议先点餐攒分")
    add("")

    if r["coupons"]:
        add("【券】")
        for c in r["coupons"][:5]:
            price = f"¥{c['price']}" if c.get("price") else "—"
            add(f"  · {c['title']}  {price}  有效: {c.get('valid') or '—'}")

    add("")
    add("-" * 62)
    add("数据来自麦当劳 MCP 实时返回，仅供参考；积分与券的实际使用规则以官方为准。")
    return "\n".join(L)


def main() -> int:
    load_dotenv()
    ap = argparse.ArgumentParser(description="麦门汇率局：把积分当货币算汇率")
    ap.add_argument("--html", metavar="PATH", help="生成 HTML 报告卡片")
    ap.add_argument("--json", metavar="PATH", help="导出原始分析结果 JSON")
    ap.add_argument("--budget", type=float, default=None, help="指定预算（积分）")
    ap.add_argument("--city", default="上海", help="门店搜索城市")
    ap.add_argument("--keyword", default="", help="门店搜索关键词")
    args = ap.parse_args()

    try:
        data = collect(args.city, args.keyword)
    except McpError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 2

    if not data["items"]:
        print("[错误] 未能从商城拿到任何可解析的积分商品，报告无法生成。", file=sys.stderr)
        return 3

    report = build_report(data, args.budget)
    print(render_text(report))

    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "account": report["account"],
            "budget": report["budget"],
            "plan": {
                "cost": report["plan"].cost,
                "value": report["plan"].value,
                "efficiency": report["plan"].efficiency,
                "picks": [
                    {"name": it.name, "point": it.point, "amount": it.amount,
                     "fx": round(it.fx, 4)}
                    for it, _ in report["plan"].picks
                ],
            },
            "decay": report["decay"],
            "tiers": [
                {"point": t.point, "count": t.count, "avg_fx": round(t.avg_fx, 4)}
                for t in report["tiers"]
            ],
            "expired_cash": round(report["expired_cash"], 2),
            "items": [
                {"name": it.name, "point": it.point, "amount": it.amount,
                 "fx": round(it.fx, 4), "cat": it.category}
                for it in report["items"]
            ],
        }
        Path(args.json).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n[json] 已写入 {args.json}")

    if args.html:
        from report import render_html
        html = render_html(report)
        out = Path(args.html)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(html, encoding="utf-8")
        print(f"[html] 已写入 {out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
