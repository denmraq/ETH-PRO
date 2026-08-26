# ETH Entry Radar PRO V1.3

- Fixed Render 403 failures from Bybit market-data endpoints.
- Bybit remains primary market source.
- Automatic Binance Futures fallback added for candles, live price, OI history, funding and aggregated trades.
- Macro ETH/BTC reaction ticker also falls back to Binance Futures.
- The dashboard no longer fails with HTTP 500 merely because Bybit blocks a Render egress IP.
