#!/usr/bin/env python3
"""
Generate a synthetic Garmin Connect Activities.csv.

Produces a plausible multi-sport athlete so the analyzer and report can be
developed, tested and demonstrated without exposing real health data.
The athlete is deliberately messy: a strap-failure year, two layoffs,
accidental sub-minute activity starts, and Garmin's '--' null token.
"""

import argparse
import numpy as np
import pandas as pd

RNG = np.random.default_rng(11)

COLUMNS = [
    "Activity Type", "Date", "Favorite", "Title", "Distance", "Calories",
    "Time", "Avg HR", "Max HR", "Aerobic TE", "Avg Speed", "Total Ascent",
    "Total Descent", "Number of Laps",
]

TITLES = {
    "Cycling": ["Morning Ride", "Lunch Ride", "Evening Ride", "Club Ride"],
    "Running": ["Morning Run", "Evening Run", "Lunch Run", "Long Run"],
    "Walking": ["Walk", "Evening Walk"],
    "Hiking": ["Hike"],
}


def hhmmss(minutes):
    total = int(round(minutes * 60))
    return f"{total // 3600}:{(total % 3600) // 60:02d}:{total % 60:02d}"


def pace_str(min_per_km):
    total = int(round(min_per_km * 60))
    return f"{total // 60}:{total % 60:02d}"


def fitness_curve(day_index, span):
    """Slow build, a peak around 60% through, mild decline, then a rebuild."""
    x = day_index / span
    return 0.55 + 0.45 * np.sin(np.pi * min(x / 0.62, 1.0)) + 0.18 * max(0.0, x - 0.82)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="sample_Activities.csv")
    ap.add_argument("--end", default="2026-09-04")
    ap.add_argument("--years", type=float, default=4.2)
    args = ap.parse_args()

    end = pd.Timestamp(args.end)
    span = int(args.years * 365)
    start = end - pd.Timedelta(days=span)

    # Two layoffs: injury, then a winter off.
    layoffs = [
        (start + pd.Timedelta(days=int(span * 0.28)), 54),
        (start + pd.Timedelta(days=int(span * 0.66)), 88),
    ]
    # A whole year where the chest strap was failing.
    bad_hr_year = (start + pd.Timedelta(days=int(span * 0.45))).year

    rows = []
    for d in range(span + 1):
        day = start + pd.Timedelta(days=d)
        if any(lo <= day < lo + pd.Timedelta(days=n) for lo, n in layoffs):
            continue

        fit = fitness_curve(d, span)
        # Seasonal: more volume in summer, less in deep winter.
        season = 1.0 + 0.35 * np.sin((day.dayofyear - 80) / 365 * 2 * np.pi)
        p_train = np.clip(0.42 * season * (0.7 + 0.5 * fit), 0.05, 0.9)
        if RNG.random() > p_train:
            continue

        n_sessions = 2 if RNG.random() < 0.08 else 1
        for _ in range(n_sessions):
            roll = RNG.random()
            if roll < 0.60:
                sport = "Cycling"
            elif roll < 0.85:
                sport = "Running"
            elif roll < 0.95:
                sport = "Walking"
            else:
                sport = "Hiking"

            hr_broken = day.year == bad_hr_year and RNG.random() < 0.75

            if sport == "Cycling":
                minutes = float(np.clip(RNG.normal(78, 34), 22, 300))
                speed = float(np.clip(RNG.normal(24 + 6 * fit, 2.6), 14, 42))
                dist = speed * minutes / 60
                hr = np.clip(RNG.normal(139 - 9 * fit, 11), 96, 178)
                ascent = dist * RNG.uniform(4, 16)
                avg_speed = f"{speed:.1f}"
            elif sport == "Running":
                minutes = float(np.clip(RNG.normal(46, 22), 16, 190))
                pace = float(np.clip(RNG.normal(6.35 - 0.85 * fit, 0.42), 3.7, 8.4))
                dist = minutes / pace
                hr = np.clip(RNG.normal(152 - 8 * fit, 10), 104, 186)
                ascent = dist * RNG.uniform(5, 22)
                avg_speed = pace_str(pace)
            elif sport == "Walking":
                minutes = float(np.clip(RNG.normal(41, 16), 12, 130))
                speed = float(np.clip(RNG.normal(5.2, 0.6), 3.4, 7.0))
                dist = speed * minutes / 60
                hr = np.clip(RNG.normal(101, 9), 78, 128)
                ascent = dist * RNG.uniform(2, 12)
                avg_speed = f"{speed:.1f}"
            else:
                minutes = float(np.clip(RNG.normal(146, 52), 45, 380))
                speed = float(np.clip(RNG.normal(4.1, 0.6), 2.4, 6.0))
                dist = speed * minutes / 60
                hr = np.clip(RNG.normal(118, 12), 90, 155)
                ascent = dist * RNG.uniform(30, 95)
                avg_speed = f"{speed:.1f}"

            if hr_broken:
                hr = RNG.uniform(62, 92)

            # Occasional race-effort run, which is what anchors a prediction.
            if sport == "Running" and RNG.random() < 0.022:
                dist = float(RNG.choice([5.0, 10.0, 21.1]))
                pace = float(np.clip(RNG.normal(5.05 - 0.8 * fit, 0.18), 3.4, 6.5))
                pace += 0.16 * np.log2(max(dist, 1) / 5)
                minutes = dist * pace
                hr = np.clip(RNG.normal(176, 4), 160, 192)
                avg_speed = pace_str(pace)

            maxhr = hr + RNG.uniform(11, 30) if not hr_broken else hr + RNG.uniform(4, 14)
            te = np.clip((hr / 190) ** 3.1 * 6.2 * (minutes / 60) ** 0.32, 0.4, 5.0)

            rows.append({
                "Activity Type": sport,
                "Date": day.strftime("%Y-%m-%d ") + f"{RNG.integers(5, 20):02d}:{RNG.integers(0, 60):02d}:00",
                "Favorite": "false",
                "Title": str(RNG.choice(TITLES[sport])),
                "Distance": f"{dist:.2f}",
                "Calories": f"{int(minutes * RNG.uniform(7, 13)):,}",
                "Time": hhmmss(minutes),
                "Avg HR": str(int(hr)),
                "Max HR": str(int(maxhr)),
                "Aerobic TE": f"{te:.1f}",
                "Avg Speed": avg_speed,
                "Total Ascent": str(int(ascent)),
                "Total Descent": str(int(ascent * RNG.uniform(0.85, 1.15))),
                "Number of Laps": str(int(max(1, minutes // 12))),
            })

    # Accidental starts and device tests: the phantom sessions that make a
    # long layoff look shorter than it was.
    for _ in range(14):
        day = start + pd.Timedelta(days=int(RNG.integers(0, span)))
        secs = int(RNG.integers(3, 110))
        rows.append({
            "Activity Type": str(RNG.choice(["Running", "Cycling", "Walking"])),
            "Date": day.strftime("%Y-%m-%d ") + "12:00:00",
            "Favorite": "false", "Title": "Untitled", "Distance": "0.01",
            "Calories": "--", "Time": f"0:00:{secs:02d}", "Avg HR": "--",
            "Max HR": "--", "Aerobic TE": "0.0", "Avg Speed": "0.0",
            "Total Ascent": "--", "Total Descent": "--", "Number of Laps": "1",
        })

    df = pd.DataFrame(rows, columns=COLUMNS)
    df = df.sort_values("Date", ascending=False)
    df.to_csv(args.out, index=False)
    print(f"wrote {args.out}: {len(df)} activities, {start.date()} to {end.date()}")
    print(f"planted: strap-failure year {bad_hr_year}, "
          f"{len(layoffs)} layoffs, 14 phantom sessions")


if __name__ == "__main__":
    main()
