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
| `report.py` | Builds a self-contained visual report from the same export |
| `make_sample.py` | Generates a synthetic athlete, so the tool is demoable |
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

### Try it without your own data

```bash
python make_sample.py
python report.py sample_Activities.csv --today 2026-09-04
```

`make_sample.py` writes a synthetic four-year multi-sport athlete with problems
deliberately planted in the data: a year of chest-strap failures, two layoffs of
two and three months, accidental sub-minute activity starts, and Garmin's `--`
null token. It exists so the tool can be tested, demonstrated and screenshotted
without anyone publishing their own health data.

## What it reports

- **Data audit** — actual date range, per-sport counts, staleness. Runs first,
  because exports frequently do not contain what the athlete believes they contain.
- **Data quality** — flags sensor failures and device-noise sessions *before* they
  contaminate trends.
- **Training load** — hours by sport and by month, identifying the primary sport.
- **Consistency** — session gaps and layoffs, usually the real limiter for amateurs.
- **Aerobic efficiency** — speed (or pace) per heartbeat by quarter, marking the peak.
- **Prediction readiness** — whether the data can support a race prediction at all.

## The visual report

`analyze.py` prints numbers. `report.py` renders the same data as a single HTML
file you can open, keep and send to a coach.

```bash
python report.py Activities.csv
python report.py Activities.csv --max-hr 188 --rest-hr 48 -o 2026-season.html
```

One file, no network calls, no chart library, no external fonts. Charts are
generated as inline SVG, so the report works offline and stays readable when
printed.

It reports:

- **Fitness, fatigue and form.** Every session becomes a load score. Fitness is
  that load exponentially averaged over 42 days, fatigue over 7, and form is the
  gap between them, placed against the zones athletes actually use. This is the
  chart that answers "am I fitter than I was in March" without you having to
  squint at monthly totals.
- **The training calendar.** One square per day for the whole history, shaded by
  that day's load. Layoffs are visible at a glance, which is the point.
- **Race projections with the guard rails intact.** Riegel projections from the
  fastest efforts of the last twelve months, shown as the spread across those
  anchors rather than a single confident number, with the anchoring efforts listed
  underneath so you can see what the estimate rests on. Targets more than 2.5×
  beyond your longest recent run are marked out of range instead of extrapolated.
  When the data cannot support a projection at all, the section says so and lists
  the specific blockers.
- **Aerobic efficiency by quarter**, per sport, with the peak quarter marked.
- **Monthly hours by sport**, stacked smallest-first so a secondary sport stays
  visible under a dominant one.
- **What the report trusted** — a closing section stating what was dropped, what
  was quarantined, and how load was scored where the sensor failed.

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

## How training load is scored

Sessions with trustworthy heart rate use Banister's TRIMP: duration weighted by
heart-rate reserve on an exponential curve, so an hour hard counts for more than
an hour easy by roughly the margin it should.

Sessions without trustworthy heart rate are the interesting case. **Aerobic TE is
not used to fill the gap.** Garmin derives it from heart rate, so in a
strap-failure year it is broken in exactly the same way and for exactly the same
reason — using it as the fallback quietly scores those months near zero and
draws a fitness collapse that never happened. Instead those sessions are scored
as a typical session of that sport for that athlete, measured from their own
clean data. The work was done; what is unknown is how hard, and the report says
which sessions were treated that way.

Maximum heart rate is taken from the 98th percentile of recorded peaks rather
than the single highest beat, because one spurious spike would otherwise
compress every load score in the file. Pass `--max-hr` if you know yours from a
test.

## Prediction guard rails

The agent refuses to predict a race time when the data cannot support one, rather
than extrapolating a confident-looking number. It declines when there is no recent
training in the relevant window, when the target distance is far beyond the longest
recorded effort, or when no near-maximal effort exists to anchor against. An honest
"not enough data, here is the rebuild path" beats a fabricated finishing time.

## Privacy

Garmin exports are personal health data and often contain location names in
activity titles. Worth repeating now that the output is a shareable HTML file:
the report contains that same data. `.gitignore` excludes `*.csv`, `data/` and
`*.html` by default. Keep it that way, and think carefully before committing your
own exports — or sending a generated report anywhere.

`sample_Activities.csv` is synthetic and safe to commit; it is the one `*.csv`
the ignore rules let through.

## License

MIT
