# Methodology

This document explains what rs-radar measures and why. The implementation lives in
[`signals.py`](../src/rs_radar/signals.py) (pure functions) and [`scanner.py`](../src/rs_radar/scanner.py)
(data orchestration). Every rule below is covered by tests.

## The question

> Did this instrument just move far more than its benchmark explains, on real volume, inside an
> established uptrend?

Each part of that sentence maps to one measurement. A signal needs all of them.

## 1. Clean, comparable data

- **Universe.** Crypto: CoinGecko's top 200 by market cap, minus non-directional assets
  (stablecoins, tokenised gold and treasuries, wrapped and liquid-staking duplicates). Stocks: ~100 US
  large caps. A ticker whose exchange price disagrees with the reference price by more than 8% is
  dropped as a *ticker collision* (the same symbol listing a different asset).
- **Alignment.** Every series is projected onto the benchmark's 1-minute grid. Thin markets have no
  bar for minutes without trades, so gaps are forward-filled with zero-volume bars. Series where fewer
  than 80% of minutes had real trades are rejected: momentum on a mostly-synthetic series is noise.
- **Liveness.** A snapshot is *live* only when the latest benchmark bar is under 10 minutes old and
  the last 16 bars are contiguous. Signals are never produced from non-live data, which rules out
  weekend data and overnight gaps that would otherwise look like a 5-minute breakout at the open.

## 2. Beta-adjusted relative strength

Raw outperformance is misleading: a coin with β = 2 *should* rise 2% when BTC rises 1%. rs-radar
estimates β from 1-minute log returns over the history window, **excluding the window being
evaluated** (the move must not bias its own baseline), and clamps it to [0.3, 3.0] because β
estimates explode when the benchmark is nearly flat.

```
excess_n = return_n(asset) − β × return_n(benchmark)
```

This is reported for 5m, 15m, 1h and 4h windows.

## 3. Surprise: is this unusual *for this instrument*?

A 2% excess move is routine for a small-cap meme coin and extraordinary for a large cap. The excess
5-minute return is divided by the standard deviation of the same instrument's non-overlapping 5-minute
excess returns over the history window:

```
z = excess_5m / stdev(prior 5m excess returns)
```

## 4. Participation

`volume_ratio = volume(last 5m) / max(median 5m volume, ½ × mean 5m volume)`

The median resists one-off spikes in the baseline; the mean floor stops thin baselines from producing
absurd ratios.

## 5. Gates

All must pass before a move is scored:

| Gate | Crypto | Stocks |
|---|---|---|
| 5m excess vs benchmark | ≥ 1.5 pp | ≥ 0.5 pp |
| 5m raw return | ≥ +1.0% | ≥ +0.4% |
| Surprise (z) | ≥ 3 | ≥ 3 |
| Volume ratio | ≥ 2× | ≥ 2× |
| Trend | price > EMA20 > EMA50 | same |
| Extension | ≤ 3 ATR above EMA20 | same |

The trend timeframe is 4h for crypto and 1h for stocks (Yahoo has no 4h bars). Trend candles are only
fetched for instruments that pass the 5m gates, which keeps a full scan to roughly one request per
instrument.

## 6. Confidence (0–100)

| Component | Points |
|---|---|
| Surprise: 10 at z = 3, +10 per extra σ | ≤ 30 |
| Size: 10 at the excess gate, scaling with excess | ≤ 20 |
| Participation: 5 at 2×, scaling with volume | ≤ 15 |
| Broke the prior 60-minute high | 10 |
| Positive excess on both 15m and 1h (not a one-bar blip) | 10 |
| Held a benchmark dip: benchmark fell ≥ 0.3% in 15m, asset lost < half its β share | 10 |
| Beat the benchmark in ≥ 4 of the last 5 one-minute bars | 5 |

A move that only just clears the gates scores about 25. Reaching the bot's default threshold of 85
requires several independent confirmations. Replaying a 12-hour window of the crypto universe produced
about 4 alerts at 85 versus 11 at 75, which is why 85 is the default.

## 7. Trade plan

- **Entry**: the live price. **Max entry**: entry + 1%; above that, the setup has run away.
- **Stop**: just under the lowest low of the last 6 closed trend candles (minus 0.25 ATR) when that
  sits 1–2 ATR below entry, otherwise entry − 1.5 ATR. The stop is therefore always 1–2 ATR away.
- **Targets**: TP1 at 1.5R (take partial profit, move the stop to entry), TP2 at 3R.

## Known limitations

- Signals are long-only for now.
- Thresholds were chosen by inspection and a short replay, not an out-of-sample backtest. A backtest
  harness is on the roadmap, and contributions are welcome.
- Free data sources can lag or rate-limit; the scanner reports skipped symbols rather than failing.
