import os
import streamlit as st
import pandas as pd
import numpy as np

import sqlite3
import re
import datetime
import calendar
from io import BytesIO, StringIO
import plotly.graph_objects as go

# ══════════════════════════════════════════════════════════════════════════════
#  ANGALA BOUTIQUE HOTEL BRAND THEME
#  Taken from angala.co.za: Angala gold (#BAAA70) and its darker bronze (#7E6F3A),
#  deep ink-teal text (#0C1C1F), slate grey (#838E91), warm cream (#FDF8F6),
#  and Playfair Display headings.
#  (BRAND keys are shared with the other Providence dashboards so the CSS and
#  chart code stays identical; only the values change.)
# ══════════════════════════════════════════════════════════════════════════════
BRAND = {
    "deep":       "#0C1C1F",   # ink-teal (headers, sidebar)
    "brown":      "#22363A",   # deep teal-slate
    "sage":       "#4F676C",   # slate teal (charts: current)
    "sage_soft":  "#A9B4B6",   # soft slate (charts: baseline)
    "clay":       "#7E6F3A",   # dark Angala gold (accent text, highlights)
    "clay_soft":  "#D6CA9E",   # pale gold
    "gold":       "#BAAA70",   # Angala gold
    "rust":       "#9C4A32",   # deep rust (variance / drops)
    "linen":      "#F6F3EC",   # page background (warm cream-stone)
    "ivory":      "#FDF8F6",   # card background (site cream)
    "text":       "#0C1C1F",   # body text
    "muted":      "#5F6A6D",   # muted text (darker than the site's #838E91 for contrast)
    "line":       "#E6E1D6",   # hairlines / borders
    "good_bg":    "rgba(79, 120, 92, 0.30)",    # positive cell tint
    "bad_bg":     "rgba(156, 74, 50, 0.30)",    # negative cell tint (rust)
    "good_txt":   "#244A30",                     # positive text
    "bad_txt":    "#73291A",                     # negative text
}

# Plotly defaults for a consistent branded look
PLOTLY_FONT = dict(family="Montserrat, 'Segoe UI', sans-serif", size=13, color=BRAND["text"])
HEAT_COLORSCALE = [
    [0.00, "#F3F0E8"],
    [0.35, "#E4DAB4"],
    [0.60, "#BAAA70"],
    [0.80, "#4F676C"],
    [1.00, "#0C1C1F"],
]


def fmt_currency(x) -> str:
    """Format numbers as ZAR currency in SA style: R1 234 567,89 (space thousands, comma decimals)."""
    if x is None:
        return ""
    try:
        if isinstance(x, str):
            s = x.strip().replace("R", "").replace(" ", "").replace(",", "")
            if s == "":
                return ""
            x = float(s)
        else:
            x = float(x)
    except Exception:
        return ""
    if pd.isna(x):
        return ""
    return "R" + f"{x:,.2f}".replace(",", " ").replace(".", ",")


def fmt_rand0(x) -> str:
    """R1 234 567 (no decimals), SA style."""
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return ""
    try:
        return "R" + f"{float(x):,.0f}".replace(",", " ")
    except Exception:
        return ""


def fmt_int(x) -> str:
    """Integer with space thousands separator, SA style."""
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return ""
    try:
        return f"{int(x):,}".replace(",", " ")
    except Exception:
        return ""

# ══════════════════════════════════════════════════════════════════════════════
#  RESREQUEST "B201 Booking Console — Providence Revenue Report" (CSV)
#
#  Layout: line 1 is the report title, line 2 the preset/filter description
#  ("Present from 1 Nov 2026 00:00 until before 31 May 2027 00:00 / ..."),
#  line 3 the column headers, then one row PER UNIT PER BOOKING, then a footer
#  line "Main Department,9 Nov 2026 12:20 (<user>),46 / 46".
#
#  Rules applied:
#   * Cancelled / No Show / Faulty rows are dropped (the preset includes them).
#   * Room nights  = "Room Nights" (already trimmed to the report range).
#   * Revenue      = "Accommodation in Range Exclusive" (ex VAT, in range).
#   * Revenue is spread evenly over the in-range nights of each unit-booking.
#   * Provisional bookings ARE counted on the books, but tracked separately so
#     the dashboard can show how much of each month is still provisional.
#   * Zero-rate stays (complimentary / house use) occupy inventory but add no
#     revenue; they are counted and reported back after upload.
#   * Report timestamp is taken from the footer line, so the snapshot is dated
#     when ResRequest produced it, not when it was uploaded.
# ══════════════════════════════════════════════════════════════════════════════
ANGALA_ROOMS = int(os.environ.get("ANGALA_ROOMS", "11"))   # sellable units (all unit types)
DROP_STATUSES = {"cancelled", "canceled", "no show", "noshow", "faulty"}
PROVISIONAL_STATUSES = {"provisional"}

# Breakfast is not separated out (ResRequest reports accommodation as sold).
# If the rate includes breakfast and you want it removed, set the per-adult,
# per-night amount EX VAT here.
BREAKFAST_NET_PER_ADULT = 0.00

_MON3 = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}


def _rr_date(s):
    """'1 Nov 2026 00:00' / '9 Nov 2026 12:20' -> datetime"""
    m = re.search(r"(\d{1,2})\s+([A-Za-z]{3})[A-Za-z]*\s+(\d{4})(?:\s+(\d{1,2}):(\d{2}))?", str(s))
    if not m or m.group(2).lower() not in _MON3:
        return None
    return datetime.datetime(int(m.group(3)), _MON3[m.group(2).lower()], int(m.group(1)),
                             int(m.group(4) or 0), int(m.group(5) or 0))


def parse_resrequest_revenue_csv(file_bytes: bytes, filename: str, hotel: str,
                                 total_rooms: int = None, as_of=None):
    """
    Parse the ResRequest revenue report CSV into (month_df, day_df, meta).

    as_of (optional date/datetime): rebuild the on-the-books position as it stood at
    that moment, using Creation Date and Cancellation Date — bookings created after
    it are ignored and bookings cancelled after it are put back. This is what a
    future same-time-last-year (STLY) section needs: run the same report for last
    year's stay dates WITH cancellations included, and call this with as_of = the
    current snapshot date minus one year.
    """
    total_rooms = int(total_rooms or ANGALA_ROOMS)
    text = file_bytes.decode("utf-8-sig", errors="replace")
    lines = text.splitlines()
    if len(lines) < 4:
        raise ValueError("The file is too short to be a ResRequest revenue report.")

    # ---- header row: first line that carries the key column names ----
    header_idx = None
    for i, ln in enumerate(lines[:15]):
        low = ln.lower()
        if "arrival date" in low and "departure date" in low and "room nights" in low:
            header_idx = i
            break
    if header_idx is None:
        raise ValueError("Couldn't find the column header row (Arrival Date / Departure Date / "
                         "Room Nights). Is this the Providence Revenue Report from ResRequest?")

    # ---- report window from the preset line ----
    preamble = " ".join(lines[:header_idx])
    win_start = win_end_excl = None
    m = re.search(r"from\s+(\d{1,2}\s+\w+\s+\d{4}(?:\s+\d{1,2}:\d{2})?)\s+until\s+(?:before\s+)?"
                  r"(\d{1,2}\s+\w+\s+\d{4}(?:\s+\d{1,2}:\d{2})?)", preamble, re.I)
    if m:
        ws, we = _rr_date(m.group(1)), _rr_date(m.group(2))
        win_start = ws.date() if ws else None
        win_end_excl = we.date() if we else None   # "until before" = exclusive

    df = pd.read_csv(StringIO("\n".join(lines[header_idx:])), dtype=str, keep_default_na=False)
    df.columns = [c.strip() for c in df.columns]

    # ---- footer: "Main Department,9 Nov 2026 12:20 (user),46 / 46" ----
    report_dt = None
    for ln in reversed(lines[-5:]):
        parts = ln.split(",")
        if len(parts) >= 2 and "(" in parts[1]:
            report_dt = _rr_date(parts[1])
            if report_dt is not None:
                break

    need = ["Arrival Date", "Departure Date", "Status", "Room Nights", "Accommodation in Range Exclusive"]
    missing = [c for c in need if c not in df.columns]
    if missing:
        raise ValueError("The report is missing these columns: " + ", ".join(missing))

    df["_arr"] = pd.to_datetime(df["Arrival Date"], format="%Y/%m/%d", errors="coerce")
    df["_dep"] = pd.to_datetime(df["Departure Date"], format="%Y/%m/%d", errors="coerce")
    df = df.dropna(subset=["_arr", "_dep"]).copy()          # drops the footer line
    df["_status"] = df["Status"].astype(str).str.strip().str.lower()
    df["_rn"] = pd.to_numeric(df["Room Nights"], errors="coerce").fillna(0).astype(int)
    df["_rev"] = pd.to_numeric(df["Accommodation in Range Exclusive"], errors="coerce").fillna(0.0)
    df["_adults"] = pd.to_numeric(df.get("Adults", 0), errors="coerce").fillna(0)
    df["_created"] = pd.to_datetime(df.get("Creation Date", ""), format="%Y/%m/%d", errors="coerce")
    df["_cancelled"] = pd.to_datetime(df.get("Cancellation Date", ""), format="%Y/%m/%d", errors="coerce")

    rows_in = len(df)
    if as_of is None:
        live = df[~df["_status"].isin(DROP_STATUSES)].copy()
    else:
        as_of = pd.Timestamp(as_of).normalize()
        created_ok = df["_created"].isna() | (df["_created"] <= as_of)
        not_yet_cancelled = df["_cancelled"].isna() | (df["_cancelled"] > as_of)
        is_noshow = df["_status"].isin({"no show", "noshow", "faulty"})
        live = df[created_ok & not_yet_cancelled & ~is_noshow].copy()
    dropped = rows_in - len(live)

    # Unique unit-booking key ("Description" is e.g. 2321.1 / CB22883.CB1)
    key_col = "Description" if "Description" in live.columns else None
    if key_col:
        live = live.drop_duplicates(subset=[key_col]).copy()
    live = live[live["_rn"] > 0].copy()

    live["_rev"] = live["_rev"] - BREAKFAST_NET_PER_ADULT * live["_adults"] * live["_rn"]

    # ---- expand to nights inside the window ----
    if win_start is None:
        win_start = live["_arr"].min().date()
    if win_end_excl is None:
        win_end_excl = live["_dep"].max().date()

    rows = []
    for i, r in live.iterrows():
        a = max(r["_arr"].date(), win_start)
        d = min(r["_dep"].date(), win_end_excl)
        span = (d - a).days
        n = int(r["_rn"])
        if span <= 0:
            continue
        if span != n:
            # Room Nights is ResRequest's own in-range count; trust it and anchor to
            # whichever end of the stay lies inside the window.
            n = min(n, span)
            if r["_arr"].date() < win_start:
                a = d - datetime.timedelta(days=n)
        per_night = float(r["_rev"]) / n
        prov = r["_status"] in PROVISIONAL_STATUSES
        occ_key = str(r[key_col]) if key_col else f"row{i}"
        for k in range(n):
            rows.append((a + datetime.timedelta(days=k), per_night, occ_key, prov))

    if not rows:
        raise ValueError("No live bookings found in the report window.")

    nights = pd.DataFrame(rows, columns=["stay_date", "rev", "occ_key", "prov"])
    nights["prov_rev"] = np.where(nights["prov"], nights["rev"], 0.0)
    day = nights.groupby("stay_date").agg(
        occupied_rooms=("occ_key", "nunique"),
        accom_rev=("rev", "sum"),
        prov_rooms=("prov", "sum"),
        prov_rev=("prov_rev", "sum"),
    ).reset_index()

    last_night = win_end_excl - datetime.timedelta(days=1)
    all_dates = pd.date_range(win_start, last_night, freq="D").date
    day = pd.DataFrame({"stay_date": all_dates}).merge(day, on="stay_date", how="left")
    for c in ["occupied_rooms", "prov_rooms"]:
        day[c] = day[c].fillna(0).astype(int)
    for c in ["accom_rev", "prov_rev"]:
        day[c] = day[c].fillna(0.0)

    overbooked = day[day["occupied_rooms"] > total_rooms]
    day["total_rooms"] = total_rooms
    day["free_rooms"] = day["total_rooms"] - day["occupied_rooms"]
    day["occupancy_pct"] = np.where(day["total_rooms"] > 0,
                                    day["occupied_rooms"] / day["total_rooms"] * 100.0, 0.0)

    day["_month"] = pd.to_datetime(day["stay_date"]).dt.to_period("M").dt.to_timestamp()
    month = day.groupby("_month").agg(
        free_rooms=("free_rooms", "sum"),
        occupied_rooms=("occupied_rooms", "sum"),
        total_rooms=("total_rooms", "sum"),
        accom_rev=("accom_rev", "sum"),
        prov_rooms=("prov_rooms", "sum"),
        prov_rev=("prov_rev", "sum"),
    ).reset_index()
    month["stay_month"] = month["_month"].dt.strftime("%Y-%m-%d")
    month["occupancy_pct"] = np.where(month["total_rooms"] > 0,
                                      month["occupied_rooms"] / month["total_rooms"] * 100.0, 0.0)
    month["fnb_rev"] = 0.0
    month["extras_rev"] = 0.0
    month["total_rev"] = month["accom_rev"]

    day["stay_date"] = pd.to_datetime(day["stay_date"]).dt.strftime("%Y-%m-%d")
    day["fnb_rev"] = 0.0
    day["extras_rev"] = 0.0
    day["total_rev"] = day["accom_rev"]

    MONTH_COLS = ["stay_month", "free_rooms", "occupied_rooms", "total_rooms", "occupancy_pct",
                  "accom_rev", "fnb_rev", "extras_rev", "total_rev", "prov_rooms", "prov_rev"]
    DAY_COLS = ["stay_date", "free_rooms", "occupied_rooms", "total_rooms", "occupancy_pct",
                "accom_rev", "fnb_rev", "extras_rev", "total_rev"]

    zero = live[(live["_rev"] <= 0)]
    meta = {
        "hotel": hotel,
        "report_datetime": (report_dt or datetime.datetime.now()).strftime("%Y-%m-%d %H:%M:%S"),
        "report_dt_from_file": report_dt is not None,
        "source_filename": filename,
        "source": "resrequest_revenue_report_csv",
        "period_start": str(win_start),
        "period_end": str(last_night),
        "inventory_rooms": total_rooms,
        "rows_read": rows_in,
        "rows_dropped": dropped,
        "zero_rate_bookings": int(len(zero)),
        "zero_rate_nights": int(zero["_rn"].sum()),
        "prov_nights": int(day["prov_rooms"].sum()),
        "prov_rev": float(day["prov_rev"].sum()),
        "overbooked_days": [str(x) for x in overbooked["stay_date"].tolist()],
    }
    return month[MONTH_COLS].copy(), day[DAY_COLS].copy(), meta

st.set_page_config(
    page_title="Angala Boutique Hotel — Production Dashboard",
    page_icon="🌿",
    layout="wide",
)

try:
    st.logo("PH.png")
except Exception:
    pass

st.markdown(
    f"""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Playfair+Display:ital,wght@0,500;0,600;0,700;1,500&family=Montserrat:wght@300;400;500;600;700&display=swap');

    /* ---------- Page canvas ---------- */
    .stApp, [data-testid="stAppViewContainer"] {{
        background-color: {BRAND["linen"]};
        color: {BRAND["text"]};
        font-family: 'Montserrat', 'Segoe UI', sans-serif;
    }}
    .block-container {{ padding-top: 1.2rem; max-width: 1400px; }}

    /* ---------- Headings — elegant serif with clay keyline ---------- */
    h1, h2, h3, [data-testid="stMarkdownContainer"] h1,
    [data-testid="stMarkdownContainer"] h2,
    [data-testid="stMarkdownContainer"] h3 {{
        font-family: 'Playfair Display', Georgia, serif !important;
        color: {BRAND["deep"]} !important;
        letter-spacing: 0.02em;
    }}
    h2, [data-testid="stMarkdownContainer"] h2 {{
        font-size: 2rem !important;
        border-bottom: 1px solid {BRAND["line"]};
        padding-bottom: .35rem;
    }}
    h3, [data-testid="stMarkdownContainer"] h3 {{ font-size: 1.5rem !important; }}

    .sl-section {{
        display:flex; align-items:center; gap:.75rem;
        margin: 2.2rem 0 0.8rem 0;
    }}
    .sl-section .rule {{ flex:1; height:1px; background:{BRAND["line"]}; }}
    .sl-section .kicker {{
        font-family:'Montserrat',sans-serif; font-size:.72rem; font-weight:600;
        letter-spacing:.22em; text-transform:uppercase; color:{BRAND["clay"]};
        white-space:nowrap;
    }}

    /* ---------- Hero header ---------- */
    .sl-hero {{
        background: linear-gradient(135deg, {BRAND["deep"]} 0%, {BRAND["brown"]} 60%, #33494E 100%);
        border-radius: 18px;
        padding: 2.1rem 2.4rem 1.9rem 2.4rem;
        margin-bottom: 1.4rem;
        box-shadow: 0 10px 30px rgba(46, 42, 38, 0.28);
        position: relative;
        overflow: hidden;
    }}
    .sl-hero:before {{
        content:""; position:absolute; inset:0;
        background: radial-gradient(1200px 300px at 90% -50%, rgba(186,170,112,.30), transparent 60%);
    }}
    .sl-hero .eyebrow {{
        font-family:'Montserrat',sans-serif; font-size:.72rem; font-weight:600;
        letter-spacing:.28em; text-transform:uppercase; color:{BRAND["clay_soft"]};
        margin-bottom:.4rem; position:relative;
    }}
    .sl-hero h1 {{
        font-family:'Playfair Display', Georgia, serif !important;
        color:#FCF8F0 !important; font-size:2.6rem !important; font-weight:600;
        margin:0 0 .3rem 0 !important; line-height:1.1; position:relative;
    }}
    .sl-hero .sub {{
        font-family:'Montserrat',sans-serif; color:#E0D8CA; font-size:.95rem;
        font-weight:300; position:relative;
    }}
    .sl-hero .gold-rule {{
        width:72px; height:3px; background:{BRAND["gold"]};
        border-radius:2px; margin:.9rem 0 0 0; position:relative;
    }}

    /* ---------- Sidebar — deep oak ---------- */
    [data-testid="stSidebar"] {{
        background: linear-gradient(180deg, {BRAND["deep"]} 0%, {BRAND["brown"]} 100%);
        border-right: 1px solid {BRAND["gold"]}33;
    }}
    [data-testid="stSidebar"] * {{ color: #EDE7DB !important; }}
    [data-testid="stSidebar"] h1, [data-testid="stSidebar"] h2, [data-testid="stSidebar"] h3 {{
        color: {BRAND["clay_soft"]} !important;
        font-family:'Playfair Display', Georgia, serif !important;
    }}
    /* Sidebar inputs — solid linen field with dark text so entries are readable */
    [data-testid="stSidebar"] input,
    [data-testid="stSidebar"] textarea,
    [data-testid="stSidebar"] [data-baseweb="input"] input,
    [data-testid="stSidebar"] [data-baseweb="select"] > div {{
        background-color: #FBF7EE !important;
        color: {BRAND["deep"]} !important;
        -webkit-text-fill-color: {BRAND["deep"]} !important;
        border: 1px solid rgba(186,170,112,.55) !important;
        font-weight: 500 !important;
    }}
    [data-testid="stSidebar"] input::placeholder,
    [data-testid="stSidebar"] textarea::placeholder {{
        color: {BRAND["muted"]} !important;
        -webkit-text-fill-color: {BRAND["muted"]} !important;
        opacity: 1 !important;
    }}
    [data-testid="stSidebar"] [data-baseweb="select"] div[value],
    [data-testid="stSidebar"] [data-baseweb="select"] span {{
        color: {BRAND["deep"]} !important;
        -webkit-text-fill-color: {BRAND["deep"]} !important;
    }}
    [data-testid="stSidebar"] [data-baseweb="popover"] * {{ color: {BRAND["text"]} !important; }}

    /* ---------- Metric cards ---------- */
    [data-testid="stMetric"] {{
        background: {BRAND["ivory"]};
        border: 1px solid {BRAND["line"]};
        border-top: 3px solid {BRAND["clay"]};
        border-radius: 14px;
        padding: 1rem 1.1rem .9rem 1.1rem;
        box-shadow: 0 4px 14px rgba(12,28,31,.07);
    }}
    [data-testid="stMetricLabel"] p {{
        font-size: .78rem !important; font-weight: 600;
        letter-spacing: .06em; text-transform: uppercase;
        color: {BRAND["muted"]} !important;
    }}
    [data-testid="stMetricValue"] {{
        font-family:'Playfair Display', Georgia, serif;
        font-size: 1.75rem !important;
        color: {BRAND["deep"]} !important;
        font-weight: 700;
    }}

    /* ---------- Buttons ---------- */
    .stButton > button, .stDownloadButton > button {{
        background: {BRAND["brown"]};
        color: #FCF8F0;
        border: 1px solid {BRAND["brown"]};
        border-radius: 999px;
        padding: .45rem 1.3rem;
        font-family:'Montserrat',sans-serif;
        font-weight: 600; font-size:.85rem; letter-spacing:.04em;
        transition: all .18s ease;
    }}
    .stButton > button:hover, .stDownloadButton > button:hover {{
        background: {BRAND["clay"]};
        border-color: {BRAND["clay"]};
        color: #FCF8F0;
    }}

    /* ---------- Expanders ---------- */
    [data-testid="stExpander"] {{
        background: {BRAND["ivory"]};
        border: 1px solid {BRAND["line"]} !important;
        border-radius: 12px !important;
        box-shadow: 0 3px 10px rgba(12,28,31,.05);
    }}
    [data-testid="stExpander"] summary {{
        font-family:'Montserrat',sans-serif; font-weight:600; color:{BRAND["brown"]};
    }}

    /* ---------- Tables ---------- */
    div[data-testid="stDataFrame"] .ag-root-wrapper,
    div[data-testid="stDataFrame"] .ag-cell,
    div[data-testid="stDataFrame"] .ag-header-cell,
    div[data-testid="stDataFrame"] .ag-header-cell-text,
    div[data-testid="stDataFrame"] .ag-cell-value {{
        font-size: 15px !important;
        font-family:'Montserrat',sans-serif !important;
    }}
    div[data-testid="stDataFrame"] .ag-cell,
    div[data-testid="stDataFrame"] .ag-cell-value {{
        text-align: center !important;
        justify-content: center !important;
    }}
    div[data-testid="stDataFrame"] .ag-header-cell-label {{ justify-content: center !important; }}
    div[data-testid="stDataFrame"] .ag-header-cell-text {{ width: 100% !important; text-align: center !important; }}

    div[data-testid="stTable"] table {{
        font-size: 14.5px !important;
        font-family:'Montserrat',sans-serif !important;
        background: {BRAND["ivory"]};
        border-radius: 10px; overflow: hidden;
    }}
    div[data-testid="stTable"] thead th {{
        background: {BRAND["deep"]} !important;
        color: {BRAND["clay_soft"]} !important;
        text-transform: uppercase; font-size:.72rem !important; letter-spacing:.08em;
    }}
    div[data-testid="stTable"] th, div[data-testid="stTable"] td {{ text-align: center !important; }}
    div[data-testid="stTable"] tbody tr:nth-child(even) {{ background: {BRAND["linen"]}; }}

    div[role="radiogroup"] label {{ font-family:'Montserrat',sans-serif; }}

    [data-baseweb="tag"] {{
        background-color: {BRAND["brown"]} !important;
        color: #fff !important;
    }}
    </style>
    """,
    unsafe_allow_html=True
)


def sl_section(kicker: str, title: str):
    """Branded section header: clay kicker + serif title with hairlines."""
    st.markdown(
        f"""
        <div class="sl-section">
            <span class="kicker">{kicker}</span>
            <span class="rule"></span>
        </div>
        <h2 style="border-bottom:none; margin-top:0; padding-bottom:0;">{title}</h2>
        """,
        unsafe_allow_html=True,
    )


def brand_plotly(fig: go.Figure, height: int | None = None) -> go.Figure:
    """Apply Angala chart styling to any plotly figure."""
    fig.update_layout(
        font=PLOTLY_FONT,
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        hoverlabel=dict(
            bgcolor=BRAND["deep"], font_color="#FCF8F0",
            font_family="Montserrat, sans-serif", bordercolor=BRAND["gold"],
        ),
        legend=dict(font=dict(size=12)),
    )
    if height:
        fig.update_layout(height=height)
    return fig


def _render_footer():
    st.markdown(
        f"""
        <div style="text-align:center; margin-top:3rem; padding:1.4rem 0 0.5rem 0;
                    border-top:1px solid {BRAND['line']};
                    font-family:'Playfair Display', Georgia, serif;
                    color:{BRAND['muted']}; font-size:1.05rem;">
            Angala Boutique Hotel · A barefoot 5-star sanctuary in the Cape Winelands
            <div style="width:48px; height:2px; background:{BRAND['clay']}; margin:.6rem auto 0 auto;"></div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def db_init(conn: sqlite3.Connection):
    c = conn.cursor()

    # ... existing tables ...

    c.execute("""
    CREATE TABLE IF NOT EXISTS budget_baseline (
        hotel TEXT NOT NULL,
        stay_month TEXT NOT NULL,
        accom_rev REAL NOT NULL,
        occupied_rooms INTEGER NOT NULL,
        total_rooms INTEGER NOT NULL,
        PRIMARY KEY (hotel, stay_month)
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS ly_weekly_pace (
        hotel TEXT NOT NULL,
        report_date TEXT NOT NULL,    -- ISO date of the weekly report
        stay_month TEXT NOT NULL,     -- ISO date "YYYY-MM-01"
        segment TEXT NOT NULL,
        occ_pct REAL,
        adr REAL,
        revenue REAL,
        source_filename TEXT,
        PRIMARY KEY (hotel, report_date, stay_month, segment)
    )
    """)

    conn.commit()
    
    conn.execute("""
    CREATE TABLE IF NOT EXISTS snapshots (
        snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
        hotel TEXT,
        report_datetime TEXT,
        period_start TEXT,
        period_end TEXT,
        source_filename TEXT
    );
    """)

    conn.execute("""
    CREATE TABLE IF NOT EXISTS month_metrics (
        snapshot_id INTEGER,
        stay_month TEXT,
        free_rooms INTEGER,
        occupied_rooms INTEGER,
        total_rooms INTEGER,
        occupancy_pct REAL,
        accom_rev REAL,
        fnb_rev REAL,
        extras_rev REAL,
        total_rev REAL,
        prov_rooms INTEGER DEFAULT 0,   -- of which provisional (ResRequest status)
        prov_rev REAL DEFAULT 0,
        PRIMARY KEY (snapshot_id, stay_month),
        FOREIGN KEY(snapshot_id) REFERENCES snapshots(snapshot_id)
    );
    """)
    
    conn.execute("""
    CREATE TABLE IF NOT EXISTS day_metrics (
    snapshot_id INTEGER,
    stay_date TEXT,
    free_rooms INTEGER,
    occupied_rooms INTEGER,
    total_rooms INTEGER,
    occupancy_pct REAL,
    accom_rev REAL,
    fnb_rev REAL,
    extras_rev REAL,
    total_rev REAL,
    PRIMARY KEY (snapshot_id, stay_date),
    FOREIGN KEY(snapshot_id) REFERENCES snapshots(snapshot_id)
    );
    """)

    

    conn.execute("""
    CREATE TABLE IF NOT EXISTS budgets (
        hotel TEXT NOT NULL,
        month TEXT NOT NULL,      -- e.g. "Jan 2026"
        budget REAL NOT NULL,
        PRIMARY KEY (hotel, month)
    );
    """)

    conn.execute("""
    CREATE TABLE IF NOT EXISTS yoy_baseline (
        hotel TEXT NOT NULL,
        stay_month TEXT NOT NULL,     -- ISO date "YYYY-MM-01"
        accom_rev REAL,
        occupied_rooms INTEGER,
        total_rooms INTEGER,
        PRIMARY KEY (hotel, stay_month)
    );
    """)

    conn.commit()



def db_insert_snapshot(conn: sqlite3.Connection, meta: dict, month_df: pd.DataFrame, day_df: pd.DataFrame) -> int:
    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO snapshots(hotel, report_datetime, period_start, period_end, source_filename)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            meta.get("hotel"),
            meta.get("report_datetime"),
            meta.get("period_start"),
            meta.get("period_end"),
            meta.get("source_filename"),
        ),
    )

    snapshot_id = int(cur.lastrowid)

    # months
    month_rows = month_df.copy()
    month_rows["snapshot_id"] = snapshot_id
    month_rows.to_sql("month_metrics", conn, if_exists="append", index=False)

    # days (optional)
    if day_df is not None and not day_df.empty:
        day_rows = day_df.copy()
        day_rows["snapshot_id"] = snapshot_id
        day_rows.to_sql("day_metrics", conn, if_exists="append", index=False)

        cur.execute("""
    CREATE TABLE IF NOT EXISTS budget_baseline (
    hotel TEXT NOT NULL,
    stay_month TEXT NOT NULL,
    accom_rev REAL NOT NULL,
    occupied_rooms INTEGER NOT NULL,
    total_rooms INTEGER NOT NULL,
    PRIMARY KEY (hotel, stay_month)
    )
    """)

    conn.commit()
    st.cache_data.clear()
    return snapshot_id



@st.cache_data(show_spinner=False)
def _cached_load_days(db_path: str, snapshot_id: int) -> pd.DataFrame:
    c = sqlite3.connect(db_path, check_same_thread=False)
    try:
        return pd.read_sql_query("""
            SELECT stay_date, occupied_rooms, total_rooms, occupancy_pct, accom_rev
            FROM day_metrics
            WHERE snapshot_id = ?
            ORDER BY stay_date
        """, c, params=(snapshot_id,))
    finally:
        c.close()

def db_load_days(conn: sqlite3.Connection, snapshot_id: int) -> pd.DataFrame:
    return _cached_load_days(DB_PATH, snapshot_id).copy()

def db_delete_snapshot(conn: sqlite3.Connection, snapshot_id: int):
    conn.execute("DELETE FROM month_metrics WHERE snapshot_id = ?", (snapshot_id,))
    conn.execute("DELETE FROM snapshots WHERE snapshot_id = ?", (snapshot_id,))
    conn.execute("DELETE FROM day_metrics WHERE snapshot_id = ?", (snapshot_id,))
    conn.commit()
    st.cache_data.clear()

@st.cache_data(show_spinner=False)
def _cached_list_snapshots(db_path: str) -> pd.DataFrame:
    c = sqlite3.connect(db_path, check_same_thread=False)
    try:
        return pd.read_sql_query("""
            SELECT snapshot_id, hotel, report_datetime, period_start, period_end, source_filename
            FROM snapshots
            ORDER BY datetime(report_datetime) DESC, snapshot_id DESC
        """, c)
    finally:
        c.close()

def db_list_snapshots(conn: sqlite3.Connection) -> pd.DataFrame:
    return _cached_list_snapshots(DB_PATH).copy()

def db_list_hotels(conn) -> list[str]:
    try:
        rows = conn.execute(
            "SELECT DISTINCT hotel FROM snapshots WHERE hotel IS NOT NULL AND TRIM(hotel) <> '' ORDER BY hotel"
        ).fetchall()
        return [r[0] for r in rows]
    except Exception:
        return []


@st.cache_data(show_spinner=False)
def _cached_load_months(db_path: str, snapshot_id: int) -> pd.DataFrame:
    c = sqlite3.connect(db_path, check_same_thread=False)
    try:
        return pd.read_sql_query("""
            SELECT stay_month, free_rooms, occupied_rooms, total_rooms, occupancy_pct, accom_rev, fnb_rev, extras_rev, total_rev,
                   COALESCE(prov_rooms, 0) AS prov_rooms, COALESCE(prov_rev, 0) AS prov_rev
            FROM month_metrics
            WHERE snapshot_id = ?
            ORDER BY stay_month
        """, c, params=(snapshot_id,))
    finally:
        c.close()

def db_load_months(conn: sqlite3.Connection, snapshot_id: int) -> pd.DataFrame:
    return _cached_load_months(DB_PATH, snapshot_id).copy()

def snapshot_window_totals(conn: sqlite3.Connection, snapshot_id: int, window_months: set[pd.Timestamp]) -> dict:
    """
    For a given snapshot_id, compute totals across the fixed month window.
    Returns totals using ACCOM revenue only.
    """
    df = db_load_months(conn, snapshot_id)
    df["stay_month"] = pd.to_datetime(df["stay_month"]).dt.to_period("M").dt.to_timestamp()

    df = df[df["stay_month"].isin(window_months)].copy()

    # ensure numeric
    for c in ["accom_rev", "occupied_rooms", "total_rooms"]:
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)

    rev = float(df["accom_rev"].sum())
    occ_rooms = float(df["occupied_rooms"].sum())
    tot_rooms = float(df["total_rooms"].sum())
    occ_pct = (occ_rooms / tot_rooms * 100.0) if tot_rooms > 0 else 0.0  # percent 0..100

    return {
        "window_rev": rev,
        "window_occ_rooms": occ_rooms,
        "window_tot_rooms": tot_rooms,
        "window_occ_pct": occ_pct,
    }


def build_daily_movers(conn: sqlite3.Connection, snapshots_df: pd.DataFrame, hotel: str, window_months: set[pd.Timestamp]) -> pd.DataFrame:
    """
    Build day-on-day deltas across snapshot history for the given hotel,
    measured over the SAME fixed month window (window_months).
    """
    hist = snapshots_df.copy()
    hotel_norm = (hotel or "").strip().lower()
    hist = hist[hist["hotel"].fillna("").astype(str).str.strip().str.lower() == hotel_norm].copy()

    if hist.empty:
        return pd.DataFrame(columns=[
    "snapshot_id", "snapshot_dt", "snapshot_day",
    "window_rev", "window_occ_rooms", "window_tot_rooms", "window_occ_pct",
    "rev_delta_day", "occ_pct_delta_day", "occ_rooms_delta_day"])
    hist["report_datetime"] = pd.to_datetime(hist["report_datetime"], errors="coerce")
    hist = hist.dropna(subset=["report_datetime"]).sort_values("report_datetime")

    rows = []
    for _, r in hist.iterrows():
        sid = int(r["snapshot_id"])
        totals = snapshot_window_totals(conn, sid, window_months)
        rows.append({
            "snapshot_id": sid,
            "snapshot_dt": r["report_datetime"],
            **totals
        })

    out = pd.DataFrame(rows)
    if out.empty:
        return pd.DataFrame(columns=[
    "snapshot_id", "snapshot_dt", "snapshot_day",
    "window_rev", "window_occ_rooms", "window_tot_rooms", "window_occ_pct",
    "rev_delta_day", "occ_pct_delta_day", "occ_rooms_delta_day"])
    # day-on-day deltas
    out["rev_delta_day"] = out["window_rev"].diff()
    out["occ_pct_delta_day"] = out["window_occ_pct"].diff()
    out["occ_rooms_delta_day"] = out["window_occ_rooms"].diff()

    # nice display date
    out["snapshot_day"] = out["snapshot_dt"].dt.strftime("%d %b %Y")

    return out

def build_daily_monthline_movers(
    conn: sqlite3.Connection,
    snapshots_df: pd.DataFrame,
    hotel: str,
    window_months: set[pd.Timestamp],
) -> pd.DataFrame:
    """
    For each snapshot day (current snapshot vs previous snapshot),
    compute month-line deltas and keep the biggest gain month and biggest drop month.
    """
    hist = snapshots_df.copy()
    hotel_norm = (hotel or "").strip().lower()
    hist = hist[hist["hotel"].fillna("").astype(str).str.strip().str.lower() == hotel_norm].copy()

    hist["report_datetime"] = pd.to_datetime(hist["report_datetime"], errors="coerce")
    hist = hist.dropna(subset=["report_datetime"]).sort_values("report_datetime")

    if len(hist) < 2:
        return pd.DataFrame(columns=[
            "snapshot_day", "type", "stay_month",
            "rev_delta", "occ_delta", "occ_rooms_delta",
        ])

    rows = []
    snap_ids = hist["snapshot_id"].astype(int).tolist()
    snap_dts = hist["report_datetime"].tolist()

    for i in range(1, len(snap_ids)):
        prev_id, cur_id = snap_ids[i-1], snap_ids[i]
        cur_dt = snap_dts[i]

        prev = db_load_months(conn, prev_id).copy()
        cur = db_load_months(conn, cur_id).copy()

        # Normalise month + filter to current window
        for df in (prev, cur):
            df["stay_month"] = pd.to_datetime(df["stay_month"], errors="coerce").dt.to_period("M").dt.to_timestamp()
            df["accom_rev"] = pd.to_numeric(df["accom_rev"], errors="coerce").fillna(0)
            df["occupied_rooms"] = pd.to_numeric(df["occupied_rooms"], errors="coerce").fillna(0)
            df["total_rooms"] = pd.to_numeric(df["total_rooms"], errors="coerce").fillna(0)
            df["occ_pct"] = pd.to_numeric(df["occupancy_pct"], errors="coerce") if "occupancy_pct" in df.columns else pd.to_numeric(df.get("occupancy_pct", 0), errors="coerce")

        prev = prev[prev["stay_month"].isin(window_months)]
        cur = cur[cur["stay_month"].isin(window_months)]

        m = cur.merge(
            prev[["stay_month", "accom_rev", "occupied_rooms", "total_rooms", "occ_pct"]],
            on="stay_month",
            how="left",
            suffixes=("_cur", "_prev"),
        ).fillna(0)

        m["rev_delta"] = m["accom_rev_cur"] - m["accom_rev_prev"]
        m["occ_rooms_delta"] = m["occupied_rooms_cur"] - m["occupied_rooms_prev"]

        # occupancy delta as percentage points on the month line (display as % change)
        # If occ_pct is already in percent (0..100), this works directly.
        m["occ_delta"] = m["occ_pct_cur"] - m["occ_pct_prev"]

        if m.empty:
            continue

        # Biggest gain month-line on this snapshot day
        g = m.loc[m["rev_delta"].idxmax()]
        rows.append({
            "snapshot_day": pd.to_datetime(cur_dt).strftime("%d %b %Y"),
            "type": "gain",
            "stay_month": g["stay_month"],
            "rev_delta": float(g["rev_delta"]),
            "occ_delta": float(g["occ_delta"]),
            "occ_rooms_delta": float(g["occ_rooms_delta"]),
        })

        # Biggest drop month-line on this snapshot day
        d = m.loc[m["rev_delta"].idxmin()]
        rows.append({
            "snapshot_day": pd.to_datetime(cur_dt).strftime("%d %b %Y"),
            "type": "drop",
            "stay_month": d["stay_month"],
            "rev_delta": float(d["rev_delta"]),
            "occ_delta": float(d["occ_delta"]),
            "occ_rooms_delta": float(d["occ_rooms_delta"]),
        })

    out = pd.DataFrame(rows)
    return out


def fmt_pct_1dp(x) -> str:
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return ""
    # handle strings like "48.31%" if they ever appear
    if isinstance(x, str):
        s = x.strip().replace("%", "")
        if not s:
            return ""
        try:
            v = float(s)
        except Exception:
            return x  # fallback: leave as-is
    else:
        try:
            v = float(x)
        except Exception:
            return ""
    # Values are already percentages (0–100). Never rescale: a genuine 0.17% month
    # would otherwise be shown as 17%. Below 1% show two decimals so it isn't 0.2%.
    if v != 0 and abs(v) < 1:
        return f"{v:.2f}%"
    return f"{v:.1f}%"



def load_budget_df(conn, hotel: str) -> pd.DataFrame:
    return pd.read_sql_query(
        "SELECT month, budget FROM budgets WHERE hotel = ? ORDER BY month",
        conn,
        params=(hotel,),
    )
def parse_budget_text(budget_text: str) -> pd.DataFrame:
    rows = []
    for raw in (budget_text or "").splitlines():
        raw = raw.strip()
        if not raw:
            continue

        # allow "Jan 2026, 1300000"
        if "," in raw:
            month_label, amt = [p.strip() for p in raw.split(",", 1)]
        else:
            # allow "Jan 2026 1300000"
            parts = raw.split()
            if len(parts) < 3:
                continue
            month_label = " ".join(parts[:2])  # "Jan 2026"
            amt = parts[2]

        amt = amt.replace("R", "").replace(" ", "").replace(",", "")
        try:
            amt = float(amt)
        except:
            continue

        rows.append({"month": month_label, "budget": amt})

    return pd.DataFrame(rows, columns=["month", "budget"])

def save_budget_df(conn, hotel: str, budget_df: pd.DataFrame) -> None:
    cur = conn.cursor()
    cur.execute("DELETE FROM budgets WHERE hotel = ?", (hotel,))
    for _, r in budget_df.iterrows():
        cur.execute(
            "INSERT OR REPLACE INTO budgets (hotel, month, budget) VALUES (?, ?, ?)",
            (hotel, str(r["month"]), float(r["budget"])),
        )
    conn.commit()

def budget_df_to_text(budget_df: pd.DataFrame) -> str:
    if budget_df.empty:
        return ""
    lines = []
    for _, r in budget_df.iterrows():
        lines.append(f'{r["month"]}, {int(round(float(r["budget"])))}')
    return "\n".join(lines)

def parse_yoy_text(yoy_text: str) -> pd.DataFrame:
    """
    Accept lines like:
      2025-01-01, 1172773.01, 623, 1240
    or:
      Jan 2025, 1172773.01, 623, 1240
    Returns columns: stay_month (ISO), accom_rev, occupied_rooms, total_rooms
    """
    rows = []
    for raw in (yoy_text or "").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        parts = [p.strip() for p in raw.split(",")]
        if len(parts) < 4:
            continue

        m = parts[0]
        rev = parts[1]
        occ = parts[2]
        tot = parts[3]

        # month parsing
        try:
            # supports ISO like 2025-01-01
            stay_month = pd.to_datetime(m, errors="raise").to_period("M").to_timestamp()
        except Exception:
            # supports "Jan 2025"
            stay_month = pd.to_datetime(m, format="%b %Y", errors="coerce")
            stay_month = (stay_month.to_period("M").to_timestamp() if pd.notnull(stay_month) else pd.NaT)

        if pd.isna(stay_month):
            continue

        rows.append({
            "stay_month": stay_month.isoformat(),
            "accom_rev": float(rev),
            "occupied_rooms": int(float(occ)),
            "total_rooms": int(float(tot)),
        })

    return pd.DataFrame(rows, columns=["stay_month", "accom_rev", "occupied_rooms", "total_rooms"])


def db_upsert_yoy_baseline(conn: sqlite3.Connection, hotel: str, df: pd.DataFrame) -> None:
    if df is None or df.empty:
        return
    cur = conn.cursor()
    for _, r in df.iterrows():
        cur.execute(
            """
            INSERT OR REPLACE INTO yoy_baseline (hotel, stay_month, accom_rev, occupied_rooms, total_rooms)
            VALUES (?, ?, ?, ?, ?)
            """,
            (hotel, str(r["stay_month"]), float(r["accom_rev"]), int(r["occupied_rooms"]), int(r["total_rooms"]))
        )
    conn.commit()
    st.cache_data.clear()


@st.cache_data(show_spinner=False)
def _cached_yoy_baseline(db_path: str, hotel: str) -> pd.DataFrame:
    c = sqlite3.connect(db_path, check_same_thread=False)
    try:
        return pd.read_sql_query(
            """
            SELECT stay_month, accom_rev, occupied_rooms, total_rooms
            FROM yoy_baseline
            WHERE hotel = ?
            ORDER BY stay_month
            """,
            c, params=(hotel,),
        )
    finally:
        c.close()

def db_load_yoy_baseline(conn: sqlite3.Connection, hotel: str) -> pd.DataFrame:
    return _cached_yoy_baseline(DB_PATH, hotel).copy()


# ══════════════════════════════════════════════════════════════════════════════
#  SAME TIME LAST YEAR (STLY) — OTB PACE  · NOT YET FED WITH DATA
#  The table, loaders and the dashboard section are all in place; the section
#  stays completely hidden until rows exist in ly_weekly_pace for this hotel.
#
#  Ways to feed it later (any of them writes the same table):
#   1. ANGALA_PACE_TEXT below: lines "YYYY-MM-DD, Mon YYYY, room_nights, revenue_ex_vat".
#      Each report date is loaded once on the next deploy and never overwrites a stored week.
#   2. ResRequest itself: run this same report for LAST year's stay dates with
#      cancellations included, and rebuild what was on the books at today-minus-one-year
#      with parse_resrequest_revenue_csv(..., as_of=<date>) — it uses Creation Date and
#      Cancellation Date — then store the months with db_upsert_weekly_pace().
# ══════════════════════════════════════════════════════════════════════════════
PACE_SEGMENT = "ANGALA"

ANGALA_PACE_TEXT = """
"""


def _pace_row(report_date, stay_month: pd.Timestamp, rooms: float, revenue: float) -> dict:
    cap = ANGALA_ROOMS * calendar.monthrange(stay_month.year, stay_month.month)[1]
    return {
        "report_date": pd.Timestamp(report_date).strftime("%Y-%m-%d"),
        "stay_month": stay_month.strftime("%Y-%m-01"),
        "segment": PACE_SEGMENT,
        "occ_pct": (rooms / cap * 100.0) if cap else None,
        "adr": (revenue / rooms) if rooms else None,
        "revenue": revenue,
    }


def parse_baked_pace_text(text: str) -> pd.DataFrame:
    out = []
    for line in (text or "").strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 4:
            continue
        sm = pd.to_datetime(parts[1], format="%b %Y", errors="coerce")
        if pd.isna(sm):
            continue
        out.append(_pace_row(parts[0], sm, float(parts[2]), float(parts[3])))
    return pd.DataFrame(out)


def pace_rows_from_months(report_date, month_df: pd.DataFrame) -> pd.DataFrame:
    """Turn a parsed month table (e.g. from parse_resrequest_revenue_csv with as_of) into pace rows."""
    out = []
    for _, r in month_df.iterrows():
        sm = pd.Timestamp(r["stay_month"])
        out.append(_pace_row(report_date, sm, float(r["occupied_rooms"]), float(r["accom_rev"])))
    return pd.DataFrame(out)


def db_upsert_weekly_pace(conn: sqlite3.Connection, hotel: str, df: pd.DataFrame,
                          source_filename: str, clear_cache: bool = True) -> int:
    cur = conn.cursor()
    for _, r in df.iterrows():
        cur.execute(
            """
            INSERT OR REPLACE INTO ly_weekly_pace
                (hotel, report_date, stay_month, segment, occ_pct, adr, revenue, source_filename)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (hotel, r["report_date"], r["stay_month"], r["segment"],
             r["occ_pct"], r["adr"], r["revenue"], source_filename),
        )
    conn.commit()
    if clear_cache:
        st.cache_data.clear()
    return len(df)


@st.cache_resource(show_spinner=False)
def _seed_baked_pace(db_path: str, hotel: str) -> int:
    """Load report dates from ANGALA_PACE_TEXT that have never been loaded before."""
    df = parse_baked_pace_text(ANGALA_PACE_TEXT)
    if df.empty:
        return 0
    c = sqlite3.connect(db_path, check_same_thread=False)
    try:
        c.execute("CREATE TABLE IF NOT EXISTS pace_seed_applied ("
                  "hotel TEXT NOT NULL, report_date TEXT NOT NULL, "
                  "PRIMARY KEY (hotel, report_date))")
        stored = {r[0] for r in c.execute(
            "SELECT DISTINCT report_date FROM ly_weekly_pace WHERE LOWER(hotel) = LOWER(?)", (hotel,))}
        applied = {r[0] for r in c.execute(
            "SELECT report_date FROM pace_seed_applied WHERE LOWER(hotel) = LOWER(?)", (hotel,))}
        new = df[~df["report_date"].isin(stored | applied)]
        for d in df["report_date"].unique():
            c.execute("INSERT OR IGNORE INTO pace_seed_applied (hotel, report_date) VALUES (?, ?)",
                      (hotel, d))
        c.commit()
        if new.empty:
            return 0
        return db_upsert_weekly_pace(c, hotel, new, "ANGALA_PACE_TEXT", clear_cache=False)
    finally:
        c.close()

@st.cache_data(show_spinner=False)
def _cached_weekly_pace(db_path: str, hotel: str) -> pd.DataFrame:
    c = sqlite3.connect(db_path, check_same_thread=False)
    try:
        return pd.read_sql_query(
            "SELECT report_date, stay_month, segment, occ_pct, adr, revenue, source_filename "
            "FROM ly_weekly_pace WHERE LOWER(hotel) = LOWER(?) ORDER BY report_date, stay_month",
            c, params=(hotel,),
        )
    finally:
        c.close()


def db_load_weekly_pace(conn: sqlite3.Connection, hotel: str) -> pd.DataFrame:
    return _cached_weekly_pace(DB_PATH, hotel).copy()

def db_upsert_budget_baseline(conn: sqlite3.Connection, hotel: str, df: pd.DataFrame) -> None:
    if df is None or df.empty:
        return
    cur = conn.cursor()
    for _, r in df.iterrows():
        cur.execute(
            """
            INSERT OR REPLACE INTO budget_baseline (hotel, stay_month, accom_rev, occupied_rooms, total_rooms)
            VALUES (?, ?, ?, ?, ?)
            """,
            (hotel, str(r["stay_month"]), float(r["accom_rev"]), int(r["occupied_rooms"]), int(r["total_rooms"]))
        )
    conn.commit()
    st.cache_data.clear()


@st.cache_data(show_spinner=False)
def _cached_budget_baseline(db_path: str, hotel: str) -> pd.DataFrame:
    c = sqlite3.connect(db_path, check_same_thread=False)
    try:
        return pd.read_sql_query(
            """
            SELECT stay_month, accom_rev, occupied_rooms, total_rooms
            FROM budget_baseline
            WHERE hotel = ?
            ORDER BY stay_month
            """,
            c, params=(hotel,),
        )
    finally:
        c.close()

def db_load_budget_baseline(conn: sqlite3.Connection, hotel: str) -> pd.DataFrame:
    return _cached_budget_baseline(DB_PATH, hotel).copy()


st.markdown(
    """
    <div class="sl-hero">
        <div class="eyebrow">Barefoot 5-Star Sanctuary · Cape Winelands, Franschhoek</div>
        <h1>Angala Boutique Hotel</h1>
        <div class="sub">Production Dashboard — On-the-Books Snapshot Tracker · Revenue · Occupancy · ADR · RevPAR</div>
        <div class="gold-rule"></div>
    </div>
    """,
    unsafe_allow_html=True,
)

with st.expander("Which report to upload", expanded=False):
    st.markdown(
        """
**ResRequest → B201 Booking Console → preset "Providence- Revenue Report", exported as CSV.**

- Keep the date range on **full calendar months**, starting on the 1st of the current month and
  running six months ahead (e.g. *1 Nov 2026 until before 1 May 2027*). Use the **same window**
  for every upload in a month so snapshot-to-snapshot changes are real, not window edges.
- Leave **Cancelled** in the status filter — cancelled bookings are dropped here, and keeping
  them in the export lets the dashboard rebuild last year's pace from the same report later.
- The snapshot is dated from the report's own timestamp (bottom line of the CSV).
        """
    )

# DB — on Railway set DB_PATH=/data/angala.db so it lands on the persistent volume
DB_PATH = os.environ.get("DB_PATH", "angala_snapshots.db")
_db_dir = os.path.dirname(DB_PATH)
if _db_dir:
    os.makedirs(_db_dir, exist_ok=True)
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
db_init(conn)

# Single-property deployment — hotel is fixed, no selector shown
hotel = "Angala Boutique Hotel"
_seed_baked_pace(DB_PATH, hotel)   # STLY pace lines, if any have been added to ANGALA_PACE_TEXT


# ---- Seed BUDGET baseline ONCE (only when SEED_BUDGET=1) ----
# Lines: "Mon YYYY, accommodation revenue ex VAT, room nights, available room nights".
# Capacity = ANGALA_ROOMS x days in month (11 rooms: 330 in a 30-day month, 341 in a 31-day month).
# Set SEED_BUDGET=1 on Railway, redeploy, open the dashboard once, then remove the variable.
HOTEL_BUDGET_TEXT = {
    "angala boutique hotel": """
""".strip(),
}
if os.getenv("SEED_BUDGET", "") == "1":
    budget_text = HOTEL_BUDGET_TEXT.get(hotel.strip().lower())
    if not budget_text:
        st.sidebar.error("SEED_BUDGET=1 is set, but HOTEL_BUDGET_TEXT has no Angala lines yet.")
    else:
        db_upsert_budget_baseline(conn, hotel, parse_yoy_text(budget_text))
        st.sidebar.success(f"Seeded BUDGET baseline for {hotel} (SEED_BUDGET=1).")


# ---- Seed LAST YEAR (final actuals) baseline ONCE (only when SEED_LY=1) ----
# Same line format as the budget, for last year's months (e.g. "Nov 2025, ...").
HOTEL_LY_TEXT = {
    "angala boutique hotel": """
""".strip(),
}
if os.getenv("SEED_LY", "") == "1":
    ly_text = HOTEL_LY_TEXT.get(hotel.strip().lower())
    if not ly_text:
        st.sidebar.error("SEED_LY=1 is set, but HOTEL_LY_TEXT has no Angala lines yet.")
    else:
        db_upsert_yoy_baseline(conn, hotel, parse_yoy_text(ly_text))
        st.sidebar.success(f"Seeded LAST YEAR baseline for {hotel} (SEED_LY=1).")


# ── Admin access ─────────────────────────────────────────────────────────────
# No default PIN: until ADMIN_PIN is set on Railway, admin mode simply can't be unlocked.
ADMIN_PIN = os.environ.get("ADMIN_PIN", "").strip()
admin_pin_input = st.sidebar.text_input("Admin PIN (hotel team leave blank)", type="password")
is_admin = bool(ADMIN_PIN) and admin_pin_input == ADMIN_PIN
# ── End Admin access ──────────────────────────────────────────────────────────

# Sidebar controls
if is_admin:
    st.sidebar.header("Upload & thresholds")
    uploaded = st.sidebar.file_uploader("Upload ResRequest Revenue Report (.csv)", type=["csv"])
else:
    uploaded = None

if is_admin:
    rev_drop = st.sidebar.number_input("Flag accommodation revenue drop ≥ (R)", min_value=0, value=50000, step=5000)
    rev_gain = st.sidebar.number_input("Flag accommodation revenue gain ≥ (R)", min_value=0, value=50000, step=5000)
    occ_drop_pp = st.sidebar.number_input("Flag occupancy drop ≥ (%)", min_value=0.0, value=3.0, step=0.5)
    occ_gain_pp = st.sidebar.number_input("Flag occupancy gain ≥ (%)", min_value=0.0, value=3.0, step=0.5)
else:
    rev_drop, rev_gain, occ_drop_pp, occ_gain_pp = 50000, 50000, 3.0, 3.0

if is_admin:
    for _m in st.session_state.pop("upload_msgs", []):
        getattr(st, _m[0])(_m[1])
    colA, colB = st.columns([1, 2])
    with colA:
        if st.button("Parse + Save snapshot"):
            try:
                if uploaded is None:
                    st.error("Please upload the ResRequest CSV first.")
                    st.stop()
                if not uploaded.name.lower().endswith(".csv"):
                    st.error("Unsupported file type. Upload the ResRequest Revenue Report as .csv.")
                    st.stop()
                month_df, day_df, meta = parse_resrequest_revenue_csv(
                    uploaded.getvalue(), uploaded.name, hotel
                )
                _dupe = conn.execute(
                    "SELECT snapshot_id FROM snapshots WHERE LOWER(hotel)=LOWER(?) AND report_datetime=? "
                    "AND period_start=? AND period_end=?",
                    (hotel, meta["report_datetime"], meta["period_start"], meta["period_end"]),
                ).fetchone()
                if _dupe:
                    st.warning(f"This report ({meta['report_datetime']}) is already saved as snapshot "
                               f"#{_dupe[0]}, so it wasn't saved again.")
                    st.stop()
                snapshot_id = db_insert_snapshot(conn, meta, month_df, day_df)
                msgs = [("success",
                         f"Saved snapshot #{snapshot_id}: report of {meta['report_datetime']}, "
                         f"stays {meta['period_start']} → {meta['period_end']}.")]
                if not meta.get("report_dt_from_file"):
                    msgs.append(("warning", "No report timestamp found at the bottom of the CSV, so the "
                                            "upload time was used as the snapshot date."))
                msgs.append(("info",
                             f"{meta['rows_dropped']} cancelled lines left out. "
                             f"Provisional on the books: {fmt_int(meta['prov_nights'])} room nights, "
                             f"{fmt_rand0(meta['prov_rev'])} (counted, and shown separately in the table)."
                             + (f" {meta['zero_rate_bookings']} zero-rate bookings "
                                f"({meta['zero_rate_nights']} room nights, e.g. complimentary/house use) "
                                f"count as occupied with no revenue." if meta['zero_rate_bookings'] else "")))
                if meta.get("overbooked_days"):
                    msgs.append(("warning", f"More than {meta['inventory_rooms']} rooms booked on: "
                                            + ", ".join(meta["overbooked_days"][:10])
                                            + ". Check ANGALA_ROOMS or the report."))
                st.session_state["upload_msgs"] = msgs
                st.rerun()
            except Exception as e:
                st.error(str(e))
    with colB:
        st.write("")

snapshots = db_list_snapshots(conn)
snapshots = snapshots[snapshots["hotel"].fillna("").str.lower() == hotel.lower()].copy()

# History actions (Undo) — admin only
if is_admin:
    st.sidebar.subheader("History")
    if st.sidebar.button("Undo last upload", help="Deletes the most recently saved snapshot (latest report_datetime)."):
        if snapshots.empty:
            st.sidebar.warning("No snapshots to undo.")
        else:
            latest_to_delete = int(snapshots.iloc[0]["snapshot_id"])
            db_delete_snapshot(conn, latest_to_delete)
            st.sidebar.success(f"Undid snapshot #{latest_to_delete}")
            st.rerun()

if snapshots.empty:
    st.info("Upload your first Excel booking summary on the left to create your baseline snapshot.")
    st.stop()

# Choose snapshots to compare
st.sidebar.header("Compare snapshots")

# Build friendly labels
def snapshot_label(row):
    hotel = (row.get("hotel") or "Unknown hotel").strip()
    dt = row.get("report_datetime") or "No datetime"
    fn = (row.get("source_filename") or "").strip()
    sid = int(row.get("snapshot_id"))
    tail = f" | {fn}" if fn else ""
    return f"{hotel} | {dt} | #{sid}{tail}"

snapshots = snapshots.copy()
snapshots["label"] = snapshots.apply(snapshot_label, axis=1)

labels = snapshots["label"].tolist()
label_to_id = dict(zip(labels, snapshots["snapshot_id"]))

current_label = st.sidebar.selectbox("Current snapshot (today)", labels, index=0)
current_id = int(label_to_id[current_label])

compare_default = 1 if len(labels) > 1 else 0
compare_label = st.sidebar.selectbox("Compare against (previous)", labels, index=compare_default)
compare_id = int(label_to_id[compare_label])

cur_meta = snapshots[snapshots["snapshot_id"] == current_id].iloc[0].to_dict()
prev_meta = snapshots[snapshots["snapshot_id"] == compare_id].iloc[0].to_dict()

cur_df = db_load_months(conn, current_id)
prev_df = db_load_months(conn, compare_id)


# Alignment + deltas
prev_df["stay_month"] = pd.to_datetime(prev_df["stay_month"]).dt.to_period("M").dt.to_timestamp()
cur_df["stay_month"]  = pd.to_datetime(cur_df["stay_month"]).dt.to_period("M").dt.to_timestamp()

merged = cur_df.merge(prev_df, on="stay_month", how="outer", suffixes=("_cur", "_prev")).sort_values("stay_month")
# Ensure numeric first (don’t format these yet)
for c in ["accom_rev_cur", "accom_rev_prev"]:
    merged[c] = pd.to_numeric(merged[c], errors="coerce")

# Calculate revenue delta from numeric values
merged["rev_delta"] = merged["accom_rev_cur"] - merged["accom_rev_prev"]

merged["accom_rev_cur"] = merged["accom_rev_cur"].fillna(0.0)
merged["accom_rev_prev"] = merged["accom_rev_prev"].fillna(0.0)
merged["occupancy_pct_cur"] = merged["occupancy_pct_cur"].fillna(0.0)
merged["occupancy_pct_prev"] = merged["occupancy_pct_prev"].fillna(0.0)
merged["occupied_rooms_cur"] = merged["occupied_rooms_cur"].fillna(0)
merged["occupied_rooms_prev"] = merged["occupied_rooms_prev"].fillna(0)

merged["rev_delta"] = merged["accom_rev_cur"] - merged["accom_rev_prev"]
merged["occ_delta_pp"] = merged["occupancy_pct_cur"] - merged["occupancy_pct_prev"]
merged["occ_rooms_delta"] = merged["occupied_rooms_cur"] - merged["occupied_rooms_prev"]

# Warning if reporting periods differ
if cur_meta.get("period_start") != prev_meta.get("period_start") or cur_meta.get("period_end") != prev_meta.get("period_end"):
    st.warning(
        f"These two snapshots use different reporting periods:\n\n"
        f"- Current: {cur_meta.get('period_start')} → {cur_meta.get('period_end')}\n"
        f"- Previous: {prev_meta.get('period_start')} → {prev_meta.get('period_end')}\n\n"
        f"This can create edge-month distortions. For clean deltas, keep the report window fixed to full months."
    )

def totals(df):
    total_rev = df["accom_rev"].sum()
    occ = df["occupied_rooms"].sum()
    rooms = df["total_rooms"].sum()
    occ_pct = (occ / rooms * 100) if rooms else 0.0
    return total_rev, occ, rooms, occ_pct

# Month filter (use current snapshot months as the primary list)
cur_months = (
    cur_df["stay_month"]
      .dropna()
      .sort_values()
      .unique()
      .tolist()
)

# Fallback to merged months if something odd happens
all_months = cur_months if cur_months else (
    merged["stay_month"].dropna().sort_values().unique().tolist()
)

# Default: first 6 months from current month onwards
today_ts = pd.Timestamp.today().to_period("M").to_timestamp()
future_months = [m for m in all_months if pd.to_datetime(m) >= today_ts]
default_months = future_months[:6] if len(future_months) >= 6 else (future_months if future_months else all_months[:6])

selected_months = st.multiselect(
    "Months to display",
    all_months,
    default=default_months,
    format_func=lambda m: pd.Timestamp(m).strftime("%b %Y"),
)

# Always sort (multiselect can return unsorted)
selected_months = sorted(selected_months)

view = merged[merged["stay_month"].isin(selected_months)].copy()

# --- Current month KPIs (first month in the selected rolling window) ---
current_month = selected_months[0] if selected_months else None


def totals(df):
    total_rev = df["accom_rev"].sum()
    occ = df["occupied_rooms"].sum()
    rooms = df["total_rooms"].sum()
    occ_pct = (occ / rooms * 100) if rooms else 0.0
    return total_rev, occ, rooms, occ_pct

if current_month is not None:
    cur_month_df = cur_df[cur_df["stay_month"] == current_month]
    prev_month_df = prev_df[prev_df["stay_month"] == current_month]

    cur_m_rev, cur_m_occ, cur_m_rooms, cur_m_occ_pct = totals(cur_month_df)
    prev_m_rev, prev_m_occ, prev_m_rooms, prev_m_occ_pct = totals(prev_month_df)

    month_lbl = pd.to_datetime(current_month).strftime("%b %Y")

    sl_section("At a glance", f"Current month performance — {month_lbl}")
    k1, k2, k3, k4 = st.columns(4)
    k1.metric(f"Accommodation revenue ({month_lbl})", fmt_currency(cur_m_rev), fmt_currency(cur_m_rev - prev_m_rev))
    k2.metric(f"Occupied room nights ({month_lbl})", fmt_int(cur_m_occ), f"{int(cur_m_occ - prev_m_occ):+d}")
    k3.metric(f"OTB occupancy % ({month_lbl})", f"{cur_m_occ_pct:.2f}%", f"{(cur_m_occ_pct - prev_m_occ_pct):+.2f}%")
    k4.metric("Snapshot times", str(cur_meta.get("report_datetime")), f"vs {prev_meta.get('report_datetime')}")
else:
    st.info("No months available to calculate current-month KPIs.")


def flag_row(r):
    flags = []
    if pd.notnull(r["rev_delta"]):
        if r["rev_delta"] <= -rev_drop:
            flags.append("Revenue DROP")
        if r["rev_delta"] >= rev_gain:
            flags.append("Revenue GAIN")
    if pd.notnull(r["occ_delta_pp"]):
        if r["occ_delta_pp"] <= -occ_drop_pp:
            flags.append("Occ DROP")
        if r["occ_delta_pp"] >= occ_gain_pp:
            flags.append("Occ GAIN")
    return ", ".join(flags)

view["flags"] = view.apply(flag_row, axis=1)

out = view[
    [
        "stay_month",
        "occupied_rooms_prev", "occupancy_pct_prev", "accom_rev_prev",
        "occupied_rooms_cur",  "occupancy_pct_cur",  "accom_rev_cur", "prov_rev_cur",
        "occ_rooms_delta", "occ_delta_pp", "rev_delta", "flags",
    ]
].copy()


# --- Revenue change helper + display (MUST happen before grid_df = out.copy()) ---
out["rev_delta_num"] = pd.to_numeric(out["rev_delta"], errors="coerce").fillna(0.0)
out["rev_delta_display"] = out["rev_delta_num"].apply(fmt_currency)

# optional: remove raw delta so you never accidentally use it
out = out.drop(columns=["rev_delta"], errors="ignore")

# --- ADR (numeric first) ---
out["adr_prev_num"] = out["accom_rev_prev"] / out["occupied_rooms_prev"].replace(0, pd.NA)
out["adr_cur_num"]  = out["accom_rev_cur"]  / out["occupied_rooms_cur"].replace(0, pd.NA)

# % change in ADR: (current - previous) / previous
out["adr_change_pct_num"] = (out["adr_cur_num"] - out["adr_prev_num"]) / out["adr_prev_num"] * 100
out["adr_change_pct"] = out["adr_change_pct_num"].apply(
    lambda x: f"{x:+.1f}%" if pd.notnull(x) else ""
)

# Format ADR for display
out["adr_prev"] = out["adr_prev_num"].apply(fmt_currency)
out["adr_cur"]  = out["adr_cur_num"].apply(fmt_currency)

# Format only the revenue columns (not rev_delta_num)
out["prov_rev_cur"] = pd.to_numeric(out["prov_rev_cur"], errors="coerce").fillna(0.0).apply(fmt_rand0)
for c in ["accom_rev_prev", "accom_rev_cur"]:
    out[c] = out[c].apply(fmt_currency)

for c in ["occupancy_pct_prev", "occupancy_pct_cur"]:
    out[c] = out[c].apply(fmt_pct_1dp)

out["occ_delta_num"] = pd.to_numeric(out["occ_delta_pp"], errors="coerce").fillna(0.0)

# Keep a numeric helper for occupancy change BEFORE converting to string
out["occ_delta_num"] = pd.to_numeric(out["occ_delta_pp"], errors="coerce").fillna(0.0)

out["occ_delta_pp"] = out["occ_delta_pp"].apply(lambda x: f"{x:+.2f}%" if pd.notnull(x) else "")
out["occ_rooms_delta"] = out["occ_rooms_delta"].apply(lambda x: f"{int(x):+d}" if pd.notnull(x) else "")

sl_section("Pace tracking", "Month-by-month change — snapshot vs snapshot")
# Format stay_month for display (e.g., "Jan 2026")
out["stay_month"] = pd.to_datetime(out["stay_month"], errors="coerce").dt.strftime("%b %Y")

table_df = out.copy()

# Display column for revenue change
table_df["Revenue change"] = table_df["rev_delta_num"].apply(fmt_currency)

# (occ_delta_pp is already a formatted string like +0.25%)
table_df = table_df.rename(columns={
    "stay_month": "Stay month",
    "occupied_rooms_prev": "Occupied RN (prev)",
    "occupancy_pct_prev": "OTB Occ % (prev)",
    "accom_rev_prev": "Accommodation rev (prev)",
    "occupied_rooms_cur": "Room Nights",
    "occupancy_pct_cur": "Occ %",
    "accom_rev_cur": "Accommodation rev",
    "prov_rev_cur": "of which provisional",
    "occ_rooms_delta": "Δ Occupied RN",
    "occ_delta_pp": "Occ % change",
    "adr_prev": "ADR (prev)",
    "adr_cur": "ADR (current)",
    "adr_change_pct": "ADR % change",
    "flags": "Flags",
})

desired_order = [
    "Stay month",
    "Room Nights",
    "Occ %",
    "Accommodation rev",
    "of which provisional",
    "ADR (current)",

    "Δ Occupied RN",
    "Occ % change",
    "Revenue change",
    "ADR % change",
    "Flags",
]
display_cols = [c for c in desired_order if c in table_df.columns]
display_df = table_df[display_cols].copy()

# Keep helpers for styling
rev_src = table_df["rev_delta_num"] if "rev_delta_num" in table_df.columns else pd.Series(0.0, index=table_df.index)
occ_src = table_df["occ_delta_num"] if "occ_delta_num" in table_df.columns else pd.Series(0.0, index=table_df.index)

table_df["__rev_delta_num"] = pd.to_numeric(rev_src, errors="coerce").fillna(0.0)
table_df["__occ_delta_num"] = pd.to_numeric(occ_src, errors="coerce").fillna(0.0)
adr_src = table_df["adr_change_pct_num"] if "adr_change_pct_num" in table_df.columns else pd.Series(0.0, index=table_df.index)
table_df["__adr_change_pct_num"] = pd.to_numeric(adr_src, errors="coerce").fillna(0.0)


# Drop columns you don't want displayed
# Drop only raw numeric/internal columns (keep the nice renamed display columns!)
table_df = table_df.drop(
    columns=["adr_prev_num", "adr_cur_num",
             "rev_delta_num", "rev_delta_display", "occ_delta_num","occupied_rooms_prev",
             "occupancy_pct_prev","accom_rev_prev"
             ],
    errors="ignore"
)

def colour_cell(v):
    if pd.isna(v) or v == 0:
        return ""
    if v > 0:
        return f"background-color: {BRAND['good_bg']}; color: {BRAND['good_txt']}; font-weight: 600;"
    return f"background-color: {BRAND['bad_bg']}; color: {BRAND['bad_txt']}; font-weight: 600;"

def style_row(row):
    styles = [""] * len(row)

    # Revenue change colour
    if "Revenue change" in row.index:
        rev_v = row.get("__rev_delta_num", 0)
        styles[row.index.get_loc("Revenue change")] = colour_cell(rev_v)

    # Occ % change colour
    if "Occ % change" in row.index:
        occ_v = row.get("__occ_delta_num", 0)
        styles[row.index.get_loc("Occ % change")] = colour_cell(occ_v)

    # ADR % change colour
    if "ADR % change" in row.index:
        adr_v = src_row.get("__adr_change_pct_num", 0.0)
        styles[row.index.get_loc("ADR % change")] = colour_cell(adr_v)

    return styles

# Force the display order (this is what makes "Flags" reliably last)
display_df = table_df.reindex(columns=display_cols).copy()


# Apply styles using the ORIGINAL table_df (with helpers), then render display_df
def style_row_for_display(row):
    # row here comes from display_df, so helpers aren't present
    # We fetch helper values from table_df using the row index
    src_row = table_df.loc[row.name]

    styles = [""] * len(row)

    # Revenue change colour
    if "Revenue change" in row.index:
        rev_v = src_row.get("__rev_delta_num", 0)
        styles[row.index.get_loc("Revenue change")] = colour_cell(rev_v)

    # Occ % change colour
    if "Occ % change" in row.index:
        occ_v = src_row.get("__occ_delta_num", 0)
        styles[row.index.get_loc("Occ % change")] = colour_cell(occ_v)

    # ADR % change colour
    if "ADR % change" in row.index:
        adr_v = src_row.get("__adr_change_pct_num", 0.0)
        styles[row.index.get_loc("ADR % change")] = colour_cell(adr_v)    

    return styles

styled = (
    display_df.style
    .apply(style_row_for_display, axis=1)
    .set_properties(**{"text-align": "center"})
    .set_table_styles([{"selector": "th", "props": [("text-align", "center")]}])
)
st.markdown(
    """
    <style>
    div[data-testid="stDataFrame"] [role="gridcell"],
    div[data-testid="stDataFrame"] [role="columnheader"]{
        text-align: center !important;
        justify-content: center !important;
    }
    </style>
    """,
    unsafe_allow_html=True
)


st.dataframe(styled, width="stretch", height=360, key="month_change_table")

# --- Day-based Biggest drops / gains (based on snapshot history) ---

# Fixed month window = whatever you're currently displaying in the 6-month view
window_months = set(pd.to_datetime(view["stay_month"]).dt.to_period("M").dt.to_timestamp().tolist())

hotel_for_history = str(cur_meta.get("hotel") or hotel).strip()
cur_days = db_load_days(conn, current_id)
prev_days = db_load_days(conn, compare_id)

def calendar_heatmap_month(df_days: pd.DataFrame, month_ts: pd.Timestamp):
    m0 = pd.Timestamp(month_ts).to_period("M").to_timestamp()
    m1 = (m0 + pd.offsets.MonthBegin(1))

    d = df_days[(df_days["stay_date"] >= m0) & (df_days["stay_date"] < m1)].copy()
    if d.empty:
        return None

    d["dow"] = d["stay_date"].dt.weekday
    first = m0
    d["week_of_month"] = ((d["stay_date"] - first).dt.days + first.weekday()) // 7

    z = d.pivot_table(index="dow", columns="week_of_month", values="occupancy_pct", aggfunc="mean")
    z = z.reindex(range(7))

    # --- build cell labels + hover labels ---
    text_grid = [["" for _ in z.columns] for _ in z.index]
    custom_grid = [["" for _ in z.columns] for _ in z.index]
    col_list = list(z.columns)

    for _, r in d.iterrows():
        rr = int(r["dow"])
        cc = int(r["week_of_month"])
        if cc not in col_list:
            continue
        ci = col_list.index(cc)

        dt = r["stay_date"]
        occ = float(r["occupancy_pct"]) if pd.notnull(r["occupancy_pct"]) else 0.0

        occ_rooms = float(r.get("occupied_rooms", 0) or 0)
        rev = float(r.get("accom_rev", 0) or 0)
        adr = (rev / occ_rooms) if occ_rooms > 0 else 0.0

        text_grid[rr][ci] = f"{occ:.0f}%"
        custom_grid[rr][ci] = (
            f"{dt:%d %b %Y}"
            f"<br>Occ: {occ:.1f}%"
            f"<br>ADR: {fmt_currency(adr)}"
        )


    fig = go.Figure(
        data=go.Heatmap(
            z=z.values,
            x=list(z.columns),
            y=["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
            text=text_grid,
            texttemplate="%{text}",
            textfont={"size": 12},
            customdata=custom_grid,
            hovertemplate="%{customdata}<extra></extra>",
            colorscale=HEAT_COLORSCALE,
            zmin=0, zmax=100,
            xgap=3, ygap=3,
            colorbar=dict(
                title=dict(text="Occ %", font=dict(size=11)),
                ticksuffix="%", thickness=12, outlinewidth=0,
            ),
        )
    )

    fig.update_layout(
        title=dict(
            text=f"Daily OTB Occupancy % — {m0:%b %Y}",
            font=dict(family="Playfair Display, Georgia, serif", size=22, color=BRAND["deep"]),
        ),
        yaxis_autorange="reversed",
        height=340,
        margin=dict(l=20, r=20, t=55, b=20),
    )
    brand_plotly(fig)

    fig.update_xaxes(
    tickmode="array",
    tickvals=list(z.columns),
    ticktext=[f"W{k+1}" for k in z.columns],)

    # --- highlight today's date (red border) ---
    today = pd.Timestamp.today().normalize()   
    today_wom = None
    today_dow = None
    if m0 <= today < m1:
        # find today's row/col in the calendar grid
        today_dow = int(today.weekday())  # Mon=0..Sun=6
        today_wom = int(((today - m0).days + m0.weekday()) // 7)
    # Only draw if that column exists in this month's heatmap
    if today_wom in list(z.columns):
        x0 = today_wom - 0.5
        x1 = today_wom + 0.5
        y0 = today_dow - 0.5
        y1 = today_dow + 0.5

        fig.add_shape(
            type="rect",
            xref="x",
            yref="y",
            x0=x0, x1=x1,
            y0=y0, y1=y1,
            line=dict(color=BRAND["clay"], width=3),
            fillcolor="rgba(0,0,0,0)",
            layer="above",
        )
    return fig


gains = drops = None      # populated below when both snapshots carry daily rows

if cur_days.empty or prev_days.empty:
    st.info("No stay-date history yet (daily rows weren’t stored for one/both snapshots).")
else:
    for d in (cur_days, prev_days):
        d["stay_date"] = pd.to_datetime(d["stay_date"], errors="coerce")
        d["accom_rev"] = pd.to_numeric(d["accom_rev"], errors="coerce").fillna(0)
        d["occupancy_pct"] = pd.to_numeric(d["occupancy_pct"], errors="coerce").fillna(0)
        d["occupied_rooms"] = pd.to_numeric(d["occupied_rooms"], errors="coerce").fillna(0)

        # keep only dates inside the same month window you’re analysing
        d["stay_month"] = d["stay_date"].dt.to_period("M").dt.to_timestamp()
        d.dropna(subset=["stay_date"], inplace=True)

    window_months = set(selected_months)  # you already have this from the multiselect :contentReference[oaicite:10]{index=10}
    cur_days = cur_days[cur_days["stay_month"].isin(window_months)]
    prev_days = prev_days[prev_days["stay_month"].isin(window_months)]


    movers = cur_days.merge(prev_days, on="stay_date", how="outer", suffixes=("_cur", "_prev"))

    for c in ["accom_rev", "occupancy_pct", "occupied_rooms"]:
        movers[f"{c}_cur"] = pd.to_numeric(movers.get(f"{c}_cur"), errors="coerce").fillna(0)
        movers[f"{c}_prev"] = pd.to_numeric(movers.get(f"{c}_prev"), errors="coerce").fillna(0)

    movers["rev_delta"] = movers["accom_rev_cur"] - movers["accom_rev_prev"]
    movers["occ_delta"] = movers["occupancy_pct_cur"] - movers["occupancy_pct_prev"]
    movers["occ_rooms_delta"] = movers["occupied_rooms_cur"] - movers["occupied_rooms_prev"]
    movers["stay_date_lbl"] = movers["stay_date"].dt.strftime("%d %b %Y")

    drops = movers[movers["rev_delta"] < 0].nsmallest(5, "rev_delta")
    gains = movers[movers["rev_delta"] > 0].nlargest(5, "rev_delta")

    cols = ["stay_date_lbl", "rev_delta", "occ_delta", "occ_rooms_delta"]

    c1, c2 = st.columns(2)
    with c1:
        st.markdown("### 🍂 Biggest drops (by stay date)")
        if drops.empty:
            st.write("—")
        else:
            st.table(
                drops[cols].assign(
                    **{
                        "Revenue change": drops["rev_delta"].apply(fmt_currency),
                        "Occ % change": drops["occ_delta"].apply(lambda x: f"{x:+.1f}%"),
                        "Δ Occupied RN": drops["occ_rooms_delta"].apply(lambda x: f"{x:+.0f}"),
                    }
                ).rename(columns={
                    "stay_date_lbl": "Stay date",
                    "occ_rooms_delta": "Δ Room Nights"
                })[["Stay date","Revenue change","Occ % change","Δ Room Nights"]]
            )

    with c2:
        st.markdown("### 🌿 Biggest gains (by stay date)")
        if gains.empty:
            st.write("—")
        else:
            st.table(
                gains[cols].assign(
                    **{
                        "Revenue change": gains["rev_delta"].apply(fmt_currency),
                        "Occ % change": gains["occ_delta"].apply(lambda x: f"{x:+.1f}%"),
                        "Δ Occupied RN": gains["occ_rooms_delta"].apply(lambda x: f"{x:+.0f}"),
                    }
                ).rename(columns={
                    "stay_date_lbl": "Stay date",
                    "occ_rooms_delta": "Δ Room nights"
                })[["Stay date","Revenue change","Occ % change","Δ Room nights"]]
            )

sl_section("Stay-date detail", "Daily OTB occupancy calendar — latest snapshot")

days = db_load_days(conn, current_id)
days["stay_date"] = pd.to_datetime(days["stay_date"], errors="coerce")
days["occupancy_pct"] = pd.to_numeric(days["occupancy_pct"], errors="coerce")
days = days.dropna(subset=["stay_date", "occupancy_pct"])

# Build month list — only current month and forward to avoid past months
# from straddle bookings appearing in the calendar
stay_dates = pd.to_datetime(days["stay_date"], errors="coerce")
all_months = sorted(stay_dates.dropna().dt.to_period("M").dt.to_timestamp().unique().tolist())

today_m = pd.Timestamp.today().to_period("M").to_timestamp()

# Filter to current month onwards only
months = [m for m in all_months if m >= today_m]
if not months:
    months = all_months  # fallback: show all if nothing in future

# Always default to current month (index 0 after filtering)
default_idx = 0

# Session-state month index
if "cal_month_idx" not in st.session_state:
    st.session_state.cal_month_idx = default_idx

# Clamp + pick month
st.session_state.cal_month_idx = max(0, min(st.session_state.cal_month_idx, len(months) - 1))
month_choice = months[st.session_state.cal_month_idx]
month_opts = months

def _cal_prev():
    if st.session_state.cal_month_idx > 0:
        st.session_state.cal_month_idx -= 1

def _cal_next():
    if st.session_state.cal_month_idx < len(month_opts) - 1:
        st.session_state.cal_month_idx += 1

c_prev, c_mid, c_next = st.columns([1, 3, 1])

with c_prev:
    st.button("◀ Prev", width="stretch", on_click=_cal_prev)

with c_mid:
    st.markdown(
        f"<div style='text-align:center; font-size:18px; font-weight:600;'>"
        f"{month_opts[st.session_state.cal_month_idx]:%b %Y}"
        f"</div>",
        unsafe_allow_html=True,
    )

with c_next:
    st.button("Next ▶", width="stretch", on_click=_cal_next)

month_choice = month_opts[st.session_state.cal_month_idx]

fig = calendar_heatmap_month(days, month_choice)
if fig is None:
        st.info("No daily data for this month.")
else:
        st.plotly_chart(fig, width="stretch")

def build_month_metrics(df: pd.DataFrame, prefix: str) -> pd.DataFrame:
    d = df.copy()
    d["stay_month"] = pd.to_datetime(d["stay_month"]).dt.to_period("M").dt.to_timestamp()

    for c in ["accom_rev", "occupied_rooms", "total_rooms"]:
        d[c] = pd.to_numeric(d[c], errors="coerce").fillna(0)

    d[f"{prefix}_occ_pct"] = d.apply(
        lambda r: (r["occupied_rooms"] / r["total_rooms"] * 100.0) if r["total_rooms"] > 0 else 0.0,
        axis=1
    )
    d[f"{prefix}_adr"] = d.apply(
        lambda r: (r["accom_rev"] / r["occupied_rooms"]) if r["occupied_rooms"] > 0 else 0.0,
        axis=1
    )
    d[f"{prefix}_revpar"] = d[f"{prefix}_adr"] * (d[f"{prefix}_occ_pct"] / 100.0)

    out = d[["stay_month", "accom_rev", f"{prefix}_occ_pct", f"{prefix}_adr", f"{prefix}_revpar"]].copy()
    out = out.rename(columns={"accom_rev": f"{prefix}_rev"})
    return out


# --- Current months in view (from latest snapshot) ---
cur_view = cur_df[cur_df["stay_month"].isin(selected_months)].copy()
cur_metrics = build_month_metrics(cur_view.rename(columns={
    "accom_rev": "accom_rev",
    "occupied_rooms": "occupied_rooms",
    "total_rooms": "total_rooms",
}), "cur")

compare_mode = st.radio("Compare against", ["Budget", "Last Year"], horizontal=True)

# Load baseline based on toggle
if compare_mode == "Budget":
    base_label = "Budget"
    base_raw = db_load_budget_baseline(conn, hotel)
    base_metrics = build_month_metrics(base_raw, "base")

    cur_metrics = cur_metrics.copy()
    cur_metrics["key_month"] = cur_metrics["stay_month"]

    base_metrics = base_metrics.copy().rename(columns={"stay_month": "key_month"})
    merged_cmp = cur_metrics.merge(base_metrics, on="key_month", how="left").sort_values("stay_month")

else:
    base_label = "Last year"
    base_raw = db_load_yoy_baseline(conn, hotel)
    base_metrics = build_month_metrics(base_raw, "base")

    cur_metrics = cur_metrics.copy()
    cur_metrics["key_month"] = cur_metrics["stay_month"] - pd.DateOffset(years=1)

    base_metrics = base_metrics.copy().rename(columns={"stay_month": "key_month"})
    merged_cmp = cur_metrics.merge(base_metrics, on="key_month", how="left").sort_values("stay_month")

merged_cmp["Month"] = merged_cmp["stay_month"].dt.strftime("%b %Y")

# Missing baseline warning (super useful)
missing = merged_cmp[merged_cmp["base_rev"].isna()]["Month"].tolist()
if missing:
    st.warning(f"Missing {base_label} baseline rows for: {', '.join(missing)}")


# deltas for styling
for metric in ["rev", "occ_pct", "adr", "revpar"]:
    merged_cmp[f"__{metric}_delta"] = (
        pd.to_numeric(merged_cmp.get(f"cur_{metric}"), errors="coerce").fillna(0.0)
        - pd.to_numeric(merged_cmp.get(f"base_{metric}"), errors="coerce").fillna(0.0)
    )

yoy_display = pd.DataFrame({
    "Month": merged_cmp["Month"],

    "Accom rev (current)": merged_cmp["cur_rev"].apply(fmt_currency),
    "Occ % (current)": merged_cmp["cur_occ_pct"].apply(lambda x: f"{x:.1f}%"),
    "ADR (current)": merged_cmp["cur_adr"].apply(fmt_currency),
    "RevPAR (current)": merged_cmp["cur_revpar"].apply(fmt_currency),

    f"Accom rev ({base_label})": merged_cmp["base_rev"].apply(lambda x: "" if pd.isna(x) else fmt_currency(x)),
    f"Occ % ({base_label})": merged_cmp["base_occ_pct"].apply(lambda x: "" if pd.isna(x) else f"{x:.1f}%"),
    f"ADR ({base_label})": merged_cmp["base_adr"].apply(lambda x: "" if pd.isna(x) else fmt_currency(x)),
    f"RevPAR ({base_label})": merged_cmp["base_revpar"].apply(lambda x: "" if pd.isna(x) else fmt_currency(x)),
})

def yoy_colour_cell(v):
    if pd.isna(v) or v == 0:
        return ""
    if v > 0:
        return f"background-color: {BRAND['good_bg']}; color: {BRAND['good_txt']}; font-weight: 600;"
    return f"background-color: {BRAND['bad_bg']}; color: {BRAND['bad_txt']}; font-weight: 600;"

def style_yoy_row(row):
    src = merged_cmp.loc[row.name]
  # use the same row index
    styles = [""] * len(row)

    col_to_delta = {
        "Accom rev (current)": "__rev_delta",
        "Occ % (current)": "__occ_pct_delta",
        "ADR (current)": "__adr_delta",
        "RevPAR (current)": "__revpar_delta",
    }

    for col_name, delta_col in col_to_delta.items():
        if col_name in row.index:
            styles[row.index.get_loc(col_name)] = yoy_colour_cell(src.get(delta_col, 0.0))

    return styles

styled_yoy = (
    yoy_display.style
      .apply(style_yoy_row, axis=1)
      .set_properties(**{"text-align": "center"})
      .set_table_styles([{"selector": "th", "props": [("text-align", "center")]}])
)

sl_section("Benchmarks", "Month comparison — current vs Budget / Last Year")
st.dataframe(styled_yoy, width="stretch", height=320, key="yoy_month_table")

# ── Monthly Comparison Chart ──────────────────────────────────────────────────
sl_section("Trends", "Monthly comparison chart")

metric_options = {
    "Accommodation Revenue": ("cur_rev",     "base_rev",     "R"),
    "Occupancy %":           ("cur_occ_pct", "base_occ_pct", "%"),
    "ADR":                   ("cur_adr",     "base_adr",     "R"),
    "RevPAR":                ("cur_revpar",  "base_revpar",  "R"),
}

chart_metric = st.selectbox(
    "Select metric",
    list(metric_options.keys()),
    key="chart_metric_select",
)

cur_col, base_col, prefix = metric_options[chart_metric]

chart_df = merged_cmp[["Month", cur_col, base_col]].copy()
chart_df[cur_col]  = pd.to_numeric(chart_df[cur_col],  errors="coerce").fillna(0)
chart_df[base_col] = pd.to_numeric(chart_df[base_col], errors="coerce").fillna(0)
chart_df["variance"] = chart_df[cur_col] - chart_df[base_col]
chart_df["var_pct"]  = chart_df.apply(
    lambda r: (r["variance"] / r[base_col] * 100) if r[base_col] != 0 else 0, axis=1
)

def _fmt_val(v, pfx):
    if pfx == "R":
        return fmt_rand0(v)
    return f"{v:.1f}%"

hover_cur  = [_fmt_val(v, prefix) for v in chart_df[cur_col]]
hover_base = [_fmt_val(v, prefix) for v in chart_df[base_col]]
hover_var  = [
    f"{'+' if v >= 0 else ''}{_fmt_val(v, prefix)}  ({r:+.1f}%)"
    for v, r in zip(chart_df["variance"], chart_df["var_pct"])
]

fig_cmp = go.Figure()

fig_cmp.add_trace(go.Bar(
    name="Current OTB",
    x=chart_df["Month"],
    y=chart_df[cur_col],
    text=hover_cur,
    textposition="outside",
    marker_color=BRAND["sage"],
    hovertemplate="<b>%{x}</b><br>Current: %{text}<extra></extra>",
    offsetgroup=0,
))

fig_cmp.add_trace(go.Bar(
    name=base_label,
    x=chart_df["Month"],
    y=chart_df[base_col],
    text=hover_base,
    textposition="outside",
    marker_color=BRAND["clay_soft"],
    hovertemplate=f"<b>%{{x}}</b><br>{base_label}: %{{text}}<extra></extra>",
    offsetgroup=1,
))

fig_cmp.add_trace(go.Scatter(
    name="Variance",
    x=chart_df["Month"],
    y=chart_df["variance"],
    mode="lines+markers+text",
    text=[f"{'+' if v >= 0 else ''}{r:.1f}%" for v, r in zip(chart_df["variance"], chart_df["var_pct"])],
    textposition="top center",
    line=dict(color=BRAND["rust"], width=2, dash="dot"),
    marker=dict(size=7, color=BRAND["rust"]),
    yaxis="y2",
    hovertemplate="<b>%{x}</b><br>Variance: %{customdata}<extra></extra>",
    customdata=hover_var,
))

y_title = f"{chart_metric} (ZAR)" if prefix == "R" else chart_metric
var_title = f"Variance ({prefix})"

fig_cmp.update_layout(
    barmode="group",
    height=420,
    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    yaxis=dict(
        title=y_title,
        tickprefix="R" if prefix == "R" else "",
        ticksuffix="" if prefix == "R" else "%",
        showgrid=True,
        gridcolor="rgba(12,28,31,0.10)",
    ),
    yaxis2=dict(
        title=var_title,
        overlaying="y",
        side="right",
        showgrid=False,
        tickprefix="R" if prefix == "R" else "",
        ticksuffix="" if prefix == "R" else "%",
        zeroline=True,
        zerolinecolor="rgba(156,74,50,0.35)",
    ),
    plot_bgcolor="rgba(0,0,0,0)",
    paper_bgcolor="rgba(0,0,0,0)",
    font=PLOTLY_FONT,
    margin=dict(t=40, b=40),
    xaxis=dict(tickangle=-30),
    bargap=0.25,
)
brand_plotly(fig_cmp)

st.plotly_chart(fig_cmp, width="stretch")
# ── End Monthly Comparison Chart ──────────────────────────────────────────────

# ══════════════════════════════════════════════════════════════════════════════
#  SAME TIME LAST YEAR — OTB PACE
#  Compares this snapshot's month-by-month on-the-books against what was on the
#  books at (approximately) the same lead time last year.
# ══════════════════════════════════════════════════════════════════════════════
pace_tbl = None
pace_all = db_load_weekly_pace(conn, hotel)

if pace_all.empty:
    pass   # no STLY data for Angala yet: the whole section stays hidden (see ANGALA_PACE_TEXT)
else:
    sl_section("Pace", "Same time last year — OTB pace")

    pace_all["report_date"] = pd.to_datetime(pace_all["report_date"])
    pace_all["stay_month"] = pd.to_datetime(pace_all["stay_month"])
    pace = pace_all[pace_all["segment"] == PACE_SEGMENT].copy()
    if pace.empty:
        pace = pace_all.copy()

    cur_dt = pd.to_datetime(cur_meta.get("report_datetime"))
    target_dt = cur_dt - pd.DateOffset(years=1)
    rep_dates = sorted(pace["report_date"].unique())
    matched = min(rep_dates, key=lambda d: abs((pd.Timestamp(d) - target_dt).days))
    gap = int((pd.Timestamp(matched) - target_dt).days)
    pace_m = pace[pace["report_date"] == matched].copy()

    st.caption(
        f"Current snapshot: {cur_dt:%d %b %Y} · matched last-year report: "
        f"{pd.Timestamp(matched):%d %b %Y} ({gap:+d} days vs exactly one year prior). "
        f"Last-year figures are what was on the books at that date — the same booking "
        f"lead time, one year ago."
    )
    if abs(gap) > 10:
        st.warning(
            f"The closest stored last-year report is {abs(gap)} days away from the same "
            f"date last year, so this comparison is approximate. Add the weekly figures "
            f"for around {target_dt:%d %b %Y} to tighten it."
        )

    cur_months_df = db_load_months(conn, current_id).copy()
    cur_months_df["stay_month"] = pd.to_datetime(cur_months_df["stay_month"]).dt.to_period("M").dt.to_timestamp()
    for c in ["accom_rev", "occupied_rooms", "total_rooms", "occupancy_pct"]:
        cur_months_df[c] = pd.to_numeric(cur_months_df[c], errors="coerce").fillna(0)

    rows = []
    for _, r in cur_months_df.sort_values("stay_month").iterrows():
        m = r["stay_month"]
        ly = pace_m[pace_m["stay_month"] == m - pd.DateOffset(years=1)]
        ly = ly.iloc[0] if not ly.empty else None
        cur_occ = float(r["occupancy_pct"])
        cur_adr = (r["accom_rev"] / r["occupied_rooms"]) if r["occupied_rooms"] > 0 else None
        cur_rev = float(r["accom_rev"])
        ly_occ = float(ly["occ_pct"]) if ly is not None and pd.notna(ly["occ_pct"]) else None
        ly_adr = float(ly["adr"]) if ly is not None and pd.notna(ly["adr"]) else None
        ly_rev = float(ly["revenue"]) if ly is not None and pd.notna(ly["revenue"]) else None
        rows.append({
            "stay_month": m, "Month": m.strftime("%b %Y"),
            "occ_now": cur_occ, "occ_ly": ly_occ,
            "occ_delta": (cur_occ - ly_occ) if ly_occ is not None else None,
            "adr_now": cur_adr, "adr_ly": ly_adr,
            "adr_delta": (cur_adr - ly_adr) if (cur_adr is not None and ly_adr is not None) else None,
            "adr_delta_pct": ((cur_adr - ly_adr) / ly_adr * 100) if (cur_adr is not None and ly_adr not in (None, 0)) else None,
            "rev_now": cur_rev, "rev_ly": ly_rev,
            "rev_delta": (cur_rev - ly_rev) if ly_rev is not None else None,
        })
    pace_tbl = pd.DataFrame(rows)

    if pace_tbl.empty or pace_tbl[["occ_ly", "adr_ly", "rev_ly"]].isna().all().all():
        st.warning("The matched last-year report has no months overlapping this snapshot's "
                   "window. Add weekly figures covering the equivalent months last year.")
    else:
        pace_metric = st.radio("Pace metric", ["Occupancy %", "ADR", "Revenue"],
                               horizontal=True, key="pace_metric_radio")
        _pmap = {"Occupancy %": ("occ_now", "occ_ly", "%"),
                 "ADR": ("adr_now", "adr_ly", "R"),
                 "Revenue": ("rev_now", "rev_ly", "R")}
        now_c, ly_c, pfx = _pmap[pace_metric]

        fig_pace = go.Figure()
        fig_pace.add_trace(go.Bar(name="Now (current snapshot)", x=pace_tbl["Month"],
                                  y=pace_tbl[now_c], marker_color=BRAND["sage"]))
        fig_pace.add_trace(go.Bar(name=f"Same time last year ({pd.Timestamp(matched):%d %b %y})",
                                  x=pace_tbl["Month"], y=pace_tbl[ly_c],
                                  marker_color=BRAND["clay_soft"]))
        fig_pace.update_layout(
            barmode="group", height=380, bargap=0.25,
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
            yaxis=dict(title=pace_metric,
                       tickprefix="R" if pfx == "R" else None,
                       ticksuffix="%" if pfx == "%" else None,
                       gridcolor="rgba(12,28,31,0.10)"),
            margin=dict(t=40, b=30),
        )
        brand_plotly(fig_pace)
        st.plotly_chart(fig_pace, width="stretch")

        def _pct(v):
            return f"{v:.1f}%".replace(".", ",") if pd.notna(v) else "—"

        disp = pd.DataFrame({
            "Month": pace_tbl["Month"],
            "Occ now": pace_tbl["occ_now"].map(_pct),
            "Occ LY pace": pace_tbl["occ_ly"].map(_pct),
            "Occ % change": pace_tbl["occ_delta"].map(lambda v: f"{v:+.1f}%".replace(".", ",") if pd.notna(v) else "—"),
            "ADR now": pace_tbl["adr_now"].map(lambda v: fmt_rand0(v) if pd.notna(v) else "—"),
            "ADR LY pace": pace_tbl["adr_ly"].map(lambda v: fmt_rand0(v) if pd.notna(v) else "—"),
            "ADR % change": pace_tbl["adr_delta_pct"].map(lambda v: f"{v:+.1f}%".replace(".", ",") if pd.notna(v) else "—"),
            "Rev now": pace_tbl["rev_now"].map(lambda v: fmt_rand0(v) if pd.notna(v) else "—"),
            "Rev LY pace": pace_tbl["rev_ly"].map(lambda v: fmt_rand0(v) if pd.notna(v) else "—"),
            "Revenue change": pace_tbl["rev_delta"].map(lambda v: ("+" if v >= 0 else "−") + fmt_currency(abs(v)) if pd.notna(v) else "—"),
        })

        def _pace_style(row):
            src = pace_tbl.iloc[row.name]
            styles = [""] * len(row)
            for col, key in [("Occ % change", "occ_delta"), ("ADR % change", "adr_delta"),
                             ("Revenue change", "rev_delta")]:
                v = src[key]
                if v is None or pd.isna(v) or v == 0:
                    continue
                i = row.index.get_loc(col)
                styles[i] = (f"background-color: {BRAND['good_bg']}; color: {BRAND['good_txt']}; font-weight: 600;"
                             if v > 0 else
                             f"background-color: {BRAND['bad_bg']}; color: {BRAND['bad_txt']}; font-weight: 600;")
            return styles

        st.dataframe(
            disp.style.apply(_pace_style, axis=1)
                .set_properties(**{"text-align": "center"})
                .set_table_styles([{"selector": "th", "props": [("text-align", "center")]}]),
            width="stretch", height=320, key="pace_table",
        )

        with st.expander("How this comparison works", expanded=False):
            st.markdown(
                """
**Pace** compares bookings *at the same lead time*, not final results. For a snapshot
taken on 5 September 2026, the fair comparison is what was on the books around
5 September **2025** — so this section picks the stored weekly figures closest to exactly
one year before the snapshot, and lines each stay month up with the same month last year.

A positive change means you're ahead of last year's pace at the same point; negative means
behind. This differs from *Benchmarks* above, which compares against last year's **final**
result — a month can be behind last year's final (still time to book) yet ahead of last
year's pace (booking faster), which is usually the more useful signal.

**Where the last-year figures come from:** on-the-books positions stored for last year's
stay months, one set per report date.
                """
            )


# ══════════════════════════════════════════════════════════════════════════════
#  GM BRIEF — an email-ready summary
#  Written for general managers rather than revenue managers: plain language, no
#  jargon, every number paired with what it means. Table-based HTML with inline
#  styles only, because that is what Outlook and Gmail keep when it is pasted into
#  a message body. Styled to the Angala fynbos-green palette.
# ══════════════════════════════════════════════════════════════════════════════
_GM = {
    "ink": "#0C1C1F", "soft": "#F6F3EC", "line": "#E6E1D6", "muted": "#5F6A6D",
    "paper": "#FDF8F6", "accent": "#BAAA70",
    "good": "#2F5A3B", "good_bg": "#E6EFE7",
    "bad": "#9C4A32", "bad_bg": "#F7E8E2",
    "flat": "#5F6A6D", "flat_bg": "#EFEDE8",
}


def _gm_pill(text: str, kind: str) -> str:
    fg = {"good": _GM["good"], "bad": _GM["bad"]}.get(kind, _GM["flat"])
    bg = {"good": _GM["good_bg"], "bad": _GM["bad_bg"]}.get(kind, _GM["flat_bg"])
    return (f'<span style="display:inline-block;padding:3px 9px;border-radius:11px;'
            f'background:{bg};color:{fg};font-size:12px;font-weight:700;'
            f'white-space:nowrap;">{text}</span>')


def _gm_kind(v, invert: bool = False) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)) or abs(v) < 1e-9:
        return "flat"
    good = (v < 0) if invert else (v > 0)
    return "good" if good else "bad"


def _gm_pct(cur, base):
    """Percentage difference vs a baseline, or None when there's nothing to compare."""
    try:
        if base in (None, 0) or pd.isna(base) or pd.isna(cur):
            return None
        return (float(cur) - float(base)) / abs(float(base)) * 100.0
    except Exception:
        return None


def build_gm_brief_html(
    property_name: str,
    snapshot_label: str,
    compare_label: str,
    headline: dict,
    movers_up: list,
    movers_down: list,
    month_rows: list,
    pace_rows: list,
) -> str:
    W = 640
    td = "padding:10px 12px;border-bottom:1px solid " + _GM["line"] + ";font-size:13px;"
    th = ("padding:9px 12px;background:" + _GM["ink"] + ";color:#F3EEDF;font-size:11px;"
          "letter-spacing:.06em;text-transform:uppercase;text-align:left;font-weight:600;")

    cards = ""
    for c in headline["cards"]:
        move = ""
        if c.get("delta_text"):
            move = ('<div style="margin-top:5px;">'
                    + _gm_pill(c["delta_text"], c.get("delta_kind", "flat")) + "</div>")
        cards += (
            f'<td width="25%" valign="top" style="padding:14px 10px;background:{_GM["paper"]};'
            f'border:1px solid {_GM["line"]};border-radius:10px;">'
            f'<div style="font-size:10.5px;letter-spacing:.07em;text-transform:uppercase;'
            f'color:{_GM["muted"]};font-weight:700;">{c["label"]}</div>'
            f'<div style="font-size:22px;color:{_GM["ink"]};font-weight:700;'
            f'font-family:Georgia,serif;margin-top:4px;">{c["value"]}</div>'
            f'{move}</td><td width="10"></td>'
        )

    def movers_table(title, rows, kind):
        if not rows:
            return (f'<div style="{td}color:{_GM["muted"]};border:0;">'
                    f'No material movement to report.</div>')
        body = ""
        for r in rows:
            body += (
                f'<tr><td style="{td}">{r["date"]}</td>'
                f'<td style="{td}text-align:right;">{_gm_pill(r["rev"], kind)}</td>'
                f'<td style="{td}text-align:right;color:{_GM["muted"]};">{r["rooms"]}</td></tr>'
            )
        return (
            f'<table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;'
            f'background:{_GM["paper"]};border:1px solid {_GM["line"]};border-radius:10px;">'
            f'<tr><th style="{th}">{title}</th><th style="{th}text-align:right;">Revenue</th>'
            f'<th style="{th}text-align:right;">Rooms</th></tr>{body}</table>'
        )

    # Benchmark columns only appear once there is a budget / last-year figure to compare with.
    show_bud = any(r.get("vs_budget_kind") != "flat" or r.get("vs_budget") not in ("no budget",) for r in month_rows)
    show_ly = any(r.get("vs_ly") != "no data" for r in month_rows)
    show_prov = any(r.get("prov") for r in month_rows)
    month_body = ""
    for r in month_rows:
        month_body += (
            f'<tr><td style="{td}font-weight:600;color:{_GM["ink"]};">{r["month"]}</td>'
            f'<td style="{td}text-align:right;">{r["otb"]}</td>'
            + (f'<td style="{td}text-align:right;color:{_GM["muted"]};">{r.get("prov") or "—"}</td>' if show_prov else "")
            + f'<td style="{td}text-align:right;color:{_GM["muted"]};">{r["occ"]}</td>'
            f'<td style="{td}text-align:right;color:{_GM["muted"]};">{r.get("adr", "")}</td>'
            + (f'<td style="{td}text-align:right;">{_gm_pill(r["vs_budget"], r["vs_budget_kind"])}</td>' if show_bud else "")
            + (f'<td style="{td}text-align:right;">{_gm_pill(r["vs_ly"], r["vs_ly_kind"])}</td>' if show_ly else "")
            + '</tr>'
        )
    month_table = (
        f'<table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;'
        f'background:{_GM["paper"]};border:1px solid {_GM["line"]};border-radius:10px;">'
        f'<tr><th style="{th}">Month</th><th style="{th}text-align:right;">On the books</th>'
        + (f'<th style="{th}text-align:right;">of which provisional</th>' if show_prov else "")
        + f'<th style="{th}text-align:right;">Occupancy</th>'
        f'<th style="{th}text-align:right;">Avg rate</th>'
        + (f'<th style="{th}text-align:right;">vs Budget</th>' if show_bud else "")
        + (f'<th style="{th}text-align:right;">vs Last year</th>' if show_ly else "")
        + f'</tr>{month_body}</table>'
    )
    month_sub = "Revenue already booked for each month"
    if show_bud and show_ly:
        month_sub += ", against budget and against last year's final result."
    elif show_bud:
        month_sub += ", against budget."
    elif show_ly:
        month_sub += ", against last year's final result."
    else:
        month_sub += "."
    if show_prov:
        month_sub += " Provisional bookings are included but not yet confirmed."

    pace_block = ""
    if pace_rows:
        pb = ""
        for r in pace_rows:
            pb += (
                f'<tr><td style="{td}font-weight:600;color:{_GM["ink"]};">{r["month"]}</td>'
                f'<td style="{td}text-align:right;">{r["now"]}</td>'
                f'<td style="{td}text-align:right;color:{_GM["muted"]};">{r["then"]}</td>'
                f'<td style="{td}text-align:right;">{_gm_pill(r["delta"], r["kind"])}</td></tr>'
            )
        pace_block = (
            f'<tr><td style="padding:22px 0 8px 0;font-family:Georgia,serif;font-size:17px;'
            f'color:{_GM["ink"]};font-weight:700;">Are we ahead of last year?</td></tr>'
            f'<tr><td style="padding-bottom:6px;font-size:12.5px;color:{_GM["muted"]};">'
            f'Comparing bookings held now against bookings held at the same point last year — '
            f'not against how last year finished.</td></tr>'
            f'<tr><td><table width="100%" cellpadding="0" cellspacing="0" '
            f'style="border-collapse:collapse;background:{_GM["paper"]};'
            f'border:1px solid {_GM["line"]};border-radius:10px;">'
            f'<tr><th style="{th}">Month</th><th style="{th}text-align:right;">Booked now</th>'
            f'<th style="{th}text-align:right;">Same time last year</th>'
            f'<th style="{th}text-align:right;">Difference</th></tr>{pb}</table></td></tr>'
        )

    return f"""<div style="margin:0;padding:0;background:#F1EEE7;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#F1EEE7;padding:20px 0;">
<tr><td align="center">
<table width="{W}" cellpadding="0" cellspacing="0" style="width:{W}px;max-width:100%;
 font-family:Arial,Helvetica,sans-serif;color:{_GM['ink']};">

  <tr><td style="background:{_GM['ink']};padding:22px 24px;border-radius:12px 12px 0 0;">
    <div style="font-size:10.5px;letter-spacing:.22em;text-transform:uppercase;
                color:{_GM['accent']};font-weight:700;">Revenue Snapshot</div>
    <div style="font-family:Georgia,serif;font-size:25px;color:#FFFFFF;margin-top:5px;">
      {property_name}</div>
    <div style="font-size:12.5px;color:#C9D0D1;margin-top:6px;">
      Position as at {snapshot_label} &nbsp;·&nbsp; compared with {compare_label}</div>
  </td></tr>

  <tr><td style="background:#FFFFFF;padding:20px 24px;border-radius:0 0 12px 12px;">

    <div style="font-family:Georgia,serif;font-size:17px;font-weight:700;margin-bottom:4px;">
      {headline['title']}</div>
    <div style="font-size:12.5px;color:{_GM['muted']};margin-bottom:14px;">
      {headline['subtitle']}</div>

    <table width="100%" cellpadding="0" cellspacing="0"><tr>{cards}</tr></table>

    <table width="100%" cellpadding="0" cellspacing="0">
      <tr><td style="padding:22px 0 8px 0;font-family:Georgia,serif;font-size:17px;font-weight:700;">
        How the coming months are tracking</td></tr>
      <tr><td style="padding-bottom:10px;font-size:12.5px;color:{_GM['muted']};">
        {month_sub}</td></tr>
      <tr><td>{month_table}</td></tr>

      {pace_block}

      <tr><td style="padding:22px 0 8px 0;font-family:Georgia,serif;font-size:17px;font-weight:700;">
        What changed since the last report</td></tr>
      <tr><td style="padding-bottom:10px;font-size:12.5px;color:{_GM['muted']};">
        The stay dates that gained or lost the most revenue since {compare_label}.</td></tr>
      <tr><td>{movers_table("Biggest gains", movers_up, "good")}</td></tr>
      <tr><td height="12"></td></tr>
      <tr><td>{movers_table("Biggest drops", movers_down, "bad")}</td></tr>
    </table>

    <div style="margin-top:24px;padding-top:14px;border-top:1px solid {_GM['line']};
                font-size:11.5px;color:{_GM['muted']};line-height:1.7;">
      <b style="color:{_GM['ink']};">Reading this report</b><br>
      <b>On the books</b> — revenue already booked for that month (confirmed and provisional). It will keep growing as
      more bookings come in, so a month that looks short today is not necessarily short.<br>
      <b>Of which provisional</b> — held but not yet confirmed; this can still fall away.<br>
      <b>Occupancy</b> — the share of rooms sold for the month so far.<br>
      <b>Average room rate</b> — average revenue per room sold, excluding VAT.<br>
      Figures exclude VAT. Questions about anything here — reply to this mail.
    </div>

  </td></tr>
</table>
</td></tr></table>
</div>"""


# The GM brief is management-only: nothing about it renders — heading included —
# until the Admin PIN is entered, so anyone else sees the dashboard end at Exports.
if is_admin:
    sl_section("Report", "GM brief — email-ready summary")

    st.markdown(
        f"""
        <div style="background:{BRAND['ivory']}; border:1px solid {BRAND['line']};
                    border-left:4px solid {BRAND['clay']}; border-radius:10px;
                    padding:.85rem 1.2rem; font-size:.9rem; color:{BRAND['muted']}; margin-bottom:.9rem;">
            A plain-language summary written for general managers. Preview it below, then
            copy it as an image straight into an email.
        </div>
        """,
        unsafe_allow_html=True,
    )

    _gm_top_n = st.slider("How many gain/drop dates to include", 3, 8, 3, 1, key="gm_top_n")

    _gm_cards = []
    if not cur_month_df.empty:
        _cur_adr = (cur_m_rev / cur_m_occ) if cur_m_occ else 0.0
        _prev_adr = (prev_m_rev / prev_m_occ) if prev_m_occ else 0.0
        _rev_move = cur_m_rev - prev_m_rev
        _occ_move = int(cur_m_occ - prev_m_occ)
        _adr_move = _cur_adr - _prev_adr
        _gm_cards = [
            {"label": "Revenue on the books", "value": fmt_rand0(cur_m_rev),
             "delta_text": f"{'+' if _rev_move >= 0 else '−'}{fmt_rand0(abs(_rev_move))} since last report",
             "delta_kind": _gm_kind(_rev_move)},
            {"label": "Rooms sold", "value": fmt_int(cur_m_occ),
             "delta_text": f"{_occ_move:+d} rooms", "delta_kind": _gm_kind(_occ_move)},
            {"label": "Occupancy", "value": f"{cur_m_occ_pct:.1f}%".replace(".", ","),
             "delta_text": f"{(cur_m_occ_pct - prev_m_occ_pct):+.1f} pts".replace(".", ","),
             "delta_kind": _gm_kind(cur_m_occ_pct - prev_m_occ_pct)},
            {"label": "Average room rate", "value": fmt_rand0(_cur_adr),
             "delta_text": f"{'+' if _adr_move >= 0 else '−'}{fmt_rand0(abs(_adr_move))}",
             "delta_kind": _gm_kind(_adr_move)},
        ]

    _gm_up, _gm_down = [], []
    if gains is not None and not gains.empty:
        for _, r in gains.nlargest(_gm_top_n, "rev_delta").iterrows():
            _gm_up.append({"date": r["stay_date_lbl"],
                           "rev": "+" + fmt_rand0(abs(r["rev_delta"])),
                           "rooms": f"{r['occ_rooms_delta']:+.0f} rooms"})
    if drops is not None and not drops.empty:
        for _, r in drops.nsmallest(_gm_top_n, "rev_delta").iterrows():
            _gm_down.append({"date": r["stay_date_lbl"],
                             "rev": "−" + fmt_rand0(abs(r["rev_delta"])),
                             "rooms": f"{r['occ_rooms_delta']:+.0f} rooms"})

    _bud_raw = db_load_budget_baseline(conn, hotel)
    _yoy_raw = db_load_yoy_baseline(conn, hotel)
    _bud_m = build_month_metrics(_bud_raw, "bud") if not _bud_raw.empty else pd.DataFrame()
    _yoy_m = build_month_metrics(_yoy_raw, "ly") if not _yoy_raw.empty else pd.DataFrame()
    if not _yoy_m.empty:
        _yoy_m = _yoy_m.copy()
        _yoy_m["stay_month"] = _yoy_m["stay_month"] + pd.DateOffset(years=1)

    _prov_by_m = dict(zip(cur_df["stay_month"], pd.to_numeric(cur_df.get("prov_rev", 0), errors="coerce").fillna(0)))
    _gm_months = []
    for _, r in cur_metrics.sort_values("stay_month").iterrows():
        _m = r["stay_month"]
        _prov = float(_prov_by_m.get(_m, 0.0) or 0.0)
        _bud = _bud_m[_bud_m["stay_month"] == _m]["bud_rev"] if not _bud_m.empty else pd.Series(dtype=float)
        _ly = _yoy_m[_yoy_m["stay_month"] == _m]["ly_rev"] if not _yoy_m.empty else pd.Series(dtype=float)
        _bud_v = float(_bud.iloc[0]) if len(_bud) else None
        _ly_v = float(_ly.iloc[0]) if len(_ly) else None
        _pb = _gm_pct(r["cur_rev"], _bud_v)
        _pl = _gm_pct(r["cur_rev"], _ly_v)
        _gm_months.append({
            "month": _m.strftime("%b %Y"),
            "otb": fmt_rand0(r["cur_rev"]),
            "occ": f"{r['cur_occ_pct']:.0f}%",
            "adr": fmt_rand0(r["cur_adr"]) if r["cur_adr"] else "—",
            "prov": fmt_rand0(_prov) if _prov > 0 else "",
            "vs_budget": "no budget" if _pb is None else f"{_pb:+.0f}%",
            "vs_budget_kind": "flat" if _pb is None else _gm_kind(_pb),
            "vs_ly": "no data" if _pl is None else f"{_pl:+.0f}%",
            "vs_ly_kind": "flat" if _pl is None else _gm_kind(_pl),
        })

    # Populated from the pace section above. If no last-year figures match this
    # snapshot's months, the "ahead of last year" block is simply omitted.
    _gm_pace = []
    try:
        if "pace_tbl" in dir() and pace_tbl is not None and not pace_tbl.empty:
            for _, r in pace_tbl.head(6).iterrows():
                _has_ly = pd.notna(r.get("rev_ly"))
                _d = r["rev_delta"] if _has_ly else None
                _gm_pace.append({
                    "month": r["Month"],
                    "now": fmt_rand0(r["rev_now"]),
                    "then": fmt_rand0(r["rev_ly"]) if _has_ly else "—",
                    "delta": (("+" if _d >= 0 else "−") + fmt_rand0(abs(_d))) if _has_ly else "no data",
                    "kind": _gm_kind(_d) if _has_ly else "flat",
                })
    except Exception:
        _gm_pace = []

    _gm_html = build_gm_brief_html(
        property_name="Angala Boutique Hotel",
        snapshot_label=(f"{pd.Timestamp(cur_meta['report_datetime']):%-d %b %Y, %H:%M}"
                        if cur_meta.get("report_datetime") else "latest snapshot"),
        compare_label=(f"{pd.Timestamp(prev_meta['report_datetime']):%-d %b %Y, %H:%M}"
                       if prev_meta.get("report_datetime") else "the previous snapshot"),
        headline={
            "title": f"Where we stand for {month_lbl}" if not cur_month_df.empty else "Current position",
            "subtitle": "Business on the books for the current month, and how it has moved since the last report.",
            "cards": _gm_cards,
        },
        movers_up=_gm_up,
        movers_down=_gm_down,
        month_rows=_gm_months,
        pace_rows=_gm_pace,
    )

    _GM_TOOLBAR = """
<div style="font-family:Arial,Helvetica,sans-serif;margin:0 0 12px 0;display:flex;
            gap:8px;align-items:center;flex-wrap:wrap;">
  <button id="gmCopy" style="background:#0C1C1F;color:#fff;border:1px solid #0C1C1F;
    border-radius:999px;padding:9px 18px;font-size:13px;font-weight:700;cursor:pointer;">
    Copy as image</button>
  <button id="gmDl" style="background:#fff;color:#0C1C1F;border:1px solid #0C1C1F;
    border-radius:999px;padding:9px 18px;font-size:13px;font-weight:700;cursor:pointer;">
    Download image (PNG)</button>
  <span id="gmStatus" style="font-size:12.5px;color:#5F6A6D;"></span>
</div>
<div id="gmCapture">__BRIEF__</div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/html2canvas/1.4.1/html2canvas.min.js"></script>
<script>
(function () {
  var status = document.getElementById('gmStatus');
  function say(t) { status.textContent = t; }
  function render() {
    return html2canvas(document.getElementById('gmCapture'), {
      scale: 2, backgroundColor: '#F1EEE7', useCORS: true, logging: false
    });
  }
  function toBlob(canvas) {
    return new Promise(function (res) { canvas.toBlob(res, 'image/png'); });
  }
  function download(blob) {
    var a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'gm_brief_angala.png';
    document.body.appendChild(a); a.click(); a.remove();
  }
  document.getElementById('gmCopy').onclick = function () {
    say('Rendering image…');
    render().then(toBlob).then(function (blob) {
      if (!navigator.clipboard || !window.ClipboardItem) throw new Error('no clipboard');
      return navigator.clipboard.write([new ClipboardItem({ 'image/png': blob })])
        .then(function () { say('Copied. Paste straight into your email.'); })
        .catch(function () {
          download(blob);
          say('Clipboard blocked here — image downloaded instead, insert it into the mail.');
        });
    }).catch(function (e) { say('Could not render image: ' + e.message); });
  };
  document.getElementById('gmDl').onclick = function () {
    say('Rendering image…');
    render().then(toBlob).then(function (blob) {
      download(blob); say('Image downloaded.');
    }).catch(function (e) { say('Could not render image: ' + e.message); });
  };
})();
</script>
"""
    st.components.v1.html(_GM_TOOLBAR.replace("__BRIEF__", _gm_html), height=1240, scrolling=True)

    _c1, _c2 = st.columns([1, 1])
    with _c1:
        st.download_button(
            "Download brief (.html)",
            data=_gm_html.encode("utf-8"),
            file_name=f"gm_brief_angala_{pd.Timestamp.today():%Y%m%d}.html",
            mime="text/html",
            width="stretch",
        )
    with _c2:
        with st.popover("How to email this", width="stretch"):
            st.markdown(
                """
    **Best for Outlook — send it as a picture.** Click **Copy as image** above, then paste
    into your email. If your browser blocks copying from an embedded frame, the image
    downloads instead — insert it with *Insert → Pictures*.

    Why a picture: Outlook's dark mode flips background colours without flipping the text,
    which gives dark text on a dark panel. An image can't be restyled, so every recipient
    sees it exactly as it looks here.

    **If you'd rather send live text** (selectable, better for screen readers): download the
    .html, open it in your browser, select all, copy, and paste.

    **One caveat with images:** they don't reflow on small phone screens and some recipients
    block images by default — worth a one-line summary in the mail body above the picture.
                """
            )

    with st.expander("Raw HTML (for mail-merge or newsletter tools)"):
        st.code(_gm_html, language="html")


sl_section("Exports", "Downloads")
csv_delta = out.to_csv(index=False).encode("utf-8")
st.download_button("Download delta table (CSV)", data=csv_delta, file_name="otb_delta.csv", mime="text/csv")

history = pd.read_sql_query("""
    SELECT s.snapshot_id, s.hotel, s.report_datetime, s.period_start, s.period_end, m.stay_month,
           m.occupied_rooms, m.occupancy_pct, m.accom_rev AS accom_rev
    FROM snapshots s
    JOIN month_metrics m ON m.snapshot_id = s.snapshot_id
    ORDER BY datetime(s.report_datetime) DESC, m.stay_month
""", conn)
csv_hist = history.to_csv(index=False).encode("utf-8")
st.download_button("Download snapshot history (CSV)", data=csv_hist, file_name="otb_snapshot_history.csv", mime="text/csv")

st.caption("Tip: Keep your reporting window fixed to full months for the cleanest day-on-day deltas (accommodation revenue).")

_render_footer()
