# ETH Entry Radar PRO V1.7

- Added Forward Outlook for next 15m and 60m: UP / RANGE / DOWN probabilities.
- Added 60m expected price distribution (10/50/90 percentiles).
- Added live breakout / breakdown probabilities and structural levels.
- Rebuilt Time Engine: TP-first / SL-first / neither and TP timing now come from 4,000 forward path simulations seeded from the current market state, not nearest historical analog outcomes.
- Historical analogs remain only as a secondary calibration reference in data-quality diagnostics.
- Added forecast confidence and fast-vs-60m momentum delta.
- Forecast uses current multi-timeframe EMA/momentum, 5m/15m flow, OI context, funding, realized volatility and regime.
- Important: this is a probabilistic forecast model; no algorithm has access to future market data.
