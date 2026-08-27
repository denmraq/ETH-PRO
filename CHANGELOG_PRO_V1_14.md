# ETH Entry Radar PRO V1.14

- PRO scalp output now exposes only one nearest TP from the live price.
- TP2/TP3 are removed from the UI/API output (kept as null compatibility fields).
- 6h/12h forecasts can no longer stretch the active scalp take-profit.
- Direction remains 1h/6h/12h LONG or SHORT only.
- Macro/news reaction is now a bounded input into the forward direction instead of a separate decorative panel only.
- News never overrides the market model: LOW/WAITING events have small weight; confirmed HIGH-impact events can shift probabilities more.
- Push logic from V1.13 is preserved.
