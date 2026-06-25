"""Generate NEXT_STEPS.pdf — a one-time action list of things only the user can do.

Run from the repo root:
    python scripts/generate_next_steps_pdf.py
"""
from __future__ import annotations

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

OUT_PATH = Path("NEXT_STEPS.pdf")


def _styles() -> dict:
    base = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=base["Heading1"], fontSize=20, spaceAfter=10, textColor=colors.HexColor("#1a1a1a"))
    h2 = ParagraphStyle("h2", parent=base["Heading2"], fontSize=14, spaceBefore=14, spaceAfter=6, textColor=colors.HexColor("#0b3d91"))
    h3 = ParagraphStyle("h3", parent=base["Heading3"], fontSize=12, spaceBefore=8, spaceAfter=4, textColor=colors.HexColor("#333333"))
    body = ParagraphStyle("body", parent=base["BodyText"], fontSize=10.5, leading=14, spaceAfter=4, alignment=TA_LEFT)
    note = ParagraphStyle("note", parent=body, fontSize=9.5, textColor=colors.HexColor("#555555"), spaceAfter=8)
    code = ParagraphStyle("code", parent=body, fontName="Courier", fontSize=9.5, backColor=colors.HexColor("#f4f4f4"), leftIndent=10, leading=12)
    warn = ParagraphStyle("warn", parent=body, fontSize=10.5, textColor=colors.HexColor("#a32100"), leftIndent=0)
    return {"h1": h1, "h2": h2, "h3": h3, "body": body, "note": note, "code": code, "warn": warn}


def _kv_table(rows: list[tuple[str, str]]) -> Table:
    t = Table([[k, v] for k, v in rows], colWidths=[2.0 * inch, 4.5 * inch])
    t.setStyle(TableStyle([
        ("FONT", (0, 0), (-1, -1), "Helvetica", 10),
        ("FONT", (0, 0), (0, -1), "Helvetica-Bold", 10),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#dddddd")),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
    ]))
    return t


def _action_table(items: list[tuple[str, str, str, str]]) -> Table:
    """items: (priority, what, why, how)"""
    data = [["Priority", "What", "Why", "How"]] + list(items)
    t = Table(data, colWidths=[0.8 * inch, 1.7 * inch, 2.0 * inch, 2.5 * inch])
    t.setStyle(TableStyle([
        ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 10),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0b3d91")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONT", (0, 1), (-1, -1), "Helvetica", 9),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cccccc")),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f7f7f7")]),
    ]))
    return t


def build(doc_path: Path) -> None:
    doc = SimpleDocTemplate(
        str(doc_path), pagesize=LETTER,
        leftMargin=0.7 * inch, rightMargin=0.7 * inch,
        topMargin=0.7 * inch, bottomMargin=0.7 * inch,
        title="tradeforme — Next Steps",
    )
    s = _styles()
    story = []

    # === Title page ===
    story.append(Paragraph("tradeforme &mdash; Next Steps", s["h1"]))
    story.append(Paragraph(
        "Action list of things only <b>you</b> can do. Everything below is blocked on "
        "your input, your credentials, or your decision &mdash; not on more code.",
        s["body"]
    ))
    story.append(Spacer(1, 0.1 * inch))

    # === Current state ===
    story.append(Paragraph("Where the project stands", s["h2"]))
    story.append(_kv_table([
        ("Repository", "https://github.com/EtixP/tradeforme (private)"),
        ("Branch", "main"),
        ("Milestones done", "M1, M2, M3 (deterministic extraction, 96.2% OK), M4, M5 (signals stored), M6/M7 paper-broker scaffolding"),
        ("Tests passing", "88 / 88"),
        ("Disclosures in DB", "111,622 across 60 trading days (Feb 27 – May 27, 2026)"),
        ("Supply-contract events", "533 candidates &rarr; 513 OK + 20 manual-review (19 value-undisclosed, 1 ratio-inconsistent), 0 blocked"),
        ("Real contract values extracted", "513 events with verified contract_value, prior_year_revenue, ratio"),
        ("Candidate signals stored", "331 at default threshold (ratio &ge; 0.08); 58 at strict threshold (ratio &ge; 0.30)"),
        ("Price data source", "pykrx (free, no auth)"),
        ("LLM extraction", "Code complete (prompts + validator + client) — optional now that deterministic parser works at 96%"),
        ("Broker integration", "PaperBroker (historical fills) ready; KIS broker needs your credentials"),
    ]))

    # === Event study results ===
    story.append(Paragraph("Threshold sweep: net-of-cost returns by ratio cutoff", s["h2"]))
    story.append(Paragraph(
        "514 contracts with real extracted values, returns measured from event-day close to "
        "T+1/T+5 close, net of 0.313% roundtrip cost. The CLAUDE.md hypothesis "
        "(filter to ratio &ge; 0.08) is the line marked &laquo; in the table.",
        s["body"]
    ))
    sweep_table = Table(
        [
            ["Threshold", "n", "T+1 net", "T+5 net", "T+1 win%", "T+5 win%", "Profit factor (5d)"],
            ["0.00 (all)", "514", "+0.35%", "+1.21%", "42.6%", "44.2%", "1.38"],
            ["0.05",       "430", "+0.43%", "+0.95%", "43.7%", "42.8%", "1.30"],
            ["0.08 «", "333", "+0.39%", "+0.55%", "41.7%", "42.3%", "1.20"],
            ["0.10",       "287", "+0.54%", "+0.87%", "40.8%", "44.3%", "1.27"],
            ["0.15",       "169", "+0.18%", "+0.89%", "38.5%", "46.2%", "1.26"],
            ["0.20",       "110", "+0.49%", "+1.15%", "38.2%", "45.5%", "1.33"],
            ["0.30",       "59",  "+1.64%", "+3.10%", "40.7%", "42.4%", "1.93"],
            ["0.50",       "23",  "+1.78%", "+2.67%", "30.4%", "39.1%", "1.77"],
        ],
        colWidths=[0.9 * inch, 0.45 * inch, 0.85 * inch, 0.85 * inch, 0.85 * inch, 0.85 * inch, 1.25 * inch],
    )
    sweep_table.setStyle(TableStyle([
        ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 9),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0b3d91")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONT", (0, 1), (-1, -1), "Helvetica", 9),
        ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cccccc")),
        ("BACKGROUND", (0, 3), (-1, 3), colors.HexColor("#fff3cd")),  # highlight 0.08 row
        ("BACKGROUND", (0, 7), (-1, 7), colors.HexColor("#d4edda")),  # highlight 0.30 row
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(sweep_table)
    story.append(Spacer(1, 0.1 * inch))
    story.append(Paragraph(
        "<b>Key finding:</b> the CLAUDE.md-suggested 8% threshold underperforms the unfiltered baseline "
        "(T+5 net +0.55% vs +1.21%). The 30%+ threshold shows real edge &mdash; T+5 net +3.10%, "
        "profit factor 1.93 &mdash; but on only 59 events, well within noise.",
        s["body"]
    ))
    story.append(Paragraph(
        "<b>What this means:</b> the project's actual research question now has a partial answer: "
        "small-to-medium supply contracts do NOT show a tradable post-disclosure drift, but very large "
        "ones (&ge; 30% of revenue) might. To confirm, the system needs more data &mdash; "
        "<i>backfill</i> is now the highest-leverage next move.",
        s["body"]
    ))
    story.append(Paragraph(
        "Raw CSVs: <font face=\"Courier\">data/event_study_results.csv</font> (all 533) and "
        "<font face=\"Courier\">data/event_study_filtered.csv</font> (ratio &ge; 0.08).",
        s["note"]
    ))

    story.append(PageBreak())

    # === Action items ===
    story.append(Paragraph("Action items", s["h2"]))
    story.append(Paragraph(
        "Sorted by priority. P1 unblocks the next milestone. P2 hardens the existing system. "
        "P3 is optional polish.",
        s["body"]
    ))
    story.append(Spacer(1, 0.05 * inch))

    actions = [
        ("P1",
         "Authorize a larger backfill (12+ months)",
         "Strongest finding so far: very large contracts (ratio >= 30%) show +3.1% T+5 net but only 59 events. We need ~3x more data to know if this is real. 12 months of DART = ~1.5M disclosures, ~2000+ supply contracts, runs in ~30 min, free.",
         "Reply: 'backfill 12 months' (or '6 months', or longer). Claude will loop scripts/ingest_disclosures.py + scripts/parse_supply_contracts.py."),
        ("P1",
         "Rotate the DART API key",
         "The current key briefly appeared in chat output during the first smoke test (before the httpx log filter was added). Still safe on your machine, but rotating is the clean call.",
         "opendart.fss.or.kr → 관리자 → API Key 재발급. Replace DART_API_KEY in .env."),
        ("P2",
         "Get an LLM API key (Anthropic or OpenAI) — now optional",
         "Demoted from P1: deterministic parser handles 96% of supply contracts without LLM, and the cross-check architecture is already in place. LLM still useful for: (a) the 18 blocked rows, (b) other event types (buybacks, dilutive financing), (c) ratio cross-check on tradeable signals.",
         "console.anthropic.com / platform.openai.com. Add ANTHROPIC_API_KEY or OPENAI_API_KEY to .env, set LLM_PROVIDER, pip install anthropic|openai."),
        ("P2",
         "Set up GitHub branch protection on main",
         "Once live trading code exists, you don't want a stray push to main to enable LIVE_TINY by accident.",
         "github.com/EtixP/tradeforme → Settings → Branches → Add rule for main: require PR review, require status checks."),
        ("P2",
         "Back up data/kdtb.db",
         "111k rows of ingested DART data — free to re-fetch but slow. Loss = ~5 min replay per 90 days.",
         "Easiest: enable iCloud Drive backup of the Desktop/tradeforme folder. Or a weekly cron: sqlite3 data/kdtb.db .dump > backup.sql."),
        ("P2",
         "Schedule daily DART ingestion",
         "Right now ingestion only runs when you invoke the CLI manually. To catch fresh disclosures during market hours, this needs to run on a schedule.",
         "Local cron: 0 19 * * 1-5 cd ~/Desktop/tradeforme && .venv/bin/python scripts/ingest_disclosures.py --date $(date +%Y-%m-%d). Or a cheap VPS (Fly.io / Railway, ~$5/mo) if you want 24/7."),
        ("P2",
         "Obtain KIS broker credentials",
         "Required for Milestone 6 (broker integration) and beyond (paper/live trading). Without this we can't even fetch live quotes, let alone place orders.",
         "Korea Investment Securities → 개인용 계좌 개설 → Open API 신청. Add KIS_APP_KEY, KIS_APP_SECRET, KIS_ACCOUNT_NO, KIS_ACCOUNT_PRODUCT_CODE to .env."),
        ("P2",
         "Decide whether to raise min_contract_to_revenue_ratio",
         "Data from this loop's threshold sweep argues the CLAUDE.md default of 0.08 is too loose. 0.30 shows real edge in this 3-month sample but on only 59 events. Need user's call on whether to update config/default.yaml (and the bar for that update).",
         "Open config/default.yaml → strategy.major_supply_contract.min_contract_to_revenue_ratio. Recommend leaving at 0.08 until a 12-month backfill validates the 0.30+ finding."),
        ("P2",
         "Review risk thresholds in config/default.yaml",
         "Defaults match CLAUDE.md (₩30k max order, ₩10k daily loss, confidence ≥ 0.80). These need your sign-off before any LIVE_TINY trading.",
         "Open config/default.yaml. Pay attention to: trading.max_order_value_krw, trading.max_daily_loss_krw."),
        ("P3",
         "Install gitleaks pre-commit hook",
         "Catches an accidental commit of .env or API keys before it reaches GitHub. Belt-and-suspenders on top of .gitignore.",
         "brew install gitleaks && gitleaks protect --staged at install time, or use the pre-commit framework."),
        ("P3",
         "Decide if PDF action lists should be committed",
         "Currently NEXT_STEPS.pdf will be committed as part of this loop's work. You may prefer them to stay local-only.",
         "If local-only: add NEXT_STEPS.pdf to .gitignore and git rm --cached it."),
        ("P3",
         "Decide on Korean market holiday handling",
         "Right now the ingestion CLI runs every calendar day. DART returns empty for holidays, which is harmless but wastes API calls.",
         "Optional: add a holiday calendar (pykrx has get_business_days). Not blocking anything yet."),
    ]
    story.append(_action_table(actions))

    story.append(PageBreak())

    # === Where to find things ===
    story.append(Paragraph("Where to find things", s["h2"]))
    story.append(_kv_table([
        ("Project root", "/Users/jsp2022310/Desktop/tradeforme"),
        ("Repo on GitHub", "https://github.com/EtixP/tradeforme"),
        ("Project spec", "CLAUDE.md (in repo root)"),
        ("Source code", "src/kdtb/ — schemas, config, data, strategy, risk, storage"),
        ("Tests", "tests/ — run with: .venv/bin/pytest"),
        ("DART ingestion CLI", "scripts/ingest_disclosures.py --date YYYY-MM-DD"),
        ("Event study CLI", "scripts/run_event_study.py [--limit N]"),
        ("Parse docs CLI", "scripts/parse_supply_contracts.py [--limit N]"),
        ("Filtered event study", "scripts/run_filtered_event_study.py [--min-ratio 0.08]"),
        ("Strategy runner (NEW)", "scripts/run_strategy.py [--min-ratio 0.30] [--dry-run]"),
        ("Local DB (gitignored)", "data/kdtb.db — SQLite, inspect with sqlite3 or DB Browser"),
        ("Event study CSV", "data/event_study_results.csv"),
        ("Secrets (gitignored)", ".env — DART_API_KEY lives here"),
        ("Config", "config/default.yaml, config/paper.yaml, config/live_tiny.yaml"),
        ("Live-trading guard", "src/kdtb/config.py — raises if YAML says LIVE without ENABLE_LIVE_TRADING=true in env"),
    ]))

    story.append(Paragraph("How to verify the project is healthy", s["h2"]))
    story.append(Paragraph(
        "Run these from the repo root any time you want to confirm nothing is broken:",
        s["body"]
    ))
    story.append(Paragraph(".venv/bin/pytest", s["code"]))
    story.append(Paragraph(".venv/bin/python -m kdtb.main", s["code"]))
    story.append(Paragraph(".venv/bin/python scripts/ingest_disclosures.py --date $(date +%Y-%m-%d)", s["code"]))

    story.append(Paragraph("What Claude built across the autonomous loops", s["h2"]))
    story.append(Paragraph("<b>Loop 1</b> (M4 + M5):", s["body"]))
    story.append(Paragraph(
        "&bull; <b>data/market_data_client.py</b> &mdash; pykrx wrapper for OHLCV + event-time returns<br/>"
        "&bull; <b>scripts/run_event_study.py</b> &mdash; batched event study CLI<br/>"
        "&bull; <b>strategy/major_supply_contract.py</b> &mdash; M5 strategy engine (was placeholder)",
        s["body"]
    ))
    story.append(Paragraph("<b>Loop 2</b> (cost model + M3 prep + paper broker):", s["body"]))
    story.append(Paragraph(
        "&bull; <b>backtest/cost_model.py</b> &mdash; Korean equity costs<br/>"
        "&bull; <b>backtest/metrics.py</b> &mdash; per-trade metrics<br/>"
        "&bull; <b>interpretation/llm_prompts.py</b>, <b>extraction_validator.py</b>, <b>llm_client.py</b><br/>"
        "&bull; <b>broker/base.py</b> + <b>broker/paper_broker.py</b>",
        s["body"]
    ))
    story.append(Paragraph("<b>Loop 3</b> (deterministic extraction + filtered event study):", s["body"]))
    story.append(Paragraph(
        "&bull; <b>data/dart_client.py</b> &mdash; new <font face=\"Courier\">fetch_document_text()</font> + ZIP/HTML/UTF-8 extractor<br/>"
        "&bull; <b>interpretation/deterministic_parser.py</b> &mdash; regex parser for 단일판매ㆍ공급계약체결 (voluntary + mandatory variants)<br/>"
        "&bull; <b>storage/extraction_store.py</b>, <b>scripts/parse_supply_contracts.py</b>, <b>scripts/run_filtered_event_study.py</b><br/>"
        "&bull; 16 new test cases",
        s["body"]
    ))
    story.append(Paragraph("<b>Loop 4</b> (end-to-end signal generation + parser refinement):", s["body"]))
    story.append(Paragraph(
        "&bull; Parser refinement: distinguishes 'value undisclosed by company' (literal dash in form) from 'value missing entirely'. "
        "Previously 18 rows were marked 'blocked'; now 0 blocked and 19 are correctly flagged "
        "<font face=\"Courier\">value_undisclosed_by_company</font> in needs_manual_review.<br/>"
        "&bull; <b>storage/signal_store.py</b> &mdash; upsert keyed on signal_id (UNIQUE), plus clear-by-strategy<br/>"
        "&bull; <b>scripts/run_strategy.py</b> &mdash; runs strategy engine across all extractions, tallies rejections by reason, "
        "stores Signal rows. <font face=\"Courier\">--min-ratio</font> override and <font face=\"Courier\">--dry-run</font> flags.<br/>"
        "&bull; End-to-end pipeline now operational: <b>331 candidate signals stored in DB</b> at default threshold (0.08), "
        "58 at the empirically-better 0.30 threshold. Mean strength 0.76, range 0.40&ndash;1.00.<br/>"
        "&bull; 4 new test cases (signal store + value-undisclosed parser test)",
        s["body"]
    ))
    story.append(Paragraph(
        "Suite: 28 &rarr; 43 &rarr; 68 &rarr; 84 &rarr; <b>88</b> passing tests. Zero regressions across four loops.",
        s["note"]
    ))

    story.append(Paragraph("Honest caveats", s["h2"]))
    story.append(Paragraph(
        "&bull; The naive event study uses title-only filtering. It does not separate small contracts from large ones, "
        "so noise dominates. M3 (LLM extraction) is needed to filter by contract/revenue ratio.<br/>"
        "&bull; The event date used is the DART receipt date (YYYYMMDD). Intraday timing of the disclosure is not captured. "
        "If a disclosure is filed at 14:00 KST, our 'T+1' return actually includes the price reaction from 14:00 to next-day close, "
        "not a clean next-day-only return.<br/>"
        "&bull; Returns are raw close-to-close, not benchmark-adjusted. A market-wide rally on event days would inflate the mean.<br/>"
        "&bull; pykrx scrapes the KRX website. It's free but can be rate-limited. We sleep 0.15s between calls.<br/>"
        "&bull; LIVE_TINY broker code is not yet implemented. Even with KIS credentials, no real orders can be placed yet.",
        s["body"]
    ))

    doc.build(story)


if __name__ == "__main__":
    build(OUT_PATH)
    print(f"Wrote {OUT_PATH.resolve()}")
