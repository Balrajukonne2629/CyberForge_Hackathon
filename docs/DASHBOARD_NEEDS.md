# Dashboard Backend Needs & Status

This document tracks backend capabilities needed by the demo dashboard.

## Status Summary

1. **`GET /tenants/{id}/stream` (Server-Sent Events)**:
   - **Status**: Implemented in `api/main.py` using PostgreSQL `NOTIFY rollup_updated`.
   - **Frontend Integration**: Dashboard abstracts all data fetching through `getTenantData()`. It currently defaults to 1 s polling with visibility awareness (paused when document is hidden) and seamlessly supports SSE streaming once connected.

2. **Scaling Results (`docs/results/scaling.json`)**:
   - **Status**: Measured and available at `docs/results/scaling.json`.
   - **Frontend Integration**: Loaded dynamically by the Scale tab via `/admin/platform/scaling`. Displays "not measured yet" if missing.

3. **Quarantine Metrics**:
   - **Status**: Exposed via `GET /admin/platform/summary` querying `rollup_1m.quarantine`.
   - **Frontend Integration**: Displayed in the Health tab along with stalled tenant counts and max lag history.

No blocking backend changes currently required.
