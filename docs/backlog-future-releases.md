# Future Release Backlog

Items explicitly deferred from the current release. Do not forget these.

---

## Semantic Map Performance (Next Version)

**Problem:** The semantic map becomes slow with 50+ indexed files. PCA computation and canvas rendering are the bottlenecks.

**Planned improvements:**
- **Stable cache** — Reduce cache invalidation frequency. Currently invalidates on any index change. Should only invalidate when file-level aggregations actually change.
- **Precomputed map data** — Compute PCA projections during indexing, not on first page load. Store alongside the index.
- **Reduced redraw cost** — Canvas renders all points with glow effects on every mouse event. Use requestAnimationFrame batching and skip glow for non-hovered points.
- **Light rendering mode** — For 200+ files, switch to a simpler dot renderer without glow/shadow effects.
- **Large dataset guardrails** — If 500+ files, show a sampling of representative points instead of all points. Downsample by cluster centroid.
- **ECharts 3D** — ECharts GL already handles large datasets better than Canvas 2D. Consider making 3D the default for large collections.

**Files likely affected:**
- `src/ragtools/service/map_data.py` — PCA computation, caching
- `src/ragtools/service/static/map.js` — Canvas rendering
- `src/ragtools/service/owner.py` — Cache invalidation

---

## Code Signing (Future)

**Problem:** Windows SmartScreen shows "Unknown publisher" warning on the installer. Scares users.

**Solution:** Purchase a code signing certificate and sign both `rag.exe` and the installer.

**Options:**
- Standard EV code signing certificate (~$300/year) — immediate SmartScreen trust
- Standard OV certificate (~$70-200/year) — builds trust over time with download volume
- Free via SignPath.io (for open source) — if the project qualifies

**Process:**
1. Obtain certificate from a trusted CA (DigiCert, Sectigo, SSL.com)
2. Add `signtool sign /tr http://timestamp.digicert.com /td sha256 /fd sha256 /a rag.exe` to build.py
3. Sign the installer after Inno Setup build
4. Verify SmartScreen no longer warns

**Files affected:**
- `scripts/build.py` — Add signing step
- CI/CD pipeline — Store certificate securely
