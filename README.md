# AGILE Reporting Platform

Ingests, validates, analyses and visualises standardised reporting data from all
AGILE state offices in Nigeria — 36 states and the FCT — across the programme's
70 KPIs and three financing cohorts.

AGILE is the **Adolescent Girls Initiative for Learning and Empowerment**. Each
state Project Implementation Unit (PIU) reports on a standard template every
period; this platform turns those submissions into a single validated national
dataset, scores their quality, computes performance at state, cohort and
national level, and generates the routine reports the National Project
Coordination Unit (NPCU) has to produce.

---

## What it does

| Module | What it delivers |
|---|---|
| **Ingestion** | Accepts `.xlsx`/`.xls`/`.csv`/`.tsv` state templates, auto-maps their columns onto a unified schema, assigns each state to its financing cohort, and versions every submission with full metadata. |
| **Validation & DQA** | Runs 20 rules across the seven data-quality dimensions, produces state scorecards and a consolidated national summary, and blocks invalid data from the analysis pipeline. |
| **KPI analysis** | Consolidates approved data and computes three layers — state vs state target, national vs national target, and each state's percentage contribution to the national result — all disaggregated by cohort, cross-sectionally and longitudinally. |
| **Cohort analytics** | Compares performance within and across the Original, Additional and Limited financing cohorts on KPI achievement, DQA, contribution, timeliness and completeness. |
| **Dashboard** | A live dashboard that refreshes as data is ingested, with trend lines, DQA scores, cohort comparisons, contribution shares and heatmaps, filterable by state, cohort, period, category and KPI. |
| **Reporting** | Monthly, quarterly, semi-annual and annual reports at national, state or cohort scope, downloadable as Markdown, HTML or PDF. |
| **Platform** | Role-based access control, a full audit trail, historical data for longitudinal tracking, and a documented JSON API for external dashboards. |

---

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env          # then edit SECRET_KEY and the admin password

python -m scripts.seed        # cohorts, 21 states, the 53-indicator framework, calendar
python -m scripts.demo        # optional: realistic demonstration data

uvicorn app.main:app --reload
```

On Windows the activate line is `.venv\Scripts\activate` and the copy is
`copy .env.example .env`; everything else is the same.

| URL | What it is |
|---|---|
| `http://localhost:8000/dashboard` | The dashboard |
| `http://localhost:8000/api/docs` | Interactive API documentation (Swagger UI) |
| `http://localhost:8000/api/redoc` | Reference API documentation |
| `http://localhost:8000/api/v1/health` | Health and readiness check |

The seed script prints the bootstrap administrator credentials (from
`BOOTSTRAP_ADMIN_EMAIL` / `BOOTSTRAP_ADMIN_PASSWORD`). **Change the password
immediately after first sign-in**, and set a strong `SECRET_KEY` before serving
real traffic.

---

## Updating an existing install

Three things move independently, and only one of them looks after itself.

| | Automatic? | What to run |
|---|---|---|
| **Database schema** | Yes | Nothing. New tables and columns are added on startup. |
| **Application code** | No | `git pull`, or download the branch as a ZIP and replace the folder. |
| **Reference data** | No | `python -m scripts.seed` |
| **Reported figures** | No | They stay as they are. |

```bash
git pull                             # or replace the folder from a fresh ZIP
pip install -r requirements.txt      # in case dependencies moved
python -m scripts.seed               # refresh the catalogue, states and rules
uvicorn app.main:app --reload
```

`init_db()` adds any table or column the models declare and the database does
not have. It only ever adds — never drops, renames or retypes — and it skips
anything it cannot add without inventing a value for the rows already there.
So a schema change never arrives as "no such column".

Re-seeding is an **upgrade**, not an accumulation: an indicator or state the
seed files no longer carry is retired rather than left active, so a framework
revision cannot leave the previous catalogue running alongside the new one.
Retired, not deleted — submissions, targets and findings still point at them,
and a report published last quarter has to stay readable.

**If your database predates the 53-indicator recode** it holds the old `KPI-0xx`
catalogue and figures reported against it. Re-seeding retires those codes
correctly, but the figures underneath them refer to indicators that no longer
exist, so the honest move is to start clean:

```bash
python -m scripts.seed --reset       # destructive: drops everything first

python -m scripts.load_npcu_models \
    --q2 "AGILE_Q2_2026_Analysis_Model_Flagged.xlsx" \
    --q1 "AGILE_Q1_2026_Analysis_Model_v3_5.xlsx" \
    --crosswalk "AGILE_Q1_vs_Q2_2026_Model_Comparison.xlsx"
```

That loads Q2 as the current period and Q1 as history, translating Q1's codes
through the crosswalk so period-over-period comparisons work across the recode.
`--skip-q1` loads Q2 on its own. Or run `python -m scripts.demo` instead for
generated data on the same 53-indicator framework.

---

## How data flows

```
 state template (.xlsx/.csv)
          │
          ▼
   ┌─────────────┐   headers auto-mapped to the unified schema; state, period,
   │  INGESTION  │   cohort, version and file hash recorded
   └──────┬──────┘
          ▼
   ┌─────────────┐   20 rules across 7 DQA dimensions
   │ VALIDATION  │──► blocking error or score below the minimum
   └──────┬──────┘        └──► REJECTED · is_current cleared · never analysed
          ▼
   ┌─────────────┐   only APPROVED + current submissions are read
   │  ANALYSIS   │   state layer · national layer · contribution layer
   └──────┬──────┘   all disaggregated by financing cohort
          ▼
   dashboard · API · routine reports
```

The gate is the point: a submission that fails a blocking rule, or scores below
`DQA_MINIMUM_SCORE`, is left `REJECTED` with `is_current` cleared, so the
analysis engine never reads it. Re-uploading creates a new version and
supersedes the previous one, so exactly one submission per state and period is
ever authoritative.

---

## The seven data-quality dimensions

Every submission is scored 0–100 on each dimension, then rolled into a weighted
overall score.

| Dimension | What is checked | Weight |
|---|---|---|
| **Integrity** | Numerator within denominator, percentages agreeing with their components, subtotals within parent totals, every row mapped | 1.0 |
| **Timeliness** | Received by the period's deadline; decays 3 points per day late | 1.0 |
| **Accuracy** | Achievement plausible against target, values not statistical outliers against the state's own history, usable denominators | 1.5 |
| **Completeness** | Share of the indicators the state was obliged to report that carry a value | 1.5 |
| **Consistency** | Period-over-period movement within band; cumulative indicators never decrease | 1.0 |
| **Validity** | Numeric, within the indicator's range, 0–100 for percentages, approved disaggregation labels | 1.5 |
| **Uniqueness** | No duplicate indicator rows, no duplicate file, one current submission per state and period | 0.5 |

Five dimensions use a penalty model — a dimension's score is
`100 × (1 − penalty ÷ checks)`, where each finding costs a fraction of one check
according to its severity. Completeness and timeliness are measured directly,
because a penalty model would understate them.

Because a weighted mean can hide one badly failing dimension, the headline
*grade* is additionally capped by the weakest dimension: the score says how much
passed, the grade says whether anything needs attention.

Rules are declared in `app/services/validation/rules.py` and mirrored into the
`validation_rules` table at startup, so an administrator can retune a threshold,
change a severity or disable a rule through the API without a redeploy.

---

## The three analysis layers

For every indicator and period the engine produces:

1. **State layer** — each state's value against its own state-level target,
   direction-aware (for indicators where lower is better, progress is measured
   along the baseline → target journey).
2. **National layer** — the consolidated national value against the national
   target. Counts are summed; percentages are weighted by their own numerators
   and denominators, so a small state at 100% cannot outweigh a large state
   at 50%.
3. **Contribution layer** — each state's percentage share of the national
   result, using the reported value for additive indicators and the numerator
   for indicators aggregated as a weighted average.

All three are disaggregated by financing cohort, and the same primitives serve
cross-sectional analysis (one period, many states) and longitudinal analysis
(one scope, many periods).

---

## Roles

| Role | Can do |
|---|---|
| **ADMIN** | Everything, including user management and deletion |
| **NPCU** | Upload, approve, analyse, report, manage reference data and API keys, read the audit trail |
| **ME_OFFICER** | Upload, approve, analyse, report, read the audit trail |
| **STATE_PIU** | Upload and read — scoped to their own state only |
| **VIEWER** | Read data and analysis |

`STATE_PIU` accounts are hard-scoped: they cannot upload for, read, or report on
any state but their own. API keys can be issued for external dashboards at
`VIEWER`, `ME_OFFICER` or `STATE_PIU` level — never `ADMIN` or `NPCU` — and are
stored only as a hash.

---

## API

All endpoints live under `/api/v1`. Authenticate with
`POST /api/v1/auth/login` and send `Authorization: Bearer <token>`, or send
`X-API-Key: <key>` for machine integrations.

```bash
TOKEN=$(curl -s localhost:8000/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"admin@agile.gov.ng","password":"ChangeMe!2024"}' | jq -r .access_token)

# Three-layer analysis for one KPI
curl -s "localhost:8000/api/v1/analytics/indicators/KPI-001?period=2026-Q2" \
  -H "Authorization: Bearer $TOKEN" | jq .

# Consolidated national DQA summary
curl -s "localhost:8000/api/v1/quality/national?period=2026-Q2" \
  -H "Authorization: Bearer $TOKEN" | jq '.national_score, .grade'

# Generate a quarterly national report
curl -s localhost:8000/api/v1/reports -X POST \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"period_code":"2026-Q2","scope":"NATIONAL","formats":["markdown","pdf"]}' | jq .
```

Full endpoint documentation is in [`docs/API.md`](docs/API.md) and, live, at
`/api/docs`.

---

## Project layout

```
app/
├── core/          config, enums, errors, security, logging, event bus
├── db/            declarative base, engine, session management
├── models/        SQLAlchemy models (reference, submission, validation, user, audit, report)
├── schemas/       Pydantic request/response models
├── services/
│   ├── ingestion/ parser · schema auto-mapper · pipeline · template generator
│   ├── validation/rule catalogue · engine and DQA scoring
│   ├── reporting/ document model · Markdown/HTML/PDF renderers · builder
│   ├── analytics.py  three-layer KPI engine, trends, heatmaps
│   ├── cohort.py     cohort analytics
│   ├── dqa.py        state, cohort and national scorecards
│   ├── dashboard.py  dashboard payloads
│   ├── reference.py  lookups and reporting-calendar generation
│   └── audit.py      audit trail writer
├── api/           dependencies, middleware, v1 routers
└── web/           dashboard templates and self-contained CSS/JS
seeds/             cohorts, 37 states, 5 categories, the 70-indicator catalogue
scripts/           seed.py · demo.py
tests/             unit and end-to-end tests
docs/              API reference, data model, deployment notes
```

---

## Configuration

Every setting is an environment variable; see [`.env.example`](.env.example) for
the annotated list. The ones that matter most:

| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./storage/agile.db` | Point at PostgreSQL for production |
| `SECRET_KEY` | development placeholder | **Must** be replaced in production |
| `DQA_MINIMUM_SCORE` | `60` | Submissions below this cannot be approved |
| `CONSISTENCY_CHANGE_THRESHOLD_PCT` | `200` | Period-over-period change that triggers a flag |
| `ACCURACY_TARGET_RATIO_PCT` | `300` | Achievement above this share of target is flagged |
| `MAX_UPLOAD_MB` | `50` | Upload size limit |

SQLite is the zero-setup default and is fine for evaluation. For 36 states + FCT
of accumulating historical data, set `DATABASE_URL` to a PostgreSQL DSN — the
engine switches to a pooled connection automatically.

---

## Scripts

```bash
python -m scripts.seed                              # reference data + calendar
python -m scripts.seed --years 2025 2026 2027       # specific fiscal years
python -m scripts.seed --reset                      # drop everything first (destructive)

python -m scripts.demo                              # last 4 quarters, all states
python -m scripts.demo --periods 2026-Q1 2026-Q2    # specific periods
python -m scripts.demo --year 2026 --format csv     # a whole year, CSV templates

python -m scripts.load_npcu_models --q2 MODEL.xlsx --q1 MODEL.xlsx \
    --crosswalk COMPARISON.xlsx                     # the real NPCU returns
python -m scripts.build_seeds                       # rebuild seeds/ from the workbooks
```

`scripts/demo.py` pushes generated templates through the **real** ingestion
pipeline, so parsing, auto-mapping, validation, scoring and approval all run as
they would in production. A handful of states are given deliberate quality
problems — a missing block of indicators, a percentage above 100, a duplicated
row, an implausible swing, a late submission — so the DQA scorecards and the
quality gate have something real to show.

---

## Tests

```bash
pytest                    # whole suite
pytest tests/test_api.py  # end-to-end API behaviour
```

The suite covers security primitives, template parsing, schema auto-mapping,
every DQA dimension and the quality gate, achievement and aggregation maths, the
three analysis layers, and end-to-end API behaviour including RBAC and state
scoping.

---

## Notes on the seed data

The 70-indicator catalogue in `seeds/indicators.csv` is a complete,
internally-consistent AGILE results framework — PDO indicators, three project
components and a cross-cutting safeguards group — with each indicator's unit,
aggregation method, direction, cumulativeness, valid range and template aliases.
Cohort assignments in `seeds/states.csv` place all 36 states and the FCT in one
of the three financing windows.

Both files are configuration, not code: edit the CSVs and re-run
`python -m scripts.seed` to match the official results framework and cohort
lists, or change them through the API (`PATCH /api/v1/reference/indicators/{code}`
and `PATCH /api/v1/reference/states/{code}`). Targets are loaded separately via
`POST /api/v1/reference/targets`, and they also define what each state is
obliged to report in a period.
