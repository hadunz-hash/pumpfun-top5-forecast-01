# Pump.fun top-5 forecast

Entry for make.fun's `pumpfun-top5-forecast-01`: predict the 5 largest
pump.fun coins by market cap at each of the house's seven answer reads.

## How it works

`main.py` (Python 3.13, standard library only) reads the house's 10-minute
snapshots from `/input` (`history.ndjson`, `task.json`) and writes one
forecast to `/output/forecast.json`.

1. **Series.** For every coin it builds a log10 market-cap (in SOL) series
   from the marks, with venue (`curve` / `pool` / `migrating`) and age.
2. **Drift and volatility.** A per-coin drift (recent log-cap slope, shrunk
   toward the median drift of its venue/age group) and volatility
   (residual std, also shrunk). Returns across a venue change are skipped:
   graduation changes the cap formula, so the measured "return" across it
   is an artifact, not a real move. Group stats use the median, so one
   graduating coin cannot drag a whole group's drift.
3. **Drift decay.** Drift decays exponentially with time (curve coins fast,
   pool coins slow): a coin rising x1.8 per 10 minutes cannot sustain that
   for hours.
4. **Monte Carlo.** With a fixed seed, it simulates every candidate coin's
   log-cap at each answer read (drift decayed, volatility growing as
   sqrt-time), ranks the simulated caps, and averages the place
   frequencies over the seven reads. That gives honest probabilities per
   coin per place.
5. **Sizes.** Each place's market-cap distribution is a normal in log10
   around the k-th largest *expected* cap (drift only, no noise),
   averaged over the reads. Monte Carlo order statistics are the wrong
   centre: independent noise spreads the top coins apart, while the real
   top 5 stay tight because new coins keep filling the gaps.
6. **Newcomers.** Part of every place is left unnamed (0.25 when the
   answer reads start soon after the history ends, 0.35 when the gap is
   2+ hours): coins born after the cut can take any place, and only the
   unnamed share scores on them.

## Tuning

The unnamed share and the size sd were tuned on backtests against the
practice history (truncate the history at a cut mark, score the forecast
on the marks after the cut, compare with the house baseline). Two lessons
from those backtests are baked into the code: the baseline's fat unnamed
share (0.50) beats a thin one whenever the winners are mostly coins the
model cannot see, and the size centre must come from expected caps, not
from Monte Carlo order statistics.

### Backtest results (local scorer, log score — higher is better)

| cut | house baseline | this entry (best) | best settings |
|-----|----------------|-------------------|---------------|
| 3   | -49.958        | -50.4             | — (history too short: winners mostly newborns) |
| 4   | -33.478        | -29.460           | unnamed 0.25, size sd 0.15 |
| 5   | -29.867        | -28.486           | unnamed 0.25, size sd 0.15 |

The size sd keeps shrinking the score the tighter it gets on these
short-horizon backtests (0.35 → -32.880 / -32.008, 0.25 → -31.378 /
-30.481, 0.20 → -30.465 / -29.543, 0.15 → -29.460 / -28.486 at
cuts 4 / 5), because the expected-cap centres are accurate over
10–20 minutes. But the server's practice score punishes the tight sd:
the first submission (size sd 0.35) scored -40.3628 on the dev phase,
the resubmission with sd 0.20 scored -42.8920 — the practice reads
sit hours after the cut, where the centres carry real error and
newborns dominate the top places, so a tight band floors the size
terms. Widening helped up to a point: sd 0.50 scored -39.5769, the
best so far, while sd 0.65 scored -39.8798 — slightly worse, so the
practice optimum sits near 0.50–0.60. The shipped sd is now 0.575,
the midpoint probe; the final's 7–13 hour gap is longer than the
practice's, which is the only reason to lean a touch wider than the
practice's exact peak.

Caveats: the backtests truncate a 6-mark practice history, so their
horizons are minutes, not the final's 7–13 hours, and the final regime
(66 marks, most winners unseen at the cut) is not representable by them.
The shipped defaults (unnamed 0.35 for a 2+ hour gap, size sd 0.575) are
chosen for that long-gap regime, where the unseen-coins share dominates
and the size centres carry more error.
The server's practice score on the submitted entry is the real validation:
the house baseline scores -40.408345 on the practice (order -21.380389,
size -19.027956), and an even-spread forecast scores -45.059447. Practice
scores by size sd: 0.20 → -42.8920 (rank 575), 0.35 → -40.3628
(rank 495), 0.50 → -39.5769 (rank 501), 0.65 → -39.8798 — the peak
sits near 0.50–0.60, and 0.575 is the midpoint probe. The final is
judged on the latest submission, so if 0.575 scores worse than 0.50,
the entry reverts to the 0.50 version.

## Practice forecast

On the full practice history the entry's top 5 is: Ansem is Back
(`7fNYfFkDefatZugpcgaHqGxQuwm6JzcRYDQrdajqpump`), MIHAO VI (`4Dgj...`),
Solar Elon (`Ghjt...`), `CKSX...`, `HErn...`. Place-1 probabilities:
Ansem is Back 0.271, Solar Elon 0.181, MIHAO VI 0.172.

## Run it

```sh
mkdir -p out
MAKEFUN_INPUT_DIR=input MAKEFUN_OUTPUT_DIR=out python3 main.py
```

Deterministic: two runs on the same history produce byte-identical output.
Runs in ~5 seconds on the practice history.

## License

MIT. See `LICENSE`.
