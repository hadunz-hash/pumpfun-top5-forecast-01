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
| 4   | -33.478        | -31.378           | unnamed 0.25, size sd 0.25 |
| 5   | -29.867        | -28.595           | unnamed 0.25, size sd 0.35 |

Caveats: the backtests truncate a 6-mark practice history, so their
horizons are minutes, not the final's 7–13 hours, and the final regime
(66 marks, most winners unseen at the cut) is not representable by them.
The shipped defaults (unnamed 0.35 for a 2+ hour gap, size sd 0.35) are
chosen for that long-gap regime, where the unseen-coins share dominates.
The server's practice score on the submitted entry is the real validation:
the house baseline scores -40.408345 on the practice (order -21.380389,
size -19.027956), and an even-spread forecast scores -45.059447.

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
