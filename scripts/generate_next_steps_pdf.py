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
        ("Milestones done", "M1 (skeleton), M2 (DART ingestion), M4 (event study + cost model), M5 (strategy engine), M6/M7 paper-broker scaffolding"),
        ("Tests passing", "68 / 68"),
        ("Disclosures in DB", "111,622 across 60 trading days (Feb 27 – May 27, 2026)"),
        ("Supply-contract events", "533 “new contract” candidates (title-filtered)"),
        ("Price data source", "pykrx (free, no auth)"),
        ("LLM extraction", "Code complete (prompts + validator + client) — waiting on your API key to actually run"),
        ("Broker integration", "PaperBroker (historical fills) ready; KIS broker needs your credentials"),
    ]))

    # === Event study results ===
    story.append(Paragraph("Naive event study, gross and net of costs", s["h2"]))
    story.append(Paragraph(
        "533 title-filtered supply-contract disclosures, returns measured from event-day "
        "close to T+1/T+2/T+5 close. Cost model: 0.015%/side broker commission + 10% VAT, "
        "0.18% sale tax, 5bps slippage &rarr; <b>0.313% roundtrip drag</b>.",
        s["body"]
    ))
    es_table = Table(
        [
            ["Horizon", "n", "Gross mean", "Net mean", "Gross win%", "Net win%", "Profit factor"],
            ["1-day", "533", "+0.66%", "+0.35%", "45.6%", "42.8%", "1.17"],
            ["2-day", "533", "+0.88%", "+0.57%", "44.5%", "43.3%", "1.19"],
            ["5-day", "533", "+1.28%", "+0.96%", "44.8%", "44.1%", "1.22"],
        ],
        colWidths=[0.7 * inch, 0.5 * inch, 0.9 * inch, 0.9 * inch, 0.95 * inch, 0.85 * inch, 1.0 * inch],
    )
    es_table.setStyle(TableStyle([
        ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 9),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0b3d91")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONT", (0, 1), (-1, -1), "Helvetica", 9),
        ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cccccc")),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(es_table)
    story.append(Spacer(1, 0.1 * inch))
    story.append(Paragraph(
        "<b>Honest read:</b> after costs the naive filter is barely above breakeven. "
        "Profit factor 1.17 means &#8361;1.17 won per &#8361;1.00 lost &mdash; mathematically positive "
        "but well within noise on 533 events. Win rate is &lt; 50% across all horizons. "
        "Median return is negative gross (a few large winners drag the mean up). "
        "<b>This is the expected result.</b> Per CLAUDE.md, the real test is filtering to "
        "contracts where <i>contract_value / prior_year_revenue &ge; 8%</i>, which requires "
        "LLM extraction (M3). The infrastructure is now ready to drop a key in.",
        s["body"]
    ))
    story.append(Paragraph(
        "Raw per-event CSV: <font face=\"Courier\">data/event_study_results.csv</font>",
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
         "Rotate the DART API key",
         "The current key briefly appeared in chat output during the first smoke test (before the httpx log filter was added). It's still safe on your machine, but rotating is the clean call.",
         "opendart.fss.or.kr → 관리자 → API Key 재발급. Replace DART_API_KEY in .env."),
        ("P1",
         "Get an LLM API key (Anthropic or OpenAI)",
         "Code is now complete (prompts + validator + client abstraction). Drop a key in .env and the M3 extraction pipeline runs end-to-end. Without it we can't filter to economically meaningful contracts.",
         "console.anthropic.com → API Keys, or platform.openai.com → API keys. Add ANTHROPIC_API_KEY or OPENAI_API_KEY to .env and set LLM_PROVIDER=anthropic|openai. Install the SDK: pip install anthropic (or openai)."),
        ("P1",
         "Decide M3 vs more backfill first",
         "M3 needs the LLM key above; tokens are not free (~$0.003/extraction with Claude Sonnet). Backfill 6–12 more months of disclosures so the post-extraction event study has more events before spending the tokens.",
         "Tell Claude which to do first. Backfill is a one-line CLI loop and free. M3 over 533 events would be ~$1.50 with Sonnet, ~$0.10 with Haiku."),
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
         "Review risk thresholds in config/default.yaml",
         "Current defaults match CLAUDE.md (₩30k max order, ₩10k daily loss, ratio ≥ 8%, confidence ≥ 0.80). These need your sign-off before any LIVE_TINY trading.",
         "Open config/default.yaml. Pay attention to: trading.max_order_value_krw, trading.max_daily_loss_krw, strategy.major_supply_contract.* thresholds."),
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
        "&bull; <b>backtest/cost_model.py</b> &mdash; Korean equity costs (commission, VAT, sale tax, slippage)<br/>"
        "&bull; <b>backtest/metrics.py</b> &mdash; per-trade metrics (win rate, profit factor, sharpe-ish, max drawdown)<br/>"
        "&bull; <b>interpretation/llm_prompts.py</b> &mdash; versioned extraction prompt template<br/>"
        "&bull; <b>interpretation/extraction_validator.py</b> &mdash; LLM JSON &rarr; Extraction with hard/soft validation<br/>"
        "&bull; <b>interpretation/llm_client.py</b> &mdash; Anthropic + OpenAI + Stub clients (no calls until key added)<br/>"
        "&bull; <b>broker/base.py</b> + <b>broker/paper_broker.py</b> &mdash; abstract Broker + HistoricalPaperBroker<br/>"
        "&bull; 4 new test files (25 new test cases)",
        s["body"]
    ))
    story.append(Paragraph(
        "Suite: 28 &rarr; 43 &rarr; <b>68</b> passing tests. Zero regressions across both loops.",
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
