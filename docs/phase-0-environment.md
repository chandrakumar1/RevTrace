# Phase 0 — Environment Inspection Report

**Date:** 2026-08-26
**Machine:** macOS 15.7.9 (build 24G830), x86_64 (Intel)
**Scope:** Read-only inspection of the development environment, followed by
repository scaffolding. No product functionality implemented.

---

## 1. Toolchain

| Item | Result | Status |
|---|---|---|
| Operating system | macOS 15.7.9 (Sequoia), x86_64 | OK |
| Shell | zsh | OK |
| Python (default) | 3.14.3 — python.org framework build | Not used; see conflicts |
| Python (selected) | **3.13.7** at `/Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13` | Selected for backend |
| Node | v24.19.0 | OK |
| npm | 11.17.0 | OK |
| pnpm / yarn / bun | Not installed | OK — standardizing on npm |
| Git | 2.39.5 (Apple Git-154) | OK |
| PostgreSQL | 16.15 (Homebrew), service `postgresql@16` started, listening on 127.0.0.1:5432 and [::1]:5432 | OK |
| Docker | Not installed (`docker`, `docker-compose`, `colima` all absent) | Not required |

Also present: `uv` at `~/.local/bin/uv`. Absent: `pyenv`, `conda`, `poetry`,
`pipx`, `nvm`.

## 2. Repository state before Phase 0

The project directory contained 16K total:

```
RazorPay_Project/
├── CLAUDE.md                  (12,190 bytes — master specification)
└── .claude/settings.local.json
```

No git repository, no `.gitignore`, no `.env`, no backend, no frontend, no
lockfiles, no `node_modules`. A clean slate.

## 3. Editor / project configuration

- No `.vscode/`, no workspace file, no `.editorconfig` (an `.editorconfig` was
  added during Phase 0).
- `.claude/settings.local.json` contained only a `permissions.allow` list.
- Git global identity was already configured, under a different email address
  from the one associated with the GitHub account (see §6.7).

## 4. Package managers available

- **Python:** `pip` 26.1.2 (bound to 3.14), `uv`.
- **Node:** `npm` 11.17.0 only.
- **System:** Homebrew (owns the PostgreSQL install and `brew services`).

## 5. Environment variables

Checked as SET/UNSET only. No values were printed, read, or logged.

| Variable | State at inspection |
|---|---|
| `RAZORPAY_KEY_ID` | UNSET |
| `RAZORPAY_KEY_SECRET` | UNSET |
| `RAZORPAY_WEBHOOK_SECRET` | UNSET |
| `GEMINI_API_KEY` | UNSET |
| `DATABASE_URL` | UNSET |

All five required secrets are absent from the shell environment. They will be
supplied via a local, gitignored `.env` created from `.env.example`.

## 6. Potential conflicts identified

### 6.1 Python 3.14 is the default interpreter — resolved

`python3` resolves to 3.14.3. The backend stack (SQLAlchemy, Pydantic, Alembic,
and particularly the PostgreSQL driver) carries real risk of missing prebuilt
wheels on a very new CPython release, which would force source builds.

**Resolution:** the backend pins Python 3.13 via `backend/.python-version` and
`requires-python = ">=3.13,<3.14"` in `backend/pyproject.toml`. The venv must be
created with `python3.13` explicitly, never `python3`.

### 6.2 Multiple `python3` binaries shadow each other

PATH contains the 3.14 framework build, the 3.13 framework build,
`/usr/local/bin/python3`, and `/usr/bin/python3`.

**Resolution:** all backend work happens inside `backend/.venv`, created from
an explicit `python3.13` invocation. Verify with `python -V` after activation.

### 6.3 Docker is not installed

**Resolution:** not required. The local Homebrew PostgreSQL 16 is the database
of record. No containerization is planned for the hackathon build.

### 6.4 Port 5432 is already occupied

The Homebrew PostgreSQL 16 service holds 5432.

**Resolution / standing constraint:** if Docker is introduced later, any
Compose-managed Postgres must map to a non-default host port to avoid a
collision.

### 6.5 No version control and no `.gitignore` — resolved

Before Phase 0 there was a live risk of committing a `.env` containing real
Razorpay and Gemini keys.

**Resolution:** `.gitignore` was written **before** `git init` and before any
`.env` could exist. It ignores `.env`, `.env.*` (with `!.env.example`), `*.pem`,
`*.key`, and `secrets/`.

### 6.6 Intel (x86_64) architecture

Not a blocker. Wheel selection and any future container images must target
amd64; do not assume arm64.

### 6.7 Git identity vs. account identity — resolved

The email on the local account differed from the one associated with the GitHub
account, which would have produced commits attributed to the wrong identity.

**Resolution:** repository-local git config set to the GitHub account's identity.
The addresses themselves are deliberately not recorded here — this document is
published, and a personal email address in a public repository is an invitation
to spam.

### 6.8 Broad Claude Code read permission — noted, not changed

`.claude/settings.local.json` granted read access to the entire home directory
rather than to the repository. Flagged for awareness only; out of Phase 0 scope.
The file is gitignored as machine-specific.

## 7. Decisions taken in Phase 0

| Decision | Rationale |
|---|---|
| Python 3.13, not 3.14 | Dependency wheel availability; avoids source builds |
| Local Homebrew PostgreSQL 16, no Docker | Docker absent; Postgres already running |
| npm as the sole Node package manager | Only one installed; no reason to add another |
| `simulator/` as a top-level sibling, not inside `backend/app/` | It is a dev/evaluation tool, not a runtime service; keeps test-data generation out of the importable production package |
| `docs/decisions/` for ADRs | The spec requires explaining architecture changes after each milestone |
| Razorpay and Gemini SDKs not declared yet | They belong to Phases 8 and 5; declaring now would pre-commit choices Phase 0 has no mandate to make |

## 8. What Phase 0 created

- Git repository, default branch `main`, repo-local identity configured.
- `.gitignore` (secrets, Python, Node, Postgres artifacts, macOS, editors).
- `.env.example` — variable names and comments only, **no values**.
- `.editorconfig`.
- `README.md` — setup, layout, milestone table.
- `docs/` — this report, `architecture.md` placeholder, `decisions/` for ADRs.
- `backend/` — full package skeleton, `pyproject.toml` (dependencies
  **declared, not installed**), `.python-version` pinning 3.13, placeholder
  `main.py`, empty `alembic/` and `tests/`.
- `frontend/` and `simulator/` — placeholder READMEs only.

## 9. What Phase 0 deliberately did NOT do

- No dependencies installed (`pip`/`npm` never invoked for packages).
- No virtualenv created.
- No database or role created.
- No `.env` file created; no secret value read, written, printed, or logged.
- No application, product, or feature code.
- No frontend scaffolding.
- No Razorpay or Gemini API calls.

## 10. Recommended next steps (Phase 1)

1. Create `.env` from `.env.example` and populate it locally.
2. Create `backend/.venv` using `python3.13`; verify the interpreter version.
3. Install backend dependencies; record the resolved versions.
4. Create the `revtrace_dev` database on the local PostgreSQL 16.
5. Initialize Alembic and define the initial entities from the specification:
   `merchants`, `customers`, `orders`, `payment_attempts`, `events`,
   `revenue_risks`, `recovery_cases`, `recovery_actions`, `audit_events`.
6. Stand up the FastAPI application with a health endpoint and confirm the
   database connection.
