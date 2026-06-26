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
        ("Milestones done", "M1, M2, M3 (deterministic extraction, 96.6% OK), M4, M5 (signals stored, v2 with KOSPI + skip-gov filters), M5b risk-engine blacklist (halt/shareholder), M6/M7 paper-broker scaffolding"),
        ("Tests passing", "108 / 108"),
        ("Disclosures in DB", "514,304 across 488 trading days (Jun 25 2024 – Jun 24 2026, full 24 months)"),
        ("Supply-contract events", "3,531 candidates &rarr; 3,412 OK + 119 manual-review (96.6% / 3.4%), 0 blocked"),
        ("Real contract values extracted", "3,412 events with verified contract_value, prior_year_revenue, ratio"),
        ("Candidate signals stored", "693 (v2 strategy: ratio ≥ 0.08, KOSPI only, skip government counterparty). Mean strength 0.72."),
        ("Price data source", "pykrx (free, no auth)"),
        ("LLM extraction", "Code complete (prompts + validator + client) — optional now that deterministic parser works at 96%"),
        ("Broker integration", "PaperBroker (historical fills) ready; KIS broker needs your credentials"),
    ]))

    # === Event study results ===
    story.append(Paragraph("Threshold sweep on 24 months / 3,412 events", s["h2"]))
    story.append(Paragraph(
        "Net of 0.313% roundtrip cost (commission + VAT + sale tax + slippage). "
        "Loop-3 ran the same sweep on only 514 events (3 months); the comparison column "
        "shows how those earlier numbers held up after 6.6&times; more data.",
        s["body"]
    ))
    sweep_table = Table(
        [
            ["Threshold", "n (24mo)", "T+1 net", "T+5 net", "T+5 win%", "PF (5d)", "Median 5d", "Loop-3 T+5 (n=514)"],
            ["0.00 (all)", "3,412", "&minus;0.12%", "+0.35%", "44.1%", "1.21", "&minus;0.68%", "+1.21% (n=514)"],
            ["0.08 «", "2,176", "&minus;0.07%", "+0.22%", "42.9%", "1.16", "&minus;0.73%", "+0.55% (n=333)"],
            ["0.15", "1,177", "&minus;0.16%", "+0.56%", "44.0%", "1.25", "&minus;0.71%", "+0.89% (n=169)"],
            ["0.20", "819",   "&minus;0.06%", "+0.59%", "42.9%", "1.25", "&minus;1.07%", "+1.15% (n=110)"],
            ["0.30", "470",   "+0.18%",       "+0.50%", "41.2%", "1.21", "&minus;1.29%", "+3.10% (n=59)  «"],
            ["0.50", "217",   "+0.47%",       "+0.92%", "38.1%", "1.31", "&minus;1.79%", "+2.67% (n=23)"],
            ["1.00", "66",    "&minus;0.06%", "&minus;1.83%", "32.3%", "0.69", "&minus;3.98%", "— "],
        ],
        colWidths=[0.85 * inch, 0.6 * inch, 0.7 * inch, 0.7 * inch, 0.7 * inch, 0.55 * inch, 0.8 * inch, 1.45 * inch],
    )
    sweep_table.setStyle(TableStyle([
        ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 8),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0b3d91")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONT", (0, 1), (-1, -1), "Helvetica", 8),
        ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cccccc")),
        ("BACKGROUND", (0, 2), (-1, 2), colors.HexColor("#fff3cd")),  # 0.08 row
        ("BACKGROUND", (0, 5), (-1, 5), colors.HexColor("#f8d7da")),  # 0.30 row (was green, now red — edge disappeared)
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(sweep_table)
    story.append(Spacer(1, 0.1 * inch))
    story.append(Paragraph(
        "<b>The big finding: the &ge; 0.30 \"edge\" from Loop 3 was largely noise.</b> "
        "Going from 59 to 470 events, the T+5 net mean dropped from +3.10% to +0.50% and "
        "profit factor from 1.93 to 1.21. Median is negative at every threshold. The 1.00+ "
        "row is actually negative &mdash; suggests very large contracts may be a slight negative signal "
        "(possibly representing distressed wins or unusual situations).",
        s["body"]
    ))
    story.append(Paragraph(
        "<b>Honest conclusion:</b> <i>after 24 months and 3,412 events, the major-supply-contract "
        "event does NOT produce a clean tradable post-disclosure drift after costs.</i> Mean net "
        "returns at every threshold are within noise of zero, win rates are 38&ndash;46%, medians are "
        "negative. This is exactly the kind of \"strong negative result\" CLAUDE.md anticipates &mdash; "
        "it answers the original research question. The market reacts efficiently to these "
        "disclosures by event-day close.",
        s["body"]
    ))
    story.append(Paragraph(
        "<b>What can still rescue this:</b> (a) sub-segment by counterparty type, sector, market cap, "
        "or recent volatility &mdash; maybe certain slices DO work. (b) try other event types (buybacks, "
        "dilutive financing, halt/resumption). (c) different exit horizons (T+10, T+20). (d) the M5b "
        "ML filter on richer features. Each is a real hypothesis to test &mdash; none is guaranteed to work.",
        s["body"]
    ))
    story.append(Spacer(1, 0.1 * inch))

    story.append(Paragraph("Loop-6 subgroup + walk-forward results", s["h2"]))
    story.append(Paragraph(
        "Sliced the 3,377 events by market, ratio bucket, contract value, day-of-week, calendar quarter, "
        "and counterparty type (regex heuristic). Then walk-forward validated the most-positive subgroup "
        "and the most-negative subgroup across four 6-month windows. Honest reading below.",
        s["body"]
    ))
    wf_table = Table(
        [
            ["Subgroup", "Aggregate T+5", "Aggregate PF", "Walk-fwd verdict (4 windows)"],
            ["KOSPI / ratio 0.15-0.30", "+1.59% (n=199)", "1.59", "ALTERNATES (+/-/-/+) — not robust"],
            ["KOSPI (any ratio)",       "+0.63% (n=1708)", "1.22", "3/4 windows positive — modest but holds"],
            ["KOSDAQ (any ratio)",      "+0.06% (n=1669)", "1.02", "no edge"],
            ["Government counterparty",  "&minus;2.14% (n=90)", "0.51", "3/4 windows NEGATIVE (sometimes &minus;5%)"],
            ["Other_korean_corp",       "&minus;0.65% (n=417)", "0.84", "consistently negative"],
            ["Ratio 1.00+",             "&minus;1.83% (n=66)",  "0.64", "small n; likely distressed/unusual events"],
        ],
        colWidths=[2.0 * inch, 1.5 * inch, 0.9 * inch, 2.4 * inch],
    )
    wf_table.setStyle(TableStyle([
        ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 9),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0b3d91")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONT", (0, 1), (-1, -1), "Helvetica", 8),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cccccc")),
        ("BACKGROUND", (0, 2), (-1, 2), colors.HexColor("#d4edda")),  # KOSPI all
        ("BACKGROUND", (0, 4), (-1, 4), colors.HexColor("#f8d7da")),  # gov
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(wf_table)
    story.append(Spacer(1, 0.1 * inch))
    story.append(Paragraph(
        "<b>Two actionable findings:</b><br/>"
        "&bull; <b>Government-counterparty contracts consistently underperform.</b> 3/4 walk-forward windows "
        "show net T+5 returns of &minus;5% to &minus;0.5%. Aligns with intuition: government contracts are "
        "pre-priced, low-margin, expected. We can't short (CLAUDE.md rule), but this is a <b>strong "
        "negative filter</b> &mdash; the strategy should skip long signals where counterparty is government.<br/>"
        "&bull; <b>KOSPI &gt; KOSDAQ</b> in 3/4 windows. Aligns with the liquidity-premium hypothesis. "
        "Worth restricting the strategy to KOSPI-only.",
        s["body"]
    ))
    story.append(Paragraph(
        "<b>Two findings ruled out:</b> the KOSPI/0.15-0.30 subgroup that looked best in aggregate (+1.59% T+5) "
        "alternates positive/negative across windows &mdash; a classic small-sample fluke. The 0.30+ ratio "
        "\"edge\" from Loop 3 stays dead at scale. Median return remains negative at every threshold.",
        s["body"]
    ))
    story.append(Paragraph(
        "Raw CSV: <font face=\"Courier\">data/subgroup_analysis.csv</font> (3,377 events x 6+ feature columns).",
        s["note"]
    ))
    story.append(Paragraph(
        "Raw CSV: <font face=\"Courier\">data/event_study_results.csv</font> (3,531 events, all "
        "horizons, gross). Loop-3 log of the smaller dataset is preserved in git history.",
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
         "Decide on the strategic pivot — Loop 10 has the full answer",
         "Loop 10 tested all 7 plausible event types with adversarial verification (3 lenses each). ZERO categories survive strict tradable-edge criteria. Bonus_issue is closest (+1.89% realistic) but n=273 is too small. Two realistic forward paths: (a) extend bonus_issue back 3-5 years and re-test — DART data is free, takes ~1 hour of backfill; (b) implement halt_resumption + shareholder_change as long-side blacklists in the risk engine (the strongest negative signals can become tradable risk filters); (c) accept the verdict and document the methodology as the project deliverable.",
         "Reply with which path(s). Honest take: do (b) regardless (it's a real risk control), then choose between (a) for one more shot at finding edge, or (c) to wind down the research phase."),
        ("P1",
         "Get an LLM API key (Anthropic or OpenAI) — needed for M5b",
         "M5b needs LLM-extracted qualitative features (counterparty type, language strength, recurrence) to feed the ML filter. Without these, M5b is just a boosted-tree on numeric ratios and probably won't beat the deterministic rule.",
         "console.anthropic.com / platform.openai.com. Add ANTHROPIC_API_KEY or OPENAI_API_KEY to .env, set LLM_PROVIDER, pip install anthropic|openai. Cost for 3,400 extractions: ~$10 with Sonnet, ~$1 with Haiku."),
        ("P2",
         "Rotate the DART API key",
         "The current key briefly appeared in chat output during the first smoke test (before the httpx log filter was added). Still safe on your machine, but rotating is the clean call. Demoted to P2 since it's been days with no incident.",
         "opendart.fss.or.kr → 관리자 → API Key 재발급. Replace DART_API_KEY in .env."),
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
        ("Parse docs CLI", "scripts/parse_supply_contracts.py [--limit N] [--skip-existing]"),
        ("Filtered event study", "scripts/run_filtered_event_study.py [--min-ratio 0.08]"),
        ("Strategy runner", "scripts/run_strategy.py [--min-ratio 0.30] [--dry-run]"),
        ("DB backup", "./scripts/backup_db.sh — gzipped SQL dump under data/backups/"),
        ("Walk-forward", "scripts/walk_forward.py — reproduces the v0/v1/v2 4-window table"),
        ("Paper backtest", "scripts/run_paper_backtest.py — realized-PnL with 3 execution assumptions"),
        ("Per-category analysis", "scripts/analyze_event_category.py --category &lt;X&gt;"),
        ("Cross-category summary", "scripts/summarize_all_categories.py"),
        ("Event study by category", "scripts/run_event_study.py --category &lt;X&gt;"),
        ("Event blacklist (NEW)", "src/kdtb/risk/event_blacklist.py — risk-engine check for recent negative events"),
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
        "&bull; Parser refinement: distinguishes 'value undisclosed' (literal dash) from 'missing entirely'. 0 blocked rows now.<br/>"
        "&bull; <b>storage/signal_store.py</b> + <b>scripts/run_strategy.py</b><br/>"
        "&bull; 331 candidate signals stored in DB at default threshold (Loop 4 snapshot)<br/>"
        "&bull; 4 new test cases",
        s["body"]
    ))
    story.append(Paragraph("<b>Loop 7</b> (codified the walk-forward filters into the strategy):", s["body"]))
    story.append(Paragraph(
        "&bull; Added <font face=\"Courier\">counterparty_name</font> + <font face=\"Courier\">counterparty_type</font> "
        "fields to Extraction schema, with idempotent ALTER TABLE migration in <font face=\"Courier\">init_db</font>.<br/>"
        "&bull; Parser now classifies counterparty (government / large_corp_korean / other_korean_corp / foreign / unknown) using the same regex heuristic the subgroup analysis used.<br/>"
        "&bull; Backfilled <font face=\"Courier\">counterparty_type</font> for all 3,531 existing extractions (94 government, 154 foreign, 331 large_corp_korean, 439 other_korean_corp, 2513 unknown).<br/>"
        "&bull; Added <font face=\"Courier\">kospi_only</font> and <font face=\"Courier\">excluded_counterparty_types</font> "
        "params to <font face=\"Courier\">MajorSupplyContractStrategy</font>. Defaults in config: "
        "<font face=\"Courier\">kospi_only: true</font>, <font face=\"Courier\">excluded_counterparty_types: [\"government\"]</font>.<br/>"
        "&bull; <b>Real PnL improvement</b> from the filters (still on the same 3,412 OK extractions):",
        s["body"]
    ))
    v2_table = Table(
        [
            ["Variant", "n", "T+1 net", "T+5 net", "T+5 win%", "PF 5d", "Notes"],
            ["v0 — no filters",             "3,377", "&minus;0.12%", "+0.35%", "44.1%", "1.10", "baseline (Loop 5)"],
            ["v1 — ratio ≥ 0.08",           "2,149", "&minus;0.07%", "+0.22%", "42.9%", "1.06", "CLAUDE.md default"],
            ["v2 — KOSPI + skip-gov",       "684",   "+0.17%",       "+0.67%", "46.6%", "1.25", "Loop-7 filters"],
        ],
        colWidths=[1.8 * inch, 0.55 * inch, 0.7 * inch, 0.7 * inch, 0.7 * inch, 0.55 * inch, 1.3 * inch],
    )
    v2_table.setStyle(TableStyle([
        ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 9),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0b3d91")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONT", (0, 1), (-1, -1), "Helvetica", 9),
        ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cccccc")),
        ("BACKGROUND", (0, 3), (-1, 3), colors.HexColor("#d4edda")),  # v2 row highlighted
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(v2_table)
    story.append(Spacer(1, 0.08 * inch))
    story.append(Paragraph(
        "<b>The v2 strategy nearly tripled T+5 net mean</b> over v1 (+0.67% vs +0.22%), pushed win rate from "
        "42.9% to 46.6%, and lifted profit factor from 1.06 to 1.25. T+1 mean turned positive for the first "
        "time. Median is still &minus;0.31% at T+5 &mdash; most individual trades still lose, but winners are bigger.",
        s["body"]
    ))
    story.append(Paragraph(
        "<b>Honest caveats:</b> 684 events over 24 months is ~28/month. Win rate is still &lt;50%. PF 1.25 means "
        "you win ₩1.25 for every ₩1 lost &mdash; thin margin once execution slippage and the 18 blocked rows "
        "drift the numbers. This is &quot;real progress&quot;, not &quot;ready to trade&quot;.",
        s["body"]
    ))
    story.append(Paragraph(
        "&bull; 92 tests still passing (+4 new for the filters).<br/>"
        "&bull; Tests cover: kospi_only rejects KOSDAQ, kospi_only off lets KOSDAQ through, "
        "excluded_counterparty_types rejects matching, None counterparty_type still passes.",
        s["note"]
    ))
    story.append(Spacer(1, 0.08 * inch))

    story.append(Paragraph("<b>Loop 8</b> (walk-forward validation of v0/v1/v2):", s["body"]))
    story.append(Paragraph(
        "The v2 aggregate looked promising; this loop verified that the improvement holds across "
        "4 non-overlapping 6-month windows (the real test of \"edge vs curve-fit\").",
        s["body"]
    ))
    wf2_table = Table(
        [
            ["Window", "v0 T+5 / PF (n)", "v1 T+5 / PF (n)", "v2 T+5 / PF (n)"],
            ["Jul24-Dec24", "+0.04% / 1.01 (785)", "&minus;0.38% / 0.90 (498)", "+1.57% / 1.58 (179) «"],
            ["Jan25-Jun25", "+0.59% / 1.23 (713)", "+0.51% / 1.19 (463)",       "+1.11% / 1.65 (138) «"],
            ["Jul25-Dec25", "+0.41% / 1.15 (930)", "+0.67% / 1.25 (573)",       "&minus;0.55% / 0.79 (200)"],
            ["Jan26-Jun26", "+0.45% / 1.10 (906)", "+0.15% / 1.03 (591)",       "+1.01% / 1.28 (163) «"],
            ["Aggregate",   "+0.37% / 1.11 (3,334)", "+0.25% / 1.07 (2,125)", "+0.72% / 1.27 (680)"],
            ["Positive windows", "4/4", "3/4", "3/4"],
        ],
        colWidths=[1.5 * inch, 1.7 * inch, 1.7 * inch, 1.7 * inch],
    )
    wf2_table.setStyle(TableStyle([
        ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 9),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0b3d91")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONT", (0, 1), (-1, -1), "Helvetica", 9),
        ("ALIGN", (1, 1), (-1, -1), "CENTER"),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cccccc")),
        ("BACKGROUND", (0, 5), (-1, 6), colors.HexColor("#fff3cd")),
        ("FONT", (0, 5), (-1, 6), "Helvetica-Bold", 9),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(wf2_table)
    story.append(Spacer(1, 0.08 * inch))
    story.append(Paragraph(
        "<b>Verdict: v2 holds up.</b> 3 of 4 windows positive, average +1.23% in the three "
        "winning windows, &minus;0.55% in the losing one. The losers are smaller in magnitude than the "
        "winners. Aggregate T+5 net mean +0.72%, profit factor 1.27, win rate 47.1%. v1 is "
        "consistently the worst of the three &mdash; the ratio filter alone removes signal without "
        "adding edge. The KOSPI + skip-government conjunction is what's doing the work.",
        s["body"]
    ))
    story.append(Paragraph(
        "<b>Still not a strategy ready for real money.</b> 47% win rate, one losing window in four, "
        "PF 1.27 (₩1.27 won per ₩1 lost) &mdash; thin margin once execution slippage drifts the numbers. "
        "But it IS the first thing in this project that survives walk-forward validation. The next "
        "moves (paper broker simulation, then ML on top) are now standing on real ground.",
        s["body"]
    ))
    story.append(Paragraph(
        "Reproduce with: <font face=\"Courier\">.venv/bin/python scripts/walk_forward.py</font>",
        s["note"]
    ))
    story.append(Spacer(1, 0.08 * inch))

    story.append(Paragraph("<b>Loop 9</b> (realistic-execution backtest — the painful finding):", s["body"]))
    story.append(Paragraph(
        "Re-priced the 684 v2 signals under three entry/exit assumptions. The "
        "&quot;idealized&quot; scenario matches the event study (buy event-day close, sell T+5 close), "
        "which assumes a fill we usually <i>can't</i> get as a retail trader.",
        s["body"]
    ))
    rb_table = Table(
        [
            ["Scenario", "Mean", "Median", "Win%", "PF", "₩/trade (on ₩30k)", "Total over 684 trades"],
            ["idealized (t0 close → t+5)",   "+0.67%", "&minus;0.31%", "46.6%", "1.25", "+₩201", "+₩137,779"],
            ["realistic (t+1 close → t+5)",  "+0.14%", "&minus;0.82%", "42.0%", "1.05", "+₩43",  "+₩29,140"],
            ["conservative (t+2 close → t+5)","+0.32%", "&minus;0.31%", "44.2%", "1.15", "+₩97",  "+₩66,078"],
        ],
        colWidths=[1.9 * inch, 0.65 * inch, 0.75 * inch, 0.55 * inch, 0.45 * inch, 1.25 * inch, 1.45 * inch],
    )
    rb_table.setStyle(TableStyle([
        ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 9),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0b3d91")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONT", (0, 1), (-1, -1), "Helvetica", 9),
        ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cccccc")),
        ("BACKGROUND", (0, 2), (-1, 2), colors.HexColor("#f8d7da")),  # realistic row highlighted (bad news)
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(rb_table)
    story.append(Spacer(1, 0.08 * inch))
    story.append(Paragraph(
        "<b>The painful finding:</b> under realistic execution (entering at the T+1 close instead of the "
        "event-day close), <b>most of v2's edge disappears.</b> Mean net per trade falls from +0.67% to "
        "+0.14%, PF from 1.25 to 1.05, win rate from 47% to 42%. Total realized PnL across 684 trades "
        "in 24 months would be roughly +₩29,000 &mdash; ~₩42/trade. Easily wiped out by execution slippage "
        "we haven't modeled.",
        s["body"]
    ))
    story.append(Paragraph(
        "<b>Distribution shows why:</b> 52% of v2 trades lose money under realistic execution (20% lose more "
        "than 5%). The 16% of trades that gain &gt;5% just barely outweigh the losers. This is a strategy "
        "whose entire \"edge\" is concentrated in the event-day reaction. If you can't trade at the close, "
        "you don't get the edge.",
        s["body"]
    ))
    story.append(Paragraph(
        "<b>What this means strategically:</b> the major-supply-contract drift is real but largely "
        "captured by the time a retail trader can act. Three realistic paths forward: "
        "(a) build infrastructure to react WITHIN seconds of DART publication &mdash; that's how Korean "
        "HFT desks trade these. CLAUDE.md explicitly excludes this as a non-goal. "
        "(b) Test other event types that may have longer-lived reactions: buybacks, dilutive financing, "
        "halt/resumption. (c) Accept the verdict and document the negative result as the project's "
        "deliverable. The deterministic-pipeline + walk-forward methodology is itself valuable; the "
        "strategy choice was the wrong horse.",
        s["body"]
    ))
    story.append(Paragraph(
        "Reproduce: <font face=\"Courier\">.venv/bin/python scripts/run_paper_backtest.py</font><br/>"
        "Per-trade detail: <font face=\"Courier\">data/paper_backtest_v2.csv</font>",
        s["note"]
    ))
    story.append(Spacer(1, 0.12 * inch))

    story.append(Paragraph("<b>Loop 10</b> — multi-event-type meta-analysis (the comprehensive answer)", s["h2"]))
    story.append(Paragraph(
        "Fetched event-study data for 6 additional event categories besides supply_contract, "
        "ran the same walk-forward + realistic-execution analysis on each, then adversarially "
        "verified every positive finding through three lenses (sample_size, execution_realism, "
        "regime_stability). Total: 17 agents in a parallel workflow.",
        s["body"]
    ))
    multi_table = Table(
        [
            ["Category", "n", "T+5 net (idealized)", "T+5 net (realistic)", "PF realistic", "WF +", "Verdict"],
            ["bonus_issue",        "273",   "+3.94%",      "+1.89%",      "1.54", "4/4", "positive but fragile (n too small)"],
            ["buyback",            "2,093", "+1.62%",      "+0.26%",      "1.13", "4/4", "execution kills 84% of edge"],
            ["supply_contract",    "3,495", "+0.39%",      "+0.13%",      "1.04", "4/4", "execution kills 67% of edge"],
            ["rights_offering",    "2,528", "+0.25%",      "+0.18%",      "1.05", "2/4", "noise (median &minus;0.94%)"],
            ["convertible_bond",   "1,864", "+0.85%",      "+0.04%",      "1.01", "2/4", "decaying, monotonic to negative"],
            ["halt_resumption",    "1,319", "&minus;2.62%","&minus;1.20%","0.72", "1/4", "consistently negative long"],
            ["shareholder_change", "834",   "&minus;2.38%","&minus;1.93%","0.52", "0/4", "consistently negative long"],
        ],
        colWidths=[1.4 * inch, 0.55 * inch, 1.0 * inch, 1.0 * inch, 0.8 * inch, 0.5 * inch, 2.0 * inch],
    )
    multi_table.setStyle(TableStyle([
        ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 9),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0b3d91")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONT", (0, 1), (-1, -1), "Helvetica", 9),
        ("ALIGN", (1, 1), (5, -1), "RIGHT"),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cccccc")),
        ("BACKGROUND", (0, 1), (-1, 1), colors.HexColor("#fff3cd")),  # bonus_issue
        ("BACKGROUND", (0, 6), (-1, 7), colors.HexColor("#d4edda")),  # negative signals as blacklist
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(multi_table)
    story.append(Spacer(1, 0.08 * inch))

    story.append(Paragraph(
        "<b>Adversarial verification verdict: ZERO categories pass.</b> "
        "Across seven Korean disclosure event categories tested with walk-forward validation "
        "and three adversarial verification lenses, zero categories survive strict tradable-edge "
        "criteria. Bonus_issue posts the highest realistic T+5 net mean at +1.89% with PF 1.54 "
        "and 4/4 positive walk-forward windows but fails on sample size (n=273, only 114 unique "
        "stocks, one window of n=50 carries the aggregate). Buyback (n=2,093) and supply_contract "
        "(n=3,495) show genuine idealized edge that collapses 84% and 67% respectively under T+1 "
        "realistic execution, ending at realistic means of just +0.26% and +0.13%. Halt_resumption "
        "(&minus;1.20%, PF 0.72, 23% win rate) and shareholder_change (&minus;1.93%, PF 0.52, 0/4 "
        "positive windows) produce the cleanest negative signals and should be implemented as "
        "deterministic long-side blacklists in the risk engine.",
        s["body"]
    ))
    story.append(Paragraph(
        "<b>Recommended next steps (from the workflow synthesis):</b>",
        s["body"]
    ))
    story.append(Paragraph(
        "&bull; <b>Extend bonus_issue dataset 3-5 years</b> back. Only reconsider paper trading if "
        "the worst walk-forward window stays above +0.30% mean with PF &gt; 1.10 after extension. "
        "Bonus_issue is the only category with realistic-execution edge above noise; needs "
        "~4&times; the current n to be statistically defensible.<br/>"
        "&bull; <b>Implement halt_resumption + shareholder_change as long-side blacklists</b> in "
        "the risk engine. These are the strongest negative findings &mdash; turning them into a "
        "deterministic do-not-trade filter is a tradable risk control even when no profitable "
        "signal exists.<br/>"
        "&bull; <b>Formalize the negative result as the Milestone 4 deliverable</b>: event-study "
        "methodology, walk-forward protocol, adversarial verification framework, per-category "
        "findings as the project's research output.<br/>"
        "&bull; <b>Defer M5b/M6/M7/M8/M9</b> (ML filter, broker integration, paper trading, live "
        "tiny) until either the extended bonus_issue dataset confirms edge OR a new untested "
        "category (earnings surprise proxy, contract cancellation, dilutive financing subtypes) "
        "clears the same adversarial bar.",
        s["body"]
    ))
    story.append(Paragraph(
        "Workflow JSON snapshot: <font face=\"Courier\">data/multi_event_meta_2026-06-26.json</font><br/>"
        "Reproduce per-category: <font face=\"Courier\">.venv/bin/python scripts/analyze_event_category.py --category &lt;X&gt;</font><br/>"
        "Cross-category table: <font face=\"Courier\">.venv/bin/python scripts/summarize_all_categories.py</font>",
        s["note"]
    ))
    story.append(Spacer(1, 0.1 * inch))

    story.append(Paragraph("<b>Loop 11</b> — halt/shareholder blacklist in the risk engine", s["h2"]))
    story.append(Paragraph(
        "Per Loop-10's findings (halt_resumption and shareholder_change are consistently "
        "negative-return event categories), implemented a deterministic do-not-trade filter "
        "in the risk engine. When the strategy generates a long signal, the risk engine now "
        "queries SQLite to check whether the subject stock has had a disclosure in any blacklisted "
        "category within the lookback window (default 30 days). If yes, the signal is rejected "
        "with reason <font face=\"Courier\">recent_negative_event:&lt;category&gt;</font>.",
        s["body"]
    ))
    story.append(Paragraph(
        "<b>Architecture:</b>",
        s["body"]
    ))
    story.append(Paragraph(
        "&bull; <b>data/event_categories.py</b> &mdash; shared CATEGORIES dict (extracted from run_event_study.py) so the event-study runner and the risk engine use the SAME patterns. DEFAULT_NEGATIVE_CATEGORIES = [halt_resumption, shareholder_change].<br/>"
        "&bull; <b>risk/event_blacklist.py</b> &mdash; EventBlacklist class wraps a SQLite connection, exposes <font face=\"Courier\">has_recent_negative_event(stock_code, now, lookback_days) → category_name | None</font>. ~5ms per query on the 514k-row table; no index needed.<br/>"
        "&bull; <b>risk/limits.py</b> &mdash; added <font face=\"Courier\">blacklist_lookback_days: int = 30</font> and <font face=\"Courier\">blacklisted_event_categories: list[str] = [halt_resumption, shareholder_change]</font>.<br/>"
        "&bull; <b>risk/checks.py</b> &mdash; new <font face=\"Courier\">check_negative_event_blacklist()</font> function. Silently skipped if blacklist is None (backwards-compatible with existing tests).<br/>"
        "&bull; <b>risk/risk_engine.py</b> &mdash; RiskEngine now accepts optional <font face=\"Courier\">blacklist</font> param in its constructor. Default None preserves the existing API.",
        s["body"]
    ))
    story.append(Paragraph(
        "<b>Validation:</b>",
        s["body"]
    ))
    story.append(Paragraph(
        "&bull; 17 new test cases covering: detection of both blacklist categories, lookback window "
        "boundary, different stock no-match, revision exclusion, supply_contract not in default list, "
        "custom category list, unknown category raises, empty stock_code returns None, zero lookback "
        "returns None, future event excluded (no look-ahead), picks most-recent when multiple.<br/>"
        "&bull; Full suite: 92 &rarr; <b>108 tests passing</b>. Zero regressions in existing risk tests "
        "(the optional <font face=\"Courier\">blacklist=None</font> default is backwards compatible).<br/>"
        "&bull; <b>Applied to the live 693 v2 stored signals</b>: 7 (1.0%) would be filtered out &mdash; "
        "4 by shareholder_change, 3 by halt_resumption. Rare-fire but high-conviction defensive filter.",
        s["body"]
    ))
    story.append(Paragraph(
        "<b>Bonus_issue extension (in progress):</b> as of this PDF write, a background backfill "
        "is extending the DART dataset by 36 more months (back to 2021-06). When complete, "
        "bonus_issue will have ~3&times; the current 273 events — enough to retest the only "
        "category with realistic-execution edge. The backfill takes ~30-40 minutes total.",
        s["body"]
    ))
    story.append(Paragraph(
        "Reproduce: <font face=\"Courier\">.venv/bin/python -c \"from kdtb.risk import EventBlacklist; ...\"</font>",
        s["note"]
    ))
    story.append(Spacer(1, 0.1 * inch))

    story.append(Paragraph("<b>Loop 5</b> (24-month backfill + scale-out + the negative finding):", s["body"]))
    story.append(Paragraph(
        "&bull; CLAUDE.md updated: ML/LLM allowed for extraction + feature enrichment + filtering, but NOT in the order-placing decision. New <b>Milestone 5b</b> added (ML-enriched signal filter).<br/>"
        "&bull; <b>scripts/backup_db.sh</b> &mdash; gzipped SQL dumps to <font face=\"Courier\">data/backups/</font>, keeps last 7, ready for cron.<br/>"
        "&bull; <b>24-month DART backfill</b>: 111,622 &rarr; <b>514,304 disclosures</b> across 488 trading days (Jun 2024 &ndash; Jun 2026).<br/>"
        "&bull; <b>Re-ran deterministic parser</b>: 533 &rarr; <b>3,412 OK extractions</b> (96.6% success rate held up at scale, 0 blocked).<br/>"
        "&bull; <b>Re-ran event study + threshold sweep</b>: the 30%+ edge from Loop 3 (T+5 +3.10%, PF 1.93) collapsed to T+5 +0.50% / PF 1.21 on 8&times; more data. Median negative at every threshold.<br/>"
        "&bull; <b>Event study script now checkpoints CSV every 100 stocks</b> &mdash; one of yesterday's runs hung overnight; we no longer lose hours of fetches.",
        s["body"]
    ))
    story.append(Paragraph(
        "Suite: 28 &rarr; 43 &rarr; 68 &rarr; 84 &rarr; <b>88</b> passing tests. Zero regressions across five loops.",
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
