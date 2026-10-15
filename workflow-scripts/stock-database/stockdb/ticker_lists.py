"""Pre-built ticker collections."""

# S&P 500 — top holdings and most liquid names
# Full list is ~503 tickers; keeping the most commonly used here.
# Use ALL_SP500 for the complete list (fetched dynamically).
SP500_CORE = [
    "AAPL", "ABBV", "ABT", "ACN", "ADBE", "ADI", "ADP", "ADSK", "AEP", "AFL",
    "AIG", "ALGN", "ALL", "AMAT", "AMGN", "AMP", "AMZN", "ANET", "ANSS", "AON",
    "APD", "APH", "APTV", "ARE", "ATO", "AVGO", "AXP", "AZO", "BA", "BAC",
    "BBY", "BDX", "BEN", "BIIB", "BK", "BKNG", "BKR", "BLK", "BMY", "BR",
    "BRK.B", "BSX", "BWA", "BXP", "C", "CAG", "CAH", "CAT", "CB", "CBOE",
    "CBRE", "CCI", "CCL", "CDNS", "CDW", "CE", "CEG", "CF", "CHD", "CHRW",
    "CI", "CINF", "CL", "CLX", "CMA", "CMCSA", "CME", "CMG", "CMI", "CMS",
    "CNC", "CNP", "COF", "COP", "COST", "CPB", "CPRT", "CRL", "CRM", "CSCO",
    "CSGP", "CSX", "CTAS", "CTLT", "CTSH", "CTVA", "CVS", "CVX", "D", "DAL",
    "DD", "DE", "DFS", "DG", "DGX", "DHI", "DHR", "DIS", "DISH", "DLTR",
    "DOV", "DOW", "DPZ", "DRI", "DTE", "DUK", "DVA", "DVN", "DXC", "DXCM",
    "EA", "EBAY", "ECL", "ED", "EFX", "EIX", "EL", "EMN", "EMR", "ENPH",
    "EOG", "EPAM", "EQIX", "EQR", "EQT", "ES", "ESS", "ETN", "ETR", "ETSY",
    "EVRG", "EW", "EXC", "EXPD", "EXPE", "EXR", "F", "FANG", "FAST", "FBHS",
    "FCX", "FDS", "FDX", "FE", "FFIV", "FIS", "FISV", "FITB", "FLT", "FMC",
    "FOX", "FOXA", "FRC", "FRT", "FTNT", "FTV", "GD", "GE", "GILD", "GIS",
    "GL", "GLW", "GM", "GNRC", "GOOG", "GOOGL", "GPC", "GPN", "GRMN", "GS",
    "GWW", "HAL", "HAS", "HBAN", "HCA", "HD", "HOLX", "HON", "HPE", "HPQ",
    "HRL", "HSIC", "HST", "HSY", "HUM", "HWM", "IBM", "ICE", "IDXX", "IEX",
    "IFF", "ILMN", "INCY", "INTC", "INTU", "INVH", "IP", "IPG", "IQV", "IR",
    "IRM", "ISRG", "IT", "ITW", "IVZ", "J", "JBHT", "JCI", "JKHY", "JNJ",
    "JNPR", "JPM", "K", "KDP", "KEY", "KEYS", "KHC", "KIM", "KLAC", "KMB",
    "KMI", "KMX", "KO", "KR", "L", "LDOS", "LEN", "LH", "LHX", "LIN",
    "LKQ", "LLY", "LMT", "LNC", "LNT", "LOW", "LRCX", "LUMN", "LUV", "LVS",
    "LW", "LYB", "LYV", "MA", "MAA", "MAR", "MAS", "MCD", "MCHP", "MCK",
    "MCO", "MDLZ", "MDT", "MET", "META", "MGM", "MHK", "MKC", "MKTX", "MLM",
    "MMC", "MMM", "MNST", "MO", "MOH", "MOS", "MPC", "MPWR", "MRK", "MRNA",
    "MRO", "MS", "MSCI", "MSFT", "MSI", "MTB", "MTCH", "MTD", "MU", "NCLH",
    "NDAQ", "NDSN", "NEE", "NEM", "NFLX", "NI", "NKE", "NOC", "NOW", "NRG",
    "NSC", "NTAP", "NTRS", "NUE", "NVDA", "NVR", "NWL", "NWS", "NWSA", "NXPI",
    "O", "ODFL", "OGN", "OKE", "OMC", "ON", "ORCL", "ORLY", "OTIS", "OXY",
    "PARA", "PAYC", "PAYX", "PCAR", "PCG", "PEAK", "PEG", "PEP", "PFE", "PFG",
    "PG", "PGR", "PH", "PHM", "PKG", "PKI", "PLD", "PM", "PNC", "PNR",
    "PNW", "POOL", "PPG", "PPL", "PRU", "PSA", "PSX", "PTC", "PVH", "PWR",
    "PXD", "PYPL", "QCOM", "QRVO", "RCL", "RE", "REG", "REGN", "RF", "RHI",
    "RJF", "RL", "RMD", "ROK", "ROL", "ROP", "ROST", "RSG", "RTX", "SBAC",
    "SBNY", "SBUX", "SCHW", "SEE", "SHW", "SIVB", "SJM", "SLB", "SNA", "SNPS",
    "SO", "SPG", "SPGI", "SRE", "STE", "STT", "STX", "STZ", "SWK", "SWKS",
    "SYF", "SYK", "SYY", "T", "TAP", "TDG", "TDY", "TECH", "TEL", "TER",
    "TFC", "TFX", "TGT", "TJX", "TMO", "TMUS", "TPR", "TRGP", "TRMB", "TROW",
    "TRV", "TSCO", "TSLA", "TSN", "TT", "TTWO", "TXN", "TXT", "TYL", "UAL",
    "UDR", "UHS", "ULTA", "UNH", "UNP", "UPS", "URI", "USB", "V", "VFC",
    "VICI", "VLO", "VMC", "VNO", "VRSK", "VRSN", "VRTX", "VTR", "VTRS", "VZ",
    "WAB", "WAT", "WBA", "WBD", "WDC", "WEC", "WELL", "WFC", "WHR", "WM",
    "WMB", "WMT", "WRB", "WRK", "WST", "WTW", "WY", "WYNN", "XEL", "XOM",
    "XRAY", "XYL", "YUM", "ZBH", "ZBRA", "ZION", "ZTS",
]

NASDAQ100 = [
    "AAPL", "ABNB", "ADBE", "ADI", "ADP", "ADSK", "AEP", "ALGN", "AMAT",
    "AMD", "AMGN", "AMZN", "ANSS", "ARM", "ASML", "AVGO", "AZN", "BIIB",
    "BKNG", "BKR", "CCEP", "CDNS", "CDW", "CEG", "CHTR", "CMCSA", "COST",
    "CPRT", "CRWD", "CSCO", "CSGP", "CTAS", "CTSH", "DASH", "DDOG", "DLTR",
    "DXCM", "EA", "EXC", "FANG", "FAST", "FTNT", "GEHC", "GFS", "GILD",
    "GOOG", "GOOGL", "HON", "IDXX", "ILMN", "INTC", "INTU", "ISRG", "KDP",
    "KHC", "KLAC", "LRCX", "LULU", "MAR", "MCHP", "MDB", "MDLZ", "MELI",
    "META", "MNST", "MRNA", "MRVL", "MSFT", "MU", "NFLX", "NVDA", "NXPI",
    "ODFL", "ON", "ORLY", "PANW", "PAYX", "PCAR", "PDD", "PEP", "PYPL",
    "QCOM", "REGN", "RIVN", "ROST", "SBUX", "SMCI", "SNPS", "SPLK", "TEAM",
    "TMUS", "TSLA", "TTD", "TTWO", "TXN", "VRSK", "VRTX", "WBD", "WDAY",
    "XEL", "ZS",
]

MAJOR_ETFS = [
    # Broad market
    "SPY", "QQQ", "IWM", "DIA", "VOO", "VTI", "IVV", "RSP",
    # Sector
    "XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE", "XLC",
    # Fixed income
    "TLT", "IEF", "SHY", "BND", "AGG", "HYG", "LQD", "JNK", "TIP",
    # Commodities
    "GLD", "SLV", "USO", "GDX", "GDXJ",
    # International
    "EFA", "EEM", "VWO", "FXI", "EWJ", "EWZ",
    # Volatility
    "VIXY", "SVXY",
    # Leveraged (popular)
    "TQQQ", "SQQQ", "SPXL", "SPXS", "UPRO", "SDS",
]

# Tickers used by the GP trading system
GP_TICKERS = [
    "SPY", "QQQ", "IWM", "DIA",   # equity indices
    "TLT", "HYG",                   # bonds
    "GLD",                           # commodities
]

# SPDR Select Sector ETFs (S&P 500 sectors)
SECTOR_ETFS = [
    "XLK",   # Technology
    "XLF",   # Financials
    "XLE",   # Energy
    "XLV",   # Health Care
    "XLI",   # Industrials
    "XLY",   # Consumer Discretionary
    "XLP",   # Consumer Staples
    "XLU",   # Utilities
    "XLB",   # Materials
    "XLRE",  # Real Estate
    "XLC",   # Communication Services
]

# Software sector ETFs
SOFTWARE = [
    "IGV",   # iShares Expanded Tech-Software ETF
    "WCLD",  # WisdomTree Cloud Computing
    "BUG",   # Global X Cybersecurity ETF
    "HACK",  # ETFMG Prime Cyber Security ETF
    "SKYY",  # First Trust Cloud Computing ETF
    "CLOU",  # Global X Cloud Computing ETF
]

# All pre-built lists accessible by name
TICKER_LISTS = {
    "sp500": SP500_CORE,
    "nasdaq100": NASDAQ100,
    "etfs": MAJOR_ETFS,
    "gp": GP_TICKERS,
    "sectors": SECTOR_ETFS,
    "software": SOFTWARE,
}


def get_list(name: str) -> list[str]:
    """Get a ticker list by name (case-insensitive)."""
    name = name.lower()
    if name not in TICKER_LISTS:
        raise ValueError(
            f"Unknown list {name!r}. Available: {list(TICKER_LISTS.keys())}"
        )
    return TICKER_LISTS[name]


def all_list_names() -> list[str]:
    """Return all available list names."""
    return list(TICKER_LISTS.keys())
