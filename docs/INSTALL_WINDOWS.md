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

**Either** your real returns — point each flag at wherever the workbook is:

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
| `no such column: ...` | You are on old code against a new database, or the reverse. Re-pull, then `python -m scripts.seed` |
| `Address already in use` | Something else holds port 8000. `uvicorn app.main:app --reload --port 8001` |
| Indicators you do not recognise | The old catalogue is still in the database. `python -m scripts.seed --reset` |

---

## Then the server

Nothing above changes when you move to a VPS — same repository, same code. You
add a `.env` with real secrets and a domain and run `docker compose up -d`.
See [DEPLOYMENT.md](DEPLOYMENT.md).

The one switch is `ENVIRONMENT=production`, which turns on checks that refuse
to start on a default signing key, the default administrator password, `DEBUG`,
or `CORS_ORIGINS=*`. Locally you stay on the defaults and nothing is enforced.
