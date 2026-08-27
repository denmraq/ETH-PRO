# ETH Entry Radar PRO V1.6

- Fixes Forward Engine target clustering after rollover.
- Rolled TP1/TP2/TP3 now have minimum forward distance from live price using 15m ATR, 1H ATR, and small percentage floors.
- Nearby 1H swing levels are still considered, but cannot collapse the entire target ladder into a few dollars above/below price.
- Enforces minimum spacing between forward targets.
- Keeps the fast 1m sensitivity, Flow, News/Macro and priority logic unchanged.
