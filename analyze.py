#!/usr/bin/env python3
"""
Garmin multi-sport training analyzer.

Reads a Garmin Connect "Activities.csv" export and reports cross-sport training
load, fitness trends, consistency, and data-quality problems.

Usage:
    python analyze.py path/to/Activities.csv
    python analyze.py path/to/Activities.csv --today 2026-09-04
"""

import argparse
import sys

import numpy as np
import pandas as pd

# Garmin uses "--" as its null token, not an empty field.
NULL_TOKENS = ["--", "", "nan"]

# Activities shorter than this are almost always accidental starts or device
# tests rather than training, and they badly skew per-session averages.
MIN_SESSION_MINUTES = 2.0

# An avg HR below this during a sustained endurance activity indicates a
# sensor/pairing failure rather than a genuinely easy effort.
IMPLAUSIBLE_HR_CEILING = 95.0


def parse_duration(value):
    """Garmin durations are 'HH:MM:SS' or 'HH:MM:SS.s'. Returns minutes."""
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
    """Running 'Avg Speed' is min/km as 'M:SS'. Returns decimal minutes."""
    try:
        parts = str(value).split(":")
        return int(parts[0]) + float(parts[1]) / 60
    except (ValueError, IndexError):
        return np.nan


def numeric(series):
    """Coerce a Garmin column to float, treating '--' as null."""
    return pd.to_numeric(series.replace(NULL_TOKENS, np.nan), errors="coerce")


def load(path):
    df = pd.read_csv(path)
    df["Date"] = pd.to_datetime(df["Date"])
    df["minutes"] = df["Time"].apply(parse_duration)
    for src, dst in [
        ("Distance", "distance"),
        ("Avg HR", "avg_hr"),
        ("Max HR", "max_hr"),
        ("Aerobic TE", "aerobic_te"),
        ("Total Ascent", "ascent"),
        ("Calories", "calories"),
    ]:
        if src in df.columns:
            df[dst] = numeric(df[src])

    # 'Avg Speed' is unit-ambiguous: km/h for cycling, min/km pace for running.
    # Parse it conditionally so the two are never compared directly.
    is_run = df["Activity Type"].str.contains("Running", na=False)
    df["speed_kmh"] = np.where(is_run, np.nan, numeric(df["Avg Speed"]))
    df["pace_min_km"] = np.where(
        is_run, df["Avg Speed"].apply(parse_pace_to_minutes), np.nan
    )
    return df


def section(title):
    print(f"\n{'=' * 68}\n{title}\n{'=' * 68}")


def report_audit(df, today):
    """Always run first: establish what is actually in the file."""
    section("DATA AUDIT")
    print(f"Rows:        {len(df)}")
    print(f"Date range:  {df.Date.min().date()} to {df.Date.max().date()}")
    span_days = (df.Date.max() - df.Date.min()).days
    print(f"Span:        {span_days} days ({span_days / 30.4:.1f} months)")
    print(f"Last activity was {(today - df.Date.max()).days} days ago\n")

    per_type = df.groupby("Activity Type").agg(
        n=("Date", "size"), first=("Date", "min"), last=("Date", "max")
    )
    per_type["first"] = per_type["first"].dt.date
    per_type["last"] = per_type["last"].dt.date
    print(per_type.sort_values("n", ascending=False).to_string())

    for window in (90, 365):
        recent = df[df.Date >= today - pd.Timedelta(days=window)]
        hours = recent.minutes.sum() / 60
        print(f"\nLast {window:>3}d: {len(recent):>3} activities, {hours:.1f} hours")


def report_quality(df):
    """Surface sensor failures and junk sessions before they poison the trends."""
    section("DATA QUALITY")
    junk = df[df.minutes < MIN_SESSION_MINUTES]
    if len(junk):
        print(f"Sessions under {MIN_SESSION_MINUTES:g} min (excluded as device noise): {len(junk)}")
        print(f"  by type: {dict(junk['Activity Type'].value_counts())}")
    else:
        print("No sub-minimum sessions found.")

    # Flag periods where HR was recorded but implausibly low for the workload.
    endurance = df[(df.minutes >= 20) & df.avg_hr.notna()]
    suspect = endurance[endurance.avg_hr < IMPLAUSIBLE_HR_CEILING]
    if len(suspect):
        years = sorted(int(y) for y in suspect.Date.dt.year.unique())
        print(
            f"\nWARNING: {len(suspect)} sustained activities with avg HR < "
            f"{IMPLAUSIBLE_HR_CEILING:g} bpm"
        )
        print(f"  affected years: {years}")
        print("  Likely a strap/pairing failure. Excluded from HR-based trends.")
        print("  Garmin's Aerobic TE for these is also unreliable (computed from HR).")
        return set(years)
    print("\nNo implausible HR readings detected.")
    return set()


def report_load(df):
    """Volume in hours, since distance is not comparable across sports."""
    section("TRAINING LOAD BY SPORT (hours, not km)")
    by_sport = df.groupby("Activity Type").agg(
        sessions=("minutes", "size"),
        hours=("minutes", lambda x: x.sum() / 60),
        avg_min=("minutes", "mean"),
        avg_hr=("avg_hr", "mean"),
        aerobic_te=("aerobic_te", "mean"),
    )
    by_sport = by_sport.sort_values("hours", ascending=False).round(1)
    print(by_sport.to_string())

    total = df.minutes.sum() / 60
    print(f"\nTotal: {total:.1f} hours across {len(df)} sessions")
    top = by_sport.index[0]
    share = by_sport.loc[top, "hours"] / total * 100
    print(f"Primary sport: {top} ({share:.0f}% of training time)")

    section("MONTHLY HOURS BY SPORT")
    monthly = df.pivot_table(
        index=df.Date.dt.to_period("M"),
        columns="Activity Type",
        values="minutes",
        aggfunc="sum",
    ).fillna(0) / 60
    monthly["TOTAL"] = monthly.sum(axis=1)
    print(monthly.round(1).to_string())


def report_consistency(df):
    """Layoffs, not session quality, are usually the amateur's real limiter."""
    section("CONSISTENCY")
    ordered = df.sort_values("Date")
    gaps = ordered.Date.diff().dt.days.dropna()
    if gaps.empty:
        print("Not enough activities to measure gaps.")
        return

    print(f"Median gap between sessions: {gaps.median():.1f} days")
    print(f"Gaps over 30 days:           {(gaps > 30).sum()}")
    print(f"Longest gap:                 {gaps.max():.0f} days")

    long_gaps = ordered[gaps.reindex(ordered.index) > 30]
    if len(long_gaps):
        print("\nLayoffs (first session back after >30d off):")
        for _, row in long_gaps.iterrows():
            print(f"  {row.Date.date()}  {row['Activity Type']}")


def report_efficiency(df, bad_years):
    """
    Efficiency index tracks aerobic fitness within a sport by holding the
    workload comparable: more speed per heartbeat means a fitter athlete.
    """
    section("AEROBIC EFFICIENCY TREND")
    clean = df[~df.Date.dt.year.isin(bad_years)] if bad_years else df
    if bad_years:
        print(f"(excluding {sorted(bad_years)} — see data quality warnings)\n")

    for sport, group in clean.groupby("Activity Type"):
        if sport == "Cycling":
            valid = group[group.speed_kmh.notna() & group.avg_hr.notna()]
            valid = valid[valid.minutes >= 10]
            if len(valid) < 4:
                continue
            index = valid.speed_kmh / valid.avg_hr * 1000
            label = "speed per beat"
        elif "Running" in sport:
            valid = group[group.pace_min_km.notna() & group.avg_hr.notna()]
            valid = valid[valid.pace_min_km.between(3, 12)]
            if len(valid) < 4:
                continue
            # Inverted so that, as with cycling, higher always means fitter.
            index = 10000 / (valid.pace_min_km * valid.avg_hr)
            label = "pace-HR index"
        else:
            continue

        print(f"\n{sport} ({label}, higher = fitter, n={len(valid)}):")
        trend = index.groupby(valid.Date.dt.to_period("Q")).mean().round(1)
        peak = trend.idxmax()
        for quarter, value in trend.items():
            marker = "  <-- peak" if quarter == peak else ""
            print(f"  {quarter}  {value:>7.1f}{marker}")


def report_prediction_readiness(df, today):
    """
    Guard rails: state plainly when the data cannot support a race prediction
    rather than extrapolating a number that looks authoritative.
    """
    section("RACE PREDICTION READINESS")
    runs = df[df["Activity Type"].str.contains("Running", na=False)]
    blockers = []

    if runs.empty:
        blockers.append("No running activities in file.")
    else:
        days_since = (today - runs.Date.max()).days
        longest = runs.distance.max()
        observed_max_hr = df.max_hr.max()
        print(f"Runs: {len(runs)} | total {runs.distance.sum():.1f} km")
        print(f"Longest run: {longest:.2f} km | median: {runs.distance.median():.2f} km")
        print(f"Last run: {days_since} days ago")
        print(f"Max HR observed (all sports): {observed_max_hr:.0f}")

        if days_since > 90:
            blockers.append(f"No running data in {days_since} days (stale by >90d).")
        if longest < 10:
            blockers.append(
                f"Longest recorded run is {longest:.2f} km; a 10K prediction would "
                f"extrapolate {10 / longest:.1f}x beyond observed range."
            )
        hard = runs[runs.avg_hr >= 0.88 * observed_max_hr]
        if hard.empty:
            blockers.append(
                "No near-maximal running efforts (>=88% max HR) to anchor a prediction."
            )

    print()
    if blockers:
        print("NOT ENOUGH DATA to predict a race time. Blockers:")
        for i, blocker in enumerate(blockers, 1):
            print(f"  {i}. {blocker}")
        print("\nReport this plainly rather than producing a number.")
    else:
        print("Sufficient data present; predictions should still state basis and spread.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", help="Path to Garmin Activities.csv export")
    parser.add_argument("--today", help="Override reference date (YYYY-MM-DD)")
    args = parser.parse_args()

    try:
        df = load(args.csv)
    except FileNotFoundError:
        sys.exit(f"error: file not found: {args.csv}")

    today = pd.Timestamp(args.today) if args.today else pd.Timestamp.today().normalize()

    report_audit(df, today)
    bad_years = report_quality(df)

    # All volume and trend reporting excludes device noise.
    clean = df[df.minutes >= MIN_SESSION_MINUTES].copy()
    report_load(clean)
    report_consistency(clean)
    report_efficiency(clean, bad_years)
    report_prediction_readiness(clean, today)
    print()


if __name__ == "__main__":
    main()
