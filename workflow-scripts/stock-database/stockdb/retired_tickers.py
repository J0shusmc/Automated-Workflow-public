"""Known inactive symbols kept for historical data continuity."""

from __future__ import annotations

RETIRED_TICKERS: dict[str, dict[str, str | None]] = {
    "ANSS": {"reason": "acquired by Synopsys; delisted July 2025", "replacement": "SNPS"},
    "BK": {"reason": "ticker changed to BNY effective May 2026", "replacement": "BNY"},
    "CMA": {"reason": "inactive after corporate action in January 2026", "replacement": None},
    "CTLT": {"reason": "acquired by Novo Holdings; delisted December 2024", "replacement": None},
    "DFS": {"reason": "acquired by Capital One; delisted May 2025", "replacement": "COF"},
    "DISH": {"reason": "merged into EchoStar; delisted December 2023; successor ticker changed to ECHO in June 2026", "replacement": "ECHO"},
    "EA": {"reason": "acquired by PIF, Silver Lake, and Affinity; delisted August 2026", "replacement": None},
    "FBHS": {"reason": "ticker changed to FBIN effective December 2022", "replacement": "FBIN"},
    "FLT": {"reason": "ticker changed to CPAY effective March 2024", "replacement": "CPAY"},
    "FRC": {"reason": "inactive after 2023 bank failure", "replacement": None},
    "HOLX": {"reason": "acquired by Blackstone and TPG; delisted April 2026", "replacement": None},
    "IPG": {"reason": "acquired by Omnicom; delisted November 2025", "replacement": "OMC"},
    "JNPR": {"reason": "acquired by HPE; delisted July 2025", "replacement": "HPE"},
    "K": {"reason": "acquired by Mars; delisted December 2025", "replacement": None},
    "MMC": {"reason": "ticker changed to MRSH effective January 2026", "replacement": "MRSH"},
    "MRO": {"reason": "acquired by ConocoPhillips; delisted November 2024", "replacement": "COP"},
    "PEAK": {"reason": "merged with Physicians Realty Trust; ticker changed to DOC", "replacement": "DOC"},
    "PKI": {"reason": "ticker changed to RVTY effective May 2023", "replacement": "RVTY"},
    "PXD": {"reason": "acquired by ExxonMobil; delisted May 2024", "replacement": "XOM"},
    "RE": {"reason": "ticker changed to EG effective July 2023", "replacement": "EG"},
    "SEE": {"reason": "acquired by CD&R; delisted April 2026", "replacement": None},
    "SIVB": {"reason": "inactive after 2023 bank failure", "replacement": None},
    "SATS": {"reason": "ticker changed to ECHO effective June 2026", "replacement": "ECHO"},
    "SPLK": {"reason": "acquired by Cisco; delisted March 2024", "replacement": "CSCO"},
    "WBA": {"reason": "acquired by Sycamore; delisted August 2025", "replacement": None},
    "WRK": {"reason": "combined with Smurfit Kappa; now Smurfit Westrock", "replacement": "SW"},
}


def is_retired(ticker: str) -> bool:
    return ticker.upper() in RETIRED_TICKERS


def retired_reason(ticker: str) -> str:
    info = RETIRED_TICKERS.get(ticker.upper())
    if not info:
        return ""
    return str(info["reason"])


def replacement_for(ticker: str) -> str | None:
    info = RETIRED_TICKERS.get(ticker.upper())
    if not info:
        return ticker.upper()
    replacement = info.get("replacement")
    return str(replacement) if replacement else None
