"""Fetch company fundamentals from DART (financial statements + shares).

OPEN DART OpenAPI endpoints:
  fnlttSinglAcntAll.json  — full financial statements (BS + IS) for one company
  stockTotqySttus.json    — total shares outstanding

We extract equity, net income, revenue, debt, and common shares — enough to
compute PBR, PER, ROE and leverage once combined with the market price.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

DART_BASE = "https://opendart.fss.or.kr/api"

# account_nm candidates (spaces stripped) for each line item, in priority order
_EQUITY = ["자본총계"]
_DEBT = ["부채총계"]
_REVENUE = ["매출액", "수익(매출액)", "영업수익"]
# Net income takes many forms across statement types, so it's matched by regex
# on the income-statement rows (IS/CIS), not an exact-name whitelist:
#   당기순이익 / 연결당기순이익            (profit; most industrials)
#   당기순손실 / 당기순손익               (loss / profit-or-loss; e.g. SK이노베이션, 한화솔루션)
#   지배기업소유주지분순이익 / 지배주주순이익  (attributable-to-owners; financials, 고려아연)
# Section prefixes (e.g. "XI.", "Ⅸ.", "1.") are stripped first, and non-net
# lines (pretax / per-share / comprehensive / minority / operating) are skipped.
_INCOME_SJ = frozenset({"IS", "CIS"})
_NI_PREFIX = re.compile(r"^\s*(?:[Ⅰ-Ⅻ]+|[IVXLCDM]+|\d+)\s*[.)]\s*")
_NI_EXCLUDE = ("법인세", "주당", "포괄", "영업", "비지배", "소수주주")
_NI_TOTAL_RE = re.compile(r"^(연결)?당기순(이익|손실|손익)(\(손실\))?$")
_NI_ATTRIB_RE = re.compile(r"^(지배기업소유주지분순이익|지배주주순이익|보통주당기순이익)$")


def _find_net_income(accounts: list[dict]) -> Optional[int]:
    """Robustly locate the period net income. Prefers the total line; falls back
    to the attributable-to-owners line when no plain total exists (financials)."""
    total = attrib = None
    for acc in accounts:
        if acc.get("sj_div") not in _INCOME_SJ:
            continue
        nm = _NI_PREFIX.sub("", str(acc.get("account_nm", ""))).replace(" ", "")
        if any(x in nm for x in _NI_EXCLUDE):
            continue
        if total is None and _NI_TOTAL_RE.match(nm):
            total = _to_int(acc.get("thstrm_amount"))
        elif attrib is None and _NI_ATTRIB_RE.match(nm):
            attrib = _to_int(acc.get("thstrm_amount"))
    return total if total is not None else attrib


def _to_int(s: Optional[str]) -> Optional[int]:
    if s is None:
        return None
    t = str(s).strip().replace(",", "")
    if t in ("", "-"):
        return None
    neg = t.startswith("(") and t.endswith(")")  # accounting negatives
    t = t.strip("()")
    try:
        v = int(float(t))
        return -v if neg else v
    except ValueError:
        return None


def _find_amount(
    accounts: list[dict],
    names: list[str],
    allowed_sj: Optional[frozenset[str]] = None,
) -> Optional[int]:
    """First account whose (space-stripped) name matches, honoring `names`
    priority order. If allowed_sj is given, only rows from those statements
    (sj_div) are considered — used to keep net income to the income statement.
    """
    for target in names:  # respect priority order of the names list
        t = target.replace(" ", "")
        for acc in accounts:
            if allowed_sj is not None and acc.get("sj_div") not in allowed_sj:
                continue
            if str(acc.get("account_nm", "")).replace(" ", "") == t:
                v = _to_int(acc.get("thstrm_amount"))
                if v is not None:
                    return v
    return None


class DartFundamentals:
    def __init__(self, api_key: str, client: Optional[httpx.Client] = None, timeout: float = 30.0):
        if not api_key:
            raise ValueError("DART API key required")
        self.api_key = api_key
        self._client = client or httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def _get(self, endpoint: str, params: dict) -> dict:
        r = self._client.get(f"{DART_BASE}/{endpoint}", params={**params, "crtfc_key": self.api_key})
        r.raise_for_status()
        return r.json()

    def financials(self, corp_code: str, year: str, reprt: str = "11011") -> Optional[dict]:
        """Return {equity, net_income, revenue, debt, fs_div} or None.

        Tries consolidated (CFS) first, then separate (OFS).
        """
        for fs_div in ("CFS", "OFS"):
            try:
                data = self._get("fnlttSinglAcntAll.json", {
                    "corp_code": corp_code, "bsns_year": year, "reprt_code": reprt, "fs_div": fs_div,
                })
            except httpx.HTTPError as e:
                logger.warning("financials %s %s %s: %s", corp_code, year, fs_div, e)
                continue
            if data.get("status") != "000":
                continue
            accts = data.get("list", [])
            equity = _find_amount(accts, _EQUITY)
            if equity is None:
                continue
            return {
                "equity": equity,
                "debt": _find_amount(accts, _DEBT),
                "revenue": _find_amount(accts, _REVENUE, _INCOME_SJ),
                "net_income": _find_net_income(accts),
                "fs_div": fs_div,
            }
        return None

    def common_shares(self, corp_code: str, year: str, reprt: str = "11011") -> Optional[int]:
        """Common shares issued (보통주 발행총수)."""
        try:
            data = self._get("stockTotqySttus.json", {
                "corp_code": corp_code, "bsns_year": year, "reprt_code": reprt,
            })
        except httpx.HTTPError as e:
            logger.warning("shares %s %s: %s", corp_code, year, e)
            return None
        if data.get("status") != "000":
            return None
        for row in data.get("list", []):
            if str(row.get("se", "")).strip().startswith("보통주"):
                return _to_int(row.get("istc_totqy"))
        return None

    def fetch(self, corp_code: str, years: tuple[str, ...] = ("2025", "2024")) -> Optional[dict]:
        """Try each year newest-first; return the first with usable financials + shares."""
        for year in years:
            fin = self.financials(corp_code, year)
            if not fin:
                continue
            shares = self.common_shares(corp_code, year)
            if not shares:
                continue
            return {**fin, "shares": shares, "fiscal_year": year}
        return None
