"""Filming pack: Markdown for Slack and the repo, PDF for Alex's phone."""

from __future__ import annotations

from pathlib import Path

from fpdf import FPDF

from .scripts import FORMAT_NAMES, Plan

# fpdf's built-in fonts are Latin-1 only; map the few characters we use.
PDF_CHARS = str.maketrans({"–": "-", "—": "-", "’": "'", "‘": "'", "“": '"', "”": '"', "…": "...", "×": "x", "•": "-",
                           "⚠": "!"})


def title(plan: Plan) -> str:
    """The pack's heading. One pack per week: publishing replaces any entry with this heading."""
    return f"Filming pack: week of {plan.week_start:%-d %B %Y}"


def render_markdown(plan: Plan, scripts: list[dict], dropped: list[dict] = (), rows: list[dict] = ()) -> str:
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
    for n, s in enumerate(scripts, 1):
        skus = []
        for sku in s["featured_skus"]:
            line = plan.stock_by_sku.get(sku)
            where = ", ".join(f"{loc}: {q}" for loc, q in line.units_by_location.items() if q) if line else ""
            desc = " ".join(x for x in [line.product_title, line.size] if x) if line else ""
            price = f", £{line.price} on the website" if line else ""
            skus.append(f"`{sku}` {desc} ({where}{price})")
        out += [
            "",
            f"## {n}. {FORMAT_NAMES[s['format']]} (about {s['est_length_seconds']}s)",
            "",
            f"**Hook (say it in the first 2 seconds):** {s['hook']}",
            "",
            "**Shots:**",
            *[f"{i}. {shot}" for i, shot in enumerate(s["shot_list"], 1)],
            "",
            "**On-screen text:**",
            *[f"> {t}" for t in s["on_screen_text"]],
            "",
            f"**Caption:** {s['caption']}",
            "",
            f"**Hashtags:** {' '.join(s['hashtags'])}",
            "",
            f"**CTA:** {s['cta']}",
        ]
        if skus:
            out += ["", "**Featured stock:**", *[f"- {x}" for x in skus]]
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
    out_dir: Path, plan: Plan, scripts: list[dict], dropped: list[dict] = (), rows: list[dict] = ()
) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    md = render_markdown(plan, scripts, dropped, rows)
    md_path = out_dir / "filming_pack.md"
    pdf_path = out_dir / "filming_pack.pdf"
    md_path.write_text(md, encoding="utf-8")
    render_pdf(md, pdf_path)
    return md_path, pdf_path
