"""
F3: SEC EDGAR 8-K collector + Loughran-McDonald NLP scorer.

For each (ticker, earnings_date) in earnings_fundamentals:
  1. Look up the company CIK on SEC EDGAR
  2. Find the 8-K (or 6-K for foreign issuers) filed within ±5 days of earnings_date
  3. Download Exhibit 99.1 (earnings press release)
  4. Parse into sections: release (full doc), mda, guidance
  5. Score each section with Loughran-McDonald (LM) word frequencies
  6. Compute deltas vs prior period (primary ML signal)
  7. Upsert into earnings_nlp_signals

LM dictionary:
  Downloaded on first use from Notre Dame SRAF and cached to data/lm_master_dict.csv.
  Override path with --lm-dict. If download fails, error message includes instructions.

EDGAR:
  Rate limit: max 10 req/s. We sleep 0.15s between calls.
  Non-US filers without EDGAR CIK → nlp_scoring_method = 'no_filing'.
  Requires SEC User-Agent header: "Company email" (set via EDGAR_USER_AGENT env var
  or defaults to a generic value).

Run:
  python -m scripts.collect_earnings_nlp
  python -m scripts.collect_earnings_nlp --tickers AAPL MSFT --max-quarters 4
  python -m scripts.collect_earnings_nlp --lm-dict data/lm_master_dict.csv
  python -m scripts.collect_earnings_nlp --dry-run
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import time
from datetime import date, timedelta
from pathlib import Path
import sys
from typing import Optional

import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from scripts.db import connect, ensure_financial_results_tables

log = logging.getLogger(__name__)

# EDGAR rate limit: 10 req/s max
EDGAR_SLEEP = 0.15
EDGAR_UA = os.environ.get(
    "EDGAR_USER_AGENT",
    "OptionsStrategyLab research@optionsstrategylab.com",
)
EDGAR_HEADERS = {"User-Agent": EDGAR_UA}

# LM dict: scrape SRAF page for current URL, fall back to GitHub mirror
_LM_SRAF_PAGE = "https://sraf.nd.edu/loughranmcdonald-master-dictionary/"
_LM_FALLBACK_URLS = [
    # GitHub mirror of the 2018 edition (word categories stable since then)
    "https://raw.githubusercontent.com/yairf11/LoughranMcDonald/master/LoughranMcDonald_MasterDictionary_2018.csv",
]

_LM_DICT_VERSION = "LM_2018_or_later"

_DEFAULT_LM_CACHE = _ROOT / "data" / "lm_master_dict.csv"

# Filing search window around earnings_date (calendar days)
_FILING_WINDOW_DAYS = 5

# Tickers to skip (ETFs, no earnings)
_NO_EARNINGS = frozenset({
    "SPY", "QQQ", "TQQQ", "IWM",
    "XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY",
    "^VIX", "VIX",
})

# Section-detection regex patterns (case-insensitive)
_SECTION_HEADERS = {
    "mda": re.compile(
        r"(?:management[\s\W]+(?:s[\s\W]+)?discussion|results\s+of\s+operations|financial\s+review)",
        re.I,
    ),
    "guidance": re.compile(
        r"(?:outlook|business\s+outlook|guidance|forward[\s\W]+looking|financial\s+targets)",
        re.I,
    ),
}

# Any ALL-CAPS line of ≥4 chars marks a new major section
_MAJOR_HEADER = re.compile(r"^[A-Z][A-Z\s\-&,/]{3,}$")


# ── LM Dictionary ──────────────────────────────────────────────────────────────

class LMDictionary:
    """Loughran-McDonald financial sentiment word sets."""

    def __init__(self, csv_path: Path):
        try:
            df = pd.read_csv(csv_path, low_memory=False)
            df.columns = [c.strip() for c in df.columns]
            word_col = "Word"
            self.negative     = frozenset(df[df["Negative"]     > 0][word_col].str.upper())
            self.positive     = frozenset(df[df["Positive"]     > 0][word_col].str.upper())
            self.uncertainty  = frozenset(df[df["Uncertainty"]  > 0][word_col].str.upper())
            self.litigious    = frozenset(df[df["Litigious"]    > 0][word_col].str.upper())
            self.constraining = frozenset(df[df["Constraining"] > 0][word_col].str.upper())
        except Exception as exc:
            raise RuntimeError(f"Failed to load LM dict from {csv_path}: {exc}") from exc

    def score(self, text: str) -> dict | None:
        """Score text. Returns None if text is empty."""
        if not text or not text.strip():
            return None
        words = re.findall(r"[A-Za-z]+", text)
        if len(words) < 20:
            return None
        upwords = [w.upper() for w in words]
        total = len(upwords)
        neg  = sum(1 for w in upwords if w in self.negative)
        pos  = sum(1 for w in upwords if w in self.positive)
        unc  = sum(1 for w in upwords if w in self.uncertainty)
        lit  = sum(1 for w in upwords if w in self.litigious)
        con  = sum(1 for w in upwords if w in self.constraining)
        return {
            "total_words":       total,
            "negative_count":    neg,
            "positive_count":    pos,
            "uncertainty_count": unc,
            "litigious_count":   lit,
            "constraining_count":con,
            "negative_pct":      round(neg  / total, 6),
            "positive_pct":      round(pos  / total, 6),
            "uncertainty_pct":   round(unc  / total, 6),
            "litigious_pct":     round(lit  / total, 6),
            "constraining_pct":  round(con  / total, 6),
        }


def _scrape_lm_url() -> str | None:
    """Scrape the SRAF page for the current LM Master Dictionary CSV download URL."""
    import re
    import urllib.request
    try:
        req = urllib.request.Request(_LM_SRAF_PAGE, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")
        matches = re.findall(r'href=["\']([^"\']*MasterDictionary[^"\']*\.csv)["\']', html, re.I)
        if matches:
            url = matches[-1]
            if url.startswith("/"):
                url = "https://sraf.nd.edu" + url
            log.info("Found LM dict URL on SRAF page: %s", url)
            return url
    except Exception as exc:
        log.warning("Could not scrape SRAF page (%s): %s", _LM_SRAF_PAGE, exc)
    return None


def _copy_from_pysentiment2(cache_path: Path) -> bool:
    """
    Use pysentiment2's internal word sets to generate a CSV in the format our
    LMDictionary class expects (Word, Negative, Positive, Uncertainty, Litigious, Constraining).
    pysentiment2 stores these as frozensets on the LM instance under various attribute names.
    """
    try:
        import subprocess, sys, os, shutil
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "-q", "pysentiment2"],
            timeout=60,
        )
        import pysentiment2
        import pandas as pd

        # ── Try 1: find any bundled CSV/txt file ─────────────────────────
        pkg_dir = os.path.dirname(pysentiment2.__file__)
        for root, _, files in os.walk(pkg_dir):
            for fname in files:
                if fname.endswith(".csv") and any(k in fname for k in ("Master", "LM_", "lm_")):
                    shutil.copy(os.path.join(root, fname), cache_path)
                    log.info("LM dict copied from pysentiment2 bundle → %s", cache_path)
                    return True

        # ── Try 2: extract from the LM instance's internal word sets ─────
        lm_obj = pysentiment2.LM()

        def _get(obj, *names):
            for n in names:
                v = getattr(obj, n, None)
                if v:
                    return set(v)
            return set()

        neg = _get(lm_obj, "_negset", "_negDict", "negative", "Negative")
        pos = _get(lm_obj, "_posset", "_posDict", "positive", "Positive")
        unc = _get(lm_obj, "_uncset", "_uncDict", "uncertainty", "Uncertainty")
        lit = _get(lm_obj, "_litset", "_litDict", "litigious", "Litigious")
        con = _get(lm_obj, "_conset", "_conDict", "constraining", "Constraining")

        all_words = neg | pos | unc | lit | con
        if not all_words:
            # Last resort: dump every frozenset/set attribute we can find
            for attr, val in vars(lm_obj).items():
                if isinstance(val, (set, frozenset)) and val:
                    log.info("pysentiment2 found attr %s with %d words", attr, len(val))
                    all_words |= set(val)

        if not all_words:
            log.warning("pysentiment2 LM instance has no extractable word sets")
            return False

        rows = [
            {
                "Word":        w,
                "Negative":    2018 if w in neg else 0,
                "Positive":    2018 if w in pos else 0,
                "Uncertainty": 2018 if w in unc else 0,
                "Litigious":   2018 if w in lit else 0,
                "Constraining":2018 if w in con else 0,
            }
            for w in sorted(all_words)
        ]
        pd.DataFrame(rows).to_csv(cache_path, index=False)
        log.info("LM dict generated from pysentiment2 word sets (%d words) → %s",
                 len(rows), cache_path)
        return True

    except Exception as exc:
        log.warning("pysentiment2 fallback failed: %s", exc)
    return False


def _load_lm_dict(cache_path: Path) -> LMDictionary:
    if not cache_path.exists():
        log.info("LM dict not found at %s — downloading...", cache_path)
        import urllib.request
        cache_path.parent.mkdir(parents=True, exist_ok=True)

        # 1. Scrape SRAF page for current URL
        candidates = []
        scraped = _scrape_lm_url()
        if scraped:
            candidates.append(scraped)
        candidates.extend(_LM_FALLBACK_URLS)

        downloaded = False
        for url in candidates:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = resp.read()
                cache_path.write_bytes(data)
                log.info("LM dict downloaded (%d bytes) → %s", len(data), cache_path)
                downloaded = True
                break
            except Exception as exc:
                log.warning("Download attempt failed (%s): %s", url, exc)

        # 2. Fall back to pysentiment2 bundled CSV
        if not downloaded:
            downloaded = _copy_from_pysentiment2(cache_path)

        if not downloaded:
            raise RuntimeError(
                f"Could not obtain LM Master Dictionary.\n"
                f"Manual fix: download the CSV from {_LM_SRAF_PAGE}\n"
                f"and save it to: {cache_path}"
            )
    return LMDictionary(cache_path)


# ── EDGAR client ───────────────────────────────────────────────────────────────

def _edgar_get(url: str) -> bytes | None:
    import urllib.request
    try:
        req = urllib.request.Request(url, headers=EDGAR_HEADERS)
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read()
    except Exception as exc:
        log.debug("EDGAR GET failed %s: %s", url, exc)
        return None


_cik_cache: dict[str, str] = {}  # ticker → zero-padded 10-char CIK

def _get_cik(ticker: str) -> str | None:
    """Return 10-digit padded CIK for ticker, or None if not found."""
    if ticker in _cik_cache:
        return _cik_cache[ticker]

    data = _edgar_get("https://www.sec.gov/files/company_tickers.json")
    time.sleep(EDGAR_SLEEP)
    if not data:
        return None

    try:
        mapping = json.loads(data)
        # {idx: {cik_str, ticker, title}}
        ticker_upper = ticker.upper().replace("-", "")
        for entry in mapping.values():
            if entry.get("ticker", "").upper().replace("-", "") == ticker_upper:
                cik_str = str(entry["cik_str"]).zfill(10)
                _cik_cache[ticker] = cik_str
                return cik_str
    except Exception as exc:
        log.warning("CIK parse error: %s", exc)
    return None


def _find_filing(cik: str, earnings_date: date) -> tuple[str | None, str | None]:
    """
    Return (accession_number, form_type) for an 8-K or 6-K filed within
    ±_FILING_WINDOW_DAYS of earnings_date. Returns (None, None) if not found.
    """
    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    data = _edgar_get(url)
    time.sleep(EDGAR_SLEEP)
    if not data:
        return None, None

    try:
        sub = json.loads(data)
        recent = sub.get("filings", {}).get("recent", {})
        forms    = recent.get("form", [])
        dates    = recent.get("filingDate", [])
        accnos   = recent.get("accessionNumber", [])
    except Exception:
        return None, None

    window_lo = earnings_date - timedelta(days=_FILING_WINDOW_DAYS)
    window_hi = earnings_date + timedelta(days=_FILING_WINDOW_DAYS)

    best_acc, best_form, best_delta = None, None, 999
    for form, dt_str, acc in zip(forms, dates, accnos):
        if form not in ("8-K", "6-K", "8-K/A"):
            continue
        try:
            filing_date = date.fromisoformat(dt_str)
        except Exception:
            continue
        if window_lo <= filing_date <= window_hi:
            delta = abs((filing_date - earnings_date).days)
            if delta < best_delta:
                best_delta = delta
                best_acc   = acc
                best_form  = form

    return best_acc, best_form


def _get_exhibit_text(cik: str, accession_no: str) -> tuple[str | None, str | None]:
    """
    Download Exhibit 99.1 text from the filing. Falls back to the full 8-K text.
    Returns (text, url_used).
    """
    acc_clean = accession_no.replace("-", "")
    idx_url = (
        f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
        f"{acc_clean}/{accession_no}-index.htm"
    )
    idx_data = _edgar_get(idx_url)
    time.sleep(EDGAR_SLEEP)

    exhibit_url = None
    if idx_data:
        # Parse index page for Exhibit 99.1 link
        try:
            from lxml.html import fromstring
            tree = fromstring(idx_data)
            for row in tree.xpath("//tr"):
                cells = row.xpath("td/text()|td/a/text()")
                hrefs = row.xpath("td/a/@href")
                row_text = " ".join(str(c) for c in cells).upper()
                if "99.1" in row_text or "EX-99" in row_text:
                    if hrefs:
                        href = hrefs[0]
                        if not href.startswith("http"):
                            href = "https://www.sec.gov" + href
                        exhibit_url = href
                        break
        except Exception:
            pass

    # Fallback: construct the full filing document URL
    if not exhibit_url:
        exhibit_url = (
            f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
            f"{acc_clean}/{accession_no}.txt"
        )

    doc_data = _edgar_get(exhibit_url)
    time.sleep(EDGAR_SLEEP)
    if not doc_data:
        return None, exhibit_url

    text = _html_to_text(doc_data)
    return text, exhibit_url


def _html_to_text(data: bytes) -> str:
    """Strip HTML tags and return clean text."""
    try:
        from lxml.html import fromstring
        return fromstring(data).text_content()
    except Exception:
        # Plain text fallback
        text = data.decode("utf-8", errors="replace")
        text = re.sub(r"<[^>]+>", " ", text)
        return text


# ── Section parser ─────────────────────────────────────────────────────────────

def _extract_sections(text: str) -> dict[str, str]:
    """
    Return {'release': full_text, 'mda': ..., 'guidance': ...}.
    'release' is always the full text. 'mda' and 'guidance' are
    extracted paragraphs; if not found they are empty strings.

    Section detection uses keyword headers; extraction stops at the
    next detected major section header.
    """
    result = {"release": text, "mda": "", "guidance": ""}
    paragraphs = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]

    for section in ("mda", "guidance"):
        pattern = _SECTION_HEADERS[section]
        collecting = False
        buf: list[str] = []
        for para in paragraphs:
            first_line = para.split("\n")[0].strip()
            if not collecting:
                if pattern.search(para[:200]):
                    collecting = True
                    buf.append(para)
            else:
                # Stop at a new major section header (all-caps line, or another keyword section)
                if _MAJOR_HEADER.match(first_line) and first_line != first_line.lower():
                    # Check if it's a different section
                    other = "guidance" if section == "mda" else "mda"
                    if _SECTION_HEADERS[other].search(first_line):
                        break
                    if len(first_line) > 60 and first_line.isupper():
                        break
                buf.append(para)
                if len(buf) > 20:  # cap section length at ~20 paragraphs
                    break
        result[section] = "\n\n".join(buf)

    return result


# ── NLP row builder ────────────────────────────────────────────────────────────

def _delta(current: float | None, prior: float | None) -> float | None:
    if current is None or prior is None:
        return None
    return round(current - prior, 6)


def _build_nlp_row(
    ticker: str,
    earnings_date: date,
    text: str,
    filing_url: str,
    form_type: str,
    lm: LMDictionary,
    prior_row: dict | None,
) -> dict:
    sections = _extract_sections(text)
    sections_found = []

    whole = lm.score(sections["release"])
    rel_s = lm.score(sections["release"])   # same as whole for release section
    mda_s = lm.score(sections["mda"]) if sections["mda"] else None
    guid_s = lm.score(sections["guidance"]) if sections["guidance"] else None

    if mda_s:
        sections_found.append("mda")
    if guid_s:
        sections_found.append("guidance")

    method = "section_level" if sections_found else "whole_doc"
    sections_found = ["release"] + sections_found

    def _p(s, k, default=None):
        return s.get(k, default) if s else default

    # Derived sentiments
    neg_pct  = _p(whole, "negative_pct")
    pos_pct  = _p(whole, "positive_pct")
    unc_pct  = _p(whole, "uncertainty_pct")
    lit_pct  = _p(whole, "litigious_pct")
    con_pct  = _p(whole, "constraining_pct")

    overall_sentiment = (round(pos_pct - neg_pct, 6)
                         if pos_pct is not None and neg_pct is not None else None)
    risk_sentiment    = (round(-(lit_pct + con_pct), 6)
                         if lit_pct is not None and con_pct is not None else None)

    mda_neg  = _p(mda_s,  "negative_pct")
    mda_pos  = _p(mda_s,  "positive_pct")
    guid_neg = _p(guid_s, "negative_pct")
    guid_pos = _p(guid_s, "positive_pct")
    guid_unc = _p(guid_s, "uncertainty_pct")

    management_sentiment = (round(mda_pos - mda_neg, 6)
                            if mda_pos is not None and mda_neg is not None
                            else overall_sentiment)
    guidance_sentiment   = (round(guid_pos - guid_unc, 6)
                            if guid_pos is not None and guid_unc is not None else None)

    # Deltas vs prior period
    p = prior_row or {}
    row = {
        "ticker":                    ticker,
        "earnings_date":             earnings_date.isoformat(),
        "total_words":               _p(whole, "total_words"),
        "positive_count":            _p(whole, "positive_count"),
        "negative_count":            _p(whole, "negative_count"),
        "uncertainty_count":         _p(whole, "uncertainty_count"),
        "litigious_count":           _p(whole, "litigious_count"),
        "constraining_count":        _p(whole, "constraining_count"),
        "positive_pct":              pos_pct,
        "negative_pct":              neg_pct,
        "uncertainty_pct":           unc_pct,
        "litigious_pct":             lit_pct,
        "constraining_pct":          con_pct,
        "positive_delta":            _delta(pos_pct,    p.get("positive_pct")),
        "negative_delta":            _delta(neg_pct,    p.get("negative_pct")),
        "uncertainty_delta":         _delta(unc_pct,    p.get("uncertainty_pct")),
        "litigious_delta":           _delta(lit_pct,    p.get("litigious_pct")),
        "constraining_delta":        _delta(con_pct,    p.get("constraining_pct")),
        # Release section (= whole doc for Exhibit 99.1)
        "release_negative_pct":      _p(rel_s, "negative_pct"),
        "release_uncertainty_pct":   _p(rel_s, "uncertainty_pct"),
        "release_positive_pct":      _p(rel_s, "positive_pct"),
        "release_negative_delta":    _delta(_p(rel_s, "negative_pct"),    p.get("release_negative_pct")),
        "release_uncertainty_delta": _delta(_p(rel_s, "uncertainty_pct"), p.get("release_uncertainty_pct")),
        # MD&A section
        "mda_negative_pct":          mda_neg,
        "mda_uncertainty_pct":       _p(mda_s, "uncertainty_pct"),
        "mda_positive_pct":          mda_pos,
        "mda_negative_delta":        _delta(mda_neg, p.get("mda_negative_pct")),
        "mda_uncertainty_delta":     _delta(_p(mda_s, "uncertainty_pct"), p.get("mda_uncertainty_pct")),
        # Guidance section
        "guidance_negative_pct":     guid_neg,
        "guidance_uncertainty_pct":  guid_unc,
        "guidance_positive_pct":     guid_pos,
        "guidance_negative_delta":   _delta(guid_neg, p.get("guidance_negative_pct")),
        "guidance_uncertainty_delta":_delta(guid_unc, p.get("guidance_uncertainty_pct")),
        # Derived sentiments
        "overall_sentiment":         overall_sentiment,
        "management_sentiment":      management_sentiment,
        "guidance_sentiment":        guidance_sentiment,
        "risk_sentiment":            risk_sentiment,
        "management_sentiment_delta":_delta(management_sentiment, p.get("management_sentiment")),
        "guidance_sentiment_delta":  _delta(guidance_sentiment,  p.get("guidance_sentiment")),
        # Metadata
        "nlp_scoring_method":        method,
        "filing_url":                filing_url,
        "lm_dict_version":           _LM_DICT_VERSION,
        "sections_found":            json.dumps(sections_found),
    }
    return row


_INSERT_COLS = [
    "ticker", "earnings_date",
    "total_words", "positive_count", "negative_count", "uncertainty_count",
    "litigious_count", "constraining_count",
    "positive_pct", "negative_pct", "uncertainty_pct", "litigious_pct", "constraining_pct",
    "positive_delta", "negative_delta", "uncertainty_delta", "litigious_delta", "constraining_delta",
    "release_negative_pct", "release_uncertainty_pct", "release_positive_pct",
    "release_negative_delta", "release_uncertainty_delta",
    "mda_negative_pct", "mda_uncertainty_pct", "mda_positive_pct",
    "mda_negative_delta", "mda_uncertainty_delta",
    "guidance_negative_pct", "guidance_uncertainty_pct", "guidance_positive_pct",
    "guidance_negative_delta", "guidance_uncertainty_delta",
    "overall_sentiment", "management_sentiment", "guidance_sentiment", "risk_sentiment",
    "management_sentiment_delta", "guidance_sentiment_delta",
    "nlp_scoring_method", "filing_url", "lm_dict_version", "sections_found",
]


def _upsert(rows: list[dict], dry_run: bool) -> int:
    if not rows or dry_run:
        return len(rows)
    ph = ", ".join(["?"] * len(_INSERT_COLS))
    sql = (
        f"INSERT OR REPLACE INTO earnings_nlp_signals "
        f"({', '.join(_INSERT_COLS)}) VALUES ({ph})"
    )
    with connect() as con:
        for row in rows:
            con.execute(sql, [row.get(c) for c in _INSERT_COLS])
        con.commit()
    return len(rows)


# ── Main ───────────────────────────────────────────────────────────────────────

def run(
    tickers: list[str] | None = None,
    max_quarters: int | None = None,
    lm_dict_path: Path | None = None,
    dry_run: bool = False,
) -> None:
    ensure_financial_results_tables()

    lm = _load_lm_dict(lm_dict_path or _DEFAULT_LM_CACHE)
    log.info(
        "LM dict loaded: %d neg, %d pos, %d unc words",
        len(lm.negative), len(lm.positive), len(lm.uncertainty),
    )

    with connect() as con:
        if tickers:
            placeholders = ", ".join("?" * len(tickers))
            rows = con.execute(
                f"SELECT ticker, earnings_date FROM earnings_fundamentals "
                f"WHERE ticker IN ({placeholders}) ORDER BY ticker, earnings_date",
                tickers,
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT ticker, earnings_date FROM earnings_fundamentals "
                "ORDER BY ticker, earnings_date"
            ).fetchall()

    if not rows:
        log.error("No rows in earnings_fundamentals — run F1 backfill first")
        return

    # Group by ticker (oldest→newest so deltas compute correctly)
    from collections import defaultdict
    by_ticker: dict[str, list[str]] = defaultdict(list)
    for t, ed in rows:
        if t not in _NO_EARNINGS:
            by_ticker[t].append(ed)

    log.info("Processing %d tickers, %d total earnings events", len(by_ticker), len(rows))

    total_inserted = 0
    total_no_filing = 0
    total_scored = 0

    for ticker, earn_dates in by_ticker.items():
        log.info("[%s] %d quarters", ticker, len(earn_dates))

        cik = _get_cik(ticker)
        time.sleep(EDGAR_SLEEP)
        if not cik:
            log.warning("  %s: no CIK found on EDGAR — skipping", ticker)
            total_no_filing += len(earn_dates)
            continue

        if max_quarters:
            earn_dates = earn_dates[-max_quarters:]  # most recent N quarters

        prior_row: dict | None = None
        ticker_rows: list[dict] = []

        for ed_str in earn_dates:
            earn_date = ed_str if isinstance(ed_str, date) else date.fromisoformat(str(ed_str))

            accno, form_type = _find_filing(cik, earn_date)
            time.sleep(EDGAR_SLEEP)

            if not accno:
                log.debug("  %s %s: no 8-K/6-K found within ±%dd", ticker, ed_str, _FILING_WINDOW_DAYS)
                no_filing_row = {
                    "ticker": ticker, "earnings_date": ed_str,
                    "nlp_scoring_method": "no_filing",
                    "lm_dict_version": _LM_DICT_VERSION,
                    "sections_found": "[]",
                }
                ticker_rows.append(no_filing_row)
                total_no_filing += 1
                continue

            text, filing_url = _get_exhibit_text(cik, accno)
            time.sleep(EDGAR_SLEEP)

            if not text or len(text.strip()) < 100:
                log.debug("  %s %s: empty exhibit text", ticker, ed_str)
                ticker_rows.append({
                    "ticker": ticker, "earnings_date": ed_str,
                    "nlp_scoring_method": "no_filing",
                    "filing_url": filing_url,
                    "lm_dict_version": _LM_DICT_VERSION,
                    "sections_found": "[]",
                })
                total_no_filing += 1
                continue

            row = _build_nlp_row(
                ticker, earn_date, text, filing_url, form_type, lm, prior_row
            )
            prior_row = row
            ticker_rows.append(row)
            total_scored += 1
            log.debug(
                "  %s %s: %s words, method=%s",
                ticker, ed_str, row.get("total_words"), row.get("nlp_scoring_method"),
            )

        n = _upsert(ticker_rows, dry_run)
        total_inserted += n
        log.info(
            "  %s: %d rows scored, %d upserted",
            ticker,
            sum(1 for r in ticker_rows if r.get("nlp_scoring_method") != "no_filing"),
            n,
        )

    print(f"\nDone.")
    print(f"  Tickers processed : {len(by_ticker)}")
    print(f"  Filings scored    : {total_scored}")
    print(f"  No filing / skip  : {total_no_filing}")
    print(f"  Rows {'would insert' if dry_run else 'inserted'}: {total_inserted}")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    ap = argparse.ArgumentParser(description="EDGAR 8-K NLP scorer")
    ap.add_argument("--tickers",      nargs="+",  default=None)
    ap.add_argument("--max-quarters", type=int,   default=None,
                    help="Limit to N most recent quarters per ticker")
    ap.add_argument("--lm-dict",      default=None,
                    help="Path to LM Master Dictionary CSV (default: data/lm_master_dict.csv)")
    ap.add_argument("--dry-run",      action="store_true")
    args = ap.parse_args()

    lm_path = Path(args.lm_dict) if args.lm_dict else None
    run(tickers=args.tickers, max_quarters=args.max_quarters,
        lm_dict_path=lm_path, dry_run=args.dry_run)
