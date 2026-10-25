from __future__ import annotations

import json
import mimetypes
import os
import re
import sqlite3
import subprocess
import signal
import sys
import threading
import tempfile
import time
import uuid
import base64
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "sui.db"
GENERATED_DIR = DATA_DIR / "generated"
FRONTEND_DIST = ROOT / "frontend" / "dist"
SCHEMA_VERSION = 1
SUPPORTED_STEP_TYPES = {
    "navigate_to_website",
    "navigate",
    "click",
    "fill",
    "delay",
    "keypress",
    "assign",
    "scrape",
    "message_box",
    "check_element_exists",
    "check_text_exists",
    "if",
    "for",
    "use_js",
}
KEYPRESS_KEYS = {
    "Enter", "Escape", "Tab", "Backspace", "Delete", "Space",
    "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Home", "End",
    "PageUp", "PageDown", "Insert", "Control", "Shift", "Alt", "Meta",
    *(f"F{index}" for index in range(1, 13)),
}
STEP_RUNS: dict[str, dict[str, Any]] = {}
STEP_RUNS_LOCK = threading.Lock()
STEP_RUNS_CHANGED = threading.Condition(STEP_RUNS_LOCK)
STEP_SESSION_START_LOCK = threading.Lock()
JOB_RUNS: dict[str, dict[str, Any]] = {}
JOB_RUNS_LOCK = threading.Lock()
JOB_RUNS_CHANGED = threading.Condition(JOB_RUNS_LOCK)


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


def validate_delay(value: Any) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Delay requires a number of seconds") from exc
    if seconds < 0.1 or seconds > 3600:
        raise ValueError("Delay must be between 0.1 and 3600 seconds")
    return round(seconds, 3)


def validate_until_load_completes(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def validate_step_disabled(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def validate_keypress(value: Any) -> str:
    key = str(value or "Enter").strip()
    if key == "Esc":
        key = "Escape"
    if key not in KEYPRESS_KEYS and len(key) != 1:
        raise ValueError("Unsupported keypress key")
    return key


def validate_navigation_action(value: Any) -> str:
    action = str(value or "back").strip().lower()
    if action not in {"back", "forward", "refresh"}:
        raise ValueError("Navigate action must be Back, Forward, or Refresh")
    return action


def validate_condition_operator(value: Any) -> str:
    operator = str(value or "=").strip()
    if operator not in {"=", "<>", "<", "<=", ">", ">=", "in"}:
        raise ValueError("Unsupported condition operator")
    return operator


def validate_variable_name(value: Any) -> str:
    name = str(value or "").strip()
    if not name or not name.replace("_", "a").isalnum() or name[0].isdigit():
        raise ValueError("Variable names must start with a letter or underscore and contain only letters, numbers, and underscores")
    return name


def normalize_variable_reference(value: Any) -> str:
    reference = str(value or "").strip()
    if reference.startswith("{{") and reference.endswith("}}"):
        reference = reference[2:-2].strip()
    elif reference.startswith("{") and reference.endswith("}"):
        reference = reference[1:-1].strip()
    return validate_variable_name(reference)


def normalize_loop_iterator(value: Any) -> tuple[str, int | None]:
    raw_iterator = str(value or "i").strip()
    if "=" not in raw_iterator:
        return normalize_variable_reference(raw_iterator), None
    iterator_reference, start_text = raw_iterator.split("=", 1)
    try:
        start_value = int(start_text.strip())
    except (TypeError, ValueError) as exc:
        raise ValueError("For loop iterator start must be an integer, such as i=1") from exc
    if start_value < -100000 or start_value > 100000:
        raise ValueError("For loop iterator start must be between -100000 and 100000")
    return normalize_variable_reference(iterator_reference), start_value


def validate_loop_range(value: Any, variable_names: set[str]) -> str:
    range_value = str(value or "").strip()
    if not range_value:
        raise ValueError("For loop requires a hardcoded integer or a recipe variable such as {count}")
    expression = re.fullmatch(r"(.+?)(?:\s*([+-])\s*(\d+))?", range_value)
    if not expression:
        raise ValueError("For loop range must be a number, variable, or variable plus/minus an integer")
    base = expression.group(1).strip()
    offset = int(expression.group(3) or 0) * (-1 if expression.group(2) == "-" else 1)
    if base.isdigit():
        count = int(base) + offset
    else:
        variable_name = normalize_variable_reference(base) if base.startswith("{") else validate_variable_name(base)
        if variable_name not in variable_names:
            raise ValueError(f"For loop range refers to unknown variable {variable_name}")
        return range_value
    if count < 0 or count > 100000:
        raise ValueError("For loop range must be between 0 and 100000")
    return range_value


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
    raw_variables = definition.get("variables", [])
    if not isinstance(raw_variables, list):
        raise ValueError("Recipe variables must be an array")
    variables: list[dict[str, str]] = []
    variable_names: set[str] = set()
    for raw_variable in raw_variables:
        if not isinstance(raw_variable, dict):
            raise ValueError("Every recipe variable must be an object")
        variable_name = validate_variable_name(raw_variable.get("name"))
        if variable_name in variable_names:
            raise ValueError(f"Variable {variable_name} is already defined")
        variable_names.add(variable_name)
        variables.append({
            "id": str(raw_variable.get("id") or f"variable_{uuid.uuid4().hex[:12]}"),
            "name": variable_name,
            "value": str(raw_variable.get("value") or ""),
        })
    def normalize_steps(raw_items: Any, branch_label: str = "steps") -> list[dict[str, Any]]:
        if not isinstance(raw_items, list):
            raise ValueError(f"Recipe {branch_label} must be an array")
        normalized_steps: list[dict[str, Any]] = []
        for index, raw_step in enumerate(raw_items):
            if not isinstance(raw_step, dict):
                raise ValueError("Every recipe step must be an object")
            step_type = str(raw_step.get("type") or "")
            if step_type not in SUPPORTED_STEP_TYPES:
                raise ValueError("Unsupported recipe activity")
            settings = raw_step.get("settings") or {}
            position = raw_step.get("position") or {}
            if step_type == "navigate_to_website":
                normalized_settings = {"url": validate_url(settings.get("url"))}
            elif step_type == "navigate":
                normalized_settings = {"action": validate_navigation_action(settings.get("action", "back"))}
            elif step_type == "fill":
                normalized_settings = {
                    "selector": validate_selector(settings.get("selector")),
                    "value": str(settings.get("value") or ""),
                }
            elif step_type == "delay":
                normalized_settings = {
                    "seconds": validate_delay(settings.get("seconds", 1)),
                    "until_load_completes": validate_until_load_completes(settings.get("until_load_completes", False)),
                }
            elif step_type == "keypress":
                normalized_settings = {"key": validate_keypress(settings.get("key", "Enter"))}
            elif step_type == "assign":
                variable_name = validate_variable_name(settings.get("variable"))
                if variable_name not in variable_names:
                    raise ValueError(f"Assign refers to unknown variable {variable_name}")
                normalized_settings = {
                    "variable": variable_name,
                    "value": str(settings.get("value") or ""),
                }
            elif step_type == "scrape":
                variable_name = validate_variable_name(settings.get("variable"))
                if variable_name not in variable_names:
                    raise ValueError(f"Scrape refers to unknown variable {variable_name}")
                normalized_settings = {
                    "selector": validate_selector(settings.get("selector")),
                    "variable": variable_name,
                }
            elif step_type == "message_box":
                normalized_settings = {"message": str(settings.get("message") or "")}
            elif step_type == "check_element_exists":
                normalized_settings = {
                    "selector": validate_selector(settings.get("selector")),
                    "timeout_seconds": validate_delay(settings.get("timeout_seconds", 5)),
                }
            elif step_type == "check_text_exists":
                text = str(settings.get("text") or "").strip()
                if not text:
                    raise ValueError("Check text exists requires text or {{variable}}")
                normalized_settings = {
                    "text": text,
                    "case_insensitive": bool(settings.get("case_insensitive", True)),
                    "timeout_seconds": validate_delay(settings.get("timeout_seconds", 5)),
                }
            elif step_type == "if":
                normalized_settings = {
                    "left": str(settings.get("left") or ""),
                    "operator": validate_condition_operator(settings.get("operator", "=")),
                    "right": str(settings.get("right") or ""),
                }
            elif step_type == "for":
                iterator, inline_start = normalize_loop_iterator(settings.get("iterator", "i"))
                start_value = inline_start if inline_start is not None else int(settings.get("start_value", 0) or 0)
                if start_value < -100000 or start_value > 100000:
                    raise ValueError("For loop iterator start must be between -100000 and 100000")
                normalized_settings = {
                    "iterator": iterator,
                    "start_value": start_value,
                    "range_value": validate_loop_range(settings.get("range_value", ""), variable_names),
                }
            elif step_type == "use_js":
                code = str(settings.get("code") or "").strip()
                if not code:
                    raise ValueError("Use JS requires a function body")
                raw_inputs = settings.get("inputs") or []
                if not isinstance(raw_inputs, list):
                    raise ValueError("Use JS inputs must be an array of variables")
                inputs = [normalize_variable_reference(item) for item in raw_inputs if str(item or "").strip()]
                if any(item not in variable_names for item in inputs):
                    raise ValueError("Use JS inputs must reference existing variables")
                output_reference = str(settings.get("output") or "").strip()
                output = normalize_variable_reference(output_reference) if output_reference else ""
                if output and output not in variable_names:
                    raise ValueError(f"Use JS output refers to unknown variable {output}")
                normalized_settings = {"code": code, "inputs": inputs, "output": output}
            else:
                normalized_settings = {"selector": validate_selector(settings.get("selector"))}

            branches: dict[str, list[dict[str, Any]]] = {}
            raw_branches = raw_step.get("branches") or {}
            if step_type in {"check_element_exists", "check_text_exists"}:
                branches = {
                    "exists": normalize_steps(raw_branches.get("exists", []), "exists branch"),
                    "not_exists": normalize_steps(raw_branches.get("not_exists", []), "not exists branch"),
                }
            elif step_type == "if":
                branches = {
                    "then": normalize_steps(raw_branches.get("then", []), "then branch"),
                    "else": normalize_steps(raw_branches.get("else", []), "else branch"),
                }
            elif step_type == "for":
                branches = {
                    "body": normalize_steps(raw_branches.get("body", []), "for loop body"),
                }
            normalized_steps.append(
                {
                    "id": str(raw_step.get("id") or f"step_{uuid.uuid4().hex[:12]}"),
                    "type": step_type,
                    "disabled": validate_step_disabled(raw_step.get("disabled", False)),
                    "settings": normalized_settings,
                    "connections": raw_step.get("connections") or {"next": []},
                    "position": {
                        "x": int(position.get("x", 48 + index * 220)),
                        "y": int(position.get("y", 48)),
                    },
                    "branches": branches,
                }
            )
        return normalized_steps

    steps = normalize_steps(raw_steps)
    """
    for index, raw_step in enumerate(raw_steps):
        if not isinstance(raw_step, dict):
            raise ValueError("Every recipe step must be an object")
        step_type = str(raw_step.get("type") or "")
        if step_type not in SUPPORTED_STEP_TYPES:
            raise ValueError("Only Navigate to website, Click, Fill, Delay, Assign, Scrape, and Message box are supported")
        settings = raw_step.get("settings") or {}
        position = raw_step.get("position") or {}
        if step_type == "navigate_to_website":
            normalized_settings = {"url": validate_url(settings.get("url"))}
        elif step_type == "fill":
            normalized_settings = {
                "selector": validate_selector(settings.get("selector")),
                "value": str(settings.get("value") or ""),
            }
        elif step_type == "delay":
            normalized_settings = {"seconds": validate_delay(settings.get("seconds", 1))}
        elif step_type == "assign":
            variable_name = validate_variable_name(settings.get("variable"))
            if variable_name not in variable_names:
                raise ValueError(f"Assign refers to unknown variable {variable_name}")
            normalized_settings = {
                "variable": variable_name,
                "value": str(settings.get("value") or ""),
            }
        elif step_type == "scrape":
            variable_name = validate_variable_name(settings.get("variable"))
            if variable_name not in variable_names:
                raise ValueError(f"Scrape refers to unknown variable {variable_name}")
            normalized_settings = {
                "selector": validate_selector(settings.get("selector")),
                "variable": variable_name,
            }
        elif step_type == "message_box":
            normalized_settings = {"message": str(settings.get("message") or "")}
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
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "project_id": project_id,
        "recipe_id": recipe_id,
        "recipe_name": recipe_name,
        "version": version,
        "steps": steps,
        "connections": definition.get("connections") or [],
        "variables": variables,
        "mode": "headless" if definition.get("mode") == "headless" else "headful",
    }


def _generated_window_support(lines: list[str]) -> None:
    lines.extend(
        [
            "def preserve_foreground_window():",
            "    if os.name != \"nt\": return None",
            "    return ctypes.windll.user32.GetForegroundWindow()",
            "",
            "",
            "def restore_foreground_window(hwnd):",
            "    if hwnd and os.name == \"nt\": ctypes.windll.user32.SetForegroundWindow(hwnd)",
            "",
            "",
            "def visible_window_handles():",
            "    if os.name != \"nt\": return set()",
            "    user32 = ctypes.windll.user32",
            "    handles = set()",
            "    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)",
            "    def visit(hwnd, _):",
            "        if user32.IsWindowVisible(hwnd): handles.add(int(hwnd))",
            "        return True",
            "    callback_ref = callback_type(visit)",
            "    user32.EnumWindows(callback_ref, 0)",
            "    return handles",
            "",
            "",
            "def is_chrome_window(hwnd):",
            "    user32 = ctypes.windll.user32",
            "    title = ctypes.create_unicode_buffer(512)",
            "    class_name = ctypes.create_unicode_buffer(256)",
            "    user32.GetWindowTextW(hwnd, title, 512)",
            "    user32.GetClassNameW(hwnd, class_name, 256)",
            "    return \"Chrome_WidgetWin\" in class_name.value or \"Google Chrome\" in title.value",
            "",
            "",
            "def maximize_browser_window(before_handles):",
            "    if os.name != \"nt\": return None",
            "    user32 = ctypes.windll.user32",
            "    candidates = []",
            "    for _ in range(20):",
            "        candidates = [hwnd for hwnd in visible_window_handles() - before_handles if is_chrome_window(hwnd)]",
            "        if candidates: break",
            "        time.sleep(0.05)",
            "    foreground = user32.GetForegroundWindow()",
            "    if not candidates and foreground and is_chrome_window(foreground): candidates = [foreground]",
            "    if not candidates: return None",
            "    hwnd = candidates[-1]",
            "    user32.ShowWindow(hwnd, 3)",
            "    return hwnd",
        ]
    )


def _generate_recursive_sync_python(definition: dict[str, Any]) -> str:
    mode = definition.get("mode") or "headful"
    steps_literal = json.dumps(json.dumps(definition.get("steps", [])))
    variables_literal = json.dumps(json.dumps({item["name"]: item.get("value", "") for item in definition.get("variables", [])}))
    lines = [
        "import base64",
        "import ctypes",
        "import json",
        "import os",
        "import re",
        "import time",
        "from rustwright.sync_api import sync_playwright",
        "",
        "",
        "def human_pause(seconds):",
        "    time.sleep(seconds)",
        "",
        "",
        "def resolve(value, variables):",
        "    text = str(value or \"\")",
        "    return re.sub(r\"\\{\\{?\\s*([A-Za-z_][A-Za-z0-9_]*)\\s*\\}\\}?\", lambda match: str(variables.get(match.group(1), match.group(0))), text)",
        "",
        "",
        "def resolve_loop_range(value, variables):",
        "    text = str(value or '').strip()",
        "    match = re.fullmatch(r'(.*?)(?:\\s*([+-])\\s*(\\d+))?', text)",
        "    if not match: raise ValueError('Invalid for loop range')",
        "    base = match.group(1).strip(); offset = int(match.group(3) or 0) * (-1 if match.group(2) == '-' else 1)",
        "    if base.isdigit(): return int(base) + offset",
        "    variable_name = base.strip('{}').strip()",
        "    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', variable_name): raise ValueError('Invalid for loop range variable')",
        "    return int(variables.get(variable_name, 0)) + offset",
        "",
        "",
        "def locator_for(page, selector):",
        "    value = str(selector or '').strip()",
        "    if value.startswith('role='):",
        "        role_query = value[5:].strip(); name = None",
        "        if '[name=' in role_query:",
        "            role_query, name_part = role_query.split('[name=', 1); name = name_part.rstrip(']').strip(\"'\\\"\")",
        "        get_by_role = getattr(page, 'get_by_role', None)",
        "        if get_by_role:",
        "            return get_by_role(role_query.strip(), name=name) if name else get_by_role(role_query.strip())",
        "        return page.locator('[role=\\\"' + role_query.strip() + '\\\"]')",
        "    return page.locator(value)",
        "",
        "",
        "def wait_for_locator(page, selector, action, timeout_ms=30000):",
        "    value = str(selector or '').strip()",
        "    if not value: raise ValueError(action + ' requires a selector')",
        "    locator = locator_for(page, value).first",
        "    try:",
        "        locator.wait_for(state='attached' if str(action).lower() == 'scrape' else 'visible', timeout=timeout_ms)",
        "    except Exception as exc:",
        "        raise RuntimeError(action + ' target not found for selector ' + repr(value)) from exc",
        "    return locator",
        "",
        "",
        "def emit_frame(page):",
        "    frame = page.screenshot(type=\"jpeg\", quality=70)",
        "    print(\"SUI_FRAME:\" + base64.b64encode(frame).decode(\"ascii\"), flush=True)",
        "",
        "",
        "USE_JS_RUNNER = \"\"\"async ({body, args}) => { const source = 'return (async function suiJSRunner(args) {' + String(body || '') + '\\\\n})(args);'; const runner = new Function('args', source); return await runner(args); }\"\"\"",
        "",
        "",
        "def evaluate_condition(left, operator, right, variables):",
        "    left_text = resolve(left, variables).strip()",
        "    right_text = resolve(right, variables).strip()",
        "    try:",
        "        left_value = float(left_text); right_value = float(right_text)",
        "    except (TypeError, ValueError):",
        "        left_value = left_text; right_value = right_text",
        "    if operator == '=': return left_value == right_value",
        "    if operator == '<>': return left_value != right_value",
        "    if operator == '<': return left_value < right_value",
        "    if operator == '<=': return left_value <= right_value",
        "    if operator == '>': return left_value > right_value",
        "    if operator == '>=': return left_value >= right_value",
        "    if operator == 'in': return left_text.lower() in {item.strip().lower() for item in right_text.split(',')}",
        "    raise ValueError('Unsupported condition operator: ' + str(operator))",
        "",
        "",
        "def show_message_box(page, message):",
        "    page.evaluate(\"\"\"(text) => { const old = document.getElementById('sui-message-box'); if (old) old.remove(); const overlay = document.createElement('div'); overlay.id = 'sui-message-box'; Object.assign(overlay.style, { position: 'fixed', inset: '0', zIndex: '2147483647', display: 'grid', placeItems: 'center', background: 'rgba(20, 25, 22, .35)' }); const box = document.createElement('div'); Object.assign(box.style, { padding: '24px', borderRadius: '12px', background: '#fff', color: '#17201d' }); const copy = document.createElement('div'); copy.textContent = text; copy.style.marginBottom = '20px'; const ok = document.createElement('button'); ok.id = 'sui-message-box-ok'; ok.textContent = 'OK'; ok.onclick = () => overlay.remove(); box.append(copy, ok); overlay.append(box); document.body.append(overlay); }\"\"\", message)",
        "",
        "",
        f"recipe_steps = json.loads({steps_literal})",
        f"variables = json.loads({variables_literal})",
        "",
        "",
        "def execute_step(page, step):",
        "    if step.get('disabled'): return",
        "    settings = step.get('settings') or {}",
        "    step_type = step.get('type')",
        "    if step_type == 'navigate_to_website':",
        "        human_pause(0.35); page.goto(resolve(settings.get('url'), variables), wait_until='domcontentloaded'); human_pause(0.65)",
        "    elif step_type == 'navigate':",
        "        action = settings.get('action', 'back'); human_pause(0.30)",
        "        if action == 'back': page.go_back(wait_until='commit')",
        "        elif action == 'forward': page.go_forward(wait_until='commit')",
        "        elif action == 'refresh': page.reload(wait_until='commit')",
        "        else: raise ValueError('Unsupported Navigate action: ' + str(action))",
        "        human_pause(0.45)",
        "    elif step_type == 'click':",
        "        human_pause(0.30); wait_for_locator(page, resolve(settings.get('selector'), variables), 'Click').click(); human_pause(0.35)",
        "    elif step_type == 'fill':",
        "        human_pause(0.30); wait_for_locator(page, resolve(settings.get('selector'), variables), 'Fill').fill(resolve(settings.get('value'), variables)); human_pause(0.35)",
        "    elif step_type == 'delay':",
        "        if settings.get('until_load_completes'): page.wait_for_load_state('load')",
        "        else: time.sleep(float(settings.get('seconds', 1)))",
        "    elif step_type == 'keypress':",
        "        human_pause(0.25); page.keyboard.press(str(settings.get('key') or 'Enter')); human_pause(0.25)",
        "    elif step_type == 'assign':",
        "        variables[settings.get('variable')] = resolve(settings.get('value'), variables); human_pause(0.25)",
        "    elif step_type == 'scrape':",
        "        variables[settings.get('variable')] = wait_for_locator(page, resolve(settings.get('selector'), variables), 'Scrape').inner_text(); human_pause(0.30)",
        "    elif step_type == 'message_box':",
        f"        show_message_box(page, resolve(settings.get('message'), variables)); page.locator('#sui-message-box-ok').click() if {json.dumps(mode)} == 'headless' else page.wait_for_function(\"() => !document.getElementById('sui-message-box')\")",
        "    elif step_type == 'check_element_exists':",
        "        selector = resolve(settings.get('selector'), variables); locator = locator_for(page, selector)",
        "        try:",
        "            locator.wait_for(state='attached', timeout=int(float(settings.get('timeout_seconds', 5)) * 1000)); exists = True",
        "        except Exception:",
        "            exists = False",
        "        execute_steps(page, (step.get('branches') or {}).get('exists' if exists else 'not_exists', []))",
        "    elif step_type == 'check_text_exists':",
        "        expected = resolve(settings.get('text'), variables); timeout = float(settings.get('timeout_seconds', 5)); deadline = time.monotonic() + timeout; exists = False",
        "        while time.monotonic() < deadline:",
        "            body = page.locator('body').inner_text(); exists = expected.lower() in body.lower() if settings.get('case_insensitive', True) else expected in body",
        "            if exists: break",
        "            time.sleep(0.2)",
        "        execute_steps(page, (step.get('branches') or {}).get('exists' if exists else 'not_exists', []))",
        "    elif step_type == 'if':",
        "        matched = evaluate_condition(settings.get('left'), settings.get('operator', '='), settings.get('right'), variables); execute_steps(page, (step.get('branches') or {}).get('then' if matched else 'else', []))",
        "    elif step_type == 'for':",
        "        loop_count = resolve_loop_range(settings.get('range_value'), variables); loop_start = int(settings.get('start_value', 0)); iterator = settings.get('iterator') or 'i'",
        "        if loop_count < 0: raise ValueError('For loop range must not be negative')",
        "        for loop_index in range(loop_start, loop_start + loop_count): variables[iterator] = loop_index; execute_steps(page, (step.get('branches') or {}).get('body', []))",
        "    elif step_type == 'use_js':",
        "        args = [variables.get(name, '') for name in (settings.get('inputs') or [])]; result = page.evaluate(USE_JS_RUNNER, {'body': settings.get('code', ''), 'args': args}); output_name = settings.get('output')",
        "        if output_name: variables[output_name] = result",
        "    else:",
        "        raise ValueError('Unsupported activity: ' + str(step_type))",
        "",
        "",
        "def execute_steps(page, steps):",
        "    for step in steps:",
        "        if step.get('disabled'):",
        "            print(f\"SUI_LOG:Step skipped: {step.get('id', 'step')} ({step.get('type')})\", flush=True)",
        "            continue",
        "        step_id = step.get('id', 'step')",
        "        print(f\"SUI_LOG:Step started: {step_id} ({step.get('type')})\", flush=True)",
        "        execute_step(page, step)",
        "        emit_frame(page)",
        "        print(f\"SUI_LOG:Step completed: {step_id} ({step.get('type')})\", flush=True)",
        "",
        "",
    ]
    _generated_window_support(lines)
    lines.extend(
        [
            "",
            "",
            "def run():",
            "    browser = None",
            "    with sync_playwright() as playwright:",
            f"        foreground_window = preserve_foreground_window() if {json.dumps(mode)} == 'headful' else None",
            "        before_handles = visible_window_handles()",
            "        browser = playwright.chromium.launch(headless=" + json.dumps(mode) + " == 'headless')",
            "        page = browser.new_page(no_viewport=True)",
            f"        maximize_browser_window(before_handles) if {json.dumps(mode)} == 'headful' else None",
            "        restore_foreground_window(foreground_window)",
            "        try:",
            "            emit_frame(page)",
            "            execute_steps(page, recipe_steps)",
            "        finally:",
            "            try:",
            "                if browser: browser.close()",
            "            except Exception: pass",
            "",
            "",
            "if __name__ == '__main__':",
            "    run()",
            "",
        ]
    )
    return "\n".join(lines)


def generate_python(definition: dict[str, Any]) -> str:
    return _generate_recursive_sync_python(definition)
    variable_values = {variable["name"]: variable.get("value", "") for variable in definition.get("variables", [])}
    chrome_mode = definition.get("mode") or "headful"
    lines = [
        "import base64",
        "import ctypes",
        "import os",
        "import re",
        "import time",
        "from rustwright.sync_api import sync_playwright",
        "",
        "",
        "def human_pause(seconds):",
        "    time.sleep(seconds)",
        "",
        "",
        "def resolve(value, variables):",
        "    text = str(value or \"\")",
        "    return re.sub(r\"\\{\\{\\s*([A-Za-z_][A-Za-z0-9_]*)\\s*\\}\\}\", lambda match: str(variables.get(match.group(1), match.group(0))), text)",
        "",
        "",
        "def emit_frame(page):",
        "    frame = page.screenshot(type=\"jpeg\", quality=70)",
        "    print(\"SUI_FRAME:\" + base64.b64encode(frame).decode(\"ascii\"), flush=True)",
        "",
        "",
        "def show_message_box(page, message):",
        "    page.evaluate(\"\"\"(text) => {",
        "        const old = document.getElementById('sui-message-box');",
        "        if (old) old.remove();",
        "        const overlay = document.createElement('div');",
        "        overlay.id = 'sui-message-box';",
        "        Object.assign(overlay.style, { position: 'fixed', inset: '0', zIndex: '2147483647', display: 'grid', placeItems: 'center', background: 'rgba(20, 25, 22, .35)', fontFamily: 'system-ui, sans-serif' });",
        "        const box = document.createElement('div');",
        "        Object.assign(box.style, { width: 'min(420px, calc(100vw - 40px))', padding: '24px', borderRadius: '12px', background: '#fff', color: '#17201d', boxShadow: '0 18px 50px rgba(0,0,0,.28)' });",
        "        const copy = document.createElement('div');",
        "        copy.textContent = text;",
        "        copy.style.cssText = 'white-space: pre-wrap; line-height: 1.5; margin-bottom: 20px;';",
        "        const ok = document.createElement('button');",
        "        ok.id = 'sui-message-box-ok';",
        "        ok.textContent = 'OK';",
        "        ok.style.cssText = 'border: 0; border-radius: 7px; padding: 9px 18px; background: #FF5B35; color: #fff; font-weight: 700; cursor: pointer;';",
        "        ok.addEventListener('click', () => overlay.remove());",
        "        box.append(copy, ok);",
        "        overlay.append(box);",
        "        document.body.append(overlay);",
        "    }\"\"\", message)",
        "",
        "",
        "def preserve_foreground_window():",
        "    if os.name != \"nt\": return None",
        "    return ctypes.windll.user32.GetForegroundWindow()",
        "",
        "",
        "def restore_foreground_window(hwnd):",
        "    if hwnd and os.name == \"nt\": ctypes.windll.user32.SetForegroundWindow(hwnd)",
        "",
        "",
        "def visible_window_handles():",
        "    if os.name != \"nt\": return set()",
        "    user32 = ctypes.windll.user32",
        "    handles = set()",
        "    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)",
        "    def visit(hwnd, _):",
        "        if user32.IsWindowVisible(hwnd): handles.add(int(hwnd))",
        "        return True",
        "    callback_ref = callback_type(visit)",
        "    user32.EnumWindows(callback_ref, 0)",
        "    return handles",
        "",
        "",
        "def is_chrome_window(hwnd):",
        "    user32 = ctypes.windll.user32",
        "    title = ctypes.create_unicode_buffer(512)",
        "    class_name = ctypes.create_unicode_buffer(256)",
        "    user32.GetWindowTextW(hwnd, title, 512)",
        "    user32.GetClassNameW(hwnd, class_name, 256)",
        "    return \"Chrome_WidgetWin\" in class_name.value or \"Google Chrome\" in title.value",
        "",
        "",
        "def maximize_browser_window(before_handles):",
        "    if os.name != \"nt\": return None",
        "    user32 = ctypes.windll.user32",
        "    candidates = []",
        "    for _ in range(20):",
        "        candidates = [hwnd for hwnd in visible_window_handles() - before_handles if is_chrome_window(hwnd)]",
        "        if candidates: break",
        "        time.sleep(0.05)",
        "    foreground = user32.GetForegroundWindow()",
        "    if not candidates and foreground and is_chrome_window(foreground): candidates = [foreground]",
        "    if not candidates: return None",
        "    hwnd = candidates[-1]",
        "    user32.ShowWindow(hwnd, 3)",
        "    return hwnd",
        "",
        "",
        "def launch_browser(playwright, mode):",
        "    browser = playwright.chromium.launch(headless=mode == \"headless\")",
        "    page = browser.new_page(no_viewport=True)",
        "    return browser, page",
        "",
        "",
        "def run():",
        "    browser = None",
        "    with sync_playwright() as playwright:",
        f"        foreground_window = preserve_foreground_window() if {json.dumps(chrome_mode)} == \"headful\" else None",
        "        before_handles = visible_window_handles()",
        f"        browser, page = launch_browser(playwright, {json.dumps(chrome_mode)})",
        f"        maximize_browser_window(before_handles) if {json.dumps(chrome_mode)} == \"headful\" else None",
        "        restore_foreground_window(foreground_window)",
        f"        variables = {json.dumps(variable_values)}",
        "        emit_frame(page)",
        "        try:",
    ]
    for step in definition["steps"]:
        # json.dumps creates a safe Python string literal; it is never executed as code.
        start_log = json.dumps(f"SUI_LOG:Step started: {step['id']} ({step['type']})")
        complete_log = json.dumps(f"SUI_LOG:Step completed: {step['id']} ({step['type']})")
        if step["type"] == "navigate_to_website":
            url_literal = json.dumps(step["settings"]["url"])
            lines.extend(
                [
                    f"            print({start_log}, flush=True)",
                    "            human_pause(0.35)",
                    f"            page.goto(resolve({url_literal}, variables), wait_until=\"domcontentloaded\")",
                    "            human_pause(0.65)",
                    f"            print(\"SUI_LOG:Navigated: {step['id']}\", flush=True)",
                    f"            print({complete_log}, flush=True)",
                ]
            )
        elif step["type"] == "fill":
            selector_literal = json.dumps(step["settings"]["selector"])
            value_literal = json.dumps(step["settings"].get("value", ""))
            lines.extend(
                [
                    f"            print({start_log}, flush=True)",
                    "            human_pause(0.30)",
                    f"            page.locator({selector_literal}).first.fill(resolve({value_literal}, variables))",
                    "            human_pause(0.35)",
                    f"            print(\"SUI_LOG:Filled: {step['id']}\", flush=True)",
                    f"            print({complete_log}, flush=True)",
                ]
            )
        elif step["type"] == "delay":
            seconds_literal = json.dumps(step["settings"].get("seconds", 1))
            lines.extend(
                [
                    f"            print({start_log}, flush=True)",
                    f"            time.sleep({seconds_literal})",
                    f"            print(\"SUI_LOG:Delayed: {step['id']}\", flush=True)",
                    f"            print({complete_log}, flush=True)",
                ]
            )
        elif step["type"] == "assign":
            variable_literal = json.dumps(step["settings"]["variable"])
            value_literal = json.dumps(step["settings"].get("value", ""))
            lines.extend(
                [
                    f"            print({start_log}, flush=True)",
                    f"            variables[{variable_literal}] = resolve({value_literal}, variables)",
                    "            human_pause(0.25)",
                    f"            print(\"SUI_LOG:Assigned: {step['id']}\", flush=True)",
                    f"            print({complete_log}, flush=True)",
                ]
            )
        elif step["type"] == "scrape":
            selector_literal = json.dumps(step["settings"]["selector"])
            variable_literal = json.dumps(step["settings"]["variable"])
            lines.extend(
                [
                    f"            print({start_log}, flush=True)",
                    f"            variables[{variable_literal}] = page.locator({selector_literal}).first.inner_text()",
                    "            human_pause(0.30)",
                    f"            print(\"SUI_LOG:Scraped: {step['id']}\", flush=True)",
                    f"            print({complete_log}, flush=True)",
                ]
            )
        elif step["type"] == "message_box":
            message_literal = json.dumps(step["settings"].get("message", ""))
            lines.extend(
                [
                    f"            print({start_log}, flush=True)",
                    f"            show_message_box(page, resolve({message_literal}, variables))",
                    *( ["            page.locator(\"#sui-message-box-ok\").click()"] if chrome_mode == "headless" else ["            page.wait_for_function(\"() => !document.getElementById('sui-message-box')\")"] ),
                    f"            print(\"SUI_LOG:Message box closed: {step['id']}\", flush=True)",
                    f"            print({complete_log}, flush=True)",
                ]
            )
        else:
            selector_literal = json.dumps(step["settings"]["selector"])
            lines.extend(
                [
                    f"            print({start_log}, flush=True)",
                    "            human_pause(0.30)",
                    f"            page.locator({selector_literal}).first.click()",
                    "            human_pause(0.35)",
                    f"            print(\"SUI_LOG:Clicked: {step['id']}\", flush=True)",
                    f"            print({complete_log}, flush=True)",
                ]
            )
        lines.append("            emit_frame(page)")
    lines.extend(
        [
            "        finally:",
            "            try:",
            "                if browser: browser.close()",
            "            except Exception: pass",
            "",
            "",
            "if __name__ == \"__main__\":",
            "    run()",
            "",
        ]
    )
    return "\n".join(lines)


def _generate_recursive_step_python(definition: dict[str, Any]) -> str:
    mode = definition.get("mode") or "headful"
    step_literal = json.dumps(json.dumps((definition.get("steps") or [{}])[0]))
    variables_literal = json.dumps(json.dumps({item["name"]: item.get("value", "") for item in definition.get("variables", [])}))
    lines = [
        "import asyncio",
        "import base64",
        "import ctypes",
        "import json",
        "import os",
        "import re",
        "import sys",
        "import time",
        "from rustwright.async_api import async_playwright",
        "",
        "",
        "async def human_pause(seconds):",
        "    await asyncio.sleep(seconds)",
        "",
        "",
        "def resolve(value, variables):",
        "    text = str(value or \"\")",
        "    return re.sub(r\"\\{\\{?\\s*([A-Za-z_][A-Za-z0-9_]*)\\s*\\}\\}?\", lambda match: str(variables.get(match.group(1), match.group(0))), text)",
        "",
        "",
        "def resolve_loop_range(value, variables):",
        "    text = str(value or '').strip()",
        "    match = re.fullmatch(r'(.*?)(?:\\s*([+-])\\s*(\\d+))?', text)",
        "    if not match: raise ValueError('Invalid for loop range')",
        "    base = match.group(1).strip(); offset = int(match.group(3) or 0) * (-1 if match.group(2) == '-' else 1)",
        "    if base.isdigit(): return int(base) + offset",
        "    variable_name = base.strip('{}').strip()",
        "    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', variable_name): raise ValueError('Invalid for loop range variable')",
        "    return int(variables.get(variable_name, 0)) + offset",
        "",
        "",
        "USE_JS_RUNNER = \"\"\"async ({body, args}) => { const source = 'return (async function suiJSRunner(args) {' + String(body || '') + '\\\\n})(args);'; const runner = new Function('args', source); return await runner(args); }\"\"\"",
        "",
        "",
        "async def emit_frame(page, capture_lock):",
        "    async with capture_lock:",
        "        frame = await page.screenshot(type=\"jpeg\", quality=70)",
        "        print(\"SUI_FRAME:\" + base64.b64encode(frame).decode(\"ascii\"), flush=True)",
        "",
        "",
        "def async_locator_for(page, selector):",
        "    value = str(selector or '').strip()",
        "    if value.startswith('role='):",
        "        role_query = value[5:].strip(); name = None",
        "        if '[name=' in role_query:",
        "            role_query, name_part = role_query.split('[name=', 1); name = name_part.rstrip(']').strip(\"'\\\"\")",
        "        get_by_role = getattr(page, 'get_by_role', None)",
        "        if get_by_role:",
        "            return get_by_role(role_query.strip(), name=name) if name else get_by_role(role_query.strip())",
        "        return page.locator('[role=\\\"' + role_query.strip() + '\\\"]')",
        "    return page.locator(value)",
        "",
        "",
        "async def wait_for_locator(page, selector, action, timeout_ms=30000):",
        "    value = str(selector or '').strip()",
        "    if not value: raise ValueError(action + ' requires a selector')",
        "    locator = async_locator_for(page, value).first",
        "    try:",
        "        await locator.wait_for(state='attached' if str(action).lower() == 'scrape' else 'visible', timeout=timeout_ms)",
        "    except Exception as exc:",
        "        raise RuntimeError(action + ' target not found for selector ' + repr(value)) from exc",
        "    return locator",
        "",
        "",
        "async def show_message_box(page, message):",
        "    await page.evaluate(\"\"\"(text) => { const old = document.getElementById('sui-message-box'); if (old) old.remove(); const overlay = document.createElement('div'); overlay.id = 'sui-message-box'; Object.assign(overlay.style, { position: 'fixed', inset: '0', zIndex: '2147483647', display: 'grid', placeItems: 'center', background: 'rgba(20, 25, 22, .35)' }); const box = document.createElement('div'); Object.assign(box.style, { padding: '24px', borderRadius: '12px', background: '#fff', color: '#17201d' }); const copy = document.createElement('div'); copy.textContent = text; copy.style.marginBottom = '20px'; const ok = document.createElement('button'); ok.id = 'sui-message-box-ok'; ok.textContent = 'OK'; ok.onclick = () => overlay.remove(); box.append(copy, ok); overlay.append(box); document.body.append(overlay); }\"\"\", message)",
        "",
        "",
        "async def evaluate_condition(left, operator, right, variables):",
        "    left_text = resolve(left, variables).strip(); right_text = resolve(right, variables).strip()",
        "    try: left_value = float(left_text); right_value = float(right_text)",
        "    except (TypeError, ValueError): left_value = left_text; right_value = right_text",
        "    if operator == '=': return left_value == right_value",
        "    if operator == '<>': return left_value != right_value",
        "    if operator == '<': return left_value < right_value",
        "    if operator == '<=': return left_value <= right_value",
        "    if operator == '>': return left_value > right_value",
        "    if operator == '>=': return left_value >= right_value",
        "    if operator == 'in': return left_text.lower() in {item.strip().lower() for item in right_text.split(',')}",
        "    raise ValueError('Unsupported condition operator: ' + str(operator))",
        "",
        "",
        f"selected_step = json.loads({step_literal})",
        f"variables = json.loads({variables_literal})",
        "",
        "",
        "async def execute_step(page, step, capture_lock):",
        "    if step.get('disabled'): return",
        "    settings = step.get('settings') or {}; step_type = step.get('type')",
        "    if step_type == 'navigate_to_website':",
        "        await human_pause(0.35); await page.goto(resolve(settings.get('url'), variables), wait_until='domcontentloaded'); await human_pause(0.65)",
        "    elif step_type == 'navigate':",
        "        action = settings.get('action', 'back'); await human_pause(0.30)",
        "        if action == 'back': await page.go_back(wait_until='commit')",
        "        elif action == 'forward': await page.go_forward(wait_until='commit')",
        "        elif action == 'refresh': await page.reload(wait_until='commit')",
        "        else: raise ValueError('Unsupported Navigate action: ' + str(action))",
        "        await human_pause(0.45)",
        "    elif step_type == 'click':",
        "        await human_pause(0.30); await (await wait_for_locator(page, resolve(settings.get('selector'), variables), 'Click')).click(); await human_pause(0.35)",
        "    elif step_type == 'fill':",
        "        await human_pause(0.30); await (await wait_for_locator(page, resolve(settings.get('selector'), variables), 'Fill')).fill(resolve(settings.get('value'), variables)); await human_pause(0.35)",
        "    elif step_type == 'delay':",
        "        if settings.get('until_load_completes'): await page.wait_for_load_state('load')",
        "        else: await asyncio.sleep(float(settings.get('seconds', 1)))",
        "    elif step_type == 'keypress':",
        "        await human_pause(0.25); await page.keyboard.press(str(settings.get('key') or 'Enter')); await human_pause(0.25)",
        "    elif step_type == 'assign':",
        "        variables[settings.get('variable')] = resolve(settings.get('value'), variables); await human_pause(0.25)",
        "    elif step_type == 'scrape':",
        "        variables[settings.get('variable')] = await (await wait_for_locator(page, resolve(settings.get('selector'), variables), 'Scrape')).inner_text(); await human_pause(0.30)",
        "    elif step_type == 'message_box':",
        "        await show_message_box(page, resolve(settings.get('message'), variables))",
        f"        if {json.dumps(mode)} == 'headless': await page.locator('#sui-message-box-ok').click()",
        "        else: await page.wait_for_function(\"() => !document.getElementById('sui-message-box')\")",
        "    elif step_type == 'check_element_exists':",
        "        selector = resolve(settings.get('selector'), variables); locator = async_locator_for(page, selector)",
        "        try:",
        "            await locator.wait_for(state='attached', timeout=int(float(settings.get('timeout_seconds', 5)) * 1000)); exists = True",
        "        except Exception:",
        "            exists = False",
        "        await execute_steps(page, (step.get('branches') or {}).get('exists' if exists else 'not_exists', []), capture_lock)",
        "    elif step_type == 'check_text_exists':",
        "        expected = resolve(settings.get('text'), variables); timeout = float(settings.get('timeout_seconds', 5)); deadline = asyncio.get_running_loop().time() + timeout; exists = False",
        "        while asyncio.get_running_loop().time() < deadline:",
        "            body = await page.locator('body').inner_text(); exists = expected.lower() in body.lower() if settings.get('case_insensitive', True) else expected in body",
        "            if exists: break",
        "            await asyncio.sleep(0.2)",
        "        await execute_steps(page, (step.get('branches') or {}).get('exists' if exists else 'not_exists', []), capture_lock)",
        "    elif step_type == 'if':",
        "        matched = await evaluate_condition(settings.get('left'), settings.get('operator', '='), settings.get('right'), variables); await execute_steps(page, (step.get('branches') or {}).get('then' if matched else 'else', []), capture_lock)",
        "    elif step_type == 'for':",
        "        loop_count = resolve_loop_range(settings.get('range_value'), variables); loop_start = int(settings.get('start_value', 0)); iterator = settings.get('iterator') or 'i'",
        "        if loop_count < 0: raise ValueError('For loop range must not be negative')",
        "        for loop_index in range(loop_start, loop_start + loop_count): variables[iterator] = loop_index; await execute_steps(page, (step.get('branches') or {}).get('body', []), capture_lock)",
        "    elif step_type == 'use_js':",
        "        args = [variables.get(name, '') for name in (settings.get('inputs') or [])]; result = await page.evaluate(USE_JS_RUNNER, {'body': settings.get('code', ''), 'args': args}); output_name = settings.get('output')",
        "        if output_name: variables[output_name] = result",
        "    else:",
        "        raise ValueError('Unsupported activity: ' + str(step_type))",
        "",
        "",
        "async def execute_steps(page, steps, capture_lock):",
        "    for step in steps:",
        "        if step.get('disabled'):",
        "            print(f\"SUI_LOG:Step skipped: {step.get('id', 'step')} ({step.get('type')})\", flush=True)",
        "            continue",
        "        step_id = step.get('id', 'step')",
        "        print(f\"SUI_LOG:Step started: {step_id} ({step.get('type')})\", flush=True)",
        "        await execute_step(page, step, capture_lock)",
        "        await emit_frame(page, capture_lock)",
        "        print(f\"SUI_LOG:Step completed: {step_id} ({step.get('type')})\", flush=True)",
        "",
        "",
        "async def inspect_at(page, x, y):",
        "    return await page.evaluate(\"\"\"({x, y}) => { const target = document.elementFromPoint(x, y); if (!target) return null; const node = target.closest('button,a,input,textarea,select,[role=button],[role=link]') || target; const esc = (value) => CSS.escape(value); const selectors = []; const add = (kind, selector) => { if (selector && !selectors.some((item) => item.selector === selector)) selectors.push({ kind, selector, unique: document.querySelectorAll(selector).length === 1, matches: document.querySelectorAll(selector).length }); }; if (node.id) add('ID', '#' + esc(node.id)); if (node.getAttribute('data-testid')) add('Test ID', '[data-testid=\"' + esc(node.getAttribute('data-testid')) + '\"]'); if (node.getAttribute('name')) add('Name', '[name=\"' + esc(node.getAttribute('name')) + '\"]'); add('CSS', node.tagName.toLowerCase()); return { selector: selectors[0]?.selector || node.tagName.toLowerCase(), selectorCandidates: selectors, metadata: { tag: node.tagName.toLowerCase(), text: String(node.innerText || '').trim().slice(0, 120) } }; }\"\"\", {x, y})",
        "",
        "async def inspect_at_safe(page, x, y):",
        "    return await page.evaluate(\"\"\"({x, y}) => { const target = document.elementFromPoint(x, y) || document.body; const node = target.closest('button,a,input,textarea,select,[role=button],[role=link]') || target; const esc = (value) => { try { return CSS.escape(String(value)); } catch { return String(value).replace(/[^A-Za-z0-9_-]/g, '\\\\$&'); } }; const selectors = []; const add = (kind, selector) => { if (!selector || selectors.some((item) => item.selector === selector)) return; try { const matches = document.querySelectorAll(selector).length; selectors.push({ kind, selector, unique: matches === 1, matches }); } catch {} }; if (node.id) add('ID', '#' + esc(node.id)); if (node.getAttribute('data-testid')) add('Test ID', '[data-testid=\"' + esc(node.getAttribute('data-testid')) + '\"]'); if (node.getAttribute('name')) add('Name', '[name=\"' + esc(node.getAttribute('name')) + '\"]'); add('CSS', node.tagName.toLowerCase()); return { selector: selectors[0]?.selector || node.tagName.toLowerCase(), selectorCandidates: selectors, metadata: { tag: node.tagName.toLowerCase(), text: String(node.innerText || '').trim().slice(0, 120) } }; }\"\"\", {x, y})",
        "",
        "def json_safe(value):",
        "    if isinstance(value, dict): return {str(key): json_safe(item) for key, item in value.items()}",
        "    if isinstance(value, (list, tuple, set)): return [json_safe(item) for item in value]",
        "    return value",
        "",
        "def inspect_wire_text(value):",
        "    if isinstance(value, str):",
        "        value = json.loads(value)",
        "    return json.dumps(",
        "        json_safe(value),",
        "        ensure_ascii=True,",
        "        separators=(\",\", \":\"),",
        "    )",
        "",
        "async def inspect_at_wire(page, x, y):",
        "    return await page.evaluate(\"\"\"({x, y}) => { const hovered = document.elementFromPoint(x, y); if (!hovered) return JSON.stringify({ error: 'No element at the selected point', selectorCandidates: [] }); const target = hovered.closest('button,a,input,textarea,select,[role=button],[role=link]') || hovered; const escape = (value) => (window.CSS && CSS.escape ? CSS.escape(String(value)) : String(value).replace(/[^a-zA-Z0-9_-]/g, '\\\\$&')); const parts = []; const targetClasses = Array.from(target.classList || []).filter(Boolean).slice(0, 2); let node = target; while (node && node.nodeType === 1 && node !== document.body) { let part = node.tagName.toLowerCase(); if (node.id) { parts.unshift('#' + escape(node.id)); break; } const classes = Array.from(node.classList || []).filter(Boolean).slice(0, 2); if (classes.length) part += classes.map((name) => '.' + escape(name)).join(''); const siblings = node.parentElement ? Array.from(node.parentElement.children).filter((item) => item.tagName === node.tagName) : []; if (siblings.length > 1) part += ':nth-of-type(' + (siblings.indexOf(node) + 1) + ')'; parts.unshift(part); node = node.parentElement; } const pathSelector = parts.join(' > '); const text = String(target.innerText || target.getAttribute('aria-label') || target.value || '').replace(/\\s+/g, ' ').trim().slice(0, 120); const candidates = []; const addCandidate = (kind, value) => { if (!value || candidates.some((candidate) => candidate.selector === value)) return; let matches = null; try { matches = document.querySelectorAll(value).length; } catch (_) {} candidates.push({ selector: value, kind, unique: matches === 1, matches }); }; if (target.id) addCandidate('ID', '#' + escape(target.id)); const testId = target.getAttribute('data-testid'); if (testId) addCandidate('Test ID', '[data-testid=\"' + escape(testId) + '\"]'); const name = target.getAttribute('name'); if (name) addCandidate('Name', '[name=\"' + escape(name) + '\"]'); const ariaLabel = target.getAttribute('aria-label'); if (ariaLabel) addCandidate('ARIA label', '[aria-label=\"' + escape(ariaLabel) + '\"]'); const role = target.getAttribute('role'); if (role) addCandidate('Role', '[role=\"' + escape(role) + '\"]'); if (targetClasses.length) addCandidate('CSS', target.tagName.toLowerCase() + targetClasses.map((name) => '.' + escape(name)).join('')); addCandidate('Path', pathSelector); const rect = target.getBoundingClientRect(); return JSON.stringify({ selector: candidates[0]?.selector || pathSelector, selectorCandidates: candidates, metadata: { tag: target.tagName.toLowerCase(), text, role: target.getAttribute('role') || '', ariaLabel: target.getAttribute('aria-label') || '' }, boundingRect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height } }); }\"\"\", {x, y})",
        "",
        "async def inspect_picker_at(page, x, y):",
        r'''    return await page.evaluate("""({x, y}) => { const hovered = document.elementFromPoint(x, y); if (!hovered) return JSON.stringify({ error: "No element at the selected point", selectorCandidates: [] }); const target = hovered.closest("button,a,input,textarea,select,label,[role=button],[role=link],[role=checkbox],[role=tab],[contenteditable=\\"true\\"]") || hovered; const normalize = (value) => String(value || "").replace(/\\s+/g, " ").trim(); const escapeCss = (value) => (window.CSS && CSS.escape ? CSS.escape(String(value)) : String(value).replace(/[^a-zA-Z0-9_-]/g, "\\\\$&")); const quoteAttr = (value) => String(value || "").replace(/\\\\/g, "\\\\\\\\").replace(/\\\"/g, "\\\\\\\""); const candidates = []; const add = (kind, selector, matchSelector = selector) => { if (!selector || candidates.some((candidate) => candidate.selector === selector)) return; let matches = null; try { matches = document.querySelectorAll(matchSelector).length; } catch (_) {} candidates.push({ kind, selector, unique: matches === 1, matches }); }; const tag = target.tagName.toLowerCase(); const attr = (name, value, prefix = tag) => prefix + "[" + name + "=\\\"" + quoteAttr(value) + "\\\"]"; const text = normalize(target.innerText || target.textContent || target.value || target.getAttribute("aria-label") || "").slice(0, 120); if (target.id) add("ID", "#" + escapeCss(target.id)); for (const dataName of ["data-testid", "data-test", "data-qa"]) { const dataValue = target.getAttribute(dataName); if (dataValue) add("Test attribute", attr(dataName, dataValue)); } const name = target.getAttribute("name"); if (name) add("Name", attr("name", name)); const ariaLabel = target.getAttribute("aria-label"); if (ariaLabel) add("ARIA label", attr("aria-label", ariaLabel)); const placeholder = target.getAttribute("placeholder"); if (placeholder) add("Placeholder", attr("placeholder", placeholder)); const title = target.getAttribute("title"); if (title) add("Title", attr("title", title)); const type = target.getAttribute("type"); if (type) add("Type", attr("type", type)); const role = target.getAttribute("role"); if (role) { const accessibleName = normalize(ariaLabel || name || target.getAttribute("placeholder") || text); const roleSelector = accessibleName ? "role=" + role + "[name='" + accessibleName.replace(/'/g, "\\\\'") + "']" : "role=" + role; const roleMatch = "[role=\\\"" + quoteAttr(role) + "\\\"]"; add("Role", roleSelector, roleMatch); add("Role attribute", roleMatch); } const classes = Array.from(target.classList || []).filter(Boolean).slice(0, 3); if (classes.length) add("CSS", tag + classes.map((className) => "." + escapeCss(className)).join("")); if (target.matches("[contenteditable=\\\"true\\\"]")) add("Contenteditable", "[contenteditable=\\\"true\\\"]"); if (text && ["button", "a", "label"].includes(tag) && text.length <= 80 && text.split(/\\s+/).filter(Boolean).length <= 12) add("Text", tag + ":has-text('" + text.replace(/'/g, "\\\\'") + "')"); const parts = []; let node = target; while (node && node.nodeType === 1 && node !== document.body) { let part = node.tagName.toLowerCase(); if (node.id) { parts.unshift("#" + escapeCss(node.id)); break; } const nodeClasses = Array.from(node.classList || []).filter(Boolean).slice(0, 2); if (nodeClasses.length) part += nodeClasses.map((className) => "." + escapeCss(className)).join(""); const siblings = node.parentElement ? Array.from(node.parentElement.children).filter((item) => item.tagName === node.tagName) : []; if (siblings.length > 1) part += ":nth-of-type(" + (siblings.indexOf(node) + 1) + ")"; parts.unshift(part); node = node.parentElement; } add("Path", parts.join(" > ")); const rect = target.getBoundingClientRect(); return JSON.stringify({ selector: candidates[0]?.selector || tag, selectorCandidates: candidates, metadata: { tag, text, role: role || "", ariaLabel: ariaLabel || "", name: name || "", placeholder: target.getAttribute("placeholder") || "", type: target.getAttribute("type") || "" }, boundingRect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height } }); }""", {x, y})''',
        "",
        "",
        "def locator_for(page, selector):",
        "    value = str(selector or '').strip()",
        "    if value.startswith('role='):",
        "        role_query = value[5:].strip(); name = None",
        "        if '[name=' in role_query:",
        "            role_query, name_part = role_query.split('[name=', 1); name = name_part.rstrip(']').strip(\"'\\\"\")",
        "        get_by_role = getattr(page, 'get_by_role', None)",
        "        if get_by_role:",
        "            return get_by_role(role_query.strip(), name=name) if name else get_by_role(role_query.strip())",
        "        role_value = role_query.strip()",
        "        if name: return page.locator('[role=\\\"' + role_value + '\\\"]')",
        "        return page.locator('[role=\\\"' + role_value + '\\\"]')",
        "    return page.locator(value)",
        "",
        "async def picker_point(page, x, y, source_width, source_height):",
        "    if not source_width or not source_height: return x, y",
        "    viewport = await page.evaluate(\"() => ({ width: window.innerWidth, height: window.innerHeight })\")",
        "    return x * float(viewport.get('width') or source_width) / float(source_width), y * float(viewport.get('height') or source_height) / float(source_height)",
        "",
        "async def highlight_picker_target(page, x, y, source_width, source_height):",
        "    await page.evaluate(\"\"\"({x, y, sourceWidth, sourceHeight}) => { const id = '__sui_picker_highlight__'; const current = document.getElementById(id); const point = sourceWidth && sourceHeight ? { x: x * window.innerWidth / sourceWidth, y: y * window.innerHeight / sourceHeight } : { x, y }; const hovered = document.elementFromPoint(point.x, point.y); const target = hovered && !hovered.closest('#' + id) ? (hovered.closest('button,a,input,textarea,select,[role=button],[role=link]') || hovered) : null; if (!target) { if (current) current.style.display = 'none'; return; } let overlay = current; if (!overlay) { overlay = document.createElement('div'); overlay.id = id; overlay.setAttribute('aria-hidden', 'true'); Object.assign(overlay.style, { position: 'fixed', pointerEvents: 'none', zIndex: '2147483647', border: '3px solid #FF5B35', background: 'rgba(255, 91, 53, 0.14)', boxShadow: '0 0 0 1px rgba(255,255,255,0.95), 0 0 18px rgba(255,91,53,0.85)', borderRadius: '3px', transition: 'none' }); document.documentElement.appendChild(overlay); } const rect = target.getBoundingClientRect(); Object.assign(overlay.style, { display: 'block', left: rect.left + 'px', top: rect.top + 'px', width: rect.width + 'px', height: rect.height + 'px' }); }\"\"\", {x, y, sourceWidth, sourceHeight})",
        "",
        "async def clear_picker_target(page):",
        "    await page.evaluate(\"\"\"() => { const overlay = document.getElementById('__sui_picker_highlight__'); if (overlay) overlay.remove(); }\"\"\")",
        "",
        "async def execute_command(page, command, capture_lock):",
        "    command_type = command.get('type')",
        "    try:",
        "        action_result = None",
        "        if command_type == 'run_step':",
        "            step = command.get('step') or {}; await execute_step(page, step, capture_lock); action_result = {'status': 'completed', 'action': step.get('type'), 'command_id': command.get('command_id')}"
        ,
        "        elif command_type == 'delay':",
        "            if command.get('until_load_completes'): await page.wait_for_load_state('load')",
        "            else: await asyncio.sleep(float(command.get('seconds') or 0))",
        "            action_result = {'status': 'completed', 'action': 'delay', 'command_id': command.get('command_id')}"
        ,
        "        elif command_type == 'assign': variables[str(command.get('variable') or '')] = str(command.get('value') or ''); action_result = {'status': 'completed', 'action': 'assign', 'command_id': command.get('command_id')}"
        ,
        "        elif command_type == 'navigate': await human_pause(0.35); await page.goto(str(command.get('url') or ''), wait_until='domcontentloaded'); await human_pause(0.65); action_result = {'status': 'completed', 'action': 'navigate', 'command_id': command.get('command_id')}"
        ,
        "        elif command_type == 'text': await page.keyboard.insert_text(str(command.get('text', '')))"
        ,
        "        elif command_type == 'key': await page.keyboard.press(str(command.get('key', '')))"
        ,
        "        elif command_type == 'click': await page.mouse.click(float(command.get('x', 0)), float(command.get('y', 0)))"
        ,
        "        elif command_type == 'selector_fill': await (await wait_for_locator(page, str(command.get('selector') or ''), 'Fill')).fill(str(command.get('value') or '')); action_result = {'status': 'completed', 'action': 'fill', 'trial_id': command.get('trial_id'), 'command_id': command.get('command_id')}"
        ,
        "        elif command_type == 'selector_scrape': action_result = {'status': 'completed', 'action': 'scrape', 'scraped_value': await (await wait_for_locator(page, str(command.get('selector') or ''), 'Scrape')).inner_text(), 'trial_id': command.get('trial_id'), 'command_id': command.get('command_id')}"
        ,
        "        elif command_type == 'selector_check': action_result = {'status': 'completed', 'action': 'check_element_exists', 'exists': await locator_for(page, str(command.get('selector') or '')).count() > 0, 'trial_id': command.get('trial_id'), 'command_id': command.get('command_id')}"
        ,
        "        elif command_type == 'selector_click': await (await wait_for_locator(page, str(command.get('selector') or ''), 'Click')).click(); action_result = {'status': 'completed', 'action': 'click', 'trial_id': command.get('trial_id'), 'command_id': command.get('command_id')}"
        ,
        "        elif command_type == 'message_box': await show_message_box(page, str(command.get('message') or '')); action_result = {'status': 'completed', 'action': 'message_box', 'command_id': command.get('command_id')}"
        ,
        "        elif command_type == 'inspect_move': await highlight_picker_target(page, float(command.get('x', 0)), float(command.get('y', 0)), float(command.get('source_width', 0)), float(command.get('source_height', 0)))"
        ,
        "        elif command_type == 'clear_picker_overlay': await clear_picker_target(page)"
        ,
        "        elif command_type == 'inspect': inspect_x, inspect_y = await picker_point(page, float(command.get('x', 0)), float(command.get('y', 0)), float(command.get('source_width', 0)), float(command.get('source_height', 0))); print('SUI_INSPECT:' + inspect_wire_text(await inspect_picker_at(page, inspect_x, inspect_y)), flush=True)"
        ,
        "        else: return"
        ,
        "        await emit_frame(page, capture_lock)"
        ,
        "        if action_result is not None: print('SUI_ACTION_COMPLETE:' + json.dumps(action_result), flush=True)"
        ,
        "        elif command_type not in {'run_step', 'inspect_move', 'clear_picker_overlay', 'inspect'}: print('SUI_ACTION_COMPLETE:' + json.dumps({'status': 'completed', 'action': command_type, 'trial_id': command.get('trial_id'), 'command_id': command.get('command_id')}), flush=True)"
        ,
        "    except asyncio.CancelledError:",
        "        print('SUI_ACTION_COMPLETE:' + json.dumps({'status': 'cancelled', 'error': 'Superseded by a newer command', 'trial_id': command.get('trial_id'), 'command_id': command.get('command_id')}), flush=True)",
        "        raise",
        "    except Exception as exc:",
        "        if command_type in {'run_step', 'delay', 'assign', 'navigate', 'selector_click', 'selector_fill', 'selector_scrape', 'selector_check', 'message_box', 'inspect', 'inspect_move', 'clear_picker_overlay'}: print('SUI_ACTION_COMPLETE:' + json.dumps({'status': 'failed', 'error': str(exc), 'trial_id': command.get('trial_id'), 'command_id': command.get('command_id')}), flush=True)",
        "",
        "",
        "async def command_loop(page, capture_lock):",
        "    current_task = None",
        "    executable_types = {'run_step', 'delay', 'assign', 'navigate', 'selector_click', 'selector_fill', 'selector_scrape', 'selector_check', 'message_box'}",
        "    while True:",
        "        line = await asyncio.to_thread(sys.stdin.readline)",
        "        if not line:",
        "            if current_task and not current_task.done(): current_task.cancel(); await asyncio.gather(current_task, return_exceptions=True)",
        "            return",
        "        try: command = json.loads(line)",
        "        except json.JSONDecodeError: continue",
        "        if command.get('type') == 'cancel_command':",
        "            if current_task and not current_task.done(): current_task.cancel(); await asyncio.gather(current_task, return_exceptions=True)",
        "            current_task = None",
        "            continue",
        "        if current_task and current_task.done():",
        "            await asyncio.gather(current_task, return_exceptions=True)",
        "            current_task = None",
        "        if current_task and command.get('type') in executable_types:",
        "            current_task.cancel()",
        "            await asyncio.gather(current_task, return_exceptions=True)",
        "        current_task = asyncio.create_task(execute_command(page, command, capture_lock))",
        "",
        "",
    ]
    _generated_window_support(lines)
    lines.extend(
        [
            "",
            "",
            "async def run():",
            "    browser = None",
            "    async with async_playwright() as playwright:",
            f"        foreground_window = preserve_foreground_window() if {json.dumps(mode)} == 'headful' else None",
            "        before_handles = visible_window_handles()",
            f"        browser = await playwright.chromium.launch(headless={json.dumps(mode)} == 'headless')",
            "        page = await browser.new_page(no_viewport=True)",
            f"        maximize_browser_window(before_handles) if {json.dumps(mode)} == 'headful' else None",
            "        restore_foreground_window(foreground_window)",
            "        stop_event = asyncio.Event()",
            "        capture_lock = asyncio.Lock()",
            "        frame_task = None",
            "        command_task = None",
            "        try:",
            "            await emit_frame(page, capture_lock)",
            "            frame_task = asyncio.create_task(stream_frames(page, stop_event, capture_lock))",
            "            print('SUI_BROWSER_READY', flush=True)",
            "            try:",
            "                await execute_steps(page, [selected_step], capture_lock)",
            "                print('SUI_ACTION_COMPLETE:' + json.dumps({'status': 'completed', 'action': selected_step.get('type'), 'command_id': selected_step.get('command_id')}), flush=True)",
            "                print('SUI_SESSION_READY', flush=True)",
            "            except Exception as exc:",
            "                print('SUI_ACTION_COMPLETE:' + json.dumps({'status': 'failed', 'error': str(exc), 'action': selected_step.get('type'), 'command_id': selected_step.get('command_id')}), flush=True)",
            "                print('SUI_LOG:Step failed: ' + str(exc), flush=True)",
            "                print('SUI_SESSION_READY', flush=True)",
            "            command_task = asyncio.create_task(command_loop(page, capture_lock))",
            "            done_tasks, pending_tasks = await asyncio.wait({frame_task, command_task}, return_when=asyncio.FIRST_COMPLETED)",
            "            for task in pending_tasks: task.cancel()",
            "            await asyncio.gather(*pending_tasks, return_exceptions=True)",
            "        finally:",
            "            stop_event.set()",
            "            if frame_task: await frame_task",
            "            if command_task: command_task.cancel()",
            "            try:",
            "                if browser: await browser.close()",
            "            except Exception: pass",
            "",
            "",
            "async def stream_frames(page, stop_event, capture_lock):",
            "    while not stop_event.is_set():",
            "        try:",
            "            if page.is_closed(): print('SUI_BROWSER_CLOSED:page is closed', flush=True); return",
            "            await emit_frame(page, capture_lock)",
            "        except Exception as exc: print('SUI_FRAME_ERROR:' + str(exc), flush=True); await asyncio.sleep(0.25)",
            "        await asyncio.sleep(0.25)",
            "",
            "",
            "if __name__ == '__main__':",
            "    asyncio.run(run())",
            "",
        ]
    )
    source = "\n".join(lines)
    # Python treats {x, y} as a set. Rustwright expects evaluate arguments
    # to be JSON objects, matching the picker implementation from 117e289.
    source = source.replace(", {x, y})", ', {"x": x, "y": y})')
    return source.replace(", {x, y, sourceWidth, sourceHeight})", ', {"x": x, "y": y, "sourceWidth": source_width, "sourceHeight": source_height})')


def generate_step_python(definition: dict[str, Any]) -> str:
    return _generate_recursive_step_python(definition)
    step = definition["steps"][0]
    chrome_mode = definition.get("mode") or "headful"
    if step["type"] == "navigate_to_website":
        action_lines = [
            "            await human_pause(0.35)",
            f"            await page.goto({json.dumps(step['settings']['url'])}, wait_until=\"domcontentloaded\")",
            "            await human_pause(0.65)",
        ]
        log_line = f"            print(\"SUI_LOG:Navigated: {step['id']}\", flush=True)"
    elif step["type"] == "fill":
        action_lines = [
            "            await human_pause(0.30)",
            f"            await page.locator({json.dumps(step['settings']['selector'])}).first.fill({json.dumps(step['settings'].get('value', ''))})",
            "            await human_pause(0.35)",
        ]
        log_line = f"            print(\"SUI_LOG:Filled: {step['id']}\", flush=True)"
    elif step["type"] == "delay":
        action_lines = [
            f"            await asyncio.sleep({json.dumps(step['settings'].get('seconds', 1))})",
        ]
        log_line = f"            print(\"SUI_LOG:Delayed: {step['id']}\", flush=True)"
    elif step["type"] == "assign":
        action_lines = [
            f"            variables[{json.dumps(step['settings']['variable'])}] = {json.dumps(step['settings'].get('value', ''))}",
            "            await human_pause(0.25)",
        ]
        log_line = f"            print(\"SUI_LOG:Assigned: {step['id']}\", flush=True)"
    elif step["type"] == "scrape":
        action_lines = [
            f"            variables[{json.dumps(step['settings']['variable'])}] = await page.locator({json.dumps(step['settings']['selector'])}).first.inner_text()",
            "            await human_pause(0.30)",
        ]
        log_line = f"            print(\"SUI_LOG:Scraped: {step['id']}\", flush=True)"
    elif step["type"] == "message_box":
        action_lines = [
            f"            await show_message_box(page, {json.dumps(step['settings'].get('message', ''))})",
            "            await emit_frame(page, capture_lock)",
        ]
        log_line = f"            print(\"SUI_LOG:Message box shown: {step['id']}\", flush=True)"
    else:
        action_lines = [
            "            await human_pause(0.30)",
            f"            await page.locator({json.dumps(step['settings']['selector'])}).first.click()",
            "            await human_pause(0.35)",
        ]
        log_line = f"            print(\"SUI_LOG:Clicked: {step['id']}\", flush=True)"
    return "\n".join(
        [
            "import asyncio",
            "import base64",
            "import ctypes",
            "import json",
            "import os",
            "import sys",
            "import time",
            "from rustwright.async_api import async_playwright",
            "",
            "",
            "async def human_pause(seconds):",
            "    await asyncio.sleep(seconds)",
            "",
            "",
            f"variables = {json.dumps({variable['name']: variable.get('value', '') for variable in definition.get('variables', [])})}",
            "",
            "",
            "async def show_message_box(page, message):",
            "    await page.evaluate(\"\"\"(text) => {",
            "        const old = document.getElementById('sui-message-box');",
            "        if (old) old.remove();",
            "        const overlay = document.createElement('div');",
            "        overlay.id = 'sui-message-box';",
            "        Object.assign(overlay.style, { position: 'fixed', inset: '0', zIndex: '2147483647', display: 'grid', placeItems: 'center', background: 'rgba(20, 25, 22, .35)', fontFamily: 'system-ui, sans-serif' });",
            "        const box = document.createElement('div');",
            "        Object.assign(box.style, { width: 'min(420px, calc(100vw - 40px))', padding: '24px', borderRadius: '12px', background: '#fff', color: '#17201d', boxShadow: '0 18px 50px rgba(0,0,0,.28)' });",
            "        const copy = document.createElement('div');",
            "        copy.textContent = text;",
            "        copy.style.cssText = 'white-space: pre-wrap; line-height: 1.5; margin-bottom: 20px;';",
            "        const ok = document.createElement('button');",
            "        ok.id = 'sui-message-box-ok';",
            "        ok.textContent = 'OK';",
            "        ok.style.cssText = 'border: 0; border-radius: 7px; padding: 9px 18px; background: #FF5B35; color: #fff; font-weight: 700; cursor: pointer;';",
            "        ok.addEventListener('click', () => overlay.remove());",
            "        box.append(copy, ok);",
            "        overlay.append(box);",
            "        document.body.append(overlay);",
            "    }\"\"\", message)",
            "",
            "",
            "async def emit_frame(page, capture_lock):",
            "    async with capture_lock:",
            "        frame = await page.screenshot(type=\"jpeg\", quality=70)",
            "        print(\"SUI_FRAME:\" + base64.b64encode(frame).decode(\"ascii\"), flush=True)",
            "",
            "",
            "def preserve_foreground_window():",
            "    if os.name != \"nt\": return None",
            "    return ctypes.windll.user32.GetForegroundWindow()",
            "",
            "",
            "def restore_foreground_window(hwnd):",
            "    if hwnd and os.name == \"nt\": ctypes.windll.user32.SetForegroundWindow(hwnd)",
            "",
            "",
            "def visible_window_handles():",
            "    if os.name != \"nt\": return set()",
            "    user32 = ctypes.windll.user32",
            "    handles = set()",
            "    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)",
            "    def visit(hwnd, _):",
            "        if user32.IsWindowVisible(hwnd): handles.add(int(hwnd))",
            "        return True",
            "    callback_ref = callback_type(visit)",
            "    user32.EnumWindows(callback_ref, 0)",
            "    return handles",
            "",
            "",
            "def is_chrome_window(hwnd):",
            "    user32 = ctypes.windll.user32",
            "    title = ctypes.create_unicode_buffer(512)",
            "    class_name = ctypes.create_unicode_buffer(256)",
            "    user32.GetWindowTextW(hwnd, title, 512)",
            "    user32.GetClassNameW(hwnd, class_name, 256)",
            "    return \"Chrome_WidgetWin\" in class_name.value or \"Google Chrome\" in title.value",
            "",
            "",
            "def maximize_browser_window(before_handles):",
            "    if os.name != \"nt\": return None",
            "    user32 = ctypes.windll.user32",
            "    candidates = []",
            "    for _ in range(20):",
            "        candidates = [hwnd for hwnd in visible_window_handles() - before_handles if is_chrome_window(hwnd)]",
            "        if candidates: break",
            "        time.sleep(0.05)",
            "    foreground = user32.GetForegroundWindow()",
            "    if not candidates and foreground and is_chrome_window(foreground): candidates = [foreground]",
            "    if not candidates: return None",
            "    hwnd = candidates[-1]",
            "    user32.ShowWindow(hwnd, 3)",
            "    return hwnd",
            "",
            "",
            "async def stream_frames(page, stop_event, capture_lock):",
            "    while not stop_event.is_set():",
            "        try:",
            "            if page.is_closed():",
            "                print(\"SUI_BROWSER_CLOSED:page is closed\", flush=True)",
            "                return",
            "            await emit_frame(page, capture_lock)",
            "        except Exception as exc:",
            "            print(\"SUI_FRAME_ERROR:\" + str(exc), flush=True)",
            "            await asyncio.sleep(0.25)",
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
            "def json_safe(value):",
            "    if isinstance(value, dict): return {str(key): json_safe(item) for key, item in value.items()}",
            "    if isinstance(value, (list, tuple, set)): return [json_safe(item) for item in value]",
            "    return value",
            "",
            "",
            "async def command_loop(page, capture_lock):",
            "    variables = {}",
            "    while True:",
            "        line = await asyncio.to_thread(sys.stdin.readline)",
            "        if not line:",
            "            return",
            "        try:",
            "            command = json.loads(line)",
            "        except json.JSONDecodeError:",
            "            continue",
        "        try:",
            "            if command.get(\"type\") == \"delay\":",
            "                if command.get(\"until_load_completes\"): await page.wait_for_load_state(\"load\")",
            "                else: await asyncio.sleep(float(command.get(\"seconds\") or 0))",
            "                print(\"SUI_ACTION_COMPLETE:\" + json.dumps({\"status\": \"completed\", \"action\": \"delay\"}), flush=True)",
            "            elif command.get(\"type\") == \"assign\":",
            "                variables[str(command.get(\"variable\") or \"\")] = str(command.get(\"value\") or \"\")",
            "                await human_pause(0.25)",
            "                print(\"SUI_ACTION_COMPLETE:\" + json.dumps({\"status\": \"completed\", \"action\": \"assign\"}), flush=True)",
            "            elif command.get(\"type\") == \"navigate\":",
            "                await human_pause(0.35)",
            "                await page.goto(str(command.get(\"url\") or \"\"), wait_until=\"domcontentloaded\")",
            "                await human_pause(0.65)",
            "                print(\"SUI_ACTION_COMPLETE:\" + json.dumps({\"status\": \"completed\", \"action\": \"navigate\"}), flush=True)",
            "            elif command.get(\"type\") == \"text\":",
            "                await page.keyboard.insert_text(str(command.get(\"text\", \"\")))",
            "            elif command.get(\"type\") == \"key\":",
            "                await page.keyboard.press(str(command.get(\"key\", \"\")))",
            "            elif command.get(\"type\") == \"click\":",
            "                await human_pause(0.20)",
            "                await page.mouse.click(float(command.get(\"x\", 0)), float(command.get(\"y\", 0)))",
            "                await human_pause(0.25)",
            "            elif command.get(\"type\") == \"selector_fill\":",
            "                await human_pause(0.30)",
            "                await page.locator(str(command.get(\"selector\") or \"\")).first.fill(str(command.get(\"value\") or \"\"))",
            "                await human_pause(0.35)",
            "                print(\"SUI_ACTION_COMPLETE:\" + json.dumps({\"status\": \"completed\", \"action\": \"fill\", \"trial_id\": command.get(\"trial_id\")}), flush=True)",
            "            elif command.get(\"type\") == \"selector_scrape\":",
            "                scraped_value = await page.locator(str(command.get(\"selector\") or \"\")).first.inner_text()",
            "                await human_pause(0.30)",
            "                print(\"SUI_ACTION_COMPLETE:\" + json.dumps({\"status\": \"completed\", \"action\": \"scrape\", \"scraped_value\": scraped_value, \"trial_id\": command.get(\"trial_id\")}), flush=True)",
            "            elif command.get(\"type\") == \"message_box\":",
            "                await show_message_box(page, str(command.get(\"message\") or \"\"))",
            "                print(\"SUI_ACTION_COMPLETE:\" + json.dumps({\"status\": \"completed\", \"action\": \"message_box\"}), flush=True)",
            "            elif command.get(\"type\") == \"selector_click\":",
            "                await human_pause(0.30)",
            "                await page.locator(str(command.get(\"selector\") or \"\")).first.click()",
            "                await human_pause(0.35)",
            "                print(\"SUI_ACTION_COMPLETE:\" + json.dumps({\"status\": \"completed\", \"action\": \"click\", \"trial_id\": command.get(\"trial_id\")}), flush=True)",
            "            elif command.get(\"type\") == \"inspect\":",
            "                try:",
            "                    result = await inspect_at(page, float(command.get(\"x\", 0)), float(command.get(\"y\", 0)))",
            "                    print(\"SUI_INSPECT:\" + json.dumps(json_safe(result)), flush=True)",
            "                except Exception as exc:",
            "                    print(\"SUI_INSPECT:\" + json.dumps({\"error\": str(exc)}), flush=True)",
            "            else:",
            "                continue",
            "            await emit_frame(page, capture_lock)",
            "        except Exception as exc:",
            "            if command.get(\"type\") in {\"delay\", \"assign\", \"navigate\", \"selector_click\", \"selector_fill\", \"selector_scrape\", \"message_box\"}:",
            "                print(\"SUI_ACTION_COMPLETE:\" + json.dumps({\"status\": \"failed\", \"error\": str(exc), \"trial_id\": command.get(\"trial_id\")}), flush=True)",
            "",
            "",
            "async def launch_browser(playwright):",
            f"    browser = await playwright.chromium.launch(headless={json.dumps(chrome_mode)} == \"headless\")",
            "    page = await browser.new_page(no_viewport=True)",
            "    return browser, page",
            "",
            "",
            "async def run():",
            "    browser = None",
            "    async with async_playwright() as playwright:",
            f"        foreground_window = preserve_foreground_window() if {json.dumps(chrome_mode)} == \"headful\" else None",
            "        before_handles = visible_window_handles()",
            "        browser, page = await launch_browser(playwright)",
            f"        maximize_browser_window(before_handles) if {json.dumps(chrome_mode)} == \"headful\" else None",
            "        stop_event = asyncio.Event()",
            "        capture_lock = asyncio.Lock()",
        "        frame_task = None",
        "        command_task = None",
            "        try:",
            "            restore_foreground_window(foreground_window)",
            "            await emit_frame(page, capture_lock)",
            "            frame_task = asyncio.create_task(stream_frames(page, stop_event, capture_lock))",
            "            print(\"SUI_BROWSER_READY\", flush=True)",
            "            try:",
            *[line.replace("            ", "                ", 1) for line in action_lines],
            log_line.replace("            ", "                ", 1),
            "                print(\"SUI_READY\", flush=True)",
            "            except Exception as exc:",
            f'                print("SUI_ACTION_COMPLETE:" + json.dumps({{"status": "failed", "action": {json.dumps("navigate" if step["type"] == "navigate_to_website" else step["type"])}, "error": str(exc)}}), flush=True)',
            "                print(\"SUI_LOG:Step failed: \" + str(exc), flush=True)",
            "            command_task = asyncio.create_task(command_loop(page, capture_lock))",
            "            await command_task",
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
            "            try:",
            "                if browser: await browser.close()",
            "            except Exception: pass",
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


def execute_generated(path: Path, env: dict[str, str] | None = None) -> tuple[int, list[str], list[str]]:
    try:
        execution_env = os.environ.copy()
        if env:
            execution_env.update(env)
        execution_env["PYTHONIOENCODING"] = "utf-8"
        completed = subprocess.run(
            [sys.executable, "-X", "utf8", str(path)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=600,
            check=False,
            env=execution_env,
        )
        return (
            completed.returncode,
            [line for line in completed.stdout.splitlines() if line.strip()],
            [line for line in completed.stderr.splitlines() if line.strip()],
        )
    except subprocess.TimeoutExpired:
        return 124, [], ["Execution timed out after 600 seconds"]
    except OSError as exc:
        return 126, [], [str(exc)]


def _json_safe_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def emit_step_event(run_id: str, event: dict[str, Any]) -> None:
    with STEP_RUNS_CHANGED:
        run = STEP_RUNS.get(run_id)
        if not run:
            return
        run["events"].append(_json_safe_value(event))
        STEP_RUNS_CHANGED.notify_all()


def emit_job_event(job_id: str, event: dict[str, Any]) -> None:
    with JOB_RUNS_CHANGED:
        run = JOB_RUNS.get(job_id)
        if not run:
            return
        run["events"].append(_json_safe_value(event))
        JOB_RUNS_CHANGED.notify_all()


def add_streamed_job_log(job_id: str, level: str, message: str) -> None:
    if not message:
        return
    add_job_log(job_id, level, message)
    emit_job_event(job_id, {"type": "log", "level": level, "message": message})


def handle_job_output(job_id: str, line: str) -> None:
    if line.startswith("SUI_FRAME:"):
        with JOB_RUNS_CHANGED:
            run = JOB_RUNS.get(job_id)
            if run:
                run["last_frame_at"] = time.monotonic()
                run["browser_healthy"] = True
        emit_job_event(job_id, {"type": "frame", "data": f"data:image/jpeg;base64,{line.removeprefix('SUI_FRAME:')}"})
    elif line.startswith("SUI_LOG:"):
        add_streamed_job_log(job_id, "info", line.removeprefix("SUI_LOG:"))
    elif line.startswith("SUI_BROWSER_CLOSED:"):
        with JOB_RUNS_CHANGED:
            run = JOB_RUNS.get(job_id)
            if run:
                run["browser_healthy"] = False
        add_streamed_job_log(job_id, "error", line.removeprefix("SUI_BROWSER_CLOSED:"))
    elif line.startswith("SUI_FRAME_ERROR:"):
        add_streamed_job_log(job_id, "warning", f"Frame delayed: {line.removeprefix('SUI_FRAME_ERROR:')}")
    elif line not in {"SUI_BROWSER_READY", "SUI_READY"}:
        add_streamed_job_log(job_id, "info", line)


def stream_job_process(job_id: str, generated_file: Path, definition: dict[str, Any]) -> tuple[int, str | None]:
    execution_env = os.environ.copy()
    execution_env["PYTHONIOENCODING"] = "utf-8"
    process = subprocess.Popen(
        [sys.executable, "-X", "utf8", "-u", str(generated_file)],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        bufsize=1,
        env=execution_env,
    )
    with JOB_RUNS_CHANGED:
        if job_id in JOB_RUNS:
            JOB_RUNS[job_id]["process"] = process

    stderr_lines: list[str] = []

    def read_stderr() -> None:
        if not process.stderr:
            return
        for raw_line in process.stderr:
            line = raw_line.strip()
            if line:
                stderr_lines.append(line)
                add_streamed_job_log(job_id, "error", line)

    stderr_thread = threading.Thread(target=read_stderr, daemon=True)
    stderr_thread.start()
    if process.stdout:
        for raw_line in process.stdout:
            line = raw_line.strip()
            if line:
                handle_job_output(job_id, line)
    stderr_thread.join()
    return_code = process.wait()
    return return_code, stderr_lines[-1] if stderr_lines else None


def active_browser_run(scope: tuple[str, str, str, str]) -> tuple[str, dict[str, Any]] | None:
    with STEP_RUNS_CHANGED:
        for run_id, run in reversed(list(STEP_RUNS.items())):
            process = run.get("process")
            if run.get("scope") != scope or run.get("done"):
                continue
            if run.get("starting"):
                return run_id, run
            if not process or process.poll() is not None or not process.stdin:
                continue
            if process and process.poll() is None and process.stdin:
                return run_id, run
    return None


def send_browser_command(run_id: str, command: dict[str, Any], reserve_step: bool = False) -> str:
    command = dict(command)
    command_id = str(command.get("command_id") or make_id("command"))
    command["command_id"] = command_id
    with STEP_RUNS_CHANGED:
        run = STEP_RUNS.get(run_id)
        process = run.get("process") if run else None
        if not process or process.poll() is not None or not process.stdin:
            raise RuntimeError("Browser session is no longer active")
        previous_command_id = run.get("busy_command_id") if reserve_step and run.get("command_busy") else None
        if reserve_step:
            run["command_busy"] = True
            run["busy_command_id"] = command_id
        try:
            if previous_command_id:
                process.stdin.write(json.dumps({"type": "cancel_command", "command_id": previous_command_id}) + "\n")
            process.stdin.write(json.dumps(command) + "\n")
            process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            if reserve_step and run.get("busy_command_id") == command_id:
                run["command_busy"] = False
                run["busy_command_id"] = None
            raise RuntimeError("Browser session is no longer active") from exc
    return command_id


def clear_command_busy_if_matching(run_id: str, command_id: str | None) -> bool:
    if not command_id:
        return False
    with STEP_RUNS_CHANGED:
        run = STEP_RUNS.get(run_id)
        if not run or run.get("busy_command_id") != command_id:
            return False
        run["command_busy"] = False
        run["busy_command_id"] = None
        STEP_RUNS_CHANGED.notify_all()
        return True


def run_step_worker(run_id: str, definition: dict[str, Any], script_path: Path) -> None:
    emit_step_event(run_id, {"type": "status", "status": "running"})
    session_ready = False
    browser_closed_error = None
    try:
        execution_env = os.environ.copy()
        execution_env["PYTHONIOENCODING"] = "utf-8"
        process = subprocess.Popen(
            [sys.executable, "-X", "utf8", "-u", str(script_path)],
            cwd=ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
            env=execution_env,
        )
        with STEP_RUNS_CHANGED:
            if run_id in STEP_RUNS:
                STEP_RUNS[run_id]["process"] = process
                STEP_RUNS[run_id]["starting"] = False
                STEP_RUNS_CHANGED.notify_all()
        assert process.stdout is not None
        for raw_line in process.stdout:
            line = raw_line.strip()
            if line.startswith("SUI_FRAME:"):
                with STEP_RUNS_CHANGED:
                    if run_id in STEP_RUNS:
                        STEP_RUNS[run_id]["last_frame_at"] = time.monotonic()
                emit_step_event(
                    run_id,
                    {"type": "frame", "data": f"data:image/jpeg;base64,{line.removeprefix('SUI_FRAME:')}"},
                )
            elif line.startswith("SUI_FRAME_ERROR:"):
                emit_step_event(run_id, {"type": "log", "message": f"Frame delayed: {line.removeprefix('SUI_FRAME_ERROR:')}", "level": "warning"})
            elif line.startswith("SUI_LOG:"):
                emit_step_event(run_id, {"type": "log", "message": line.removeprefix("SUI_LOG:")})
            elif line.startswith("SUI_BROWSER_CLOSED:"):
                browser_closed_error = "Browser target closed; run the first Navigate to website step to launch a fresh browser"
                with STEP_RUNS_CHANGED:
                    if run_id in STEP_RUNS:
                        STEP_RUNS[run_id]["command_busy"] = False
                        STEP_RUNS[run_id]["busy_command_id"] = None
                emit_step_event(run_id, {"type": "session", "status": "dead", "error": browser_closed_error})
                emit_step_event(run_id, {"type": "complete", "status": "failed", "error": browser_closed_error})
                if process.poll() is None:
                    process.terminate()
            elif line.startswith("SUI_INSPECT:"):
                try:
                    element = json.loads(line.removeprefix("SUI_INSPECT:"))
                    emit_step_event(run_id, {"type": "inspect", "element": element})
                except json.JSONDecodeError:
                    emit_step_event(run_id, {"type": "log", "message": "Browser returned invalid picker data", "level": "error"})
            elif line.startswith("SUI_ACTION_COMPLETE:"):
                try:
                    action = json.loads(line.removeprefix("SUI_ACTION_COMPLETE:"))
                    clear_command_busy_if_matching(run_id, action.get("command_id"))
                    emit_step_event(run_id, {"type": "action_complete", **action})
                except json.JSONDecodeError:
                    emit_step_event(run_id, {"type": "action_complete", "status": "failed", "error": "Browser returned invalid action data"})
            elif line in {"SUI_SESSION_READY", "SUI_READY"}:
                session_ready = True
                emit_step_event(run_id, {"type": "session", "status": "ready"})
        stderr = process.stderr.read() if process.stderr else ""
        return_code = process.wait()
        stderr_lines = [line.strip() for line in stderr.splitlines() if line.strip()]
        for line in stderr_lines:
            emit_step_event(run_id, {"type": "log", "message": line, "level": "error"})
        status = "completed" if return_code == 0 else "failed"
        error = None
        if return_code != 0:
            error = browser_closed_error or stderr_lines[-1] if stderr_lines else browser_closed_error or f"Browser step exited with code {return_code}"
        if return_code != 0:
            emit_step_event(run_id, {"type": "complete", "status": status, "error": error})
        elif session_ready:
            emit_step_event(run_id, {"type": "session", "status": "dead", "error": "Browser session ended"})
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
                STEP_RUNS[run_id]["starting"] = False
                STEP_RUNS[run_id]["command_busy"] = False
                STEP_RUNS[run_id]["busy_command_id"] = None
            STEP_RUNS_CHANGED.notify_all()


def start_step_run(payload: dict[str, Any]) -> tuple[str, int, str]:
    step = payload.get("step") or {}
    project_id = str(payload.get("project_id") or "draft-project")
    recipe_id = str(payload.get("recipe_id") or "draft-recipe")
    studio_session_id = str(payload.get("studio_session_id") or "default-studio")
    mode = "headless" if payload.get("mode") == "headless" else "headful"
    scope = (project_id, recipe_id, studio_session_id, mode)
    definition = normalize_definition(
        {"steps": [step], "variables": payload.get("variables") or [], "mode": mode},
        project_id, recipe_id, "step-run", 1, require_first_navigation=False,
    )
    normalized_step = definition["steps"][0]
    if normalized_step["type"] == "navigate_to_website":
        command = {"type": "navigate", "url": normalized_step["settings"]["url"]}
    elif normalized_step["type"] in {"navigate", "click", "fill", "check_element_exists", "check_text_exists", "if", "for", "use_js", "keypress", "message_box", "scrape"}:
        command = {"type": "run_step", "step": normalized_step}
    elif normalized_step["type"] == "delay":
        command = {
            "type": "delay",
            "seconds": normalized_step["settings"].get("seconds", 1),
            "until_load_completes": normalized_step["settings"].get("until_load_completes", False),
        }
    elif normalized_step["type"] == "assign":
        command = {"type": "assign", "variable": normalized_step["settings"]["variable"], "value": normalized_step["settings"].get("value", "")}
    else:
        command = {"type": "selector_click", "selector": normalized_step["settings"]["selector"]}
    command_id = make_id("command")
    command["command_id"] = command_id
    normalized_step["command_id"] = command_id
    definition["command_id"] = command_id

    # Serialize the active-session lookup and launch reservation. This prevents
    # two UI clicks arriving together from creating two browser processes.
    with STEP_SESSION_START_LOCK:
        active = active_browser_run(scope)
        if active:
            active_run_id, active_run = active
            if not active_run.get("process"):
                deadline = time.monotonic() + 5
                with STEP_RUNS_CHANGED:
                    while active_run.get("starting") and not active_run.get("done") and not active_run.get("process"):
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        STEP_RUNS_CHANGED.wait(timeout=min(0.1, remaining))
                active = active_browser_run(scope)
            if active:
                active_run_id, _ = active
                with STEP_RUNS_CHANGED:
                    event_index = len(STEP_RUNS[active_run_id]["events"])
                send_browser_command(active_run_id, command, reserve_step=True)
                return active_run_id, event_index, command_id

        run_id = make_id("step_run")
        script_path = Path(tempfile.gettempdir()) / f"sui-{run_id}.py"
        script_path.write_text(generate_step_python(definition), encoding="utf-8")
        with STEP_RUNS_CHANGED:
            STEP_RUNS[run_id] = {
                "events": [],
                "done": False,
                "starting": True,
                "command_busy": True,
                "busy_command_id": command_id,
                "last_frame_at": 0.0,
                "scope": scope,
                "project_id": project_id,
                "recipe_id": recipe_id,
                "studio_session_id": studio_session_id,
                "mode": mode,
            }
        threading.Thread(target=run_step_worker, args=(run_id, definition, script_path), daemon=True).start()
        return run_id, 0, command_id


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
    emit_job_event(job_id, {"type": "status", "status": "running"})
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
    add_job_log(job_id, "info", f"Launching {'headless' if definition.get('mode') == 'headless' else 'headful'} Chrome for full recipe")
    emit_job_event(job_id, {"type": "log", "level": "info", "message": f"Launching {'headless' if definition.get('mode') == 'headless' else 'headful'} Chrome for full recipe"})
    try:
        code, stderr_tail = stream_job_process(job_id, generated_file, definition)
    except OSError as exc:
        code, stderr_tail = 126, str(exc)
    if code == 0:
        add_job_log(job_id, "info", "Recipe completed successfully")
        emit_job_event(job_id, {"type": "complete", "status": "completed", "error": None})
        finish_job(job_id, "completed", None)
    else:
        error = stderr_tail or f"Generated recipe exited with code {code}"
        emit_job_event(job_id, {"type": "complete", "status": "failed", "error": error})
        finish_job(job_id, "failed", error)
    with JOB_RUNS_CHANGED:
        run = JOB_RUNS.get(job_id)
        if run:
            run["process"] = None
            run["done"] = True
        JOB_RUNS_CHANGED.notify_all()


def create_job(recipe_id: str, studio_session_id: str = "projects") -> dict[str, Any]:
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
    with JOB_RUNS_CHANGED:
        JOB_RUNS[job_id] = {
            "events": [{"type": "status", "status": "queued"}],
            "done": False,
            "process": None,
            "browser_healthy": False,
            "last_frame_at": 0.0,
            "project_id": recipe["project_id"],
            "recipe_id": recipe_id,
            "studio_session_id": studio_session_id,
        }
        JOB_RUNS_CHANGED.notify_all()
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
                query = parse_qs(urlparse(self.path).query)
                return self.stream_step_events(path.split("/")[3], int(query.get("after", [0])[0] or 0))
            if path == "/api/jobs":
                return self.send_payload({"jobs": list_jobs()})
            if path.startswith("/api/jobs/") and path.endswith("/events"):
                query = parse_qs(urlparse(self.path).query)
                return self.stream_job_events(path.split("/")[3], int(query.get("after", [0])[0] or 0))
            if path.startswith("/api/jobs/"):
                job = get_job(path.split("/")[3])
                return self.send_payload(job or {"error": "Job not found"}, HTTPStatus.OK if job else HTTPStatus.NOT_FOUND)
            return self.serve_static(path)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return
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
                return self.send_payload(create_job(path.split("/")[3], str(body.get("studio_session_id") or "projects")), HTTPStatus.ACCEPTED)
            if path.startswith("/api/step-runs/") and path.endswith("/input"):
                run_id = path.split("/")[3]
                command_type = str(body.get("type") or "")
                if command_type not in {"delay", "assign", "navigate", "text", "key", "click", "selector_click", "selector_fill", "selector_scrape", "selector_check", "message_box", "inspect", "inspect_move", "clear_picker_overlay", "run_step"}:
                    raise ValueError("Unsupported browser input type")
                command = {"type": command_type}
                if command_type == "delay":
                    command["seconds"] = validate_delay(body.get("seconds"))
                    command["until_load_completes"] = validate_until_load_completes(body.get("until_load_completes", False))
                elif command_type == "assign":
                    command["variable"] = validate_variable_name(body.get("variable"))
                    command["value"] = str(body.get("value") or "")
                elif command_type == "navigate":
                    command["url"] = validate_url(body.get("url"))
                elif command_type == "text":
                    command["text"] = str(body.get("text") or "")
                elif command_type == "key":
                    command["key"] = str(body.get("key") or "")
                elif command_type in {"selector_click", "selector_fill", "selector_scrape", "selector_check"}:
                    command["selector"] = validate_selector(body.get("selector"))
                    command["trial_id"] = str(body.get("trial_id") or "")
                    if command_type == "selector_fill":
                        command["value"] = str(body.get("value") or "")
                elif command_type == "message_box":
                    command["message"] = str(body.get("message") or "")
                elif command_type == "run_step":
                    raw_step = body.get("step") or {}
                    command["step"] = normalize_definition(
                        {"steps": [raw_step], "variables": body.get("variables") or []},
                        "draft-project", "draft-recipe", "step-run", 1, require_first_navigation=False,
                    )["steps"][0]
                else:
                    command["x"] = float(body.get("x", 0))
                    command["y"] = float(body.get("y", 0))
                    if command_type in {"click", "inspect", "inspect_move"}:
                        command["source_width"] = float(body.get("source_width", 0) or 0)
                        command["source_height"] = float(body.get("source_height", 0) or 0)
                reserve_command = command_type in {"delay", "assign", "navigate", "selector_click", "selector_fill", "selector_scrape", "selector_check", "message_box", "run_step"}
                try:
                    with STEP_RUNS_CHANGED:
                        event_index = len(STEP_RUNS.get(run_id, {}).get("events", []))
                    command_id = send_browser_command(run_id, command, reserve_step=reserve_command)
                except RuntimeError as exc:
                    return self.send_error_payload(str(exc), HTTPStatus.CONFLICT)
                return self.send_payload({"ok": True, "event_index": event_index, "command_id": command_id})
            if path == "/api/step-runs":
                run_id, event_index, command_id = start_step_run(body)
                return self.send_payload({"run_id": run_id, "event_index": event_index, "command_id": command_id, "status": "queued"}, HTTPStatus.ACCEPTED)
            return self.send_error_payload("Route not found", HTTPStatus.NOT_FOUND)
        except sqlite3.IntegrityError:
            self.send_error_payload("A project or recipe with that name already exists")
        except RuntimeError as exc:
            self.send_error_payload(str(exc), HTTPStatus.CONFLICT)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return
        except (sqlite3.Error, ValueError, KeyError) as exc:
            self.send_error_payload(str(exc), HTTPStatus.BAD_REQUEST)

    def stream_step_events(self, run_id: str, start_index: int = 0) -> None:
        with STEP_RUNS_CHANGED:
            if run_id not in STEP_RUNS:
                return self.send_error_payload("Step run not found", HTTPStatus.NOT_FOUND)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        index = max(0, int(start_index))
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
                    payload = f"id: {index}\ndata: {json.dumps(event)}\n\n".encode("utf-8")
                    self.wfile.write(payload)
                    self.wfile.flush()
                    index += 1
                if done and index >= len(STEP_RUNS[run_id]["events"]):
                    return
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return

    def stream_job_events(self, job_id: str, start_index: int = 0) -> None:
        with JOB_RUNS_CHANGED:
            if job_id not in JOB_RUNS:
                return self.send_error_payload("Job event stream not found", HTTPStatus.NOT_FOUND)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        index = max(0, int(start_index))
        try:
            while True:
                with JOB_RUNS_CHANGED:
                    run = JOB_RUNS[job_id]
                    events = run["events"][index:]
                    done = run["done"]
                    if not events and not done:
                        JOB_RUNS_CHANGED.wait(timeout=0.5)
                        continue
                for event in events:
                    payload = f"id: {index}\ndata: {json.dumps(event)}\n\n".encode("utf-8")
                    self.wfile.write(payload)
                    self.wfile.flush()
                    index += 1
                if done and index >= len(JOB_RUNS[job_id]["events"]):
                    return
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
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
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return
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


def _listening_process_ids(port: int) -> list[int]:
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["netstat", "-ano", "-p", "tcp"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        except OSError:
            return []
        process_ids = []
        for raw_line in result.stdout.splitlines():
            parts = raw_line.split()
            if len(parts) < 5 or parts[-2].upper() != "LISTENING":
                continue
            local_address = parts[1].rsplit(":", 1)
            if len(local_address) != 2 or local_address[1] != str(port):
                continue
            try:
                process_ids.append(int(parts[-1]))
            except ValueError:
                continue
        return sorted(set(process_ids))

    try:
        result = subprocess.run(
            ["lsof", "-ti", f"TCP:{port}", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return []
    process_ids = []
    for value in result.stdout.splitlines():
        try:
            process_ids.append(int(value.strip()))
        except ValueError:
            continue
    return sorted(set(process_ids))


def _process_image_path(process_id: int) -> str:
    if os.name == "nt":
        try:
            result = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    f"(Get-Process -Id {int(process_id)} -ErrorAction SilentlyContinue).Path",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            return result.stdout.strip()
        except OSError:
            return ""
    try:
        return os.readlink(f"/proc/{int(process_id)}/exe")
    except OSError:
        return ""


def stop_existing_python_backend(port: int) -> None:
    """Replace a Python listener on this app's port, leaving other services alone."""
    current_pid = os.getpid()
    for process_id in _listening_process_ids(port):
        if process_id == current_pid:
            continue
        image_path = _process_image_path(process_id)
        if "python" not in Path(image_path).name.lower():
            print(f"Port {port} is already used by a non-Python process; leaving it running.")
            continue
        try:
            if os.name == "nt":
                subprocess.run(
                    [
                        "powershell.exe",
                        "-NoProfile",
                        "-NonInteractive",
                        "-Command",
                        f"Stop-Process -Id {int(process_id)} -Force -ErrorAction Stop",
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
            else:
                os.kill(process_id, signal.SIGTERM)
            print(f"Stopped existing Python backend process {process_id} on port {port}.")
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"Unable to stop existing Python backend process {process_id}: {exc}")


def main() -> None:
    host = os.environ.get("SUI_HOST", "127.0.0.1")
    port = int(os.environ.get("SUI_PORT", "8000"))
    stop_existing_python_backend(port)
    initialize_database()
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
