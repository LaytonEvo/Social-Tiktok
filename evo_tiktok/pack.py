"""Filming pack: Markdown for Slack and the repo, PDF for Alex's phone."""

from __future__ import annotations

from pathlib import Path

from fpdf import FPDF

from . import production
from .config import Settings
from .scripts import FORMAT_NAMES, Plan

# fpdf's built-in fonts are Latin-1 only; map the few characters we use.
PDF_CHARS = str.maketrans({"–": "-", "—": "-", "’": "'", "‘": "'", "“": '"', "”": '"', "…": "...", "×": "x", "•": "-",
                           "⚠": "!"})


FILMING_CHECKLIST = [
    "Phone upright (vertical video), lens wiped, Do Not Disturb on, brightness up.",
    "Film the shots in the order listed. Hold each one 2–3 seconds longer than its time so the editor has room.",
    "Say the hook in the first 2 seconds of shot 1, word for word.",
    "Real hands, real products, real voices. Products can be in shot; no logos or campaign images as graphics.",
    "When a video is filmed, set its Status to Filmed in the Production tab of the dashboard Sheet.",
]


def title(plan: Plan) -> str:
    """The pack's heading. One pack per week: publishing replaces any entry with this heading."""
    return f"Filming pack: week of {plan.week_start:%-d %B %Y}"


def week_pull_list(plan: Plan, scripts: list[dict]) -> dict[str, list[str]]:
    """Everything to pull for filming, grouped by where it is, with the videos it's for."""
    videos: dict[str, list[int]] = {}
    items: dict[str, dict] = {}
    for n, s in enumerate(scripts, 1):
        for item in production.pull_items(s, plan):
            items[item["sku"]] = item
            videos.setdefault(item["sku"], []).append(n)
    by_place: dict[str, list[str]] = {}
    for sku, item in sorted(items.items(), key=lambda kv: (kv[1]["product"], kv[1]["size"])):
        place = max(item["where"], key=item["where"].get) if item["where"] else "Not in stock"
        label = item["product"] + (f", {item['size']}" if item["size"] else "")
        others = {k: v for k, v in item["where"].items() if k != place}
        extra = f"; also {production.where_text(others)}" if others else ""
        by_place.setdefault(place, []).append(
            f"{label} (SKU {sku}, ×{item['where'].get(place, 0)}{extra}) for video"
            + ("s " if len(videos[sku]) > 1 else " ") + ", ".join(str(v) for v in videos[sku])
        )
    return by_place


def render_markdown(
    plan: Plan, scripts: list[dict], dropped: list[dict] = (), rows: list[dict] = (), settings: Settings | None = None
) -> str:
    out = [f"# {title(plan)}", ""]
    if plan.metrics_note:
        out += [f"**⚠ {plan.metrics_note}**", ""]
    out += [
        "Real footage and real voices only. Nothing here is posted automatically: "
        "film, check, then schedule by hand. Every £ figure was checked against live website prices.",
        "",
        "- **This week:** " + ", ".join(f"{n} × {FORMAT_NAMES[f]}" for f, n in plan.mix.items() if n),
    ]
    if plan.roulette_sizes:
        out.append("- **Size Roulette sizes:** " + ", ".join(f"{s} ({p} pairs)" for s, p in plan.roulette_sizes))
    if plan.giveaway:
        sizes = sorted({l.size for l in plan.giveaway_lines if l.size})
        out.append(
            f"- **Giveaway prize:** {plan.giveaway.product_title}"
            + (f" (winner chooses from {', '.join(sizes)})" if len(sizes) > 1 else "")
            + f". Post on {plan.giveaway_post:%A %-d %B}; entries close {plan.giveaway_close:%A %-d %B %Y}. "
            f"T&Cs: {plan.terms_url}"
        )
    for note in plan.notes:
        out.append(f"- {note}")
    for s in dropped:
        out.append(
            f"- **Dropped a {FORMAT_NAMES[s['format']]} video** (\"{s['hook']}\") because it still failed "
            f"our checks after a retry: {'; '.join(s['errors'])}"
        )
    when = production.schedule(plan, scripts, settings) if settings is not None and scripts else [None] * len(scripts)
    if scripts:
        out += ["", "## Before you film", ""]
        if when[0]:
            out.append(f"Film everything by **{when[0]['film_by']:%A %-d %B}**, edit by "
                       f"**{when[0]['edit_by']:%A %-d %B}**. Each video's posting day is under its heading.")
            out.append("")
        out += [f"- {x}" for x in FILMING_CHECKLIST]
        pulls = week_pull_list(plan, scripts)
        if pulls:
            out += ["", "**Pull this stock before you start:**"]
            for place, items in pulls.items():
                out += ["", f"**{place}**", *[f"- {x}" for x in items]]
    for n, (s, w) in enumerate(zip(scripts, when), 1):
        items = production.pull_items(s, plan)
        heading = f"## {n}. {FORMAT_NAMES[s['format']]} ({production.timed_shots(s)[-1]['to']} long)"
        out += ["", heading + (f", posts {w['post_on']:%A %-d %B}" if w else ""), ""]
        out += [
            f"**Hook (say it in the first 2 seconds):** {s['hook']}",
            "",
            f"**Setup:** {s['setup']}",
            "",
        ]
        if items:
            out += ["**Have in hand:**", *[
                f"- {i['product']}" + (f", {i['size']}" if i["size"] else "")
                + f" (SKU `{i['sku']}`, {production.where_text(i['where'])}, £{i['price']} on the website)"
                for i in items
            ], ""]
        out.append("**Shots (in the edit, in this order):**")
        for i, shot in enumerate(production.timed_shots(s), 1):
            text = f"{i}. **{shot['from']}–{shot['to']}** Film: {shot['film']}"
            if shot["say"].strip():
                text += f' **Say:** "{shot["say"]}"'
            if shot["on_screen"].strip():
                text += f" **On screen:** {shot['on_screen']}"
            out.append(text)
        out += [
            "",
            f"**Caption:** {s['caption']}",
            "",
            f"**Hashtags:** {' '.join(s['hashtags'])}",
            "",
            f"**CTA:** {s['cta']}",
        ]
    if rows:
        out += ["", "## Price checks (live website prices)", "",
                "| Video | Hook | Product | Live price | Price claims |", "|---|---|---|---|---|"]
        out += [
            f"| {r['script']}{'' if r['kept'] else ' (dropped)'} | {r['hook']} | {r['product']} | "
            f"{r['live_price']} | {r['price_check']} |"
            for r in rows
        ]
    return "\n".join(out) + "\n"


def render_pdf(markdown: str, path: Path) -> None:
    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    width = pdf.w - pdf.l_margin - pdf.r_margin
    for raw in markdown.splitlines():
        line = raw.translate(PDF_CHARS).encode("latin-1", "replace").decode("latin-1")
        if line.startswith("# "):
            pdf.set_font("Helvetica", "B", 16)
            pdf.multi_cell(width, 8, line[2:], new_x="LMARGIN", new_y="NEXT")
        elif line.startswith("## "):
            if pdf.get_y() > pdf.h - 80:
                pdf.add_page()
            pdf.ln(3)
            pdf.set_font("Helvetica", "B", 13)
            pdf.multi_cell(width, 7, line[3:], new_x="LMARGIN", new_y="NEXT")
        elif not line.strip():
            pdf.ln(2)
        else:
            pdf.set_font("Helvetica", "", 10)
            text = line[2:] if line.startswith("> ") else line
            pdf.multi_cell(width, 5, text.replace("`", ""), markdown=True, new_x="LMARGIN", new_y="NEXT")
    pdf.output(str(path))


def write_pack(
    out_dir: Path, plan: Plan, scripts: list[dict], dropped: list[dict] = (), rows: list[dict] = (),
    settings: Settings | None = None,
) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    md = render_markdown(plan, scripts, dropped, rows, settings)
    md_path = out_dir / "filming_pack.md"
    pdf_path = out_dir / "filming_pack.pdf"
    md_path.write_text(md, encoding="utf-8")
    render_pdf(md, pdf_path)
    return md_path, pdf_path
