#!/usr/bin/env python3
"""
plot_terrain.py — OI地形とGEXプロファイルを1枚のPNGに描く (Discord添付用)

advisor.py から --plot で呼ばれる。matplotlib が無い環境では import に失敗するので
呼び出し側で握りつぶすこと。
"""

# ---- 暗背景のトークン (dataviz reference palette / dark) ----
SURFACE = "#1a1a19"
INK = "#ffffff"
INK_2 = "#c3c2b7"
MUTED = "#898781"
GRID = "#2c2c2a"
AXIS = "#383835"
CALL = "#3987e5"      # categorical slot 1
PUT = "#d95926"       # categorical slot 2
POS = "#3987e5"       # diverging: 正のガンマ
NEG = "#e34948"       # diverging: 負のガンマ

BAND = 0.25           # 現値から±この割合のストライクだけ描く


def _font():
    from matplotlib import font_manager

    for name in ("Noto Sans CJK JP", "Noto Sans JP", "Droid Sans Fallback",
                 "IPAGothic", "DejaVu Sans"):
        try:
            font_manager.findfont(name, fallback_to_default=False)
            return name
        except Exception:
            continue
    return "DejaVu Sans"


def _usd(x):
    a = abs(x)
    if a >= 1e9:
        return f"${x/1e9:.1f}B"
    if a >= 1e6:
        return f"${x/1e6:.0f}M"
    if a >= 1e3:
        return f"${x/1e3:.0f}K"
    return f"${x:.0f}"


def render(path, spot, book, m, g, instruments, now, net_gex_at=None):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FuncFormatter

    plt.rcParams.update({
        "font.family": _font(),
        "figure.facecolor": SURFACE,
        "axes.facecolor": SURFACE,
        "savefig.facecolor": SURFACE,
        "text.color": INK_2,
        "axes.labelcolor": MUTED,
        "xtick.color": MUTED,
        "ytick.color": MUTED,
        "axes.edgecolor": AXIS,
        "grid.color": GRID,
        "font.size": 11,
        "text.parse_math": False,   # ドル記号を数式として解釈させない
    })

    lo, hi = spot * (1 - BAND), spot * (1 + BAND)
    strikes = sorted(k for k in book if lo <= k <= hi)
    if not strikes:
        return None

    fig, (ax1, ax2) = plt.subplots(
        1, 2, figsize=(15, 8.5), dpi=110, sharey=True,
        gridspec_kw={"width_ratios": [1.5, 1], "wspace": 0.06})

    # ---------------- 左: ストライク別 OI (Put=左 / Call=右) ----------------
    gaps = [b - a for a, b in zip(strikes, strikes[1:])] or [1000]
    h = max(min(gaps) * 0.62, spot * 0.0035)
    calls = [book[k]["call"] for k in strikes]
    puts = [-book[k]["put"] for k in strikes]

    ax1.barh(strikes, calls, height=h, color=CALL, label="Call OI")
    ax1.barh(strikes, puts, height=h, color=PUT, label="Put OI")
    ax1.axvline(0, color=AXIS, lw=1)
    ax1.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{abs(v):,.0f}"))
    ax1.set_xlabel("建玉 (枚)   ← Put   /   Call →", color=MUTED, labelpad=10)
    ax1.grid(axis="x", lw=0.8, alpha=0.6)
    ax1.set_axisbelow(True)
    ax1.set_title("ストライク別 建玉 — どこに壁と支持があるか",
                  color=INK, fontsize=13, pad=14, loc="left")
    leg = ax1.legend(loc="lower right", frameon=False, labelcolor=INK_2)
    for t in leg.get_texts():
        t.set_color(INK_2)

    # 注記をバーの外に置くぶんの余白を確保する
    xmax = max(max(calls, default=0), max(-p for p in puts) if puts else 0) * 1.45
    ax1.set_xlim(-xmax, xmax)

    # 注記は「効いている3点」だけ。バーに重ならないよう、必ずバーの外側に置く
    pad = xmax * 0.025

    def _dy(k):
        # 現値の白線と重なる位置なら少し上にずらす
        return (hi - lo) * 0.016 if abs(k - spot) < (hi - lo) * 0.013 else 0.0

    def note_right(k, text, color):
        ax1.annotate(text, xy=(book[k]["call"] + pad, k + _dy(k)), color=color,
                     fontsize=10.5, va="center", ha="left")

    def note_left(k, text, color):
        ax1.annotate(text, xy=(-book[k]["put"] - pad, k + _dy(k)), color=color,
                     fontsize=10.5, va="center", ha="right")

    if m.get("call_walls"):
        k = m["call_walls"][0][0]
        if lo <= k <= hi:
            note_right(k, f"▲ 壁 ${k:,.0f}", CALL)
    if m.get("put_supports"):
        k = m["put_supports"][0][0]
        if lo <= k <= hi:
            note_left(k, f"▼ 支持 ${k:,.0f}", PUT)
    if m.get("max_pain") and lo <= m["max_pain"] <= hi:
        k = m["max_pain"]
        if k in book:
            note_left(k, f"◎ Max Pain ${k:,.0f}", INK_2)

    # 現値。右側は壁の注記と衝突しうるので左の余白に置く
    for ax in (ax1, ax2):
        ax.axhline(spot, color=INK, lw=1.6, alpha=0.9)
    ax1.annotate(f"現在値 ${spot:,.0f}", xy=(-xmax * 0.98, spot),
                 xytext=(-xmax * 0.98, spot + (hi - lo) * 0.008),
                 color=INK, fontsize=11.5, ha="left", va="bottom")

    ax1.set_ylim(lo, hi)
    ax1.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"${v:,.0f}"))
    ax1.tick_params(axis="y", labelsize=10)
    for sp in ("top", "right"):
        ax1.spines[sp].set_visible(False)

    # ---------------- 右: ネットGEX プロファイル ----------------
    if instruments and net_gex_at:
        prices = [lo + (hi - lo) * i / 90 for i in range(91)]
        gex = [net_gex_at(p, instruments) for p in prices]
        ax2.fill_betweenx(prices, 0, gex, where=[v >= 0 for v in gex],
                          color=POS, alpha=0.22, interpolate=True)
        ax2.fill_betweenx(prices, 0, gex, where=[v < 0 for v in gex],
                          color=NEG, alpha=0.22, interpolate=True)
        ax2.plot(gex, prices, color=INK_2, lw=2)
        ax2.axvline(0, color=AXIS, lw=1)

        gpos, gneg = max(gex), min(gex)
        right = max(gpos * 1.35, abs(gneg) * 0.3)
        left = min(gneg * 1.6, -right * 0.33)   # 負側にもラベルが置ける幅を残す
        ax2.set_xlim(left, right)
        ax2.xaxis.set_major_formatter(FuncFormatter(lambda v, _: _usd(v)))
        ax2.set_xlabel("ネットGEX (1%変動あたり)", color=MUTED, labelpad=10)
        ax2.grid(axis="x", lw=0.8, alpha=0.6)
        ax2.set_axisbelow(True)
        ax2.set_title("ガンマ・プロファイル — ヘッジが反転する価格",
                      color=INK, fontsize=13, pad=14, loc="left")

        # 色だけに意味を持たせない: 領域に直接ラベルを置く
        ax2.annotate("＋圏\n値動きを抑制", xy=(right * 0.42, hi - (hi - lo) * 0.05),
                     color=POS, fontsize=10, ha="center", va="top", linespacing=1.6)
        ax2.annotate("−圏\n値動きを増幅", xy=(left * 0.52, lo + (hi - lo) * 0.05),
                     color=NEG, fontsize=10, ha="center", va="bottom", linespacing=1.6)

        if g and g.get("flip") and lo <= g["flip"] <= hi:
            f = g["flip"]
            ax2.axhline(f, color=NEG, lw=1.4)
            ax2.annotate(f"ガンマフリップ ${f:,.0f}  ({(f-spot)/spot*100:+.1f}%)",
                         xy=(right * 0.97, f), xytext=(right * 0.97, f + (hi - lo) * 0.012),
                         color=NEG, fontsize=10.5, ha="right", va="bottom")
        for sp in ("top", "right", "left"):
            ax2.spines[sp].set_visible(False)
        ax2.tick_params(axis="y", left=False, labelleft=False)
    else:
        ax2.set_visible(False)

    # ---------------- 見出しとフッタ ----------------
    net = g.get("net") if g else None
    sub = f"PCR {m['pcr']:.2f}"
    if net is not None:
        sub += f" ・ ネットGEX {_usd(net)}/1%"
    if m.get("magnets"):
        sub += f" ・ 最大の磁力 ${m['magnets'][0][0]:,.0f}"
    fig.suptitle("BTC オプション地形", color=INK, fontsize=17,
                 x=0.045, y=0.975, ha="left")
    fig.text(0.045, 0.933, sub, color=MUTED, fontsize=11, ha="left")
    fig.text(0.045, 0.022,
             f"Deribit 全満期の建玉 / GEXは mark IV からの自前計算 ・ "
             f"{now:%Y-%m-%d %H:%M UTC}",
             color=MUTED, fontsize=9.5, ha="left")

    fig.subplots_adjust(top=0.87, bottom=0.10, left=0.075, right=0.975)
    fig.savefig(path)
    plt.close(fig)
    return path


def caption(spot, book, m, g):
    """図のどこを見ればいいかを、描いた内容そのものから起こす"""
    lines = []

    wall = m["call_walls"][0] if m.get("call_walls") else None
    sup = m["put_supports"][0] if m.get("put_supports") else None
    if wall and sup:
        wk, wo = wall[0], book.get(wall[0], {}).get("call", 0)
        sk, so = sup[0], book.get(sup[0], {}).get("put", 0)
        lines.append(
            f"左図 ▲印: 現値のすぐ上 {(wk-spot)/spot*100:+.1f}% に Call の壁 "
            f"${wk:,.0f} ({wo:,.0f}枚)。▼印の支持は ${sk:,.0f} で "
            f"{(sk-spot)/spot*100:+.1f}%、この間が値動きの器。")
        gap = (spot - sk) / spot * 100
        if gap >= 4 and so < wo * 0.6:
            lines.append(
                f"左図: 支持までの {gap:.1f}% はPutのバーが短く、"
                "受け止めが薄い区間。下に走ると止まりにくい。")

    if m.get("max_pain"):
        mp = m["max_pain"]
        b = book.get(mp, {})
        lines.append(
            f"左図 ◎印: 最も長いバーが並ぶ ${mp:,.0f} "
            f"(Call {b.get('call', 0):,.0f}枚 / Put {b.get('put', 0):,.0f}枚) が Max Pain。"
            "満期が近づくほどここへ引かれやすい。")

    if g and g.get("flip"):
        net, flip = g.get("net"), g["flip"]
        side = "プラス" if (net or 0) >= 0 else "マイナス"
        lines.append(
            f"右図: 曲線は現値の高さで{side}圏 ({_usd(net)}/1%)。"
            f"ゼロと交わる ${flip:,.0f} ({(flip-spot)/spot*100:+.1f}%) がガンマフリップで、"
            "ここを割ると青(抑制)から赤(増幅)に変わる。")
        if abs(flip - spot) / spot < 0.05:
            lines.append("右図: フリップが現値の目前。ヘッジの向きが反転しやすく、"
                         "値動きが荒くなりやすい位置。")

    return "\n".join("▸ " + ln for ln in lines)
