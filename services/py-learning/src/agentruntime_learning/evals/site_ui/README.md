# Evaluation website

The three public routes (`/`, `/results`, `/datasets`) share a React 19 / HeroUI 3 application. The structure, palette and typography follow `SITE-UI.fig`; component composition and restrained surfaces follow the HeroUI v3 dashboard template. It uses HeroUI Button, Card, Chip, Input and Skeleton, and the Pro NativeSelect web component.

## Build

Install the exact dependencies from `package.json` using an account with access to HeroUI Pro, then run `npm run build`. The committed output lives in `../site_pages/assets/`; the learning image only needs Python to serve it. IBM Plex font files are self-hosted under their included OFL license. Production never downloads packages, fonts or scripts from a third party.

When licensed packages already exist in a separate local installation, set `HEROUI_MODULES` to that `node_modules` directory; `BUILD_MODULES` and `FONT_MODULES` can independently point to build tools and Fontsource packages. These are build-time paths only and are not written into the output.

## Data

- `/api/live.json`: live VictoriaMetrics evaluation records, polled every 20 seconds while the page is visible. Returning to the page triggers an immediate refresh. Event timestamps and last fetch timestamps are displayed separately. Counts are formatted as counts; only explicitly identified ratio metrics become percentages.
- `/api/site.json` and `/api/datasets/<name>.json`: catalog, question contents, scoring views and provenance; refreshed every minute. Held-out questions remain filtered by the existing backend.
- `/api/reports.json`: public historical report summaries, refreshed every minute. The archive is separate from recent metrics to avoid presenting the same evaluation as two new runs. Existing contexts, benchmarks, tuning confidence intervals and failure summaries remain accessible.

`python scripts/eval_dashboard.py --public ... --out DIR/index.html` now writes `DIR/reports.json` atomically alongside the compatibility HTML. Publish `reports.json` in the evaluation archive volume. Existing deployments that mount only `index.html` continue to work through strict JSON extraction; the application never serves that HTML as the results page. Archive refreshes do not require rebuilding the UI.

The server only serves explicitly allowlisted assets and JSON routes. There are no analytics, third-party requests, or embedded report values in the application bundle.

## Validation

Run the existing `test_eval_dashboard_server.py`, `test_eval_site_server.py`, `test_dashboard_live.py`, `test_eval_dashboard.py` and `test_eval_site_pages.py` suites. Set `AR_TEST_BROWSER` and `AR_REQUIRE_BROWSER=1` for required browser coverage. The page suite exercises nonempty historical reports, catalog counts, search, grouping, pairwise question expansion, language switching and desktop/mobile overflow against the actual HTTP service.
