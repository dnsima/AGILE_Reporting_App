# Installing on Windows

Getting the 21-state, 53-indicator version running on a PC. Two routes —
**with Git** and **without** — that differ only in step 1.

You need Python 3.11 or newer. Check with `python --version`.

---

## Before you start: the old folder

A folder installed before the recode holds the previous `KPI-001…070`
catalogue and figures reported against those codes. Those indicators no longer
exist, so the figures underneath them refer to nothing.

**Do not install over it.** Put the new version in a new folder and keep the
old one until you are satisfied — then delete it.

---

## Route A — with Git

Install Git for Windows from <https://git-scm.com/download/win> if you do not
have it (accept every default), then **close and reopen PowerShell** so `git`
is on your path.

```powershell
cd C:\Projects
git clone -b claude/agile-reporting-platform-yecfje https://github.com/dnsima/AGILE_Reporting_App.git
cd AGILE_Reporting_App
```

Later updates are then one command from inside that folder:

```powershell
git pull
```

## Route B — without Git

1. Open <https://github.com/dnsima/AGILE_Reporting_App>
2. Switch the branch dropdown to **claude/agile-reporting-platform-yecfje**
3. **Code → Download ZIP**
4. Extract it. You get `AGILE_Reporting_App-claude-agile-reporting-platform-yecfje`
   — rename it to something shorter, e.g. `C:\Projects\AGILE_Reporting_App`

```powershell
cd C:\Projects\AGILE_Reporting_App
```

To update later, download the ZIP again into a **new** folder and copy your
`.env` across. There is no `git pull` without Git.

---

## The rest is the same either way

### 1. Create the environment and install

```powershell
python -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Your prompt should now start with `(.venv)`. If PowerShell blocks the activate
script, run this once and try again:

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```

The install step downloads about 40 MB, most of it numpy and pandas. On a slow
or contended connection pip's 15-second default timeout gives up mid-download
and ends in a long traceback finishing `ReadTimeoutError`. Nothing is broken and
nothing is lost — whatever already came down is cached. Re-run with more
patience:

```powershell
pip install --timeout 120 --retries 10 --prefer-binary -r requirements.txt
```

If it still stalls, take the two heavy packages on their own first, then the
rest:

```powershell
pip install --timeout 180 --retries 10 numpy pandas
pip install --timeout 180 --retries 10 -r requirements.txt
```

Both are safe to repeat as often as you need; pip skips anything already
installed.

### 2. Configure

```powershell
copy .env.example .env
```

The defaults are fine for a machine on your desk. Nothing to edit yet.

### 3. Create the database

```powershell
python -m scripts.seed
```

Expect: 3 cohorts, **21 states**, 10 sub-components, 210 applicability rows,
**54 indicators** (53 reported + 1 the platform derives), the reporting
calendar, and 26 validation rules. It prints the administrator sign-in.

### 4. Load data

**The normal route — the Kobo backend export.** States file the AGILE Results
Framework form on KoBoToolbox; you download the quarter's backend dataset and
load it here. That is the entry point, and nothing needs re-keying:

```powershell
python -m scripts.load_kobo --period 2026-Q2 `
    --file "C:\AGILE\Q2_2026_AGILE_PF_Backend_Dataset.xlsx" --dry-run
```

Start with `--dry-run`. It reads the file, resolves every question column to an
indicator and tells you what it found, without loading anything. If a column
does not match the results framework it says which, and refuses — the form and
the framework have drifted apart and that is worth knowing before a figure
lands in the wrong field. Drop `--dry-run` to load it:

```powershell
python -m scripts.load_kobo --period 2026-Q2 `
    --file "C:\AGILE\Q2_2026_AGILE_PF_Backend_Dataset.xlsx" --approve
```

Each state becomes one submission, validated on its own, with every finding
raised as a query against the state that reported it. A state that filed badly
does not hold up the seventeen that did not. `--approve` lets the figures enter
the national totals now rather than waiting on review; the queries stand either
way.

The `--period` is required. States file in the fortnight after a quarter ends,
so the dates inside the file would put half the returns in the following
quarter.

**Or** an NPCU analysis workbook, if you are loading history:

```powershell
python -m scripts.load_npcu_models `
    --q2 "C:\AGILE\AGILE_Q2_2026_Analysis_Model_Flagged.xlsx" `
    --q1 "C:\AGILE\AGILE_Q1_2026_Analysis_Model_v3_5.xlsx" `
    --crosswalk "C:\AGILE\AGILE_Q1_vs_Q2_2026_Model_Comparison.xlsx"
```

Expect 18 states ingested for each of Q1 and Q2. Q1 loads as history, its codes
translated through the crosswalk so period-on-period comparison survives the
recode.

**Or** generated data on the same framework, if you just want to click around:

```powershell
python -m scripts.demo
```

### 5. Run it

```powershell
uvicorn app.main:app --reload
```

Open <http://localhost:8000/dashboard> and sign in with the account step 3
printed. **Change that password** from inside the app.

To stop the server, press `Ctrl+C` in that window. If it prints `Waiting for
connections to close` and then sits there, the dashboard's live-update stream
is still open in your browser — that connection is meant to stay open, so the
polite shutdown waits for something that will never close. Close the browser
tab, or press `Ctrl+C` a second time to force it. Nothing is lost either way.

---

## Upgrading an install you already have

Your data survives. The database adds the columns the new version declares the
first time the app starts, and nothing is dropped, renamed or retyped.

**1. Get the new code.** With Git, from the project folder:

```powershell
git pull origin claude/agile-reporting-platform-yecfje
```

Without Git, download the ZIP again (Route B above) and extract it over the
project folder, replacing files when Windows asks. Your `storage` folder and
your `.env` are not in the ZIP, so neither is touched.

**2. Install what the new version needs.**

```powershell
.venv\Scripts\activate
pip install --timeout 120 --retries 10 -r requirements.txt
```

This release adds `python-docx` (Word export) and `matplotlib` (the figures in
the technical report).

**3. Re-validate what you have already loaded.**

```powershell
python -m scripts.revalidate
```

Figures loaded earlier were checked against the rules in force when they
arrived. This puts them through the current set and settles each state's
fitness verdict, which cannot be judged one state at a time because it depends
on the national totals. **No reported figure is changed** -- only findings,
labels and verdicts. It takes a minute or two.

Skipping this step leaves every state without a verdict and the new rules
unapplied, which looks like the upgrade did nothing.

**4. Start the app.**

```powershell
uvicorn app.main:app --reload
```

National totals change the moment you start the new version, before you
re-validate anything: figures under query are no longer held out of them. That
is the point of the release, not a fault.

### What looks different

**There is no DQA score and no grade.** They are gone, not hidden. A pass rate
over automated checks sat near 100 for any plausible return and read
"Excellent" beside a state that had lost 5,833 schools between quarters. In
their place each return carries a verdict — FIT, FIT WITH NOTES, NOT FIT FOR
USE — with a note saying why. Scoring a return is work for a data quality
assessment with a field visit behind it, and will be designed as its own
process.

**The dashboard has no filter bar.** It carried five global filters applied to
panels that needed different subsets of them, and the panels disagreed: a
reporting rate once read "18 of 11 states". The board now has one scope, the
reporting period, chosen at the top right. Cohort is a dimension two overview
charts cut by; a state is a column in every table.

**There is a Download the analysis workbook button**, in the strip under the
header. It builds the workbook you would otherwise assemble by hand — Cover,
National Summary, PDO, the three components, Data Quality Flags and a Full Data
Table — from the same figures the screen is showing.

### Backing up first

If you want a copy of the database before upgrading, copy **all three** files:

```powershell
copy storage\agile.db* C:\AGILE\backup\
```

`agile.db` alone is not the database. SQLite keeps recent writes in
`agile.db-wal` alongside it, so copying only the first file can lose everything
since the last checkpoint -- which on a freshly loaded install is everything.

---

## Starting over

```powershell
python -m scripts.seed --reset
```

Drops every table and rebuilds. Everything uploaded is lost — which is the
point when the catalogue underneath it has changed.

---

## If something goes wrong

| What you see | What it means |
|---|---|
| `'git' is not recognized` | Use Route B, or install Git and reopen PowerShell |
| `'python' is not recognized` | Python is not on PATH. Reinstall it and tick **Add python.exe to PATH** |
| `cannot be loaded because running scripts is disabled` | Run the `Set-ExecutionPolicy` line above |
| `ReadTimeoutError` from `files.pythonhosted.org` | The download stalled, not a failure. Re-run with `--timeout 120 --retries 10` |
| `no such column: ...` | You are on old code against a new database, or the reverse. Re-pull, then `python -m scripts.seed` |
| `Address already in use` | Something else holds port 8000. `uvicorn app.main:app --reload --port 8001` |
| `Waiting for connections to close` after `Ctrl+C` | The live-update stream is still open. Close the browser tab, or press `Ctrl+C` again |
| Indicators you do not recognise | The old catalogue is still in the database. `python -m scripts.seed --reset` |
| Every state shows no verdict after an upgrade | `python -m scripts.revalidate` was not run |
| A restored backup is empty | Only `agile.db` was copied. It needs `agile.db-wal` too |

---

## Then the server

Nothing above changes when you move to a VPS — same repository, same code. You
add a `.env` with real secrets and a domain and run `docker compose up -d`.
See [DEPLOYMENT.md](DEPLOYMENT.md).

The one switch is `ENVIRONMENT=production`, which turns on checks that refuse
to start on a default signing key, the default administrator password, `DEBUG`,
or `CORS_ORIGINS=*`. Locally you stay on the defaults and nothing is enforced.
