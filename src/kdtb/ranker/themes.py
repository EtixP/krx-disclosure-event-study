"""Korean market theme taxonomy + stock→theme mapping.

Korea trades in violent thematic rotations (defense, nuclear, batteries,
shipbuilding, AI, bio, ...). This module defines the themes and maps stocks to
them DETERMINISTICALLY by curated ticker membership (high precision). Coverage
is limited to the curated lists; the optional LLM layer
(`theme_context` / future enrichment) extends recall to the full universe when
an API key is present.

A stock can belong to multiple themes. "Theme strength" is measured elsewhere
(theme_momentum) from the actual price momentum of each theme's basket — a fact,
not an opinion.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Theme:
    key: str
    name_kr: str
    tickers: frozenset[str] = field(default_factory=frozenset)
    name_keywords: tuple[str, ...] = ()


# Curated as of 2026; extend freely. Tickers are the durable membership signal;
# name_keywords are a weak secondary (only distinctive substrings to avoid false hits).
THEMES: tuple[Theme, ...] = (
    Theme("defense", "방산", frozenset({
        "012450", "079550", "064350", "047810", "272210", "103140", "012510"}), ("방산",)),
    Theme("nuclear", "원전", frozenset({
        "034020", "052690", "051600", "083650", "105840", "100090"}), ()),
    Theme("batteries", "2차전지", frozenset({
        "373220", "006400", "096770", "247540", "086520", "003670", "066970",
        "005070", "020150", "137400"}), ("에코프로",)),
    Theme("semiconductors", "반도체", frozenset({
        "005930", "000660", "042700", "000990", "058470", "240810", "357780",
        "095340", "036930"}), ("반도체",)),
    Theme("shipbuilding", "조선", frozenset({
        "009540", "010140", "042660", "329180", "010620", "075580"}), ("조선", "중공업")),
    Theme("ai_internet", "AI·인터넷", frozenset({
        "035420", "035720", "012510", "053800", "067160"}), ()),
    Theme("bio", "바이오", frozenset({
        "207940", "068270", "000100", "326030", "196170", "302440", "145020"}), ("바이오", "제약")),
    Theme("automotive", "자동차", frozenset({
        "005380", "000270", "012330", "018880", "204320", "011210"}), ()),
    Theme("entertainment", "엔터·K콘텐츠", frozenset({
        "352820", "035900", "041510", "122870", "035760", "253450"}), ("엔터",)),
    Theme("steel_materials", "철강·소재", frozenset({
        "005490", "004020", "010130", "103140", "000880"}), ("제철", "금속")),
    Theme("chemicals", "화학", frozenset({
        "051910", "011170", "011780", "120110", "285130"}), ("화학", "케미칼")),
    Theme("finance", "금융", frozenset({
        "105560", "055550", "086790", "138040", "316140", "071050", "029780"}), ("금융", "지주")),
    Theme("power_utility", "전력·유틸", frozenset({
        "015760", "336260", "267260", "051600"}), ()),
    Theme("aerospace_space", "우주항공", frozenset({
        "047810", "272210", "189300", "451760"}), ("항공", "우주")),
    Theme("robotics", "로봇", frozenset({
        "277810", "454910", "108490", "117730"}), ("로봇",)),
    Theme("cosmetics", "화장품", frozenset({
        "090430", "051900", "192820", "002790", "237880"}), ("화장품",)),
)

_BY_KEY = {t.key: t for t in THEMES}


def theme_name(key: str) -> str:
    t = _BY_KEY.get(key)
    return t.name_kr if t else key


def map_stock_to_themes(stock_code: str, corp_name: str = "") -> list[str]:
    """Return the theme keys a stock belongs to (deterministic, may be empty)."""
    code = str(stock_code).strip()
    name = str(corp_name or "")
    out: list[str] = []
    for t in THEMES:
        if code in t.tickers or any(kw and kw in name for kw in t.name_keywords):
            out.append(t.key)
    return out
