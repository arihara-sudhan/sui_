from __future__ import annotations

import json
import mimetypes
import os
import sqlite3
import subprocess
import sys
import threading
import tempfile
import uuid
import base64
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "sui.db"
GENERATED_DIR = DATA_DIR / "generated"
CHROME_PROFILE_DIR = DATA_DIR / "chrome-profile"
FRONTEND_DIST = ROOT / "frontend" / "dist"
SCHEMA_VERSION = 1
SUPPORTED_STEP_TYPES = {"navigate_to_website", "click", "fill"}
STEP_RUNS: dict[str, dict[str, Any]] = {}
STEP_RUNS_LOCK = threading.Lock()
STEP_RUNS_CHANGED = threading.Condition(STEP_RUNS_LOCK)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def make_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def db_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize_database() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    with db_connection() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS recipes (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                current_version INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(project_id, name)
            );
            CREATE TABLE IF NOT EXISTS recipe_versions (
                recipe_id TEXT NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
                version INTEGER NOT NULL,
                definition_json TEXT NOT NULL,
                generated_path TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(recipe_id, version)
            );
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                recipe_id TEXT NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
                recipe_version INTEGER NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT,
                ended_at TEXT,
                created_at TEXT NOT NULL,
                error TEXT
            );
            CREATE TABLE IF NOT EXISTS job_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                level TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )


def parse_json_body(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0"))
    raw = handler.rfile.read(length) if length else b"{}"
    try:
        value = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("Request body must be valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("Request body must be a JSON object")
    return value


def validate_url(value: Any) -> str:
    url = str(value or "").strip()
    if not url or not (url.startswith("http://") or url.startswith("https://")):
        raise ValueError("Navigate to website requires a URL beginning with http:// or https://")
    return url


def validate_selector(value: Any) -> str:
    selector = str(value or "").strip()
    if not selector:
        raise ValueError("Click requires a selector. Pick an element in the live browser first")
    if selector.lower().startswith("javascript:"):
        raise ValueError("JavaScript selectors are not supported")
    return selector


def normalize_definition(
    definition: dict[str, Any],
    project_id: str,
    recipe_id: str,
    recipe_name: str,
    version: int,
    require_first_navigation: bool = True,
) -> dict[str, Any]:
    raw_steps = definition.get("steps", [])
    if not isinstance(raw_steps, list):
        raise ValueError("Recipe steps must be an array")
    if require_first_navigation and raw_steps:
        first_step = raw_steps[0]
        if not isinstance(first_step, dict) or str(first_step.get("type") or "") != "navigate_to_website":
            raise ValueError("The first recipe step must be Navigate to website")
    steps: list[dict[str, Any]] = []
    for index, raw_step in enumerate(raw_steps):
        if not isinstance(raw_step, dict):
            raise ValueError("Every recipe step must be an object")
        step_type = str(raw_step.get("type") or "")
        if step_type not in SUPPORTED_STEP_TYPES:
            raise ValueError("Only Navigate to website, Click, and Fill are supported")
        settings = raw_step.get("settings") or {}
        position = raw_step.get("position") or {}
        if step_type == "navigate_to_website":
            normalized_settings = {"url": validate_url(settings.get("url"))}
        elif step_type == "fill":
            normalized_settings = {
                "selector": validate_selector(settings.get("selector")),
                "value": str(settings.get("value") or ""),
            }
        else:
            normalized_settings = {"selector": validate_selector(settings.get("selector"))}
        steps.append(
            {
                "id": str(raw_step.get("id") or f"step_{uuid.uuid4().hex[:12]}"),
                "type": step_type,
                "settings": normalized_settings,
                "connections": raw_step.get("connections") or {"next": []},
                "position": {
                    "x": int(position.get("x", 48 + index * 220)),
                    "y": int(position.get("y", 48)),
                },
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "project_id": project_id,
        "recipe_id": recipe_id,
        "recipe_name": recipe_name,
        "version": version,
        "steps": steps,
        "connections": definition.get("connections") or [],
    }


def generate_python(definition: dict[str, Any]) -> str:
    lines = [
        "import os",
        "import shutil",
        "import socket",
        "import subprocess",
        "import tempfile",
        "import time",
        "import urllib.request",
        "from rustwright.sync_api import sync_playwright",
        "",
        "",
        "def free_port():",
        "    sock = socket.socket()",
        "    sock.bind((\"127.0.0.1\", 0))",
        "    port = sock.getsockname()[1]",
        "    sock.close()",
        "    return port",
        "",
        "",
        "def wait_for_cdp(port):",
        "    endpoint = f\"http://127.0.0.1:{port}/json/version\"",
        "    for _ in range(100):",
        "        try:",
        "            urllib.request.urlopen(endpoint, timeout=1).close()",
        "            return",
        "        except OSError:",
        "            time.sleep(0.1)",
        "    raise RuntimeError(\"Chrome CDP endpoint did not start\")",
        "",
        "",
        "def run():",
        "    chrome_process = None",
        "    profile_dir = None",
        "    browser = None",
        "    with sync_playwright() as playwright:",
        "        chrome_candidates = [os.environ.get(\"SUI_CHROME_PATH\", \"\"), r\"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe\", r\"C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe\"]",
        "        chrome_path = next((candidate for candidate in chrome_candidates if candidate and os.path.exists(candidate)), shutil.which(\"chrome\"))",
        "        if not chrome_path: raise RuntimeError(\"Installed Chrome executable not found. Set SUI_CHROME_PATH.\")",
        "        port = free_port()",
        "        profile_dir = tempfile.mkdtemp(prefix=\"sui-recipe-\")",
        "        chrome_process = subprocess.Popen([chrome_path, \"--headless=new\", \"--disable-gpu\", \"--no-first-run\", \"--no-default-browser-check\", \"--window-size=1280,720\", f\"--remote-debugging-port={port}\", f\"--user-data-dir={profile_dir}\", \"about:blank\"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)",
        "        wait_for_cdp(port)",
        "        browser = playwright.chromium.connect_over_cdp(f\"http://127.0.0.1:{port}\")",
        "        context = browser.contexts[0]",
        "        page = context.pages[0] if context.pages else context.new_page()",
        "        try:",
    ]
    for step in definition["steps"]:
        # json.dumps creates a safe Python string literal; it is never executed as code.
        if step["type"] == "navigate_to_website":
            url_literal = json.dumps(step["settings"]["url"])
            lines.extend(
                [
                    f"            page.goto({url_literal}, wait_until=\"domcontentloaded\")",
                    f"            print(\"Navigated: {step['id']}\")",
                ]
            )
        elif step["type"] == "fill":
            selector_literal = json.dumps(step["settings"]["selector"])
            value_literal = json.dumps(step["settings"].get("value", ""))
            lines.extend(
                [
                    f"            page.locator({selector_literal}).first.fill({value_literal})",
                    f"            print(\"Filled: {step['id']}\")",
                ]
            )
        else:
            selector_literal = json.dumps(step["settings"]["selector"])
            lines.extend(
                [
                    f"            page.locator({selector_literal}).first.click()",
                    f"            print(\"Clicked: {step['id']}\")",
                ]
            )
    lines.extend(
        [
        "        finally:",
        "            if browser: browser.close()",
        "            if chrome_process and chrome_process.poll() is None:",
        "                chrome_process.terminate()",
        "                try: chrome_process.wait(timeout=5)",
        "                except subprocess.TimeoutExpired: chrome_process.kill()",
        "            if profile_dir: shutil.rmtree(profile_dir, ignore_errors=True)",
            "",
            "",
            "if __name__ == \"__main__\":",
            "    run()",
            "",
        ]
    )
    return "\n".join(lines)


def generate_step_python(definition: dict[str, Any]) -> str:
    step = definition["steps"][0]
    if step["type"] == "navigate_to_website":
        action_lines = [
            f"            await page.goto({json.dumps(step['settings']['url'])}, wait_until=\"domcontentloaded\")",
        ]
        log_line = f"            print(\"SUI_LOG:Navigated: {step['id']}\", flush=True)"
    elif step["type"] == "fill":
        action_lines = [
            f"            await page.locator({json.dumps(step['settings']['selector'])}).first.fill({json.dumps(step['settings'].get('value', ''))})",
        ]
        log_line = f"            print(\"SUI_LOG:Filled: {step['id']}\", flush=True)"
    else:
        action_lines = [
            f"            await page.locator({json.dumps(step['settings']['selector'])}).first.click()",
        ]
        log_line = f"            print(\"SUI_LOG:Clicked: {step['id']}\", flush=True)"
    return "\n".join(
        [
            "import asyncio",
            "import base64",
            "import ctypes",
            "import json",
            "import os",
            "import shutil",
            "import socket",
            "import subprocess",
            "import tempfile",
            "import time",
            "import urllib.request",
            "import sys",
            "from rustwright.async_api import async_playwright",
            "",
            "",
            "def free_port():",
            "    sock = socket.socket()",
            "    sock.bind((\"127.0.0.1\", 0))",
            "    port = sock.getsockname()[1]",
            "    sock.close()",
            "    return port",
            "",
            "",
            "def wait_for_cdp(port):",
            "    endpoint = f\"http://127.0.0.1:{port}/json/version\"",
            "    for _ in range(100):",
            "        try:",
            "            urllib.request.urlopen(endpoint, timeout=1).close()",
            "            return",
            "        except OSError:",
            "            time.sleep(0.1)",
            "    raise RuntimeError(\"Chrome CDP endpoint did not start\")",
            "",
            "",
            "def maximize_window(process):",
            "    if os.name != \"nt\": return",
            "    user32 = ctypes.windll.user32",
            "    target = ctypes.c_void_p()",
            "    process_id = ctypes.c_ulong()",
            "    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)",
            "    def find_window(hwnd, _lparam):",
            "        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))",
            "        if process_id.value == process.pid and user32.IsWindowVisible(hwnd):",
            "            target.value = hwnd",
            "            return False",
            "        return True",
            "    user32.EnumWindows(callback_type(find_window), 0)",
            "    if target.value:",
            "        user32.ShowWindow(target, 3)",
            "        user32.SetForegroundWindow(target)",
            "",
            "",
            "async def emit_frame(page):",
            "    frame = await page.screenshot(type=\"jpeg\", quality=70)",
            "    print(\"SUI_FRAME:\" + base64.b64encode(frame).decode(\"ascii\"), flush=True)",
            "",
            "",
            "async def stream_frames(page, stop_event):",
            "    while not stop_event.is_set():",
            "        try:",
            "            await emit_frame(page)",
            "        except Exception:",
            "            pass",
            "        await asyncio.sleep(0.25)",
            "",
            "",
            "async def inspect_at(page, x, y):",
            "    return await page.evaluate(\"\"\"(point) => {",
            "        const hovered = document.elementFromPoint(point.x, point.y);",
            "        if (!hovered) return null;",
            "        const target = hovered.closest('button,a,input,textarea,select,[role=button],[role=link]') || hovered;",
            "        const escape = (value) => (window.CSS && CSS.escape ? CSS.escape(value) : value.replace(/[^a-zA-Z0-9_-]/g, '\\\\$&'));",
            "        const parts = [];",
            "        const targetClasses = Array.from(target.classList || []).filter(Boolean).slice(0, 2);",
            "        let node = target;",
            "        while (node && node.nodeType === 1 && node !== document.body) {",
            "            let part = node.tagName.toLowerCase();",
            "            if (node.id) { parts.unshift('#' + escape(node.id)); break; }",
            "            const classes = Array.from(node.classList || []).filter(Boolean).slice(0, 2);",
            "            if (classes.length) part += classes.map((name) => '.' + escape(name)).join('');",
            "            const siblings = node.parentElement ? Array.from(node.parentElement.children).filter((item) => item.tagName === node.tagName) : [];",
            "            if (siblings.length > 1) part += ':nth-of-type(' + (siblings.indexOf(node) + 1) + ')';",
            "            parts.unshift(part);",
            "            node = node.parentElement;",
            "        }",
            "        const selector = parts.join(' > ');",
            "        const text = String(target.innerText || target.getAttribute('aria-label') || target.value || '').replace(/\\\\s+/g, ' ').trim().slice(0, 120);",
            "        const candidates = [];",
            "        const addCandidate = (kind, value) => {",
            "            if (!value || candidates.some((candidate) => candidate.selector === value)) return;",
            "            let matches = null;",
            "            try { matches = document.querySelectorAll(value).length; } catch (_) {}",
            "            candidates.push({ selector: value, kind, unique: matches === 1, matches });",
            "        };",
            "        if (target.id) addCandidate('ID', '#' + escape(target.id));",
            "        const testId = target.getAttribute('data-testid');",
            "        if (testId) addCandidate('Test ID', '[data-testid=\"' + escape(testId) + '\"]');",
            "        const name = target.getAttribute('name');",
            "        if (name) addCandidate('Name', '[name=\"' + escape(name) + '\"]');",
            "        const ariaLabel = target.getAttribute('aria-label');",
            "        if (ariaLabel) addCandidate('ARIA label', '[aria-label=\"' + escape(ariaLabel) + '\"]');",
            "        const role = target.getAttribute('role');",
            "        if (role) addCandidate('Role', '[role=\"' + escape(role) + '\"]');",
            "        if (targetClasses.length) addCandidate('CSS', target.tagName.toLowerCase() + targetClasses.map((name) => '.' + escape(name)).join(''));",
            "        addCandidate('Path', selector);",
            "        const rect = target.getBoundingClientRect();",
            "        return { selector: candidates[0]?.selector || selector, selectorCandidates: candidates, metadata: { tag: target.tagName.toLowerCase(), text, role: target.getAttribute('role') || '', ariaLabel: target.getAttribute('aria-label') || '' }, boundingRect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height } };",
            "    }\"\"\", {\"x\": x, \"y\": y})",
            "",
            "",
            "async def command_loop(page):",
            "    while True:",
            "        line = await asyncio.to_thread(sys.stdin.readline)",
            "        if not line:",
            "            return",
            "        try:",
            "            command = json.loads(line)",
            "        except json.JSONDecodeError:",
            "            continue",
            "        try:",
            "            if command.get(\"type\") == \"navigate\":",
            "                await page.goto(str(command.get(\"url\") or \"\"), wait_until=\"domcontentloaded\")",
            "                print(\"SUI_ACTION_COMPLETE:\" + json.dumps({\"status\": \"completed\", \"action\": \"navigate\"}), flush=True)",
            "            elif command.get(\"type\") == \"text\":",
            "                await page.keyboard.insert_text(str(command.get(\"text\", \"\")))",
            "            elif command.get(\"type\") == \"key\":",
            "                await page.keyboard.press(str(command.get(\"key\", \"\")))",
            "            elif command.get(\"type\") == \"click\":",
            "                await page.mouse.click(float(command.get(\"x\", 0)), float(command.get(\"y\", 0)))",
            "            elif command.get(\"type\") == \"selector_fill\":",
            "                await page.locator(str(command.get(\"selector\") or \"\")).first.fill(str(command.get(\"value\") or \"\"))",
            "                print(\"SUI_ACTION_COMPLETE:\" + json.dumps({\"status\": \"completed\", \"action\": \"fill\", \"trial_id\": command.get(\"trial_id\")}), flush=True)",
            "            elif command.get(\"type\") == \"selector_click\":",
            "                await page.locator(str(command.get(\"selector\") or \"\")).first.click()",
            "                print(\"SUI_ACTION_COMPLETE:\" + json.dumps({\"status\": \"completed\", \"action\": \"click\", \"trial_id\": command.get(\"trial_id\")}), flush=True)",
            "            elif command.get(\"type\") == \"inspect\":",
            "                try:",
            "                    result = await inspect_at(page, float(command.get(\"x\", 0)), float(command.get(\"y\", 0)))",
            "                    print(\"SUI_INSPECT:\" + json.dumps(result), flush=True)",
            "                except Exception as exc:",
            "                    print(\"SUI_INSPECT:\" + json.dumps({\"error\": str(exc)}), flush=True)",
            "            else:",
            "                continue",
            "            await emit_frame(page)",
            "        except Exception as exc:",
            "            if command.get(\"type\") in {\"navigate\", \"selector_click\", \"selector_fill\"}:",
            "                print(\"SUI_ACTION_COMPLETE:\" + json.dumps({\"status\": \"failed\", \"error\": str(exc), \"trial_id\": command.get(\"trial_id\")}), flush=True)",
            "",
            "",
            "async def run():",
            "    chrome_process = None",
            "    profile_dir = None",
            "    browser = None",
            "    async with async_playwright() as playwright:",
            "        chrome_candidates = [os.environ.get(\"SUI_CHROME_PATH\", \"\"), r\"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe\", r\"C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe\"]",
            "        chrome_path = next((candidate for candidate in chrome_candidates if candidate and os.path.exists(candidate)), shutil.which(\"chrome\"))",
            "        if not chrome_path: raise RuntimeError(\"Installed Chrome executable not found. Set SUI_CHROME_PATH.\")",
            "        port = free_port()",
            "        profile_dir = os.environ.get(\"SUI_CHROME_PROFILE_DIR\") or os.path.join(os.getcwd(), \"data\", \"chrome-profile\")",
            "        os.makedirs(profile_dir, exist_ok=True)",
            "        chrome_process = subprocess.Popen([chrome_path, \"--disable-features=IsolateOrigins,site-per-process\", \"--no-first-run\", \"--no-default-browser-check\", \"--start-maximized\", \"--window-position=0,0\", f\"--remote-debugging-port={port}\", f\"--user-data-dir={profile_dir}\", \"about:blank\"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)",
            "        await asyncio.to_thread(wait_for_cdp, port)",
            "        maximize_window(chrome_process)",
            "        browser = await playwright.chromium.connect_over_cdp(f\"http://127.0.0.1:{port}\")",
            "        context = browser.contexts[0]",
            "        page = context.pages[0] if context.pages else await context.new_page()",
            "        stop_event = asyncio.Event()",
            "        frame_task = None",
            "        command_task = None",
            "        try:",
            *action_lines,
            "            await emit_frame(page)",
            "            frame_task = asyncio.create_task(stream_frames(page, stop_event))",
            log_line,
            "            print(\"SUI_READY\", flush=True)",
            "            command_task = asyncio.create_task(command_loop(page))",
            "            await asyncio.sleep(300)",
            "        finally:",
            "            stop_event.set()",
            "            if frame_task:",
            "                await frame_task",
            "            if command_task:",
            "                command_task.cancel()",
            "                try:",
            "                    await command_task",
            "                except asyncio.CancelledError:",
            "                    pass",
            "            if browser: await browser.close()",
            "            if chrome_process and chrome_process.poll() is None:",
            "                chrome_process.terminate()",
            "                try: chrome_process.wait(timeout=5)",
            "                except subprocess.TimeoutExpired: chrome_process.kill()",
            "",
            "",
            "if __name__ == \"__main__\":",
            "    asyncio.run(run())",
            "",
        ]
    )


def recipe_response(row: sqlite3.Row, definition: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "name": row["name"],
        "current_version": row["current_version"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "definition": definition,
    }


def latest_definition(connection: sqlite3.Connection, recipe_id: str) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT definition_json FROM recipe_versions WHERE recipe_id = ? ORDER BY version DESC LIMIT 1",
        (recipe_id,),
    ).fetchone()
    return json.loads(row["definition_json"]) if row else None


def project_response(connection: sqlite3.Connection, project_row: sqlite3.Row, include_definitions: bool = True) -> dict[str, Any]:
    recipes = []
    rows = connection.execute(
        "SELECT * FROM recipes WHERE project_id = ? ORDER BY created_at ASC", (project_row["id"],)
    ).fetchall()
    for recipe in rows:
        definition = latest_definition(connection, recipe["id"]) if include_definitions else None
        recipes.append(recipe_response(recipe, definition))
    return {
        "id": project_row["id"],
        "name": project_row["name"],
        "created_at": project_row["created_at"],
        "updated_at": project_row["updated_at"],
        "recipes": recipes,
    }


def list_projects() -> list[dict[str, Any]]:
    with db_connection() as connection:
        rows = connection.execute("SELECT * FROM projects ORDER BY updated_at DESC").fetchall()
        return [project_response(connection, row, include_definitions=False) for row in rows]


def save_recipe(project_id: str, recipe_id: str | None, name: str, raw_definition: dict[str, Any]) -> dict[str, Any]:
    recipe_name = str(name or "").strip()
    if not recipe_name:
        raise ValueError("Recipe name is required")
    with db_connection() as connection:
        project = connection.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if not project:
            raise ValueError("Project not found")
        recipe = None
        if recipe_id:
            recipe = connection.execute("SELECT * FROM recipes WHERE id = ?", (recipe_id,)).fetchone()
            if not recipe or recipe["project_id"] != project_id:
                raise ValueError("Recipe not found")
        else:
            recipe = connection.execute(
                "SELECT * FROM recipes WHERE project_id = ? AND name = ?", (project_id, recipe_name)
            ).fetchone()
            if recipe:
                recipe_id = recipe["id"]
        recipe_id = recipe_id or make_id("recipe")
        next_version = int(recipe["current_version"]) + 1 if recipe else 1
        definition = normalize_definition(raw_definition, project_id, recipe_id, recipe_name, next_version)
        generated_path = GENERATED_DIR / project_id / recipe_id / f"v{next_version}.py"
        generated_path.parent.mkdir(parents=True, exist_ok=True)
        generated_path.write_text(generate_python(definition), encoding="utf-8")
        timestamp = now_iso()
        if recipe:
            connection.execute(
                "UPDATE recipes SET name = ?, current_version = ?, updated_at = ? WHERE id = ?",
                (recipe_name, next_version, timestamp, recipe_id),
            )
        else:
            connection.execute(
                "INSERT INTO recipes (id, project_id, name, current_version, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (recipe_id, project_id, recipe_name, next_version, timestamp, timestamp),
            )
        connection.execute(
            "INSERT INTO recipe_versions (recipe_id, version, definition_json, generated_path, created_at) VALUES (?, ?, ?, ?, ?)",
            (recipe_id, next_version, json.dumps(definition), str(generated_path.relative_to(ROOT)), timestamp),
        )
        connection.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (timestamp, project_id))
        saved = connection.execute("SELECT * FROM recipes WHERE id = ?", (recipe_id,)).fetchone()
        return recipe_response(saved, definition)


def add_job_log(job_id: str, level: str, message: str) -> None:
    with db_connection() as connection:
        connection.execute(
            "INSERT INTO job_logs (job_id, level, message, created_at) VALUES (?, ?, ?, ?)",
            (job_id, level, message, now_iso()),
        )


def execute_generated(path: Path) -> tuple[int, list[str], list[str]]:
    try:
        completed = subprocess.run(
            [sys.executable, str(path)], cwd=ROOT, capture_output=True, text=True, timeout=120, check=False
        )
        return (
            completed.returncode,
            [line for line in completed.stdout.splitlines() if line.strip()],
            [line for line in completed.stderr.splitlines() if line.strip()],
        )
    except subprocess.TimeoutExpired:
        return 124, [], ["Execution timed out after 120 seconds"]
    except OSError as exc:
        return 126, [], [str(exc)]


def emit_step_event(run_id: str, event: dict[str, Any]) -> None:
    with STEP_RUNS_CHANGED:
        run = STEP_RUNS.get(run_id)
        if not run:
            return
        run["events"].append(event)
        STEP_RUNS_CHANGED.notify_all()


def active_browser_run() -> tuple[str, dict[str, Any]] | None:
    with STEP_RUNS_CHANGED:
        for run_id, run in reversed(list(STEP_RUNS.items())):
            process = run.get("process")
            if not run.get("done") and process and process.poll() is None and process.stdin:
                return run_id, run
    return None


def send_browser_command(run_id: str, command: dict[str, Any]) -> None:
    with STEP_RUNS_CHANGED:
        run = STEP_RUNS.get(run_id)
        process = run.get("process") if run else None
        if not process or process.poll() is not None or not process.stdin:
            raise RuntimeError("Browser session is no longer active")
        try:
            process.stdin.write(json.dumps(command) + "\n")
            process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise RuntimeError("Browser session is no longer active") from exc


def run_step_worker(run_id: str, definition: dict[str, Any], script_path: Path) -> None:
    emit_step_event(run_id, {"type": "status", "status": "running"})
    completion_sent = False
    try:
        process = subprocess.Popen(
            [sys.executable, "-u", str(script_path)],
            cwd=ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        with STEP_RUNS_CHANGED:
            if run_id in STEP_RUNS:
                STEP_RUNS[run_id]["process"] = process
        assert process.stdout is not None
        for raw_line in process.stdout:
            line = raw_line.strip()
            if line.startswith("SUI_FRAME:"):
                emit_step_event(
                    run_id,
                    {"type": "frame", "data": f"data:image/jpeg;base64,{line.removeprefix('SUI_FRAME:')}"},
                )
            elif line.startswith("SUI_LOG:"):
                emit_step_event(run_id, {"type": "log", "message": line.removeprefix("SUI_LOG:")})
            elif line.startswith("SUI_INSPECT:"):
                try:
                    element = json.loads(line.removeprefix("SUI_INSPECT:"))
                    emit_step_event(run_id, {"type": "inspect", "element": element})
                except json.JSONDecodeError:
                    emit_step_event(run_id, {"type": "log", "message": "Browser returned invalid picker data", "level": "error"})
            elif line.startswith("SUI_ACTION_COMPLETE:"):
                try:
                    action = json.loads(line.removeprefix("SUI_ACTION_COMPLETE:"))
                    emit_step_event(run_id, {"type": "action_complete", **action})
                except json.JSONDecodeError:
                    emit_step_event(run_id, {"type": "action_complete", "status": "failed", "error": "Browser returned invalid action data"})
            elif line == "SUI_READY":
                completion_sent = True
                emit_step_event(run_id, {"type": "complete", "status": "completed", "error": None})
        stderr = process.stderr.read() if process.stderr else ""
        return_code = process.wait()
        stderr_lines = [line.strip() for line in stderr.splitlines() if line.strip()]
        for line in stderr_lines:
            emit_step_event(run_id, {"type": "log", "message": line, "level": "error"})
        status = "completed" if return_code == 0 else "failed"
        error = None
        if return_code != 0:
            error = stderr_lines[-1] if stderr_lines else f"Browser step exited with code {return_code}"
        if not completion_sent or return_code != 0:
            emit_step_event(run_id, {"type": "complete", "status": status, "error": error})
    except OSError as exc:
        emit_step_event(run_id, {"type": "complete", "status": "failed", "error": str(exc)})
    finally:
        try:
            script_path.unlink(missing_ok=True)
        except OSError:
            pass
        with STEP_RUNS_CHANGED:
            if run_id in STEP_RUNS:
                STEP_RUNS[run_id]["process"] = None
                STEP_RUNS[run_id]["done"] = True
            STEP_RUNS_CHANGED.notify_all()


def start_step_run(step: dict[str, Any]) -> str:
    definition = normalize_definition(
        {"steps": [step]}, "step-run", "step-run", "step-run", 1, require_first_navigation=False
    )
    normalized_step = definition["steps"][0]
    active = active_browser_run()
    if active:
        active_run_id, _ = active
        command = (
            {"type": "navigate", "url": normalized_step["settings"]["url"]}
            if normalized_step["type"] == "navigate_to_website"
            else {"type": "selector_click", "selector": normalized_step["settings"]["selector"]}
        )
        try:
            send_browser_command(active_run_id, command)
            return active_run_id
        except RuntimeError:
            pass
    run_id = make_id("step_run")
    script_path = Path(tempfile.gettempdir()) / f"sui-{run_id}.py"
    script_path.write_text(generate_step_python(definition), encoding="utf-8")
    CHROME_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    with STEP_RUNS_CHANGED:
        STEP_RUNS[run_id] = {"events": [], "done": False}
    threading.Thread(target=run_step_worker, args=(run_id, definition, script_path), daemon=True).start()
    return run_id


def finish_job(job_id: str, status: str, error: str | None) -> None:
    with db_connection() as connection:
        connection.execute(
            "UPDATE jobs SET status = ?, ended_at = ?, error = ? WHERE id = ?",
            (status, now_iso(), error, job_id),
        )
    if error:
        add_job_log(job_id, "error", error)


def run_job(job_id: str) -> None:
    with db_connection() as connection:
        job = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not job:
            return
        connection.execute("UPDATE jobs SET status = 'running', started_at = ? WHERE id = ?", (now_iso(), job_id))
        version = connection.execute(
            "SELECT generated_path, definition_json FROM recipe_versions WHERE recipe_id = ? AND version = ?",
            (job["recipe_id"], job["recipe_version"]),
        ).fetchone()
    add_job_log(job_id, "info", f"Running recipe version v{job['recipe_version']}")
    if not version:
        return finish_job(job_id, "failed", "Generated recipe version was not found")
    generated_file = ROOT / version["generated_path"]
    try:
        # Recipe JSON is the source of truth. Refresh the existing version's
        # generated file so recipes saved before a generator fix use the same
        # current Rustwright/Chrome execution path without creating a version.
        definition = json.loads(version["definition_json"])
        generated_file.parent.mkdir(parents=True, exist_ok=True)
        generated_file.write_text(generate_python(definition), encoding="utf-8")
    except (OSError, TypeError, json.JSONDecodeError) as exc:
        return finish_job(job_id, "failed", f"Unable to prepare generated recipe: {exc}")
    code, stdout, stderr = execute_generated(generated_file)
    for line in stdout:
        add_job_log(job_id, "info", line)
    for line in stderr:
        add_job_log(job_id, "error", line)
    if code == 0:
        add_job_log(job_id, "info", "Recipe completed successfully")
        finish_job(job_id, "completed", None)
    else:
        finish_job(job_id, "failed", f"Generated recipe exited with code {code}")


def create_job(recipe_id: str) -> dict[str, Any]:
    with db_connection() as connection:
        recipe = connection.execute("SELECT * FROM recipes WHERE id = ?", (recipe_id,)).fetchone()
        if not recipe or not recipe["current_version"]:
            raise ValueError("Recipe must be saved before it can run")
        job_id = make_id("job")
        timestamp = now_iso()
        connection.execute(
            "INSERT INTO jobs (id, project_id, recipe_id, recipe_version, status, created_at) VALUES (?, ?, ?, ?, 'queued', ?)",
            (job_id, recipe["project_id"], recipe_id, recipe["current_version"], timestamp),
        )
        job = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    threading.Thread(target=run_job, args=(job_id,), daemon=True).start()
    return job_response(job, [])


def job_response(row: sqlite3.Row, logs: list[sqlite3.Row]) -> dict[str, Any]:
    with db_connection() as connection:
        project = connection.execute("SELECT name FROM projects WHERE id = ?", (row["project_id"],)).fetchone()
        recipe = connection.execute("SELECT name FROM recipes WHERE id = ?", (row["recipe_id"],)).fetchone()
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "project_name": project["name"] if project else "Unknown project",
        "recipe_id": row["recipe_id"],
        "recipe_name": recipe["name"] if recipe else "Unknown recipe",
        "recipe_version": row["recipe_version"],
        "status": row["status"],
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
        "created_at": row["created_at"],
        "error": row["error"],
        "logs": [dict(log) for log in logs],
    }


def list_jobs() -> list[dict[str, Any]]:
    with db_connection() as connection:
        rows = connection.execute("SELECT * FROM jobs ORDER BY created_at DESC").fetchall()
        return [job_response(row, []) for row in rows]


def get_job(job_id: str) -> dict[str, Any] | None:
    with db_connection() as connection:
        row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        logs = connection.execute("SELECT * FROM job_logs WHERE job_id = ? ORDER BY id ASC", (job_id,)).fetchall()
    return job_response(row, logs) if row else None


class SuiHandler(BaseHTTPRequestHandler):
    server_version = "Sui/0.1"

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[{now_iso()}] {format % args}")

    def send_payload(self, payload: Any, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def send_error_payload(self, message: str, status: int = HTTPStatus.BAD_REQUEST) -> None:
        self.send_payload({"error": message}, status)

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, OPTIONS")
        self.end_headers()

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        try:
            if path == "/api/health":
                return self.send_payload({"ok": True})
            if path == "/api/projects":
                return self.send_payload({"projects": list_projects()})
            if path.startswith("/api/projects/"):
                project_id = path.split("/")[3]
                with db_connection() as connection:
                    project = connection.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
                    if not project:
                        return self.send_error_payload("Project not found", HTTPStatus.NOT_FOUND)
                    return self.send_payload(project_response(connection, project))
            if path.startswith("/api/step-runs/") and path.endswith("/events"):
                return self.stream_step_events(path.split("/")[3])
            if path == "/api/jobs":
                return self.send_payload({"jobs": list_jobs()})
            if path.startswith("/api/jobs/"):
                job = get_job(path.split("/")[3])
                return self.send_payload(job or {"error": "Job not found"}, HTTPStatus.OK if job else HTTPStatus.NOT_FOUND)
            return self.serve_static(path)
        except (sqlite3.Error, ValueError, KeyError) as exc:
            self.send_error_payload(str(exc), HTTPStatus.BAD_REQUEST)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            body = parse_json_body(self)
            if path == "/api/projects":
                name = str(body.get("name") or "").strip()
                if not name:
                    raise ValueError("Project name is required")
                project_id = make_id("project")
                timestamp = now_iso()
                with db_connection() as connection:
                    connection.execute(
                        "INSERT INTO projects (id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
                        (project_id, name, timestamp, timestamp),
                    )
                    project = connection.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
                    return self.send_payload(project_response(connection, project), HTTPStatus.CREATED)
            if path.startswith("/api/projects/") and path.endswith("/recipes"):
                project_id = path.split("/")[3]
                saved = save_recipe(project_id, None, body.get("name", ""), body.get("definition") or {})
                return self.send_payload(saved, HTTPStatus.CREATED)
            if path.startswith("/api/recipes/") and path.endswith("/run"):
                return self.send_payload(create_job(path.split("/")[3]), HTTPStatus.ACCEPTED)
            if path.startswith("/api/step-runs/") and path.endswith("/input"):
                run_id = path.split("/")[3]
                command_type = str(body.get("type") or "")
                if command_type not in {"navigate", "text", "key", "click", "selector_click", "selector_fill", "inspect"}:
                    raise ValueError("Unsupported browser input type")
                command = {"type": command_type}
                if command_type == "navigate":
                    command["url"] = validate_url(body.get("url"))
                elif command_type == "text":
                    command["text"] = str(body.get("text") or "")
                elif command_type == "key":
                    command["key"] = str(body.get("key") or "")
                elif command_type in {"selector_click", "selector_fill"}:
                    command["selector"] = validate_selector(body.get("selector"))
                    command["trial_id"] = str(body.get("trial_id") or "")
                    if command_type == "selector_fill":
                        command["value"] = str(body.get("value") or "")
                else:
                    command["x"] = float(body.get("x", 0))
                    command["y"] = float(body.get("y", 0))
                try:
                    send_browser_command(run_id, command)
                except RuntimeError as exc:
                    return self.send_error_payload(str(exc), HTTPStatus.CONFLICT)
                return self.send_payload({"ok": True})
            if path == "/api/step-runs":
                step = body.get("step") or {}
                run_id = start_step_run(step)
                return self.send_payload({"run_id": run_id, "status": "queued"}, HTTPStatus.ACCEPTED)
            return self.send_error_payload("Route not found", HTTPStatus.NOT_FOUND)
        except sqlite3.IntegrityError:
            self.send_error_payload("A project or recipe with that name already exists")
        except (sqlite3.Error, ValueError, KeyError) as exc:
            self.send_error_payload(str(exc), HTTPStatus.BAD_REQUEST)

    def stream_step_events(self, run_id: str) -> None:
        with STEP_RUNS_CHANGED:
            if run_id not in STEP_RUNS:
                return self.send_error_payload("Step run not found", HTTPStatus.NOT_FOUND)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        index = 0
        try:
            while True:
                with STEP_RUNS_CHANGED:
                    run = STEP_RUNS[run_id]
                    events = run["events"][index:]
                    done = run["done"]
                    if not events and not done:
                        STEP_RUNS_CHANGED.wait(timeout=0.5)
                        continue
                for event in events:
                    payload = f"data: {json.dumps(event)}\n\n".encode("utf-8")
                    self.wfile.write(payload)
                    self.wfile.flush()
                    index += 1
                if done and index >= len(STEP_RUNS[run_id]["events"]):
                    return
        except (BrokenPipeError, ConnectionResetError):
            return

    def do_PUT(self) -> None:
        path = urlparse(self.path).path
        try:
            body = parse_json_body(self)
            if path.startswith("/api/recipes/"):
                recipe_id = path.split("/")[3]
                with db_connection() as connection:
                    recipe = connection.execute("SELECT project_id FROM recipes WHERE id = ?", (recipe_id,)).fetchone()
                if not recipe:
                    return self.send_error_payload("Recipe not found", HTTPStatus.NOT_FOUND)
                return self.send_payload(save_recipe(recipe["project_id"], recipe_id, body.get("name", ""), body.get("definition") or {}))
            return self.send_error_payload("Route not found", HTTPStatus.NOT_FOUND)
        except sqlite3.IntegrityError:
            self.send_error_payload("A recipe with that name already exists in this project")
        except (sqlite3.Error, ValueError, KeyError) as exc:
            self.send_error_payload(str(exc), HTTPStatus.BAD_REQUEST)

    def do_DELETE(self) -> None:
        path = urlparse(self.path).path
        try:
            if path.startswith("/api/recipes/"):
                recipe_id = path.split("/")[3]
                with db_connection() as connection:
                    recipe = connection.execute("SELECT id FROM recipes WHERE id = ?", (recipe_id,)).fetchone()
                    if not recipe:
                        return self.send_error_payload("Recipe not found", HTTPStatus.NOT_FOUND)
                    connection.execute("DELETE FROM recipes WHERE id = ?", (recipe_id,))
                return self.send_payload({"deleted": recipe_id})
            return self.send_error_payload("Route not found", HTTPStatus.NOT_FOUND)
        except sqlite3.IntegrityError:
            self.send_error_payload("Recipe cannot be deleted because it has job history")
        except sqlite3.Error as exc:
            self.send_error_payload(str(exc), HTTPStatus.BAD_REQUEST)

    def serve_static(self, path: str) -> None:
        if not FRONTEND_DIST.exists():
            return self.send_error_payload("Frontend build not found. Run npm run build first.", HTTPStatus.NOT_FOUND)
        candidate = (FRONTEND_DIST / (path.lstrip("/") or "index.html")).resolve()
        if FRONTEND_DIST not in candidate.parents and candidate != FRONTEND_DIST:
            return self.send_error_payload("Invalid path", HTTPStatus.BAD_REQUEST)
        if not candidate.is_file():
            candidate = FRONTEND_DIST / "index.html"
        body = candidate.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mimetypes.guess_type(str(candidate))[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    initialize_database()
    host = os.environ.get("SUI_HOST", "127.0.0.1")
    port = int(os.environ.get("SUI_PORT", "8000"))
    server = ThreadingHTTPServer((host, port), SuiHandler)
    print(f"Sui backend running at http://{host}:{port}")
    print("Build the frontend with `npm run build` for the backend-served UI.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Sui backend")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
