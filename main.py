#!/usr/bin/env python3
"""pumpfun-top5-forecast-01 entry (Python 3.13, standard library only).

Forecasts the 5 largest pump.fun coins by market cap at each of the house's
seven answer reads, from the house's 10-minute snapshots in /input.

Method
------
1. Build each coin's log10 market-cap (in SOL) series from the marks.
2. Estimate a per-coin drift (recent log-cap slope, shrunk toward the
   median drift of its venue/age group) and volatility (residual std,
   also shrunk). Returns across a venue change are skipped: graduation
   changes the cap formula, so the measured "return" across it is an
   artifact, not a real move. Group stats use the median, so one
   graduating coin cannot drag a whole group's drift.
3. Drift decays exponentially with time (curve coins fast, pool coins
   slow), because a coin rising x1.8 per 10 minutes cannot sustain that
   for hours. The decayed drift over a horizon is drift_total().
4. Monte Carlo (fixed seed, deterministic): simulate every candidate
   coin's log-cap at each answer read with the decayed drift and
   sqrt-time volatility, rank the simulated caps, and average the place
   frequencies over the seven reads. That gives honest probabilities per
   coin per place.
5. Sizes: each place's market-cap distribution is a normal in log10
   around the k-th largest EXPECTED cap (drift only, no noise),
   averaged over the reads. Monte Carlo order statistics are the wrong
   centre: independent noise spreads the top coins apart, while the real
   top 5 stay tight because new coins keep filling the gaps.
6. Leave part of every place unnamed (coins born after the cut can take
   any place, and only the unnamed share scores on them); the share is
   larger when the answer reads start long after the history ends.
   Tuned on backtests against the practice history: 0.35 for a 2+ hour
   gap, 0.25 otherwise; size sd 0.65.

Reads /input/task.json (places, size bins, answer reads) and
/input/history.ndjson. Writes /output/forecast.json.
$MAKEFUN_INPUT_DIR / $MAKEFUN_OUTPUT_DIR stand in for /input / /output.
"""

import json
import math
import os
import random
import sys
from collections import defaultdict

INPUT_DIR = os.environ.get("MAKEFUN_INPUT_DIR", "/input")
OUTPUT_DIR = os.environ.get("MAKEFUN_OUTPUT_DIR", "/output")

SEED = 20261112
# 4000 sims: ~5 s on the practice history, far inside the 300 s limit;
# the final's 66-mark history costs about the same (the sim count, not
# the history length, dominates the runtime).
N_SIMS = 4000
MAX_CANDIDATES = 260
MAX_ROWS_PER_PLACE = 100
MIN_KEEP_P = 0.0005          # drop named rows below this; a dropped coin
                              # falls back to its place's unnamed share,
                              # which is usually worth more than 0.0005
MIN_KEEP_BIN_P = 0.002       # drop size bins below this
SIZE_SD = 0.65               # sd of a place's log10 cap around its expected cap
MICRO = 1_000_000

# Tunables (env overrides for backtesting; defaults are the shipped values)
UNNAMED_ENV = os.environ.get("MF_UNNAMED")
SIZE_SD_ENV = os.environ.get("MF_SIZE_SD")


def parse_time(s):
    # ISO 8601 UTC like 2026-11-12T02:00:00.000Z -> epoch seconds (float)
    from datetime import datetime, timezone
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def log10_sol(market_cap_lamports):
    return math.log10(int(market_cap_lamports)) - 9.0


def age_bucket(age_min):
    if age_min < 60:
        return 0
    if age_min < 180:
        return 1
    return 2


def above(x, mean, sd):
    """P(X >= x) for X normal with mean `mean` and sd `sd`."""
    if x == -math.inf:
        return 1.0
    if x == math.inf:
        return 0.0
    return 0.5 * math.erfc((x - mean) / (sd * math.sqrt(2.0)))


def drift_total(mu, minutes, tau):
    """Drift accumulated over `minutes` when the per-10-min drift `mu`
    decays exponentially with time constant `tau` (minutes):
    integral of mu*exp(-t/tau) dt, in units of 10-minute steps."""
    return mu * (tau / 10.0) * (1.0 - math.exp(-minutes / tau))


def estimate_stats(mint, pts, coin_returns, group_mu, group_sd, all_mu, t0):
    """Per-coin (drift per 10 min, vol per 10 min, venue, last log-cap).

    Drift and vol are the coin's own recent returns, shrunk toward its
    venue/age group's robust stats. A coin with no usable returns just
    takes the group stats. Young curve coins that just appeared tend to
    keep rising briefly; old coins that stopped moving tend to keep
    fading -- both are already in the group's median, so no extra term.
    """
    venue = pts[-1][2] if pts else "curve"
    age_min = (t0 + pts[-1][0] * 60.0 - pts[-1][4]) / 60.0 if pts else 0.0
    g = (venue, age_bucket(age_min))
    gmu = group_mu.get(g, all_mu)
    gsd = group_sd.get(g, 0.15)
    rets = coin_returns.get(mint, [])
    if len(rets) >= 2:
        mu = sum(rets) / len(rets)
        var = sum((v - mu) ** 2 for v in rets) / (len(rets) - 1)
        sd = math.sqrt(var) if var > 0 else gsd
        w = len(rets) / (len(rets) + 3.0)  # shrink toward group mean
        mu = w * mu + (1 - w) * gmu
        sd = w * sd + (1 - w) * gsd        # shrink vol toward group vol
        sd = min(max(sd, 0.03), 0.30)
    elif len(rets) == 1:
        mu = 0.5 * rets[0] + 0.5 * gmu
        sd = gsd
    else:
        mu = gmu
        sd = gsd
    return (mu, sd, venue, pts[-1][1] if pts else 2.0)


def expected_caps(candidates, stats, horizon_min):
    """Expected log10 caps of every candidate at `horizon_min` minutes
    from the last mark (drift only, no noise), largest first."""
    exps = []
    for mint in candidates:
        mu, sd, venue, x0 = stats[mint]
        tau = 300.0 if venue == "pool" else 120.0
        exps.append(x0 + drift_total(mu, horizon_min, tau))
    exps.sort(reverse=True)
    return exps


def main():
    with open(os.path.join(INPUT_DIR, "task.json")) as f:
        task = json.load(f)
    places = int(task["places"])
    edges = [float(e) for e in task["size"]["edges"]]
    reads = [parse_time(r) for r in task["reads"]]
    output_name = task.get("output", "forecast.json")

    marks = []
    with open(os.path.join(INPUT_DIR, "history.ndjson")) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            m = json.loads(line)
            if m.get("ok") is True and isinstance(m.get("rows"), list):
                marks.append(m)
    if not marks:
        raise SystemExit("the history holds no ok mark")
    marks.sort(key=lambda m: m["at"])
    last_at = parse_time(marks[-1]["at"])

    # ---- per-coin series: mint -> list of (t_min, log10sol, venue, complete, created_epoch)
    t0 = parse_time(marks[0]["at"])
    series = {}
    for m in marks:
        t = (parse_time(m["at"]) - t0) / 60.0
        for r in m["rows"]:
            mint = r["mint"]
            series.setdefault(mint, []).append(
                (t, log10_sol(r["market_cap_lamports"]), r.get("venue", "curve"),
                 bool(r.get("complete", False)), parse_time(r["created_at"]))
            )
    last_mark = {r["mint"]: r for r in marks[-1]["rows"]}
    last_rows = sorted(marks[-1]["rows"], key=lambda r: r["rank"])

    # ---- recent 10-minute log returns, grouped by (venue, age bucket)
    # age bucket: 0 = born < 60 min ago, 1 = 60-180 min, 2 = older
    group_returns = defaultdict(list)
    coin_returns = {}
    for mint, pts in series.items():
        rets = []
        for (t0, x0, v0, _, _), (t1, x1, v1, _, _) in zip(pts, pts[1:]):
            dt = t1 - t0
            # Skip venue changes: graduation curve->pool changes the cap
            # formula, so the measured "return" across it is an artifact,
            # not a real move (it once poisoned the whole pool group mean).
            if v0 != v1:
                continue
            if 5 <= dt <= 15:
                rets.append((x1 - x0) / (dt / 10.0))
        if rets:
            coin_returns[mint] = rets
            age_min = (t0 + pts[-1][0] * 60.0 - pts[-1][4]) / 60.0
            venue = pts[-1][2]
            group_returns[(venue, age_bucket(age_min))].append(sum(rets) / len(rets))

    # Robust group stats: median (not mean) so one graduating coin's fake
    # jump cannot drag a whole group's drift.
    group_mu = {}
    group_sd = {}
    for g, vals in group_returns.items():
        vals = sorted(vals)
        med = vals[len(vals) // 2]
        mad = sorted(abs(v - med) for v in vals)[len(vals) // 2]
        group_mu[g] = med
        group_sd[g] = min(max(1.4826 * mad, 0.05), 0.30)
    all_mu = sorted(group_mu.values())[len(group_mu) // 2] if group_mu else 0.0

    # ---- candidates: last mark's chart + recent top charts + fast risers
    cand = {}
    for r in last_rows:
        cand[r["mint"]] = 0
    for m in marks[-3:]:
        for r in sorted(m["rows"], key=lambda r: r["rank"])[:60]:
            cand.setdefault(r["mint"], 1)
    # fast risers: biggest recent 10-min jump among coins seen in last 2 marks
    risers = []
    for mint, rets in coin_returns.items():
        if mint in cand:
            continue
        risers.append((max(rets), mint))
    risers.sort(reverse=True)
    for _, mint in risers[:40]:
        cand.setdefault(mint, 2)
    # rank candidates by last-mark rank, then by recency, cap the list
    def cand_key(mint):
        r = last_mark.get(mint)
        return (r["rank"] if r else 10 ** 6, -len(series.get(mint, [])))
    candidates = sorted(cand, key=cand_key)[:MAX_CANDIDATES]

    # ---- per-coin drift (per 10 min) and vol (per 10 min), shrunk to group
    stats = {}
    for mint in candidates:
        stats[mint] = estimate_stats(mint, series.get(mint, []),
                                      coin_returns, group_mu, group_sd,
                                      all_mu, t0)

    # ---- horizons: minutes from the last mark to each answer read
    horizons = [max(10.0, (rt - last_at) / 60.0) for rt in reads]
    gap_hours = (reads[0] - last_at) / 3600.0
    # Share left unnamed per place: the only way to score on coins born
    # after the cut. Backtests show the baseline's fat unnamed share
    # (0.50) beats a thin one whenever the winners are mostly coins the
    # model cannot see, so the share grows with the horizon gap.
    if UNNAMED_ENV is not None:
        unnamed = float(UNNAMED_ENV)
    else:
        unnamed = 0.35 if gap_hours >= 2.0 else 0.25

    rng = random.Random(SEED)
    n = len(candidates)
    # place counts: place -> {mint: count}
    place_counts = [defaultdict(int) for _ in range(places)]
    total_sims = N_SIMS * len(reads)

    for _ in range(N_SIMS):
        # one gaussian per coin, reused across reads with horizon scaling:
        # X(h) = x0 + drift_total(h) + sd * sqrt(steps(h)) * Z
        zs = [rng.gauss(0.0, 1.0) for _ in range(n)]
        for h in horizons:
            steps = h / 10.0
            caps = []
            for i, mint in enumerate(candidates):
                mu, sd, venue, x0 = stats[mint]
                tau = 300.0 if venue == "pool" else 120.0
                x = x0 + drift_total(mu, h, tau) + sd * math.sqrt(steps) * zs[i]
                caps.append((x, i))
            caps.sort(reverse=True)
            for p in range(places):
                mint = candidates[caps[p][1]]
                place_counts[p][mint] += 1

    probabilities = []
    for p in range(places):
        counts = place_counts[p]
        ranked = sorted(counts.items(), key=lambda kv: -kv[1])
        kept = [(mint, c / total_sims) for mint, c in ranked
                if c / total_sims >= MIN_KEEP_P][:MAX_ROWS_PER_PLACE]
        s = sum(pr for _, pr in kept)
        if s <= 0:
            continue
        scale = (1.0 - unnamed) / s
        for mint, pr in kept:
            u = math.floor(pr * scale * MICRO)
            if u > 0:
                probabilities.append({"mint": mint, "place": p + 1,
                                      "p": u / MICRO})

    # top5 by expected rank (lower is better); not placing counts as rank 6,
    # so a coin that places rarely does not beat a steady top-5 coin
    exp_rank = {}
    for p in range(places):
        for mint, c in place_counts[p].items():
            exp_rank[mint] = exp_rank.get(mint, 0.0) + (p + 1) * c / total_sims
    placed_mass = {}
    for p in range(places):
        for mint, c in place_counts[p].items():
            placed_mass[mint] = placed_mass.get(mint, 0.0) + c / total_sims
    for mint in exp_rank:
        exp_rank[mint] += (places + 1) * (1.0 - placed_mass.get(mint, 0.0))
    top5 = [mint for mint, _ in sorted(exp_rank.items(), key=lambda kv: kv[1])[:places]]
    # fill with last-mark chart if fewer than `places` coins ever placed
    if len(top5) < places:
        for r in last_rows:
            if r["mint"] not in top5:
                top5.append(r["mint"])
            if len(top5) == places:
                break

    # Sizes: a normal in log10(cap in SOL) around the expected cap at each
    # place. The centre is the k-th largest EXPECTED cap (drift only, no
    # noise), averaged over the answer reads. The Monte Carlo order
    # statistics are the wrong centre: independent noise spreads the top
    # coins apart, while the real top 5 stay tight (new coins keep filling
    # the gaps), so the MC place-cap means sit far too low at places 2-5.
    size_sd = float(SIZE_SD_ENV) if SIZE_SD_ENV is not None else SIZE_SD
    centers = [0.0] * places
    for h in horizons:
        exps = expected_caps(candidates, stats, h)
        for p in range(places):
            centers[p] += exps[p] / len(horizons)
    sizes = []
    for p in range(places):
        mean = centers[p]
        for b in range(len(edges) + 1):
            lo = -math.inf if b == 0 else edges[b - 1]
            hi = math.inf if b == len(edges) else edges[b]
            pr = above(lo, mean, size_sd) - above(hi, mean, size_sd)
            u = math.floor(pr * 0.92 * MICRO)
            if u > 0:
                sizes.append({"place": p + 1, "bin": b, "p": u / MICRO})

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(os.path.join(OUTPUT_DIR, output_name), "w") as f:
        f.write(json.dumps({"probabilities": probabilities, "top5": top5,
                            "sizes": sizes}) + "\n")


if __name__ == "__main__":
    main()
