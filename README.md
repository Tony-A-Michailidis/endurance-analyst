# Endurance Training Analyst

An LLM agent specification and companion analysis tool for reading Garmin Connect
exports across **all activity types** — running, cycling, walking, hiking, and
whatever else is in the file.

Most Garmin analysis tooling is single-sport. That produces a specific failure: a
cyclist who occasionally runs looks like a *detrained runner*, because 50 hours of
cycling are invisible to a running-only view. This project treats the athlete as
one aerobic system and reports volume in **hours rather than kilometres**, since
distance is not comparable across sports. 

## Contents

| Path | Purpose |
|---|---|
| `prompts/endurance-analyst.md` | The agent specification — system prompt for an LLM analyst |
| `analyze.py` | Standalone analyzer that computes the metrics the spec calls for |
| `requirements.txt` | Python dependencies |

## Usage

```bash
pip install -r requirements.txt
python analyze.py path/to/Activities.csv
```

Pin the reference date for reproducible output:

```bash
python analyze.py Activities.csv --today 2026-09-04
```

Get your `Activities.csv` from Garmin Connect: **Activities → All Activities →
Export CSV**. Do not pre-filter by sport; the point is to see everything.

## What it reports

- **Data audit** — actual date range, per-sport counts, staleness. Runs first,
  because exports frequently do not contain what the athlete believes they contain.
- **Data quality** — flags sensor failures and device-noise sessions *before* they
  contaminate trends.
- **Training load** — hours by sport and by month, identifying the primary sport.
- **Consistency** — session gaps and layoffs, usually the real limiter for amateurs.
- **Aerobic efficiency** — speed (or pace) per heartbeat by quarter, marking the peak.
- **Prediction readiness** — whether the data can support a race prediction at all.

## Design notes

Three behaviours exist because the naive version gets them wrong:

**`Avg Speed` is unit-ambiguous.** Garmin stores km/h for cycling but min/km pace
for running, in the same column. Parsed conditionally on activity type so the two
are never compared.

**Bad HR data is excluded, not averaged in.** Sustained endurance activities logged
with avg HR under 95 bpm indicate a strap or pairing failure. Garmin's Aerobic TE
is derived from HR, so those sessions score near zero and will understate training
load. The tool detects affected years and drops them from HR-based trends rather
than quietly folding them into an average.

**Sub-2-minute sessions are dropped.** Accidental starts and device tests otherwise
distort per-session averages and, more importantly, misreport recency — a
4.9-second phantom "run" makes a long layoff look months shorter than it is.

## Prediction guard rails

The agent refuses to predict a race time when the data cannot support one, rather
than extrapolating a confident-looking number. It declines when there is no recent
training in the relevant window, when the target distance is far beyond the longest
recorded effort, or when no near-maximal effort exists to anchor against. An honest
"not enough data, here is the rebuild path" beats a fabricated finishing time.

## Privacy

Garmin exports are personal health data and often contain location names in
activity titles. `.gitignore` excludes `*.csv` and `data/` by default. Keep it that
way, and think carefully before committing your own exports to a public repository.

## License

MIT
