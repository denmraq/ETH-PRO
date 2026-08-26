# ETH Entry Radar PRO V1.0

- Forked from Core V0.3.7.
- Replaced the old last-1000-trades proxy with accumulated 5m / 15m Flow windows.
- Added Flow 5m / 15m fields to API and iPhone UI.
- Added official Fed/BLS/SEC news ingestion.
- Added HIGH/MEDIUM/LOW impact classification.
- Added post-detection ETH/BTC reaction tracking and News ETH Bias score.
- Added server background monitor.
- Added optional Web Push for signal changes and HIGH-impact news.
- Added SQLite state and Render persistent-disk blueprint.
- Core directional logic remains separate from News Engine: news is displayed as an independent PRO module and does not overwrite the Core LONG/SHORT score in V1.0.
