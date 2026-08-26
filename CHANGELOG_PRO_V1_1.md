# ETH Entry Radar PRO V1.1

- Server monitor continues every 60s independently of iPhone/PWA state.
- Added confirmed priority layer for push notifications.
- LONG priority only when LONG score >= SHORT score + 5.
- SHORT priority only when SHORT score >= LONG score + 5.
- Gap < 5: no priority push and previous confirmed priority is retained.
- Push fires only when confirmed priority changes LONG ↔ SHORT.
- Push message includes previous priority, new priority, elapsed time, and both scores.
- Example: LONG 30 / SHORT 35 => confirmed SHORT priority.
- Initial server startup seeds the current priority silently to avoid a false "change" alert.
