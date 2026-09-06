#!/usr/bin/env python3
"""
Endurance Analyst — visual report.

Reads a Garmin Connect "Activities.csv" export and writes a single
self-contained HTML file: fitness and form curve, training calendar,
race projections with explicit guard rails, efficiency trends and
volume history. No network, no chart library, no external assets.

Usage:
    python report.py Activities.csv
    python report.py Activities.csv -o 2026-season.html --today 2026-09-04
    python report.py Activities.csv --max-hr 188 --rest-hr 48
"""

import argparse
import html
import math
import sys
from datetime import date

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Constants. Kept together so an athlete can tune them without hunting.
# ---------------------------------------------------------------------------

NULL_TOKENS = ["--", "", "nan"]
MIN_SESSION_MINUTES = 2.0        # below this it is an accidental start
IMPLAUSIBLE_HR_CEILING = 95.0    # sustained work under this means strap failure
CTL_DAYS = 42                    # chronic load time constant
ATL_DAYS = 7                     # acute load time constant
RIEGEL_EXPONENT = 1.06
DEFAULT_REST_HR = 52.0

RUN_TARGETS = [("5K", 5.0), ("10K", 10.0), ("Half", 21.0975), ("Marathon", 42.195)]

# Colour tokens, mirrored in CSS. Charts need them in Python too.
C = {
    "ground": "#0B1418", "panel": "#101D22", "rule": "#1E343B",
    "ink": "#E6EFEC", "dim": "#7D9BA1", "faint": "#3A5960",
    "fitness": "#4FB8B0", "fatigue": "#E0705C", "form": "#EFC15A",
    "alert": "#C25470", "run": "#EFC15A", "bike": "#4FB8B0",
    "walk": "#5B7F94", "hike": "#8C7CB0", "other": "#4A6670",
}

SPORT_COLOURS = ["#4FB8B0", "#EFC15A", "#5B7F94", "#8C7CB0", "#B0685A", "#6FA36B"]

# Recognisable sports keep a fixed colour across every chart in the report,
# so the eye can carry a sport from the calendar to the volume bars.
SPORT_ALIASES = [
    (("run", "treadmill"), C["run"]),
    (("cycl", "bik", "ride", "spin"), C["bike"]),
    (("walk",), C["walk"]),
    (("hik", "trek"), C["hike"]),
    (("swim",), "#6FA36B"),
]


def sport_colours(names):
    out, spare = {}, list(SPORT_COLOURS)
    for name in names:
        low = str(name).lower()
        match = next((c for keys, c in SPORT_ALIASES if any(k in low for k in keys)),
                     None)
        if match and match not in out.values():
            out[name] = match
    for name in names:
        if name not in out:
            out[name] = next((c for c in spare if c not in out.values()),
                             C["other"])
    return out


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def parse_duration(value):
    """Garmin durations are 'HH:MM:SS' or 'MM:SS'. Returns minutes."""
    try:
        parts = [float(x) for x in str(value).split(":")]
    except (ValueError, AttributeError):
        return np.nan
    if len(parts) == 3:
        return parts[0] * 60 + parts[1] + parts[2] / 60
    if len(parts) == 2:
        return parts[0] + parts[1] / 60
    return np.nan


def parse_pace_to_minutes(value):
    """Running 'Avg Speed' is min/km written as 'M:SS'. Returns decimal minutes."""
    try:
        parts = str(value).split(":")
        return int(parts[0]) + float(parts[1]) / 60
    except (ValueError, IndexError):
        return np.nan


def numeric(series):
    """Coerce a Garmin column to float. '--' is null; '1,024' is one thousand."""
    cleaned = series.astype(str).str.replace(",", "", regex=False)
    return pd.to_numeric(cleaned.replace(NULL_TOKENS, np.nan), errors="coerce")


def load(path):
    df = pd.read_csv(path)
    if "Date" not in df.columns or "Activity Type" not in df.columns:
        sys.exit("error: this does not look like a Garmin Activities.csv export")

    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    df = df[df.Date.notna()].copy()
    df["day"] = df.Date.dt.normalize()
    df["minutes"] = df["Time"].apply(parse_duration) if "Time" in df else np.nan

    for src, dst in [("Distance", "distance"), ("Avg HR", "avg_hr"),
                     ("Max HR", "max_hr"), ("Aerobic TE", "aerobic_te"),
                     ("Total Ascent", "ascent"), ("Calories", "calories")]:
        df[dst] = numeric(df[src]) if src in df.columns else np.nan

    # 'Avg Speed' is unit-ambiguous: km/h for cycling, min/km pace for running.
    is_run = df["Activity Type"].str.contains("Run", case=False, na=False)
    df["is_run"] = is_run
    if "Avg Speed" in df.columns:
        df["speed_kmh"] = np.where(is_run, np.nan, numeric(df["Avg Speed"]))
        df["pace_min_km"] = np.where(
            is_run, df["Avg Speed"].apply(parse_pace_to_minutes), np.nan)
    else:
        df["speed_kmh"] = np.nan
        df["pace_min_km"] = np.nan

    # Fall back to pace derived from distance and time where the column is absent
    # or unparseable, so a run is never silently dropped from the projections.
    derived = df.minutes / df.distance.replace(0, np.nan)
    df["pace_min_km"] = np.where(
        is_run & df.pace_min_km.isna(), derived, df.pace_min_km)
    return df


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def find_bad_hr_years(df):
    """Years where sustained work was logged with implausibly low heart rate."""
    endurance = df[(df.minutes >= 20) & df.avg_hr.notna()]
    suspect = endurance[endurance.avg_hr < IMPLAUSIBLE_HR_CEILING]
    if suspect.empty:
        return set(), 0
    years = {int(y) for y in suspect.Date.dt.year.unique()}
    # Only quarantine a year if the failure was widespread, not a single odd day.
    keep = set()
    for y in years:
        in_year = endurance[endurance.Date.dt.year == y]
        if len(in_year) and (in_year.avg_hr < IMPLAUSIBLE_HR_CEILING).mean() > 0.25:
            keep.add(y)
    return keep, len(suspect)


def estimate_hr_bounds(df, max_hr_arg, rest_hr_arg):
    if max_hr_arg:
        hr_max = float(max_hr_arg)
    else:
        observed = df.max_hr.dropna()
        # 98th percentile, not the outright maximum: single-beat spikes are noise.
        hr_max = float(np.percentile(observed, 98)) if len(observed) > 20 else 190.0
    hr_rest = float(rest_hr_arg) if rest_hr_arg else DEFAULT_REST_HR
    return max(hr_max, hr_rest + 60), hr_rest


def session_load(row, hr_max, hr_rest, bad_years):
    """
    Banister TRIMP for sessions with trustworthy heart rate. Returns None
    for the rest, which are filled in afterwards.

    Aerobic TE is deliberately not used as the fallback. Garmin computes it
    from heart rate, so in a strap-failure year it is broken in exactly the
    same way and for exactly the same reason.
    """
    minutes = row.minutes
    if not minutes or minutes != minutes:
        return 0.0
    hr = row.avg_hr
    if hr != hr or hr < IMPLAUSIBLE_HR_CEILING:
        return None
    if int(row.Date.year) in bad_years:
        return None
    reserve = np.clip((hr - hr_rest) / (hr_max - hr_rest), 0.0, 1.0)
    return float(minutes * reserve * 0.64 * math.exp(1.92 * reserve))


def daily_load(df, hr_max, hr_rest, bad_years, today):
    """
    Score every session, then fill the gaps left by unusable heart rate with
    the athlete's own typical intensity for that sport.

    The alternative is to score those sessions near zero, which turns a year
    of sensor failure into a fitness collapse that never happened. This says
    the honest thing instead: the work was done, we do not know how hard.
    """
    scored = [session_load(r, hr_max, hr_rest, bad_years) for r in df.itertuples()]
    df = df.assign(load=[np.nan if v is None else v for v in scored],
                   load_basis=["hr" if v is not None else "typical" for v in scored])

    known = df[df.load_basis == "hr"]
    rates = {}
    if not known.empty:
        global_rate = float((known.load / known.minutes).median())
        for sport, group in known.groupby("Activity Type"):
            if len(group) >= 8:
                rates[sport] = float((group.load / group.minutes).median())
    else:
        global_rate = 1.0

    gaps = df.load_basis == "typical"
    if gaps.any():
        per_min = df.loc[gaps, "Activity Type"].map(rates).fillna(global_rate)
        df.loc[gaps, "load"] = df.loc[gaps, "minutes"] * per_min

    series = df.groupby("day").load.sum()
    index = pd.date_range(df.day.min(), max(df.day.max(), today), freq="D")
    return df, series.reindex(index, fill_value=0.0), {"global": global_rate,
                                                       "by_sport": rates}


def impulse_response(daily, half_life_days):
    """Exponentially weighted load. Rest days decay it; that is the point."""
    alpha = 1 - math.exp(-1 / half_life_days)
    out, acc = [], 0.0
    for value in daily.values:
        acc = acc + alpha * (value - acc)
        out.append(acc)
    return pd.Series(out, index=daily.index)


def consistency(df, today):
    days = pd.Series(sorted(df.day.unique()))
    gaps = days.diff().dt.days.dropna()
    layoffs = []
    for i, gap in gaps.items():
        if gap > 30:
            layoffs.append((days[i - 1].date(), days[i].date(), int(gap)))
    # Consecutive weeks containing at least one session, counted back from the
    # most recent active week. Counting back from today would report zero for
    # anyone taking a planned rest week, which is not the message.
    weekset = set(df.day.dt.to_period("W").unique())
    cursor = max(weekset)
    live = cursor >= pd.Timestamp(today).to_period("W") - 1
    streak = 0
    while cursor in weekset:
        streak += 1
        cursor -= 1
    return {
        "streak_live": live,
        "streak_end": max(weekset).end_time.date(),
        "median_gap": float(gaps.median()) if len(gaps) else float("nan"),
        "longest_gap": int(gaps.max()) if len(gaps) else 0,
        "layoffs": layoffs,
        "streak_weeks": streak,
        "training_days": len(days),
    }


def best_efforts(runs, today, window_days=365):
    """
    Fastest run in each distance band within the window. These are the only
    honest anchors available from a summary export: whole-activity averages,
    not segment bests.
    """
    recent = runs[runs.day >= pd.Timestamp(today) - pd.Timedelta(days=window_days)]
    recent = recent[recent.distance.notna() & recent.pace_min_km.notna()]
    recent = recent[(recent.pace_min_km > 2.5) & (recent.pace_min_km < 12)]
    bands = [(3, 6), (6, 12), (12, 25), (25, 100)]
    anchors = []
    for low, high in bands:
        band = recent[(recent.distance >= low) & (recent.distance < high)]
        if band.empty:
            continue
        best = band.loc[band.pace_min_km.idxmin()]
        anchors.append({
            "distance": float(best.distance),
            "pace": float(best.pace_min_km),
            "time": float(best.distance * best.pace_min_km),
            "date": best.day.date(),
            "avg_hr": float(best.avg_hr) if best.avg_hr == best.avg_hr else None,
        })
    return anchors


def project_races(anchors, runs, today, hr_max):
    """Riegel projection from every anchor, reported as a spread, plus blockers."""
    blockers = []
    if runs.empty:
        return [], ["No running activities in this export."]
    days_since = (pd.Timestamp(today) - runs.day.max()).days
    if days_since > 90:
        blockers.append(
            f"No run recorded in {days_since} days. Anything projected from "
            f"data this stale describes a past athlete, not the current one.")
    if not anchors:
        blockers.append("No run of 3 km or more with a usable pace to anchor against.")
    hard = runs[runs.avg_hr.notna() & (runs.avg_hr >= 0.86 * hr_max)]
    if hard.empty:
        blockers.append(
            "No run above 86% of max heart rate. Every projection would be "
            "extrapolating race pace from training pace.")
    if blockers:
        return [], blockers

    longest = max(a["distance"] for a in anchors)
    rows = []
    for label, target in RUN_TARGETS:
        estimates = [a["time"] * (target / a["distance"]) ** RIEGEL_EXPONENT
                     for a in anchors]
        if target > longest * 2.5:
            rows.append({"label": label, "distance": target, "out_of_range": True,
                         "reach": target / longest})
            continue
        rows.append({
            "label": label, "distance": target, "out_of_range": False,
            "median": float(np.median(estimates)),
            "low": float(min(estimates)), "high": float(max(estimates)),
            "n": len(estimates),
        })
    return rows, []


def efficiency_trend(df, bad_years):
    """Speed per heartbeat by quarter. Higher always means fitter."""
    clean = df[~df.Date.dt.year.isin(bad_years)] if bad_years else df
    out = []
    for sport, group in clean.groupby("Activity Type"):
        run_like = bool(group.is_run.iloc[0])
        if run_like:
            valid = group[group.pace_min_km.notna() & group.avg_hr.notna()]
            valid = valid[valid.pace_min_km.between(3, 12) & (valid.minutes >= 12)]
            if len(valid) < 6:
                continue
            index = 10000 / (valid.pace_min_km * valid.avg_hr)
            unit = "metres per beat, scaled"
        else:
            valid = group[group.speed_kmh.notna() & group.avg_hr.notna()]
            valid = valid[(valid.speed_kmh > 3) & (valid.minutes >= 12)]
            if len(valid) < 6:
                continue
            index = valid.speed_kmh / valid.avg_hr * 1000
            unit = "speed per beat, scaled"
        trend = index.groupby(valid.Date.dt.to_period("Q")).mean()
        if len(trend) < 3:
            continue
        out.append({"sport": sport, "unit": unit, "trend": trend, "n": len(valid)})
    out.sort(key=lambda d: -d["n"])
    return out


# ---------------------------------------------------------------------------
# SVG primitives
# ---------------------------------------------------------------------------

def esc(text):
    return html.escape(str(text), quote=True)


def svg_open(width, height, extra=""):
    return (f'<svg viewBox="0 0 {width} {height}" width="100%" '
            f'preserveAspectRatio="xMidYMid meet" '
            f'xmlns="http://www.w3.org/2000/svg" role="img" {extra}>')


def text(x, y, content, fill, size=11, anchor="start", weight=400, opacity=1.0,
         tabular=True):
    features = ' style="font-variant-numeric: tabular-nums"' if tabular else ""
    return (f'<text x="{x:.1f}" y="{y:.1f}" fill="{fill}" font-size="{size}" '
            f'text-anchor="{anchor}" font-weight="{weight}" '
            f'opacity="{opacity}"{features}>{esc(content)}</text>')


def line(x1, y1, x2, y2, stroke, width=1, dash=None, opacity=1.0):
    d = f' stroke-dasharray="{dash}"' if dash else ""
    return (f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
            f'stroke="{stroke}" stroke-width="{width}" opacity="{opacity}"{d}/>')


def scaler(dmin, dmax, pmin, pmax):
    span = (dmax - dmin) or 1
    return lambda v: pmin + (v - dmin) / span * (pmax - pmin)


# ---------------------------------------------------------------------------
# Charts
# ---------------------------------------------------------------------------

def chart_form(ctl, atl, tsb, today):
    """The hero: chronic load, acute load and the balance between them."""
    W, H = 1160, 300
    L, R, T, B = 46, 96, 26, 34
    if len(ctl) > 1100:                      # keep the path light on long histories
        step = len(ctl) // 1100 + 1
        ctl, atl, tsb = ctl[::step], atl[::step], tsb[::step]

    x = scaler(0, len(ctl) - 1, L, W - R)
    top = max(float(ctl.max()), float(atl.max())) * 1.12 or 1
    y = scaler(0.0, top, H - B, T)
    zero = y(0)

    parts = [svg_open(W, H, 'aria-label="Fitness, fatigue and form over time"')]

    # Gridlines carry the load scale; no axis box, it would only add ink.
    for value in np.linspace(0, top, 5)[1:]:
        parts.append(line(L, y(value), W - R, y(value), C["rule"], 1, opacity=0.55))
        parts.append(text(L - 9, y(value) + 3.5, f"{value:.0f}", C["faint"], 10,
                          anchor="end"))

    # Year boundaries, so a four-year history stays readable.
    years = pd.Series(ctl.index).dt.year
    for i in range(1, len(years)):
        if years.iloc[i] != years.iloc[i - 1]:
            parts.append(line(x(i), T - 8, x(i), H - B, C["rule"], 1, dash="2 4"))
            parts.append(text(x(i) + 6, T - 12, str(years.iloc[i]), C["faint"], 10))

    def path(series, close=False):
        pts = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(series.values))
        if close:
            return (f"M {x(0):.1f},{zero:.1f} L {pts} "
                    f"L {x(len(series)-1):.1f},{zero:.1f} Z")
        return "M " + pts

    parts.append(f'<path d="{path(ctl, close=True)}" fill="{C["fitness"]}" '
                 f'opacity="0.13"/>')
    parts.append(f'<polyline points="'
                 + " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(atl.values))
                 + f'" fill="none" stroke="{C["fatigue"]}" stroke-width="1.2" '
                 f'opacity="0.85"/>')
    parts.append(f'<polyline points="'
                 + " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(ctl.values))
                 + f'" fill="none" stroke="{C["fitness"]}" stroke-width="2.2"/>')
    parts.append(line(L, zero, W - R, zero, C["faint"], 1, opacity=0.8))

    # End-of-line labels, the way a price chart marks the last traded value.
    last = len(ctl) - 1
    for series, colour, name in ((ctl, C["fitness"], "Fitness"),
                                 (atl, C["fatigue"], "Fatigue")):
        value = float(series.iloc[-1])
        parts.append(f'<circle cx="{x(last):.1f}" cy="{y(value):.1f}" r="3" '
                     f'fill="{colour}"/>')
        parts.append(text(x(last) + 8, y(value) - 4, name, colour, 10.5, weight=600))
        parts.append(text(x(last) + 8, y(value) + 9, f"{value:.0f}", colour, 12.5,
                          weight=600))

    parts.append(text(L, H - 10, str(ctl.index[0].date()), C["faint"], 10))
    parts.append(text(W - R, H - 10, str(today), C["faint"], 10, anchor="end"))
    parts.append("</svg>")
    return "".join(parts)


def chart_form_bar(tsb):
    """Form as a single reading against the zones runners actually use."""
    W, H = 1160, 74
    value = float(tsb.iloc[-1])
    lo, hi = -35.0, 30.0
    x = scaler(lo, hi, 8, W - 8)
    zones = [(-35, -18, "Overloaded", C["alert"]),
             (-18, -8, "Building", C["fatigue"]),
             (-8, 6, "Neutral", C["dim"]),
             (6, 18, "Fresh", C["fitness"]),
             (18, 30, "Detraining", C["form"])]
    parts = [svg_open(W, H, 'aria-label="Current training form"')]
    for z0, z1, label, colour in zones:
        parts.append(f'<rect x="{x(z0):.1f}" y="26" width="{x(z1)-x(z0):.1f}" '
                     f'height="12" fill="{colour}" opacity="0.22"/>')
        parts.append(text((x(z0) + x(z1)) / 2, 54, label, C["dim"], 10,
                          anchor="middle"))
    pos = x(np.clip(value, lo, hi))
    parts.append(f'<rect x="{pos-1.5:.1f}" y="20" width="3" height="24" '
                 f'fill="{C["ink"]}"/>')
    label_x = min(max(pos, 26), W - 26)      # never let the readout clip the edge
    parts.append(text(label_x, 14, f"{value:+.0f}", C["ink"], 13, anchor="middle",
                      weight=700))
    parts.append("</svg>")
    return "".join(parts)


def chart_calendar(daily, today):
    """A year per row, a square per day, shaded by that day's training load."""
    cell, gap = 11, 2.5
    years = sorted({d.year for d in daily.index})
    row_h = 7 * (cell + gap) + 30
    W = 40 + 54 * (cell + gap) + 60
    H = len(years) * row_h + 26
    scale_max = float(np.percentile(daily[daily > 0], 92)) if (daily > 0).any() else 1

    parts = [svg_open(W, H, 'aria-label="Daily training load calendar")')]
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    for yi, year in enumerate(years):
        y0 = yi * row_h + 22
        parts.append(text(0, y0 + 26, str(year), C["dim"], 12, weight=600))
        for m in range(12):
            first = pd.Timestamp(year=year, month=m + 1, day=1)
            wk = int(first.strftime("%W"))
            parts.append(text(40 + wk * (cell + gap), y0 - 4, months[m], C["faint"], 9))
        for day, value in daily[daily.index.year == year].items():
            wk = int(day.strftime("%W"))
            dow = day.weekday()
            px = 40 + wk * (cell + gap)
            py = y0 + dow * (cell + gap)
            if value <= 0:
                fill, op = C["rule"], 0.5
            else:
                op = 0.22 + 0.78 * min(value / scale_max, 1.0)
                fill = C["fitness"]
            outline = ""
            if day.date() == today:
                outline = f' stroke="{C["ink"]}" stroke-width="1.2"'
            title = (f'{day.date()} — {value:.0f} load'
                     if value > 0 else f'{day.date()} — rest')
            parts.append(
                f'<rect x="{px:.1f}" y="{py:.1f}" width="{cell}" height="{cell}" '
                f'rx="2" fill="{fill}" opacity="{op:.2f}"{outline}>'
                f'<title>{esc(title)}</title></rect>')
    parts.append("</svg>")
    return "".join(parts)


def chart_volume(df, colours):
    """Monthly hours, stacked by sport."""
    W, H = 1160, 260
    L, R, T, B = 44, 12, 18, 44
    pivot = (df.pivot_table(index=df.Date.dt.to_period("M"),
                            columns="Activity Type", values="minutes",
                            aggfunc="sum").fillna(0) / 60)
    full = pd.period_range(pivot.index.min(), pivot.index.max(), freq="M")
    pivot = pivot.reindex(full, fill_value=0)
    # Smallest sport at the base. Stacked on top of a dominant sport, a
    # two-hour month of running becomes an invisible sliver at the tip.
    order = pivot.sum().sort_values().index.tolist()
    pivot = pivot[order]
    top = float(pivot.sum(axis=1).max()) * 1.1 or 1
    y = scaler(0, top, H - B, T)
    bw = (W - L - R) / len(pivot) * 0.78
    step = (W - L - R) / len(pivot)

    parts = [svg_open(W, H, 'aria-label="Monthly training hours by sport"')]
    for value in np.linspace(0, top, 5)[1:]:
        parts.append(line(L, y(value), W - R, y(value), C["rule"], 1, opacity=0.5))
        parts.append(text(L - 8, y(value) + 3.5, f"{value:.0f}h", C["faint"], 10,
                          anchor="end"))
    for i, (period, row) in enumerate(pivot.iterrows()):
        px = L + i * step + (step - bw) / 2
        base = y(0)
        for sport in order:
            hours = float(row[sport])
            if hours <= 0:
                continue
            h = base - y(hours)
            parts.append(
                f'<rect x="{px:.1f}" y="{base-h:.1f}" width="{bw:.1f}" '
                f'height="{h:.1f}" fill="{colours[sport]}" opacity="0.9">'
                f'<title>{esc(f"{period} — {sport}: {hours:.1f}h")}</title></rect>')
            base -= h
        if period.month == 1 or i == 0:
            parts.append(text(px, H - 22, str(period.year), C["faint"], 10))
    parts.append(line(L, y(0), W - R, y(0), C["faint"], 1))
    parts.append("</svg>")
    return "".join(parts)


def chart_sparkline(trend, colour):
    W, H = 260, 62
    values = trend.values
    x = scaler(0, len(values) - 1, 4, W - 42)
    lo, hi = float(values.min()), float(values.max())
    pad = (hi - lo) * 0.25 or 1
    y = scaler(lo - pad, hi + pad, H - 12, 10)
    peak = int(np.argmax(values))
    parts = [svg_open(W, H)]
    parts.append('<polyline points="'
                 + " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(values))
                 + f'" fill="none" stroke="{colour}" stroke-width="1.8"/>')
    parts.append(f'<circle cx="{x(peak):.1f}" cy="{y(values[peak]):.1f}" r="3.2" '
                 f'fill="{C["ground"]}" stroke="{colour}" stroke-width="1.6"/>')
    parts.append(f'<circle cx="{x(len(values)-1):.1f}" '
                 f'cy="{y(values[-1]):.1f}" r="2.6" fill="{colour}"/>')
    parts.append(text(W - 38, y(values[-1]) + 4, f"{values[-1]:.0f}", colour, 12,
                      weight=600))
    parts.append(text(4, H - 1, str(trend.index[0]), C["faint"], 9))
    parts.append(text(W - 42, H - 1, str(trend.index[-1]), C["faint"], 9,
                      anchor="end"))
    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

CSS = """
:root{
  --ground:#0B1418; --panel:#101D22; --rule:#1E343B; --ink:#E6EFEC;
  --dim:#7D9BA1; --faint:#3A5960; --fitness:#4FB8B0; --fatigue:#E0705C;
  --form:#EFC15A; --alert:#C25470;
  --sans:"Inter","SF Pro Text",-apple-system,BlinkMacSystemFont,"Segoe UI",
         Roboto,"Helvetica Neue",Arial,sans-serif;
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{
  margin:0; background:var(--ground); color:var(--ink); font-family:var(--sans);
  font-size:15px; line-height:1.55; letter-spacing:-0.005em;
  -webkit-font-smoothing:antialiased;
}
.wrap{max-width:1200px; margin:0 auto; padding:0 20px 96px}
.num{font-variant-numeric:tabular-nums}

header.masthead{padding:56px 0 30px; border-bottom:1px solid var(--rule)}
.masthead h1{
  margin:0; font-size:clamp(30px,4.4vw,50px); line-height:1.02;
  font-weight:650; letter-spacing:-0.03em;
}
.masthead p{margin:14px 0 0; color:var(--dim); max-width:64ch; font-size:15px}

.figures{display:flex; flex-wrap:wrap; gap:2px; margin:30px 0 0}
.fig{flex:1 1 150px; padding:14px 18px 15px; background:var(--panel)}
.fig b{display:block; font-size:26px; font-weight:650; letter-spacing:-0.02em}
.fig span{display:block; color:var(--dim); font-size:12.5px; margin-top:3px}

section{padding:46px 0 8px; border-bottom:1px solid var(--rule)}
section:last-of-type{border-bottom:0}
h2{
  margin:0 0 6px; font-size:13.5px; font-weight:600; color:var(--dim);
  letter-spacing:0.01em;
}
.lede{margin:0 0 26px; color:var(--dim); font-size:14px; max-width:70ch}
.lede em{color:var(--ink); font-style:normal; font-weight:550}

.legend{display:flex; flex-wrap:wrap; gap:20px; margin:14px 0 0; font-size:12.5px;
        color:var(--dim)}
.legend i{display:inline-block; width:22px; height:3px; vertical-align:middle;
          margin-right:7px; border-radius:2px}

table{width:100%; border-collapse:collapse; font-size:14px}
th{
  text-align:left; font-weight:600; color:var(--dim); font-size:12.5px;
  padding:0 12px 8px 0; border-bottom:1px solid var(--rule);
}
td{padding:10px 12px 10px 0; border-bottom:1px solid var(--rule)}
td.n,th.n{text-align:right; font-variant-numeric:tabular-nums}
tr:last-child td{border-bottom:0}
.muted{color:var(--dim)}

.race{display:flex; flex-wrap:wrap; gap:2px}
.race .card{flex:1 1 200px; background:var(--panel); padding:18px 20px 20px}
.race .card b{display:block; font-size:32px; font-weight:650; letter-spacing:-0.03em;
              font-variant-numeric:tabular-nums}
.race .card u{display:block; text-decoration:none; color:var(--dim); font-size:12.5px;
              margin-bottom:9px}
.race .card span{display:block; color:var(--dim); font-size:12.5px; margin-top:6px;
                 font-variant-numeric:tabular-nums}
.race .card.void b{font-size:17px; color:var(--faint); letter-spacing:-0.01em}

.refusal{border-left:3px solid var(--alert); padding:4px 0 4px 20px; max-width:74ch}
.refusal h3{margin:0 0 8px; font-size:16px; font-weight:600}
.refusal ol{margin:0; padding-left:20px; color:var(--dim)}
.refusal li{margin:6px 0}
.refusal p{color:var(--dim); margin:14px 0 0}

.sparks{display:flex; flex-wrap:wrap; gap:2px}
.spark{flex:1 1 250px; background:var(--panel); padding:16px 18px 10px}
.spark b{font-size:15px; font-weight:600}
.spark span{display:block; color:var(--dim); font-size:12px; margin:2px 0 6px}

.notes{color:var(--dim); font-size:13.5px; max-width:76ch}
.notes p{margin:0 0 10px}
.notes b{color:var(--ink); font-weight:600}
footer{padding:34px 0 0; color:var(--faint); font-size:12.5px}
.scroll{overflow-x:auto}

@media (max-width:640px){
  .fig b{font-size:22px}
  .race .card b{font-size:26px}
  section{padding:34px 0 8px}
}
@media print{body{background:#fff; color:#111}}
"""


def fmt_time(minutes):
    total = int(round(minutes * 60))
    h, m, s = total // 3600, (total % 3600) // 60, total % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_pace(min_per_km):
    total = int(round(min_per_km * 60))
    return f"{total // 60}:{total % 60:02d}/km"


def build(df, args):
    today = (pd.Timestamp(args.today).date() if args.today else date.today())
    today_ts = pd.Timestamp(today)

    raw_rows = len(df)
    phantom = int((df.minutes < MIN_SESSION_MINUTES).sum())
    df = df[df.minutes >= MIN_SESSION_MINUTES].copy()
    if df.empty:
        sys.exit("error: no activities left after removing sub-2-minute sessions")

    bad_years, suspect_n = find_bad_hr_years(df)
    hr_max, hr_rest = estimate_hr_bounds(df, args.max_hr, args.rest_hr)
    df, daily, factors = daily_load(df, hr_max, hr_rest, bad_years, today_ts)

    ctl = impulse_response(daily, CTL_DAYS)
    atl = impulse_response(daily, ATL_DAYS)
    tsb = ctl - atl

    total_hours = df.minutes.sum() / 60
    by_sport = (df.groupby("Activity Type")
                  .agg(sessions=("minutes", "size"),
                       hours=("minutes", lambda s: s.sum() / 60),
                       avg_min=("minutes", "mean"),
                       distance=("distance", "sum"),
                       ascent=("ascent", "sum"))
                  .sort_values("hours", ascending=False))
    colours = sport_colours(list(by_sport.index))
    primary = by_sport.index[0]

    cons = consistency(df, today)
    runs = df[df.is_run]
    anchors = best_efforts(runs, today)
    projections, blockers = project_races(anchors, runs, today, hr_max)
    effs = efficiency_trend(df, bad_years)

    last_90 = df[df.day >= today_ts - pd.Timedelta(days=90)]
    hours_90 = last_90.minutes.sum() / 60
    days_since = (today_ts - df.day.max()).days
    ramp = float(atl.iloc[-1] / ctl.iloc[-1]) if ctl.iloc[-1] > 0 else 0.0

    o = []
    a = o.append
    a('<!doctype html><html lang="en"><head><meta charset="utf-8">')
    a('<meta name="viewport" content="width=device-width,initial-scale=1">')
    a(f'<title>Training report — {esc(today)}</title>')
    a(f"<style>{CSS}</style></head><body><div class='wrap'>")

    # ---- masthead ---------------------------------------------------------
    a('<header class="masthead">')
    a(f"<h1>{esc(f'{total_hours:,.0f} hours of aerobic work')}</h1>")
    a(f'<p>{esc(df.day.min().date())} to {esc(df.day.max().date())}. '
      f'{len(by_sport)} sports, treated as one aerobic system and measured in '
      f'hours, because kilometres do not compare across them.</p>')
    a('<div class="figures">')
    figures = [
        (f"{by_sport.loc[primary, 'hours'] / total_hours * 100:.0f}%",
         f"of your time is {esc(primary).lower()}"),
        (f"{cons['training_days']:,}", "days with a session"),
        (f"{hours_90:.0f}h", "in the last 90 days"),
        (f"{cons['streak_weeks']}",
         "consecutive weeks trained" if cons["streak_live"]
         else f"week run, ended {cons['streak_end']}"),
        (f"{days_since}d", "since the last session"),
    ]
    for value, label in figures:
        a(f'<div class="fig"><b class="num">{value}</b><span>{label}</span></div>')
    a("</div></header>")

    # ---- hero: form -------------------------------------------------------
    form_now = float(tsb.iloc[-1])
    verdict = ("carrying more fatigue than fitness" if form_now < -18 else
               "absorbing a genuine training block" if form_now < -8 else
               "in balance" if form_now < 6 else
               "fresh and race-ready" if form_now < 18 else
               "rested past the point of usefulness")
    a("<section>")
    a("<h2>Fitness and fatigue</h2>")
    a(f'<p class="lede">Every session becomes a load score, weighted by heart '
      f'rate where the data supports it. Fitness is that load averaged over '
      f'{CTL_DAYS} days, fatigue over {ATL_DAYS}. The gap between them is form. '
      f'Right now you are <em>{verdict}</em>.</p>')
    a(f'<div class="scroll">{chart_form(ctl, atl, tsb, today)}</div>')
    a('<div class="legend">'
      f'<span><i style="background:{C["fitness"]}"></i>Fitness ({CTL_DAYS}-day load)</span>'
      f'<span><i style="background:{C["fatigue"]}"></i>Fatigue ({ATL_DAYS}-day load)</span>'
      f'<span><i style="background:{C["rule"]}"></i>year boundary</span></div>')
    a(f'<div class="scroll" style="margin-top:26px">{chart_form_bar(tsb)}</div>')
    a(f'<p class="lede" style="margin-top:18px">Acute-to-chronic ratio '
      f'<em class="num">{ramp:.2f}</em>. '
      + ("Above 1.5 is where injury risk climbs steeply in the literature; "
         "back off before the calendar makes you." if ramp > 1.5 else
         "Below 0.8 means you are shedding fitness faster than you are building it."
         if ramp < 0.8 else
         "Between 0.8 and 1.3 is the range most training plans aim to sit in.")
      + "</p>")
    a("</section>")

    # ---- calendar ---------------------------------------------------------
    a("<section><h2>Every day you trained</h2>")
    a('<p class="lede">One square per day, shaded by how hard that day was. '
      'Gaps are the story: for most amateurs consistency, not session quality, '
      'is the limiter.</p>')
    a(f'<div class="scroll">{chart_calendar(daily, today)}</div>')
    a("</section>")

    # ---- race projections -------------------------------------------------
    a("<section><h2>Race projections</h2>")
    if blockers:
        a('<div class="refusal"><h3>Not enough data to predict a race time.</h3>')
        a("<ol>" + "".join(f"<li>{esc(b)}</li>" for b in blockers) + "</ol>")
        a("<p>A projection could be produced anyway. It would be a number with "
          "no evidence behind it, and it would be wrong in a direction you "
          "could not estimate. The rebuild path is below: restore consistency "
          "first, then add one hard effort to anchor against.</p></div>")
    else:
        a(f'<p class="lede">Riegel projections from your {len(anchors)} fastest '
          f'efforts of the last twelve months. The range is the spread across '
          f'those anchors, not a confidence interval — it shows how much your '
          f'own efforts disagree with each other.</p>')
        a('<div class="race">')
        for row in projections:
            if row["out_of_range"]:
                a(f'<div class="card void"><u>{esc(row["label"])}</u>'
                  f'<b>Out of range</b>'
                  f'<span>{row["reach"]:.1f}× your longest recent run</span></div>')
            else:
                a(f'<div class="card"><u>{esc(row["label"])}</u>'
                  f'<b>{fmt_time(row["median"])}</b>'
                  f'<span>{fmt_time(row["low"])} to {fmt_time(row["high"])}</span>'
                  f'<span>{fmt_pace(row["median"]/row["distance"])}</span></div>')
        a("</div>")
        a('<table style="margin-top:30px"><thead><tr><th>Anchor effort</th>'
          '<th class="n">Distance</th><th class="n">Pace</th>'
          '<th class="n">Time</th><th class="n">Avg HR</th></tr></thead><tbody>')
        for anchor in anchors:
            hr = f"{anchor['avg_hr']:.0f}" if anchor["avg_hr"] else "—"
            a(f'<tr><td>{esc(anchor["date"])}</td>'
              f'<td class="n">{anchor["distance"]:.2f} km</td>'
              f'<td class="n">{fmt_pace(anchor["pace"])}</td>'
              f'<td class="n">{fmt_time(anchor["time"])}</td>'
              f'<td class="n">{hr}</td></tr>')
        a("</tbody></table>")
    a("</section>")

    # ---- efficiency -------------------------------------------------------
    if effs:
        a("<section><h2>Aerobic efficiency by quarter</h2>")
        listed = ", ".join(str(y) for y in sorted(bad_years))
        note = (f" {listed} {'is' if len(bad_years) == 1 else 'are'} excluded: "
                f"the heart rate recorded there is not real."
                if bad_years else "")
        a(f'<p class="lede">Distance covered per heartbeat, quarter by quarter. '
          f'Higher is fitter. The ring marks your best quarter.{esc(note)}</p>')
        a('<div class="sparks">')
        for item in effs:
            colour = colours.get(item["sport"], C["fitness"])
            peak = item["trend"].idxmax()
            a(f'<div class="spark"><b>{esc(item["sport"])}</b>'
              f'<span>{esc(item["unit"])} · peak {esc(peak)} · '
              f'{item["n"]} sessions</span>'
              f'{chart_sparkline(item["trend"], colour)}</div>')
        a("</div></section>")

    # ---- volume -----------------------------------------------------------
    a("<section><h2>Monthly hours by sport</h2>")
    a('<p class="lede">Volume in hours. A cyclist who runs occasionally is not '
      'a detrained runner, and this is the view that shows the difference.</p>')
    a(f'<div class="scroll">{chart_volume(df, colours)}</div>')
    a('<div class="legend">' + "".join(
        f'<span><i style="background:{colours[s]}"></i>{esc(s)}</span>'
        for s in by_sport.index) + "</div>")
    a('<table style="margin-top:30px"><thead><tr><th>Sport</th>'
      '<th class="n">Sessions</th><th class="n">Hours</th>'
      '<th class="n">Share</th><th class="n">Avg session</th>'
      '<th class="n">Distance</th><th class="n">Ascent</th>'
      '</tr></thead><tbody>')
    for sport, row in by_sport.iterrows():
        a(f'<tr><td>{esc(sport)}</td>'
          f'<td class="n">{int(row.sessions)}</td>'
          f'<td class="n">{row.hours:,.0f}</td>'
          f'<td class="n">{row.hours / total_hours * 100:.0f}%</td>'
          f'<td class="n">{row.avg_min:.0f} min</td>'
          f'<td class="n">{row.distance:,.0f} km</td>'
          f'<td class="n">{row.ascent:,.0f} m</td></tr>')
    a("</tbody></table></section>")

    # ---- consistency ------------------------------------------------------
    a("<section><h2>Layoffs</h2>")
    if cons["layoffs"]:
        a(f'<p class="lede">Median gap between sessions is '
          f'<em class="num">{cons["median_gap"]:.1f} days</em>. '
          f'Longest gap <em class="num">{cons["longest_gap"]} days</em>. '
          f'Breaks over thirty days cost more fitness than any single block '
          f'builds.</p>')
        a('<table><thead><tr><th>Stopped</th><th>Resumed</th>'
          '<th class="n">Days off</th></tr></thead><tbody>')
        for stopped, resumed, gap in cons["layoffs"]:
            a(f'<tr><td>{esc(stopped)}</td><td>{esc(resumed)}</td>'
              f'<td class="n">{gap}</td></tr>')
        a("</tbody></table>")
    else:
        a('<p class="lede">No break longer than thirty days in the whole '
          'history. That is the rarest thing in this report.</p>')
    a("</section>")

    # ---- data trust -------------------------------------------------------
    a("<section><h2>What this report trusted</h2><div class='notes'>")
    a(f"<p><b>{raw_rows:,} rows read.</b> {phantom} were shorter than "
      f"{MIN_SESSION_MINUTES:g} minutes and were dropped as accidental starts. "
      f"They matter more than their count suggests: a four-second phantom run "
      f"makes a long layoff look months shorter than it was.</p>")
    if bad_years:
        a(f"<p><b>Heart rate quarantine.</b> {suspect_n} sustained sessions were "
          f"logged with an average heart rate under {IMPLAUSIBLE_HR_CEILING:g} bpm, "
          f"which is a strap or pairing failure rather than an easy day. "
          f"{', '.join(str(y) for y in sorted(bad_years))} "
          f"{'is' if len(bad_years) == 1 else 'are'} excluded from efficiency "
          f"trends, and load for those sessions was scored from duration at "
          f"your own typical intensity for that sport instead.</p>")
    else:
        a("<p><b>Heart rate looks clean.</b> No sustained sessions with "
          "implausibly low readings.</p>")
    basis = df.load_basis.value_counts()
    typical = int(basis.get("typical", 0))
    a(f"<p><b>How load was scored.</b> {int(basis.get('hr', 0)):,} sessions from "
      f"heart rate, using Banister's TRIMP.")
    if typical:
        a(f" The other {typical:,} had no usable heart rate, so they were scored "
          f"as a typical session of that sport for you, measured from your own "
          f"clean data. Aerobic TE was not used to fill the gap: Garmin computes "
          f"it from heart rate, so where the strap failed it failed too. Scoring "
          f"those months near zero would invent a fitness collapse that never "
          f"happened.")
    a("</p>")
    a(f"<p><b>Heart rate bounds.</b> Max {hr_max:.0f}, resting {hr_rest:.0f}"
      + ("." if args.max_hr else
         ", with the max taken from the 98th percentile of your recorded peaks "
         "rather than the single highest beat. Pass --max-hr if you know it "
         "from a test.") + "</p>")
    a("</div></section>")

    a(f'<footer>Generated {esc(today)} from {esc(args.csv)} · '
      f'endurance-analyst · this file contains personal health data, '
      f'so think before sharing it.</footer>')
    a("</div></body></html>")
    return "".join(o)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", help="Path to Garmin Activities.csv export")
    ap.add_argument("-o", "--out", default="report.html", help="Output HTML file")
    ap.add_argument("--today", help="Override reference date (YYYY-MM-DD)")
    ap.add_argument("--max-hr", type=float, help="Known max heart rate")
    ap.add_argument("--rest-hr", type=float, help="Known resting heart rate")
    args = ap.parse_args()

    try:
        df = load(args.csv)
    except FileNotFoundError:
        sys.exit(f"error: file not found: {args.csv}")

    output = build(df, args)
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(output)
    print(f"wrote {args.out} ({len(output) / 1024:.0f} KB, self-contained)")


if __name__ == "__main__":
    main()
