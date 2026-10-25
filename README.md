# Sui

![Sui logo](assets/images/logo.png)

Sui is a local RPA studio. It provides a React interface, a Python backend, SQLite persistence, Rustwright browser execution, recipe generation, and local data-table tools.

## Requirements

- Windows, macOS, or Linux
- Python 3.13 (the repository pins this in `.python-version`)
- [`uv`](https://docs.astral.sh/uv/)
- Node.js with npm
- A Chromium/Chrome installation usable by Rustwright for browser automation

The commands below use PowerShell syntax where shell-specific commands are needed. On macOS/Linux, replace `.venv\Scripts\Activate.ps1` with the activation command for your shell.

## Reproducible setup

From the repository root:

```powershell
uv sync --locked
.\.venv\Scripts\Activate.ps1

Set-Location frontend
npm ci
Set-Location ..
```

`uv sync --locked` installs the exact Python dependency versions recorded in `uv.lock`. `npm ci` installs the exact frontend dependency tree recorded in `frontend/package-lock.json`.

## Environment

Create a `.env` file in the repository root when using Smartie:

```dotenv
GOOGLE_GEMINI_KEY=your-gemini-api-key
```

Do not commit `.env`. It is ignored by Git. Smartie currently uses Google Gemini and the `gemini-2.5-flash` model.

## Run the development app

Use two terminals, both from the repository root.

Terminal 1 - backend:

```powershell
uv run python main.py
```

The backend listens on `http://127.0.0.1:8000`.

Terminal 2 - frontend:

```powershell
Set-Location frontend
npm run dev
```

Open the Vite URL printed by the frontend, normally `http://localhost:5173`.

The main local routes are:

- `/` - Studio
- `/projects` - saved projects and recipes
- `/jobs` - recipe executions and logs
- `/data` - View and Craft for local SQLite data tables

## Build and serve the frontend from Python

For a backend-served UI instead of the Vite development server:

```powershell
Set-Location frontend
npm run build
Set-Location ..
uv run python main.py
```

Open `http://127.0.0.1:8000`. Re-run `npm run build` after frontend changes.

## Verify the checkout

Run the focused regression suite, Python compilation, and frontend production build:

```powershell
uv run python -m unittest discover -s tests -p "test_*.py"
uv run python -m py_compile main.py

Set-Location frontend
npm run build
Set-Location ..
```

To verify the Gemini key independently without exposing it:

```powershell
uv run python test.py
```

## First-use workflow

1. Open Studio and create a project.
2. Add a recipe and begin it with **Navigate to website** for standard recipes.
3. Add supported activities, configure their fields, and save the project.
4. Reopen the project from Projects to restore the recipe from its JSON definition.
5. Run a step in Studio or run the full recipe from Projects.
6. Inspect execution status and logs in Jobs.
7. Use `/data` to inspect or craft local SQLite data tables.

## Local persistence

Runtime files are intentionally local and ignored by Git:

- `data/sui.db` - projects, recipes, jobs, logs, and data tables
- `data/generated/` - generated Python for saved recipe versions
- `.venv/` - the local Python environment

Recipe JSON is the editable source of truth. Generated Python is recreated from the saved recipe when needed and is executed by the backend, never by React.

To reproduce a specific state, keep the same Git commit, `uv.lock`, `frontend/package-lock.json`, `.env` configuration, and `data/` directory. A fresh checkout starts with an empty local runtime database unless a copy of `data/` is restored.

## Useful troubleshooting

Check that the backend is reachable:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/health
```

Expected response:

```text
ok
--
True
```

If the frontend reports that the backend is offline, start `uv run python main.py` from the repository root and refresh the page. If the backend-served UI reports that the frontend build is missing, run `npm run build` inside `frontend`.
