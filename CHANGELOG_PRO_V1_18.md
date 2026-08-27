# PRO V1.18 PREDICTOR

- 1h forecast is frozen inside each closed 5m candle window.
- 6h forecast updates only on a new closed 15m candle.
- 12h forecast updates once per 30-minute bucket.
- Normal LONG/SHORT reversals require confirmation in two consecutive forecast windows.
- Very strong reversal probabilities may flip immediately.
- Repeated manual refreshes no longer make the 1h forecast chase the current candle.
- TP/SL still refresh from the current price while direction remains the locked forecast.
