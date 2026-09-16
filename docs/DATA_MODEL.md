# Data model

All tables use surrogate integer primary keys and carry `created_at` /
`updated_at`. Foreign keys are enforced (including on SQLite, where the pragma
is set on every connection).

```
cohorts ──< states ──< submissions >── reporting_periods
                           │
                           ├──< indicator_values >── indicators >── indicator_categories
                           ├──< validation_issues
                           └──< dqa_scores

targets >── indicators, reporting_periods, states (nullable for national targets)
users ──< submissions (uploaded_by, approved_by), api_keys, audit_logs
```

## Reference tables

### `cohorts`
The three AGILE financing cohorts. `code` is one of `ORIGINAL`, `ADDITIONAL`,
`LIMITED`; `sort_order` fixes their presentation order everywhere.

### `states`
The 36 states and the FCT. `code` is the short key used in uploads and API
calls; `cohort_id` assigns the financing cohort and can be reassigned through
the API as states move between windows.

### `indicator_categories`
The results-framework grouping — PDO, the three components, and the
cross-cutting safeguards group.

### `indicators`
The 70 KPIs. Beyond name and definition, each row carries the metadata the rest
of the platform reasons about:

| Column | Why it matters |
|---|---|
| `unit` | `NUMBER`, `PERCENT`, `RATIO`, `CURRENCY_NGN_M`, `SCORE` |
| `aggregation_method` | How states roll up nationally: `SUM`, `WEIGHTED_AVERAGE`, `AVERAGE`, `MAX`, `MIN`, `LATEST` |
| `direction` | `INCREASE` or `DECREASE` — whether higher or lower is better |
| `is_cumulative` | Cumulative totals may never fall between periods |
| `requires_numerator_denominator` | Needed so national percentages can be weighted correctly |
| `baseline_value` | Anchors achievement for lower-is-better indicators |
| `min_value` / `max_value` | Enforced by the validity dimension |
| `aliases` | Alternative header spellings, used by the schema auto-mapper |
| `is_core` | Whether the indicator is expected when no targets are set |

### `reporting_periods`
Monthly, quarterly, semi-annual and annual windows. `code` is the public
identifier (`2026-M03`, `2026-Q2`, `2026-H1`, `2026-A`), `due_date` drives the
timeliness dimension, and `is_open` controls whether new submissions are
accepted.

### `targets`
One row per indicator, period and level. `level` is `STATE` (with `state_id`) or
`NATIONAL` (`state_id` null). A unique constraint prevents conflicting targets
for the same combination. Where state targets exist for a period, they also
define that state's reporting obligation for the completeness dimension.

## Submission tables

### `submissions`
One upload of one state's template for one period. Unique on
`(state_id, period_id, version)`.

| Column | Notes |
|---|---|
| `version`, `is_current` | Re-uploading increments the version and supersedes the previous current row |
| `status` | `DRAFT`, `UPLOADED`, `VALIDATED`, `REJECTED`, `APPROVED`, `SUPERSEDED` |
| `file_hash` | SHA-256 of the uploaded bytes; detects accidental re-uploads |
| `stored_file_path` | The original file is retained for audit |
| `row_count`, `mapped_count`, `unmapped_count` | Ingestion counters |
| `dqa_score`, `dqa_grade`, `error_count`, `warning_count` | Validation outcome |
| `ingestion_report` | JSON mapping diagnostics, kept for the audit trail |
| `rejection_reason` | Why the quality gate refused it |

Only `is_current` **and** `APPROVED` rows are read by the analysis engine.

### `indicator_values`
One reported figure. Alongside `value`, rows keep `numerator`/`denominator` (so
national percentages can be weighted), the verbatim `raw_value` (so parsing
decisions stay auditable), a JSON `disaggregation` (`{"sex": "female"}`), the
`data_source`, and `source_row` pointing back at the line in the uploaded file.

## Validation tables

### `validation_rules`
The rule catalogue, mirrored from code at startup. Administrators may change
`severity`, `config` (thresholds), `is_blocking` and `is_active`; descriptive
fields are refreshed from code on each start, and operator overrides are left
alone.

### `validation_issues`
One row per finding: the rule, its dimension and severity, the indicator and
field, a human-readable message, the observed and expected values, the source
row, and whether the finding blocks the pipeline.

### `dqa_scores`
One row per submission and dimension — score, weight, checks run, checks failed,
and a JSON `details` payload (days late, indicators expected versus reported).

## Platform tables

### `users`
Email, name, bcrypt hash, role, and — for `STATE_PIU` accounts — the `state_id`
that scopes everything they can see or do.

### `api_keys`
Read-oriented credentials for external dashboards. Only the SHA-256 hash is
stored; the key itself is shown once at creation. Carries a role, an optional
state scope, an expiry and a revocation flag.

### `audit_logs`
Append-only. Every ingestion, approval, rejection, reference edit and
administrative action records the actor, the entity, before/after JSON
snapshots, the request id, and the caller's IP and user agent.

### `generated_reports`
The report library: title, scope, period, formats rendered, on-disk paths, the
parameters used, and the executive summary.

## Indexing

Indexes exist on every foreign key and on the access paths that matter:
`submissions(state_id, period_id, is_current)`,
`indicator_values(submission_id, indicator_id)`,
`targets(period_id, level, state_id)`, `validation_issues(submission_id,
dimension)`, and `audit_logs(entity_type, entity_id)` plus `created_at`.
