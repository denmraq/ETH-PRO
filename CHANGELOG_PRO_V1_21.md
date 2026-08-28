# PRO V1.21 — Real Liquidation Engine

- Added optional real liquidation map/levels integration.
- Primary provider: CoinGlass Pair Liquidation Map for Binance ETHUSDT (1d).
- Fallback provider: Hyblock Liquidation Levels for ETH Binance perpetual.
- No API key = explicit "not connected" state; the app no longer presents swing-level proxy as a liquidation map.
- Liquidation clusters are converted into a distance-weighted directional feature:
  - clusters above spot = potential short-liquidation fuel / upward magnet;
  - clusters below spot = potential long-liquidation fuel / downward magnet.
- The feature is deliberately bounded to about ±6 percentage points on the 1h forecast, ±4 on 6h, ±3 on 12h.
- A liquidation map cannot flip a reasonably strong base forecast by itself.
- UI now shows provider, map bias, nearest clusters above/below, 1h adjustment and number of levels used.
- Liquidation API responses are cached for 5 minutes to avoid unnecessary API usage.
