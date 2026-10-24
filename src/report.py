"""把分析结果渲染成一张自包含的 HTML 卡片。

为什么是 HTML 而不是图片：
1. 不依赖任何外部字体/图片/CDN，断网也能开，截图效果稳定
2. 用户可以自己改文案转发，比固定图片更有传播力
3. 零依赖是参赛项目的加分项，渲染层也不例外
"""

from __future__ import annotations

import html
import re
from datetime import datetime

# 麦当劳品牌色，仅用于视觉贴合，不代表官方背书
MCD_RED = "#DA291C"
MCD_YELLOW = "#FFC72C"
INK = "#171717"
MUTED = "#6E6E73"
LINE = "#EDE6DD"


def _esc(s) -> str:
    return html.escape(str(s), quote=True)


def _bar_chart(tiers: list) -> str:
    """档位 → 汇率 的柱状图。纯 SVG，不引图表库。"""
    if not tiers:
        return ""
    w, h = 620, 190
    pad_l, pad_b = 44, 34
    plot_w = w - pad_l - 16
    plot_h = h - pad_b - 22
    n = len(tiers)
    slot = plot_w / n
    bar_w = min(slot * 0.52, 54)
    vmax = max(t.avg_fx for t in tiers) or 1.0

    parts = [f'<svg viewBox="0 0 {w} {h}" width="100%" role="img" '
             f'aria-label="积分档位与汇率关系">']
    # 网格线
    for i in range(4):
        y = 22 + plot_h * i / 3
        val = vmax * (3 - i) / 3
        parts.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{w-16}" y2="{y:.1f}" '
                     f'stroke="{LINE}" stroke-width="1"/>')
        parts.append(f'<text x="{pad_l-8}" y="{y+4:.1f}" text-anchor="end" '
                     f'font-size="10" fill="{MUTED}">{val:.3f}</text>')
    # 柱子
    for i, t in enumerate(tiers):
        bh = max(plot_h * (t.avg_fx / vmax), 3)
        x = pad_l + slot * i + (slot - bar_w) / 2
        y = 22 + plot_h - bh
        # 最后一个档位（最大面额）用灰色示警，前面的用品牌红
        color = MUTED if i == n - 1 else MCD_RED
        parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" '
                     f'height="{bh:.1f}" rx="3" fill="{color}" opacity="0.9"/>')
        parts.append(f'<text x="{x+bar_w/2:.1f}" y="{y-6:.1f}" text-anchor="middle" '
                     f'font-size="11" font-weight="600" fill="{INK}">'
                     f'{t.avg_fx:.3f}</text>')
        parts.append(f'<text x="{x+bar_w/2:.1f}" y="{22+plot_h+18:.1f}" '
                     f'text-anchor="middle" font-size="11" fill="{MUTED}">'
                     f'{t.point}分</text>')
    parts.append("</svg>")
    return "".join(parts)


def render_html(r: dict) -> str:
    acct = r["account"]
    decay = r.get("decay", {})
    plan = r["plan"]
    worst = r["worst"]
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    # 方案列表
    plan_rows = "".join(
        f'<li><span class="nm">{_esc(it.name)}</span>'
        f'<span class="pt">{it.point} 分</span>'
        f'<span class="am">¥{it.amount:.1f}</span></li>'
        for it, _ in plan.picks
    ) or '<li class="empty">积分不足任何档位，先点餐攒分</li>'

    worst_rows = "".join(
        f'<li><span class="nm">{_esc(it.name)}</span>'
        f'<span class="pt">{it.point} 分</span>'
        f'<span class="am">¥{it.amount:.1f}</span></li>'
        for it, _ in worst.picks
    )

    def _short_valid(v: str | None) -> str:
        """券的有效期原文很长（含时分、星期、时段），卡片上压成 MM/DD–MM/DD。"""
        if not v:
            return "—"
        dates = re.findall(r"\d{4}-(\d{2})-(\d{2})", v)
        if len(dates) >= 2:
            a, b = dates[0], dates[-1]
            return f"{a[0]}/{a[1]}–{b[0]}/{b[1]}"
        if len(dates) == 1:
            return f"{dates[0][0]}/{dates[0][1]} 起"
        return v.split(" 周")[0].strip()[:18]

    coupon_rows = "".join(
        f'<li><span class="nm">{_esc(c["title"])}</span>'
        f'<span class="cv">{_esc(_short_valid(c.get("valid")))}</span></li>'
        for c in r["coupons"][:4]
    ) or '<li class="empty">当前账户无可用券</li>'

    # 汇率衰减结论
    if decay.get("available"):
        decay_line = (
            f'{decay["low_tier"]} 分档汇率 {decay["low_fx"]:.4f} 元/分，'
            f'{decay["high_tier"]} 分档只有 {decay["high_fx"]:.4f} 元/分 —— '
            f'<b>掉了 {(1 - decay["high_fx"]/decay["low_fx"])*100:.0f}%</b>，'
            f'相差 {decay["low_fx"]/decay["high_fx"]:.1f} 倍。'
        )
        monotonic_badge = (
            '<span class="badge ok">单调递减已确认</span>' if decay.get("monotonic")
            else '<span class="badge warn">非严格单调</span>'
        )
    else:
        decay_line = "当前商品池样本不足，无法计算档位衰减。"
        monotonic_badge = ""

    gap_block = ""
    if r["gap"] > 0.5 and worst.cost > 0:
        gap_block = f"""
    <section class="vs">
      <h3>对照组：同样 {worst.cost} 分，闭眼换会怎样</h3>
      <ul class="rows muted-rows">{worst_rows}</ul>
      <p class="vs-sum">只有 <b>¥{worst.value:.1f}</b> —— 比最优方案少拿
        <b class="neg">¥{r["gap"]:.1f}</b>（最优是最差的 {r["gap_ratio"]:.1f} 倍）</p>
    </section>"""

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>麦门汇率局 · 积分汇率报告</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
                 "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
    background: #F7F3EC; color: {INK};
    display: flex; justify-content: center; padding: 28px 16px;
    -webkit-font-smoothing: antialiased;
  }}
  .card {{
    width: 100%; max-width: 720px; background: #fff;
    border-radius: 18px; overflow: hidden;
    box-shadow: 0 10px 40px rgba(23,23,23,.08), 0 1px 3px rgba(23,23,23,.06);
  }}
  header {{
    background: linear-gradient(135deg, {MCD_RED} 0%, #B71C12 100%);
    color: #fff; padding: 26px 28px 24px; position: relative;
  }}
  header::after {{
    content: ""; position: absolute; right: -30px; top: -30px;
    width: 140px; height: 140px; border-radius: 50%;
    background: {MCD_YELLOW}; opacity: .16;
  }}
  h1 {{ font-size: 22px; letter-spacing: .5px; }}
  .sub {{ font-size: 13px; opacity: .85; margin-top: 7px; line-height: 1.6; }}
  .stamp {{ font-size: 11px; opacity: .65; margin-top: 12px; }}

  section {{ padding: 20px 28px; border-bottom: 1px solid {LINE}; }}
  section:last-of-type {{ border-bottom: none; }}
  h3 {{
    font-size: 13px; font-weight: 600; color: {MUTED};
    letter-spacing: .4px; margin-bottom: 14px;
  }}

  .assets {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }}
  .ast {{
    background: #FAF8F5; border-radius: 12px; padding: 14px 12px; text-align: center;
  }}
  .ast .k {{ font-size: 11px; color: {MUTED}; margin-bottom: 7px; }}
  .ast .v {{ font-size: 25px; font-weight: 700; font-variant-numeric: tabular-nums; }}
  .ast .u {{ font-size: 11px; color: {MUTED}; margin-top: 3px; }}
  .ast.hot {{ background: #FFF3D6; }}
  .ast.hot .v {{ color: #A87400; }}
  .ast.bad {{ background: #FDECEA; }}
  .ast.bad .v {{ color: {MCD_RED}; }}

  .chart {{ margin: 2px 0 6px; }}
  .decay-note {{
    font-size: 13px; line-height: 1.7; color: #3A3A3C;
    background: #FAF8F5; border-left: 3px solid {MCD_RED};
    padding: 12px 14px; border-radius: 0 8px 8px 0; margin-top: 10px;
  }}
  .badge {{
    display: inline-block; font-size: 11px; padding: 2px 8px;
    border-radius: 20px; margin-left: 6px; vertical-align: middle;
  }}
  .badge.ok {{ background: #E7F6EC; color: #1B7A3D; }}
  .badge.warn {{ background: #FDF0D5; color: #8A6100; }}

  ul.rows {{ list-style: none; }}
  ul.rows li {{
    display: grid; grid-template-columns: 1fr auto auto; gap: 12px;
    align-items: center; padding: 10px 0; border-bottom: 1px dashed {LINE};
    font-size: 13.5px;
  }}
  ul.rows li:last-child {{ border-bottom: none; }}
  .nm {{ color: #2C2C2E; }}
  .pt {{ color: {MUTED}; font-variant-numeric: tabular-nums; font-size: 12.5px; }}
  .am {{ font-weight: 600; color: {MCD_RED}; font-variant-numeric: tabular-nums; }}
  .muted-rows .am {{ color: {MUTED}; }}
  li.empty {{ display: block; color: {MUTED}; font-size: 13px; }}

  .plan-total {{
    display: flex; justify-content: space-between; align-items: baseline;
    margin-top: 14px; padding-top: 14px; border-top: 2px solid {INK};
    font-size: 13px; color: {MUTED};
  }}
  .plan-total b {{ font-size: 19px; color: {INK}; }}
  .plan-total .gain b {{ color: #1B7A3D; }}
  .plan-total .eff {{ font-variant-numeric: tabular-nums; }}

  .vs {{ background: #FCFBF9; }}
  .vs-sum {{ font-size: 13.5px; margin-top: 12px; line-height: 1.6; }}
  .neg {{ color: {MCD_RED}; }}
  .cv {{ font-size: 11px; color: {MUTED}; text-align: right; white-space: nowrap; }}

  footer {{
    padding: 18px 28px 24px; font-size: 11px; color: #9A9A9E;
    line-height: 1.75; background: #FAF8F5;
  }}
  @media (max-width: 560px) {{
    header, section, footer {{ padding-left: 18px; padding-right: 18px; }}
    .assets {{ grid-template-columns: 1fr; }}
    .ast {{ display: flex; justify-content: space-between; align-items: center; }}
    .ast .k {{ margin: 0; }}
  }}
</style>
</head>
<body>
<div class="card">
  <header>
    <h1>麦门汇率局</h1>
    <p class="sub">你的麦麦积分不是「分」，是一种会过期、有汇率、还能被悄悄稀释的货币。<br>
       这份报告只做一件事：把它的真实汇率算清楚。</p>
    <p class="stamp">数据生成于 {now} · 麦当劳 MCP 实时接口</p>
  </header>

  <section>
    <h3>资产盘点</h3>
    <div class="assets">
      <div class="ast">
        <div class="k">可用积分</div>
        <div class="v">{acct['available']:.0f}</div>
        <div class="u">累计 {acct['accumulated']:.0f}</div>
      </div>
      <div class="ast hot">
        <div class="k">本月将过期</div>
        <div class="v">{acct['expiring_this_month']:.0f}</div>
        <div class="u">≈ ¥{r['expiring_cash']:.0f} 有截止日期</div>
      </div>
      <div class="ast bad">
        <div class="k">已经蒸发</div>
        <div class="v">{acct['expired']:.0f}</div>
        <div class="u">≈ ¥{r['expired_cash']:.0f} 再也拿不回</div>
      </div>
    </div>
  </section>

  <section>
    <h3>汇率衰减曲线 {monotonic_badge}</h3>
    <div class="chart">{_bar_chart(r['tiers'])}</div>
    <p class="decay-note">{decay_line}</p>
  </section>

  <section>
    <h3>最优兑换方案 · 预算 {_esc(r['budget_label'])}</h3>
    <ul class="rows">{plan_rows}</ul>
    <div class="plan-total">
      <span>花掉 <b>{plan.cost}</b> 分 → 换回 <b class="gain">¥{plan.value:.1f}</b></span>
      <span class="eff">综合汇率 {plan.efficiency:.4f} 元/分</span>
    </div>
  </section>
{gap_block}
  <section>
    <h3>账户内的券</h3>
    <ul class="rows">{coupon_rows}</ul>
  </section>

  <footer>
    本报告由「麦门汇率局」基于麦当劳 MCP 实时数据生成，为 1024 程序员节创意开发大赛参赛作品，
    非麦当劳官方产品。<br>
    汇率 = 兑换商品标价 ÷ 所需积分，仅衡量「同等积分能换到多少等价餐品」，
    不构成投资、营养或医疗建议。积分与券的实际可用范围、限购与有效期以麦当劳官方渠道为准；<br>
    「已蒸发」为机会成本估算，不代表实际现金损失。
  </footer>
</div>
</body>
</html>"""
