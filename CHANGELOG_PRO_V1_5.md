# ETH Entry Radar PRO V1.5

- Added Dynamic Target Rollover / Forward Engine.
- Once TP levels are consumed, already-hit targets are removed from the active forecast and replaced with forward levels based on live price, 5m consolidation structure, 1H swings and ATR.
- Added MARKET STAGE: SETUP / TP1_HIT / TP2_HIT / TARGET_BREAKOUT / CONSOLIDATION_ABOVE / CONSOLIDATION_BELOW.
- Added closed-candle 1m sensitivity layer (small score weight) so the radar reacts faster without allowing 1m noise to dominate 15m/1H/4H.
- Time Engine is recalculated from the rolled-forward geometry after target rollover.
- UI shows MARKET STAGE, FAST 1m and whether targets were rebuilt.
