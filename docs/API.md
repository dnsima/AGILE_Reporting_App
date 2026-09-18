# API reference

Base path: `/api/v1`. Interactive documentation is served at `/api/docs`, and
the OpenAPI schema at `/api/openapi.json`.

## Authentication

Two credential types are accepted.

**Bearer token** — for people and for the dashboard:

```bash
curl -s localhost:8000/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"admin@agile.gov.ng","password":"ChangeMe!2024"}'
```

```json
{
  "access_token": "eyJhbGciOi...",
  "token_type": "bearer",
  "expires_in": 43200,
  "user": { "id": 1, "email": "admin@agile.gov.ng", "role": "ADMIN", "permissions": ["..."] }
}
```

Send it as `Authorization: Bearer <token>`. The same token is also set as an
`HttpOnly` cookie, which is what authenticates the dashboard's event stream —
`EventSource` cannot send headers.

**API key** — for external dashboards and machine integrations. Send
`X-API-Key: <key>`. Keys are issued by an administrator, stored only as a
SHA-256 hash, shown exactly once, and cannot carry `ADMIN` or `NPCU` rights.

## Error envelope

Every error returns the same shape, with an HTTP status and a stable machine
code:

```json
{
  "error": {
    "code": "ingestion_error",
    "message": "Human-readable explanation.",
    "details": { "optional": "structured context" }
  },
  "request_id": "a1b2c3d4e5f6"
}
```

`request_id` also travels in the `X-Request-ID` response header and appears on
every log line for that request.

| Code | Status | Meaning |
|---|---|---|
| `authentication_error` | 401 | Missing, malformed, expired or invalid credentials |
| `permission_denied` | 403 | The caller's role lacks the permission, or the state is out of scope |
| `not_found` | 404 | Unknown state, period, indicator, submission or report |
| `conflict` | 409 | Duplicate file, or a decision the submission's state does not allow |
| `ingestion_error` | 422 | The file could not be read or mapped |
| `validation_error` | 422 | Input was structurally unusable |
| `request_validation_error` | 422 | The request body failed schema validation |
| `internal_error` | 500 | Unexpected failure; quote the `request_id` |

---

## System

| Method | Path | Notes |
|---|---|---|
| `GET` | `/health` | Public. Reports readiness, seeded-data counts and the current data version. |

## Authentication

| Method | Path | Permission |
|---|---|---|
| `POST` | `/auth/login` | public |
| `POST` | `/auth/logout` | public (clears the session cookie) |
| `GET` | `/auth/me` | any authenticated caller |
| `POST` | `/auth/change-password` | any signed-in user |

## Reference data

| Method | Path | Permission |
|---|---|---|
| `GET` | `/reference/cohorts` | `data:read` |
| `GET` | `/reference/states` | `data:read` — filter by `cohort`, `zone` |
| `PATCH` | `/reference/states/{code}` | `reference:manage` — reassign cohort, contact, status |
| `GET` | `/reference/indicator-categories` | `data:read` |
| `GET` | `/reference/indicators` | `data:read` — filter by `category`, `search` |
| `GET` | `/reference/indicators/{code}` | `data:read` |
| `PATCH` | `/reference/indicators/{code}` | `reference:manage` — retune ranges, aliases, direction |
| `GET` | `/reference/periods` | `data:read` — filter by `period_type`, `fiscal_year` |
| `GET` | `/reference/periods/current` | `data:read` — latest closed period |
| `POST` | `/reference/periods` | `reference:manage` |
| `POST` | `/reference/periods/generate` | `reference:manage` — a full fiscal-year calendar |
| `POST` | `/reference/periods/{code}/close` | `reference:manage` |
| `GET` | `/reference/targets` | `data:read` |
| `POST` | `/reference/targets` | `reference:manage` — bulk upsert |

Targets carry a second meaning: where a state has targets for a period, those
indicators are what that state is *obliged* to report, and the completeness
dimension is measured against them.

## Reporting cycles

A period closes when the NPCU has consolidated it and published from it. After
that, a state must not be able to change published figures by uploading a new
file — that is exactly the silent overwrite the change process exists to
prevent.

| Method | Path | Permission |
|---|---|---|
| `POST` | `/reference/periods/{code}/close` | `reference:manage` |
| `POST` | `/reference/periods/{code}/reopen` | `reference:manage` — every state |
| `GET` | `/reference/periods/{code}/reopenings` | `data:read` — state-scoped |
| `POST` | `/reference/periods/{code}/reopenings` | `reference:manage` — one state |
| `POST` | `/reopenings/{id}/revoke` | `reference:manage` |

### Closing is not freezing

Two routes still reach a closed period, and the difference between them is the
whole design:

* **Correcting a figure** goes through the query workflow, unchanged. The state
  proposes a correction with evidence, the NPCU accepts it, the original is kept
  and the restatement is reported. A closed period needs no special permission
  for this, because the permission *is* the acceptance.
* **Re-filing a return** — the wrong file was uploaded, or a return was never
  filed before the cycle closed — is what a correction cannot reach, and it
  needs a reopening.

The refusal says so, rather than being a wall:

```
2026-Q1 is closed, so it takes no new submission from Kebbi. Kebbi's last
reopening for this period was used. To correct a figure, respond to its query
with evidence -- that works on a closed period and keeps the original on
record. To re-file the return itself, ask the NPCU to grant a reopening for
Kebbi.
```

### Reopenings are per state

Reopening a whole period so that one state can re-file lets twenty others change
figures nobody asked about. A reopening is therefore granted to **one named
state**, for a **stated reason** (required — a reopening nobody can account for
is not a control), good for **one submission**, and it **expires** (14 days by
default). Its status is `ACTIVE`, `USED`, `EXPIRED` or `REVOKED`; a used grant
cannot be withdrawn, and a state cannot hold two active grants for one period.

`POST /reference/periods/{code}/reopen` exists for a cycle closed in error or
genuinely resumed. It is blunt on purpose.

### Where the gate sits

`periods.assert_can_submit` runs in the ingestion pipeline before the file is
stored, so a refused upload leaves nothing on disk to reconcile later. It covers
both the multipart upload and the JSON submission endpoint. `PeriodRead` carries
`locked_at`, `lock_note` and `reopened_for` (the states currently holding an
unused grant), so the dashboard can say what is closed and who may still file.
The report's metadata states whether the cycle was open or closed when it was
generated.

## Data queries

A validation finding is not a wall the submission hits. It becomes a query
owned by the state that reported the figure, answered with evidence, and
settled by the NPCU. Nothing here rejects a return, and no figure changes
without an accepted response behind it.

| Method | Path | Permission |
|---|---|---|
| `GET` | `/queries` | `data:read` — worklist, state-scoped |
| `GET` | `/queries/summary` | `data:read` — counts for the caller's scope |
| `GET` | `/queries/{id}` | `data:read` — the query and every response on it |
| `POST` | `/queries/{id}/responses` | `data:upload` — **own state only** |
| `POST` | `/queries/{id}/responses/{rid}/evidence` | `data:upload` — attach a document |
| `POST` | `/queries/{id}/accept` | `data:approve` |
| `POST` | `/queries/{id}/reject` | `data:approve` — back to the state |
| `POST` | `/queries/{id}/verification` | `data:approve` — refer for a site check |
| `POST` | `/queries/{id}/withdraw` | `data:approve` — raised in error |
| `GET` | `/queries/sheets/{state}` | `data:read` — correction sheet |
| `POST` | `/queries/sheets/{state}` | `data:upload` — completed sheet |

### The control

A `STATE_PIU` has `data:upload` and not `data:approve`, so it can respond and
nothing else; the endpoints also check that the query belongs to the caller's
own state, so one state can neither read, answer for, nor pull a correction
sheet belonging to another. Nobody clears their own response into the national
figures.

Only `accept` moves a stored figure, and only when the response proposed one.
`IndicatorValue.original_value` keeps what was first reported, so "as first
reported" and "as currently stated" are both always available and a published
report can be reconciled against the live dashboard.

### Statuses

`OPEN` → `RESPONDED` → `ACCEPTED`, with `REJECTED` returning it to the state
(still open, still the state's work) and `VERIFICATION` parking it for a
physical check on the next supervision or DQA visit. `WITHDRAWN` closes a query
raised in error and releases the figure.

### The held figure

A finding the rules cannot work around quarantines its figure: excluded from
every aggregation, but still on record and visible, rather than taking the
whole return down with it. Two rules can flag one figure, so settling a query
releases it **only if no other open query still holds it** — `held_by` on the
query detail names the ones that do.

### Restating an earlier period

`restates_period_code` names the period a correction applies to, which is not
always the period queried. A cumulative figure that appears to fall is usually
put right by restating the *earlier* period, once evidence shows the original
was overstated.

### Correction sheets

`GET /queries/sheets/{state}` issues a sheet containing only that state's
flagged figures — never the whole return. Each row carries the query reference,
what was reported and what was flagged, with open columns for the corrected
figure, the evidence and the explanation. Restricting it to flagged rows is the
control: a figure nobody queried cannot be changed by this route. Uploading the
completed sheet turns every filled row into a response on the query it names,
and those responses still wait for the NPCU.

## Reconciliation

States report the same 53 indicators twice: monthly through the performance
tracker and quarterly through the results framework. The two must agree.

| Method | Path | Permission |
|---|---|---|
| `GET` | `/reconciliation` | `data:read` — every state for one period |
| `GET` | `/reconciliation/{state_code}` | `data:read` — one state, state-scoped |

Reconciling is not a subtraction, because a quarter is not built from its months
the same way for every indicator. Each one is folded on its own **time basis**:

| Basis | The quarter is | Needs |
|---|---|---|
| `SNAPSHOT` | its last reported month | only the final month |
| `LATEST` | the most recent Yes/No answer | only the final month |
| `SUM` | its months added together | every month |
| `MAX` / `MIN` | the highest / lowest month | every month |

This is a different axis from `aggregation_method`, which combines *states* into
a national figure. "Girls enrolled" sums across states but does **not** sum
across months, because each month's tracker figure is already a position.

The basis is derived from the catalogue, and the derivation defaults to
`SNAPSHOT`. `is_cumulative` being false means "not a running total since
inception", not "a within-period flow" — fourteen of the 53 are marked that
way, and reading them as sums would tell every state its quarterly enrolment
should equal April + May + June. A wrong `SNAPSHOT` misses a discrepancy; a
wrong `SUM` manufactures hundreds. So `SUM` is opt-in through the indicator's
`time_basis` column (blank in `seeds/indicators.csv` — nothing in the current
framework is a flow), and the reporting template prints the same answer in its
*Monthly rolls up as* column so the two halves cannot drift apart.

### Verdicts

| Status | Meaning |
|---|---|
| `MATCHED` | The streams agree, within tolerance |
| `MISMATCH` | Both figures present and they disagree |
| `TRACKER_MISSING` | Reported for the quarter, absent from a complete tracker |
| `FRAMEWORK_MISSING` | Reported monthly, left out of the quarterly return |
| `INCOMPLETE` | Not enough months are in to reach a verdict |

Counts must agree exactly; rates tolerate 0.1 percentage points for rounding.
A state that has filed **no** tracker return produces no lines at all: 53 rows
of "missing from the tracker" would be true and useless, and would make the
national agreement figure a statement about who has started rather than about
whether figures agree. `states_tracking` is the denominator that agreement is
measured over.

### Queries

Three rules turn a failed reconciliation into the ordinary query workflow:
`REC-001` (the figures disagree), `REC-002` (reported monthly, missing from the
quarter) and `REC-003` (reported quarterly, absent from a complete tracker).
All three are warnings, not blocks — the figure stays, and changes only through
the change-management process — and all three stay silent until the state has
actually filed a tracker return.

A quarterly return is usually filed before the third month of its tracker, so
the verdict is not available when it arrives. Ingesting a monthly return
re-checks the quarter that encloses it, which is when the verdict appears.

## Ingestion

| Method | Path | Permission |
|---|---|---|
| `GET` | `/ingestion/template` | `data:read` — the `.xlsx` template for one state and period |
| `POST` | `/ingestion/upload` | `data:upload` — multipart file upload |
| `POST` | `/ingestion/submissions` | `data:upload` — JSON submission for system integrations |
| `GET` | `/ingestion/submissions` | `data:read` — state-scoped for `STATE_PIU` |
| `GET` | `/ingestion/submissions/{id}` | `data:read` — includes every mapped value |
| `GET` | `/ingestion/submissions/{id}/validation` | `data:read` — findings and DQA scores |
| `POST` | `/ingestion/submissions/{id}/approve` | `data:approve` |
| `POST` | `/ingestion/submissions/{id}/reject` | `data:approve` |
| `POST` | `/ingestion/submissions/{id}/revalidate` | `data:approve` — after a rule change |
| `DELETE` | `/ingestion/submissions/{id}` | `data:delete` (administrators) |

### The reporting template

`GET /ingestion/template?state=KEBBI&period=2026-Q2` builds the workbook that
state fills in. It is generated per state and per period rather than handed out
as one generic file, and that is what closes the gaps the NPCU's consolidation
notes record:

* **every row carries its indicator code**, so a reworded label cannot silently
  drop a figure — monthly-to-quarterly reconciliation becomes a code join, not
  a match on indicator names;
* **the state is bound at upload**, never read from the file, so a copy of
  another state's workbook cannot submit under the wrong name;
* **only the state's own approved target is shown.** National targets are
  deliberately withheld: a state shown the national figure reports against it;
* **each row states the basis its figure must be on** — cumulative to date, a
  month-end snapshot, a rate, Yes/No — instead of one column header that cannot
  say all four at once;
* **rows for sub-components the state does not implement are locked** and
  marked not applicable, so a blank there is never read as a data gap. Ekiti is
  served 51 of the 53 rows, a Limited Financing state 37.

The period type picks the layout: a monthly period issues the performance
tracker, anything else the results framework. Both are built from the one
indicator catalogue, so the two streams carry identical codes and wording.

The sheet is protected, with only the entry, data-source and comment cells
unlocked. There is no password — a state can unlock it if it must, and the
platform validates what arrives regardless.

### Uploading

`POST /ingestion/upload` takes multipart form data:

| Field | Required | Notes |
|---|---|---|
| `file` | yes | `.xlsx`, `.xlsm`, `.xls`, `.csv` or `.tsv` |
| `state_code` | no | Detected from the file's cover block if omitted |
| `period_code` | no | Detected from the file's cover block if omitted |
| `notes` | no | Free text kept with the submission |
| `auto_approve` | no | Honoured only for `ADMIN`, `NPCU` and `ME_OFFICER` |
| `allow_duplicate` | no | Permits re-submitting a byte-identical file |

The response reports whether the data was accepted, how each column was mapped,
and every validation finding:

```json
{
  "accepted": true,
  "message": "Ingested 70 value(s) for KN/2026-Q2. DQA score 97.4 (Excellent).",
  "submission": { "id": 412, "version": 2, "status": "APPROVED", "dqa_score": 97.4, "...": "..." },
  "diagnostics": {
    "template_profile": "long",
    "detected_state": "KN",
    "detected_period": "2026-Q2",
    "header_row": 6,
    "mapped_rows": 70,
    "unmapped_rows": 0,
    "column_mappings": [
      { "source_header": "Actual Achievement", "mapped_field": "value", "confidence": 1.0, "strategy": "exact" }
    ],
    "unmatched_indicators": []
  },
  "validation": {
    "passed": true, "blocking": false, "overall_score": 97.4, "grade": "Excellent",
    "dimensions": [ { "dimension": "COMPLETENESS", "score": 100.0, "grade": "Excellent" } ],
    "issues": []
  }
}
```

When `accepted` is `false` the submission is left `REJECTED` with `is_current`
cleared, and no analysis endpoint will ever read it.

## Data quality

| Method | Path | Permission |
|---|---|---|
| `GET` | `/quality/scorecards/{state_code}` | `data:read` — state DQA scorecard |
| `GET` | `/quality/national` | `data:read` — consolidated national summary |
| `GET` | `/quality/rules` | `data:read` — the rule catalogue |
| `PATCH` | `/quality/rules/{code}` | `reference:manage` — severity, thresholds, on/off |

### Why the grade can sit below the score

Two numbers, answering two different questions. **The score** is the proportion
of applicable checks that passed, weighted across the seven dimensions. **The
grade** additionally accounts for what a per-check score cannot see.

A per-check pass rate is pinned near 100 for any plausible return: 53
indicators against 26 rules is thousands of checks, and a hundred failures
against that is 99%. Kebbi's Q2 scored 94.8 with **sixteen of its fifty-three
figures held out of the national totals** — "Excellent" by score, and
misleading.

So the grade is capped:

| Condition | Grade drops |
|---|---|
| Below 90% of figures counting towards the totals | one band |
| Below 75% | two bands |
| Weakest dimension below 80 | one band |
| Weakest dimension below 60 | two bands |

The caps are taken at their **maximum**, not added: held figures usually *are*
the findings driving a weak dimension, and charging twice for one problem would
be its own kind of dishonesty. Wherever a capped grade appears it carries
`grade_note` saying why, because a grade nobody can account for is worse than
no grade. `usable_share_pct`, `figures_reported` and `figures_counting` are on
the scorecard and the national summary.

### Checks are counted only where they could run

A rule's weight is the number of checks it contributes to its dimension's
denominator. Left at "one per reported figure" it counted checks nothing could
perform: `INT-001` needs a numerator and a denominator, and no figure on the Q2
return carries either, so it declared 929 checks and ran none. Six rules were
in that position, inflating the period's denominator from 7,872 real checks to
15,139 and diluting every genuine finding against thousands that never
happened.

Each such rule now weighs exactly the figures it iterates, through a shared
candidate function, so the weight and the rule body cannot drift apart when one
of them is edited. `checks_run` on every dimension is now a true count.

Fixing this had a second effect worth knowing: the weak-dimension cap, which
had been in the code all along, had never once fired. With an honest
denominator Kebbi's consistency reads 76 and Plateau's integrity 67, and the
cap does its job.

### Dimensions that could not be assessed

A dimension where nothing applicable could be checked scores `null`, not 100.
It is an unanswered question, not a perfect one, and it is left out of the
weighted mean rather than carried into it as a free pass. No `dqa_scores` row
is written for it, because any number stored there would be a claim the data
does not support. This is reachable in practice: the first submission of a
brand-new period has no previous figures, no targets and no peers, so accuracy
and consistency are genuinely unassessable.

## KPI analysis

| Method | Path | Notes |
|---|---|---|
| `GET` | `/analytics/indicators/{code}` | The full three-layer analysis for one KPI |
| `GET` | `/analytics/scorecard` | Every indicator for one scope and period |
| `GET` | `/analytics/contribution/{code}` | States ranked by share of the national result |
| `GET` | `/analytics/trend/{code}` | Longitudinal series |
| `GET` | `/analytics/heatmap` | States × indicators |

All require `analytics:read`. Common query parameters: `period` (defaults to the
latest closed period), `scope` (`NATIONAL`, `STATE`, `COHORT`), `state`,
`cohort`, `category`, `indicator_codes`.

## Cohort analytics

| Method | Path | Notes |
|---|---|---|
| `GET` | `/cohorts/summary` | One row per cohort: KPI, DQA, contribution, timeliness, completeness |
| `GET` | `/cohorts/compare` | Across-cohort comparison, optionally for one KPI |
| `GET` | `/cohorts/{code}/states` | Within-cohort state ranking |
| `GET` | `/cohorts/{code}/dqa` | Cohort-level DQA summary |

## Dashboard

| Method | Path | Notes |
|---|---|---|
| `GET` | `/dashboard/overview` | Headline tiles, cohort rows, reporting status |
| `GET` | `/dashboard/state-rankings` | States ranked by average achievement |
| `GET` | `/dashboard/dqa-heatmap` | States × periods DQA scores |
| `GET` | `/dashboard/version` | Cheap poll target: current data version |
| `GET` | `/dashboard/stream` | Server-sent events; pushes on every data change |

The stream emits `hello` on connect, `change` whenever data is ingested,
validated or approved, and a comment heartbeat every 20 seconds so proxies keep
the connection open.

## Reporting

| Method | Path | Permission |
|---|---|---|
| `POST` | `/reports` | `reports:generate` |
| `GET` | `/reports` | `reports:generate` — the report library |
| `GET` | `/reports/{id}/download?format=markdown\|html\|pdf` | `reports:generate` |

Request body:

```json
{
  "period_code": "2026-Q2",
  "scope": "NATIONAL",
  "scope_ref": null,
  "indicator_codes": null,
  "category_codes": ["PDO"],
  "formats": ["markdown", "html", "pdf"],
  "include_dqa": true,
  "include_queries": true,
  "include_trends": true,
  "include_narratives": true,
  "include_state_tables": true,
  "trend_periods": 6
}
```

`scope_ref` is a state code for `STATE` scope and a cohort code for `COHORT`
scope. `STATE_PIU` accounts may only generate state-scoped reports for their own
state.

Section numbers are computed from the order the sections were added, so a
report with a section switched off is still numbered 1..n with no gap.

### Figures under query

A reporting cycle cannot wait on a query, so the report publishes anyway. What
it must not do is print a provisional total as though it were settled, and
`include_queries` (default true) is what stops it. The section sits **before**
the KPI numbers, because what is unconfirmed qualifies every figure that
follows and a reader who meets it in an appendix has already drawn conclusions.
The same caveat is repeated in the executive summary, which is the part that
actually gets read; it stands even when the detail section is switched off.

The section carries:

1. **How much is unresolved** — open, overdue, sitting with the NPCU, settled,
   and how many were settled by an accepted correction.
2. **Which printed totals are provisional** — the indicators in this report
   whose national total excludes a held figure, and which states are held out.
   This is the part that makes the section load-bearing rather than an
   appendix: a national total that silently included a figure the rules could
   not accept would be neither what the states reported nor what the NPCU can
   stand behind.
3. **Where the work sits** — unresolved queries per state, with the earliest
   response date outstanding.
4. **The figures themselves** — reference, state, KPI, the figure as reported
   and the finding verbatim, capped at 40 with a note when there are more.
5. **Referred for physical verification** — the items to carry into the next
   supportive supervision or DQA exercise. An unconfirmed figure becomes
   something someone goes and looks at.
6. **Figures restated for this period** — what changed and why, so a report
   published earlier can be reconciled against this one.

Scope is honoured: a state report covers that state's queries, a cohort report
that cohort's.

## Administration

| Method | Path | Permission |
|---|---|---|
| `GET` | `/admin/users` | `users:manage` |
| `POST` | `/admin/users` | `users:manage` |
| `PATCH` | `/admin/users/{id}` | `users:manage` |
| `POST` | `/admin/users/{id}/deactivate` | `users:manage` |
| `GET` | `/admin/api-keys` | `apikeys:manage` |
| `POST` | `/admin/api-keys` | `apikeys:manage` |
| `DELETE` | `/admin/api-keys/{id}` | `apikeys:manage` |
| `GET` | `/admin/audit` | `audit:read` — filter by entity, action, actor, state |

The audit trail is append-only: it has read endpoints and no update or delete.
