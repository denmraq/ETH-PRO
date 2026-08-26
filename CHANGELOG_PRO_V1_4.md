# ETH Entry Radar PRO V1.4

- Added OKX perpetual market data as fallback before Binance.
- Added Coinbase Exchange spot market data as final fallback for candles, live price and trade flow.
- OI history now degrades gracefully instead of crashing when derivative venues block Render.
- Funding uses Bybit -> OKX -> Binance and becomes neutral/unavailable only if all fail.
- Macro ETH/BTC reaction ticker now uses Bybit -> OKX -> Binance -> Coinbase.
- Goal: keep /api/radar alive on Render even when Bybit returns 403 and Binance Futures returns 451.
