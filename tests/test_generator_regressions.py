import ast
import difflib
import json
import os
import re
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

import main


class GeneratorRegressionTests(unittest.TestCase):
    def setUp(self):
        self.definition = {
            "mode": "headless",
            "steps": [
                {
                    "id": "step_navigation",
                    "type": "navigate_to_website",
                    "settings": {"url": "https://example.com"},
                }
            ],
        }

    def test_picker_coordinates_are_json_objects_and_js_destructuring_is_preserved(self):
        source = main.generate_step_python(self.definition)

        self.assertGreaterEqual(source.count(', {"x": x, "y": y})'), 3)
        self.assertNotIn(", {x, y})", source)
        self.assertIn("({x, y}) =>", source)

    def test_picker_hover_overlay_is_generated_in_the_browser(self):
        source = main.generate_step_python(self.definition)

        self.assertIn("async def highlight_picker_target", source)
        self.assertIn("#FF5B35", source)
        self.assertIn("pointerEvents: 'none'", source)
        self.assertIn("inspect_move", source)

    def test_picker_matches_rcs_selector_families_and_role_execution(self):
        source = main.generate_step_python(self.definition)

        for marker in (
            "data-testid",
            "data-test",
            "data-qa",
            "placeholder",
            "role=",
            ":has-text(",
            "contenteditable",
            "nth-of-type",
            "async def inspect_picker_at",
            "def locator_for",
        ):
            self.assertIn(marker, source)
        self.assertIn("get_by_role", source)

    def test_picker_input_returns_event_cursor_and_stream_honors_it(self):
        source = Path(main.__file__).read_text(encoding="utf-8")

        self.assertIn('"event_index": event_index', source)
        self.assertIn("index = max(0, int(start_index))", source)

    def test_picker_action_frame_is_emitted_before_completion(self):
        source = main.generate_step_python(self.definition)
        command_loop = source[source.index("async def execute_command"):]

        self.assertLess(
            command_loop.index("await emit_frame(page, capture_lock)"),
            command_loop.index("if action_result is not None"),
        )

    def test_message_box_is_a_normalized_browser_action(self):
        source = main.generate_step_python(self.definition)

        self.assertIn("action_result = {'status': 'completed', 'action': 'message_box', 'command_id': command.get('command_id')}", source)
        self.assertIn("await show_message_box(page, str(command.get('message') or ''))", source)

    def test_command_busy_clears_only_for_matching_completion(self):
        run_id = "regression-command-run"
        main.STEP_RUNS[run_id] = {
            "process": None,
            "command_busy": True,
            "busy_command_id": "command-expected",
        }
        try:
            self.assertFalse(main.clear_command_busy_if_matching(run_id, "command-other"))
            self.assertTrue(main.STEP_RUNS[run_id]["command_busy"])
            self.assertTrue(main.clear_command_busy_if_matching(run_id, "command-expected"))
            self.assertFalse(main.STEP_RUNS[run_id]["command_busy"])
        finally:
            main.STEP_RUNS.pop(run_id, None)

    def test_replacing_busy_command_sends_cancel_before_new_command(self):
        class FakeStdin:
            def __init__(self):
                self.lines = []

            def write(self, value):
                self.lines.append(value)

            def flush(self):
                return None

        class FakeProcess:
            def __init__(self):
                self.stdin = FakeStdin()

            def poll(self):
                return None

        run_id = "regression-replace-run"
        process = FakeProcess()
        main.STEP_RUNS[run_id] = {
            "process": process,
            "command_busy": True,
            "busy_command_id": "command-old",
        }
        try:
            new_id = main.send_browser_command(run_id, {"type": "delay", "seconds": 1}, reserve_step=True)
            self.assertEqual(main.STEP_RUNS[run_id]["busy_command_id"], new_id)
            self.assertEqual(json.loads(process.stdin.lines[0]), {"type": "cancel_command", "command_id": "command-old"})
            self.assertEqual(json.loads(process.stdin.lines[1]), {"type": "delay", "seconds": 1, "command_id": new_id})
        finally:
            main.STEP_RUNS.pop(run_id, None)

    def test_new_executable_command_cancels_the_previous_command(self):
        source = Path(main.__file__).read_text(encoding="utf-8")
        generated = main.generate_step_python(self.definition)

        self.assertIn('"type": "cancel_command"', source)
        self.assertIn("previous_command_id", source)
        self.assertIn("Superseded by a newer command", generated)
        self.assertIn("command.get('type') in executable_types", generated)
        self.assertIn("command_id={step.get('command_id', '')}", generated)
        self.assertIn("current_command_id", source)
        self.assertNotIn('A step is already running in this browser session', source)

    def test_use_js_allows_empty_bindings_and_normalizes_braced_references(self):
        definition = {
            "mode": "headless",
            "variables": [
                {"id": "variable_name", "name": "name", "value": "Ada"},
                {"id": "variable_result", "name": "result", "value": ""},
            ],
            "steps": [{
                "id": "step_js",
                "type": "use_js",
                "settings": {"code": "return args[0] || 'ok';", "inputs": ["{name}"], "output": "{result}"},
            }],
        }
        normalized = main.normalize_definition(definition, "project", "recipe", "recipe", 1, require_first_navigation=False)
        self.assertEqual(normalized["steps"][0]["settings"]["inputs"], ["name"])
        self.assertEqual(normalized["steps"][0]["settings"]["output"], "result")

        empty = main.normalize_definition({"mode": "headless", "steps": [{
            "id": "step_js",
            "type": "use_js",
            "settings": {"code": "return 'ok';", "inputs": [], "output": ""},
        }]}, "project", "recipe", "recipe", 1, require_first_navigation=False)
        self.assertEqual(empty["steps"][0]["settings"]["inputs"], [])
        self.assertEqual(empty["steps"][0]["settings"]["output"], "")

        generated = main.generate_step_python(empty)
        self.assertIn("function suiJSRunner(args)", generated)
        self.assertIn("'body': settings.get('code', '')", generated)

    def test_use_js_can_reference_a_for_loop_iterator(self):
        definition = {
            "mode": "headless",
            "steps": [{
                "id": "step_for",
                "type": "for",
                "settings": {"iterator": "i", "start_value": 1, "range_value": "3"},
                "branches": {"body": [{
                    "id": "step_js",
                    "type": "use_js",
                    "settings": {"code": "return args[0];", "inputs": ["{i}"], "output": ""},
                }]},
            }],
        }
        normalized = main.normalize_definition(definition, "project", "recipe", "recipe", 1, require_first_navigation=False)
        self.assertEqual(normalized["steps"][0]["branches"]["body"][0]["settings"]["inputs"], ["i"])

        with self.assertRaises(ValueError):
            main.normalize_definition({
                "mode": "headless",
                "steps": [{
                    "id": "step_for",
                    "type": "for",
                    "settings": {"iterator": "i", "start_value": 1, "range_value": "3"},
                    "branches": {"body": [{
                        "id": "step_js",
                        "type": "use_js",
                        "settings": {"code": "return args[0];", "inputs": ["{missing}"], "output": ""},
                    }]},
                }],
            }, "project", "recipe", "recipe", 1, require_first_navigation=False)

    def test_check_text_exists_waits_until_timeout_before_not_exists_branch(self):
        definition = {
            "mode": "headless",
            "steps": [{
                "id": "step_check",
                "type": "check_text_exists",
                "settings": {"text": "Post your Need", "timeout_seconds": 2, "case_insensitive": True},
                "branches": {"exists": [], "not_exists": []},
            }],
        }
        normalized = main.normalize_definition(definition, "project", "recipe", "recipe", 1, require_first_navigation=False)
        self.assertEqual(normalized["steps"][0]["settings"]["timeout_seconds"], 2.0)
        sync_source = main.generate_python(normalized)
        async_source = main.generate_step_python(normalized)
        for source in (sync_source, async_source):
            self.assertIn("deadline", source)
            self.assertIn("if exists: break", source)
            self.assertIn("not_exists", source)

    def test_keypress_activity_normalizes_and_generates_browser_key_action(self):
        definition = {
            "mode": "headless",
            "steps": [{"id": "step_key", "type": "keypress", "settings": {"key": "Esc"}}],
        }
        normalized = main.normalize_definition(definition, "project", "recipe", "recipe", 1, require_first_navigation=False)
        self.assertEqual(normalized["steps"][0]["settings"]["key"], "Escape")
        source = main.generate_step_python(normalized)
        self.assertIn("step_type == 'keypress'", source)
        self.assertIn("page.keyboard.press", source)

        with self.assertRaises(ValueError):
            main.normalize_definition({
                "mode": "headless",
                "steps": [{"id": "step_key", "type": "keypress", "settings": {"key": "NotAKey"}}],
            }, "project", "recipe", "recipe", 1, require_first_navigation=False)

    def test_delay_can_wait_for_load_or_use_seconds(self):
        for until_load_completes in (False, True):
            normalized = main.normalize_definition({
                "mode": "headless",
                "steps": [{
                    "id": "step_delay",
                    "type": "delay",
                    "settings": {"seconds": 2, "until_load_completes": until_load_completes},
                }],
            }, "project", "recipe", "recipe", 1, require_first_navigation=False)
            settings = normalized["steps"][0]["settings"]
            self.assertEqual(settings["seconds"], 2.0)
            self.assertEqual(settings["until_load_completes"], until_load_completes)

        source = main.generate_python(normalized)
        step_source = main.generate_step_python(normalized)
        for generated in (source, step_source):
            self.assertIn("until_load_completes", generated)
            self.assertIn("wait_for_load_state", generated)
            self.assertIn("settings.get('seconds', 1)", generated)

    def test_disabled_steps_are_preserved_and_skipped_by_both_runtimes(self):
        normalized = main.normalize_definition({
            "mode": "headless",
            "steps": [{
                "id": "step_disabled",
                "type": "message_box",
                "disabled": True,
                "settings": {"message": "do not run"},
            }],
        }, "project", "recipe", "recipe", 1, require_first_navigation=False)
        self.assertTrue(normalized["steps"][0]["disabled"])
        for generated in (main.generate_python(normalized), main.generate_step_python(normalized)):
            self.assertIn("if step.get('disabled')", generated)
            self.assertIn("SUI_LOG:Step skipped", generated)

    def test_navigate_activity_supports_history_and_refresh_actions(self):
        for action in ("back", "forward", "refresh"):
            normalized = main.normalize_definition({
                "mode": "headless",
                "steps": [{"id": "step_navigate", "type": "navigate", "settings": {"action": action}}],
            }, "project", "recipe", "recipe", 1, require_first_navigation=False)
            self.assertEqual(normalized["steps"][0]["settings"]["action"], action)
            source = main.generate_step_python(normalized)
            self.assertIn("step_type == 'navigate'", source)
            self.assertIn("wait_until='commit'", source)
            self.assertIn("page.go_back", source)
            self.assertIn("page.go_forward", source)
            self.assertIn("page.reload", source)

        with self.assertRaises(ValueError):
            main.normalize_definition({
                "mode": "headless",
                "steps": [{"id": "step_navigate", "type": "navigate", "settings": {"action": "home"}}],
            }, "project", "recipe", "recipe", 1, require_first_navigation=False)

    def test_writer_normalizes_safe_table_and_generates_expression_sql_append(self):
        definition = {
            "mode": "headless",
            "variables": [
                {"id": "one", "name": "varOne", "value": "Rasa"},
                {"id": "two", "name": "varTwo", "value": "New York"},
            ],
            "steps": [{
                "id": "step_writer",
                "type": "writer",
                "settings": {
                    "table_name": "events",
                    "columns": [
                        {"name": "organizer", "type": "TEXT", "value": '{varOne + "Organizer: " + varTwo}'},
                        {"name": "event_count", "type": "INTEGER", "value": "2"},
                    ],
                },
            }],
        }
        normalized = main.normalize_definition(definition, "project", "recipe", "recipe", 1, require_first_navigation=False)
        settings = normalized["steps"][0]["settings"]
        self.assertEqual(settings["table_name"], "events")
        self.assertEqual([column["name"] for column in settings["columns"]], ["organizer", "event_count"])
        self.assertEqual(settings["columns"][0]["value"], '{varOne + "Organizer: " + varTwo}')
        self.assertFalse(settings["semantic_duplicate_removal"])
        for source in (main.generate_python(normalized), main.generate_step_python(normalized)):
            ast.parse(source)
            self.assertIn("write_values_to_sqlite", source)
            self.assertIn("semantic_row_similarity", source)
            self.assertIn("CREATE TABLE IF NOT EXISTS", source)
            self.assertIn("INSERT INTO", source)
            self.assertIn("evaluate_value_expression", source)

        with tempfile.TemporaryDirectory() as directory:
            source_tree = ast.parse(main.generate_python(normalized))
            helper_nodes = [node for node in source_tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "write_values_to_sqlite"]
            namespace = {"os": os, "sqlite3": sqlite3, "WRITER_DB_PATH": str(Path(directory) / "writer.db")}
            exec(compile(ast.Module(body=helper_nodes, type_ignores=[]), "writer_helper.py", "exec"), namespace)
            namespace["write_values_to_sqlite"]("events", [
                {"name": "organizer", "type": "TEXT", "value": "Rasa Organizer: New York", "write": True},
                {"name": "event_count", "type": "INTEGER", "value": "2", "write": True},
            ])
            namespace["write_values_to_sqlite"]("events", [
                {"name": "organizer", "type": "TEXT", "value": "Second row", "write": True},
                {"name": "event_count", "type": "INTEGER", "value": "3", "write": True},
                {"name": "source", "type": "TEXT", "value": "sulekha", "write": True},
            ])
            connection = sqlite3.connect(Path(directory) / "writer.db")
            try:
                rows = connection.execute('SELECT organizer, event_count FROM "events" ORDER BY rowid').fetchall()
                columns = connection.execute('PRAGMA table_info("events")').fetchall()
            finally:
                connection.close()
            self.assertEqual(rows, [("Rasa Organizer: New York", 2), ("Second row", 3)])
            self.assertIn("source", {row[1] for row in columns})

            update = namespace["write_values_to_sqlite"]("events", [
                {"name": "organizer", "type": "TEXT", "value": "Updated organizer", "write": True},
                {"name": "event_count", "type": "INTEGER", "value": "", "write": True},
            ], False, 0.88, {"__sui_table": "events", "__sui_rowid": 1})
            self.assertTrue(update["updated"])
            connection = sqlite3.connect(Path(directory) / "writer.db")
            try:
                updated_rows = connection.execute('SELECT organizer, event_count FROM "events" ORDER BY rowid').fetchall()
            finally:
                connection.close()
            self.assertEqual(updated_rows, [("Updated organizer", 2), ("Second row", 3)])

            skipped = namespace["write_values_to_sqlite"]("events", [
                {"name": "organizer", "type": "TEXT", "value": "", "write": True},
            ], False, 0.88, {"__sui_table": "events", "__sui_rowid": 1})
            self.assertTrue(skipped["skipped"])
            connection = sqlite3.connect(Path(directory) / "writer.db")
            try:
                self.assertEqual(connection.execute('SELECT COUNT(*) FROM "events"').fetchone()[0], 2)
            finally:
                connection.close()

        with self.assertRaises(ValueError):
            main.normalize_definition({
                "mode": "headless",
                "steps": [{"id": "step_writer", "type": "writer", "settings": {"value": "x", "table_name": ""}}],
            }, "project", "recipe", "recipe", 1, require_first_navigation=False)

    def test_craft_table_creates_and_extends_schema(self):
        original_db_path = main.DB_PATH
        with tempfile.TemporaryDirectory() as directory:
            main.DB_PATH = Path(directory) / "sui.db"
            try:
                created = main.craft_table("events", [{"name": "organizer", "type": "TEXT"}])
                extended = main.craft_table("events", [{"name": "organizer", "type": "TEXT"}, {"name": "event_count", "type": "INTEGER"}])
                edited = main.craft_table("events", [{"name": "organizer", "original_name": "organizer", "type": "REAL"}, {"name": "event_count", "original_name": "event_count", "type": "INTEGER"}])
                self.assertTrue(created["created"])
                self.assertFalse(extended["created"])
                self.assertEqual([(column["name"], column["type"]) for column in extended["columns"]], [("organizer", "TEXT"), ("event_count", "INTEGER")])
                self.assertEqual([(column["name"], column["type"]) for column in edited["columns"]], [("organizer", "REAL"), ("event_count", "INTEGER")])
                connection = main.db_connection()
                try:
                    connection.execute('INSERT INTO "events" (organizer, event_count) VALUES (?, ?)', ("Saved row", 3))
                    connection.commit()
                finally:
                    connection.close()
                trimmed = main.craft_table("events", [{"name": "organizer", "original_name": "organizer", "type": "TEXT"}])
                self.assertEqual([column["name"] for column in trimmed["columns"]], ["organizer"])
                connection = main.db_connection()
                try:
                    self.assertEqual(connection.execute('SELECT organizer FROM "events"').fetchone()[0], "Saved row")
                finally:
                    connection.close()
            finally:
                main.DB_PATH = original_db_path

    def test_writer_semantic_duplicates_are_stored_without_inserting_main_row(self):
        definition = {
            "mode": "headless",
            "steps": [{
                "id": "step_writer",
                "type": "writer",
                "settings": {
                    "table_name": "events",
                    "semantic_duplicate_removal": True,
                    "columns": [
                        {"name": "title", "type": "TEXT", "value": "Live concert organizer", "write": True},
                        {"name": "city", "type": "TEXT", "value": "New York", "write": True},
                    ],
                },
            }],
        }
        normalized = main.normalize_definition(definition, "project", "recipe", "recipe", 1, require_first_navigation=False)
        self.assertTrue(normalized["steps"][0]["settings"]["semantic_duplicate_removal"])
        source_tree = ast.parse(main.generate_python(normalized))
        helper_names = {"semantic_row_text", "semantic_row_similarity", "record_semantic_duplicate", "write_values_to_sqlite"}
        helper_nodes = [node for node in source_tree.body if isinstance(node, ast.FunctionDef) and node.name in helper_names]
        with tempfile.TemporaryDirectory() as directory:
            namespace = {
                "ast": ast,
                "difflib": difflib,
                "json": json,
                "os": os,
                "re": re,
                "sqlite3": sqlite3,
                "time": time,
                "WRITER_DB_PATH": str(Path(directory) / "writer.db"),
            }
            exec(compile(ast.Module(body=helper_nodes, type_ignores=[]), "writer_helper.py", "exec"), namespace)
            write = namespace["write_values_to_sqlite"]
            write("events", [
                {"name": "title", "type": "TEXT", "value": "Live concert organizer", "write": True},
                {"name": "city", "type": "TEXT", "value": "New York", "write": True},
            ], True, 0.88)
            result = write("events", [
                {"name": "title", "type": "TEXT", "value": "Live concert organizers", "write": True},
                {"name": "city", "type": "TEXT", "value": "New York", "write": True},
            ], True, 0.88)
            self.assertTrue(result["duplicate"])
            connection = sqlite3.connect(Path(directory) / "writer.db")
            try:
                event_rows = connection.execute('SELECT COUNT(*) FROM "events"').fetchone()[0]
                duplicate = connection.execute('SELECT source_table, incoming_row, existing_row FROM "duplicates"').fetchone()
            finally:
                connection.close()
            self.assertEqual(event_rows, 1)
            self.assertEqual(duplicate[0], "events")
            self.assertIn("Live concert organizers", duplicate[1])
            self.assertIn("Live concert organizer", duplicate[2])

    def test_orchest_recipe_normalizes_and_generates_recipe_call_activity(self):
        definition = {
            "mode": "headless",
            "recipe_type": "orchest",
            "steps": [{
                "id": "step_call",
                "type": "invoke_recipe",
                "settings": {"recipe_id": "recipe_child", "recipe_name": "child"},
            }],
        }
        normalized = main.normalize_definition(definition, "project", "recipe_parent", "parent", 1, require_first_navigation=False)
        self.assertEqual(normalized["recipe_type"], "orchest")
        self.assertEqual(normalized["steps"][0]["settings"]["recipe_id"], "recipe_child")
        for source in (main.generate_python(normalized), main.generate_step_python(normalized)):
            ast.parse(source)
            self.assertIn("run_called_recipe", source)
            self.assertIn("Recursive recipe calls are not allowed", source)
            self.assertIn("if target_id:", source)
            self.assertNotIn("? = '')", source)

        with self.assertRaises(ValueError):
            main.normalize_definition({
                "recipe_type": "standard",
                "steps": [{"id": "step_call", "type": "call_recipe", "settings": {"recipe_name": "child"}}],
            }, "project", "recipe_parent", "parent", 1, require_first_navigation=False)

    def test_orchest_recipe_does_not_require_initial_navigation(self):
        normalized = main.normalize_definition({
            "recipe_type": "orchest",
            "steps": [{
                "id": "step_message",
                "type": "message_box",
                "settings": {"message": "start"},
            }],
        }, "project", "recipe_orchest", "orchestrator", 1)
        self.assertEqual(normalized["recipe_type"], "orchest")
        self.assertEqual(normalized["steps"][0]["type"], "message_box")

    def test_craft_table_can_insert_and_update_rows(self):
        original_db_path = main.DB_PATH
        with tempfile.TemporaryDirectory() as directory:
            main.DB_PATH = Path(directory) / "sui.db"
            try:
                main.craft_table("events", [{"name": "title", "type": "TEXT"}, {"name": "count", "type": "INTEGER"}])
                inserted = main.data_table_row("events", "insert", raw_values={"title": "Opening night", "count": "1"})
                self.assertEqual(inserted["rows"][0]["title"], "Opening night")
                row_id = inserted["rows"][0]["__sui_rowid"]
                updated = main.data_table_row("events", "update", row_id=row_id, column_name="count", value="2")
                self.assertEqual(updated["rows"][0]["count"], 2)
                deleted = main.data_table_row("events", "delete", row_id=row_id)
                self.assertEqual(deleted["rows"], [])
            finally:
                main.DB_PATH = original_db_path

    def test_action_destination_waits_and_active_page_propagates(self):
        definition = {
            "mode": "headful",
            "steps": [
                {"id": "step_navigation", "type": "navigate_to_website", "settings": {"url": "https://example.com"}},
                {"id": "step_click", "type": "click", "settings": {
                    "selector": "#event-{i}",
                    "expected_url": "https://example.com/detail",
                    "destination_selector": "#organizer",
                    "wait_timeout_seconds": 20,
                }},
                {"id": "step_key", "type": "keypress", "settings": {
                    "key": "Enter",
                    "destination_selector": "#after-key",
                }},
                {"id": "step_js", "type": "use_js", "settings": {
                    "code": "return args[0];",
                    "inputs": [],
                    "output": "",
                    "destination_selector": "#after-js",
                }},
            ],
            "variables": [{"id": "variable_i", "name": "i", "value": "1"}],
        }
        normalized = main.normalize_definition(definition, "project", "recipe", "recipe", 1, require_first_navigation=False)
        self.assertEqual(normalized["steps"][1]["settings"]["destination_selector"], "#organizer")
        self.assertEqual(normalized["steps"][1]["settings"]["wait_timeout_seconds"], 20.0)
        for source in (main.generate_python(normalized), main.generate_step_python({**normalized, "steps": [normalized["steps"][1]]})):
            self.assertIn("wait_for_action_destination", source)
            self.assertIn("destination_selector", source)
            self.assertIn("new_pages", source)
            self.assertIn("active_page=", source)
            self.assertIn("SUI_FRAME_ERROR", source)
            self.assertIn("PAGE_SOURCES", source)
            self.assertIn("source_url and page_url(page) == previous_url", source)
            self.assertIn("Click raised after navigation; continuing", source)
            self.assertIn("if not navigated: raise", source)
        self.assertIn("page = execute_steps(page, recipe_steps)", main.generate_python(normalized))
        self.assertIn("page = await execute_steps(page, (step.get('branches') or {}).get('body', []), capture_lock)", main.generate_step_python({**normalized, "steps": [normalized["steps"][1]]}))

    def test_for_loop_normalizes_range_and_generates_nested_body_execution(self):
        definition = {
            "mode": "headless",
            "variables": [{"id": "variable_count", "name": "count", "value": "3"}],
            "steps": [{
                "id": "step_for",
                "type": "for",
                "settings": {"iterator": "{i}=1", "range_value": "count+1"},
                "branches": {"body": [{
                    "id": "step_message",
                    "type": "message_box",
                    "settings": {"message": "{i}"},
                }, {
                    "id": "step_click",
                    "type": "click",
                    "settings": {"selector": "#position-spotlight-{i}"},
                }]},
            }],
        }
        normalized = main.normalize_definition(definition, "project", "recipe", "recipe", 1, require_first_navigation=False)
        self.assertEqual(normalized["steps"][0]["settings"], {"iterator": "i", "start_value": 1, "range_value": "count+1"})
        self.assertEqual(normalized["steps"][0]["branches"]["body"][0]["type"], "message_box")
        self.assertEqual(normalized["steps"][0]["branches"]["body"][1]["settings"]["selector"], "#position-spotlight-{i}")
        for source in (main.generate_python(normalized), main.generate_step_python(normalized)):
            self.assertIn("step_type == 'for'", source)
            self.assertIn("range(loop_start, loop_start + loop_count)", source)
            self.assertIn("resolve_loop_range", source)
            self.assertIn("get('body', [])", source)

        with self.assertRaises(ValueError):
            main.normalize_definition({
                "mode": "headless",
                "steps": [{"id": "step_for", "type": "for", "settings": {"iterator": "i", "range_value": "{missing}"}}],
            }, "project", "recipe", "recipe", 1, require_first_navigation=False)

    def test_for_loop_reads_table_rows_or_column_values(self):
        row_definition = {
            "mode": "headless",
            "steps": [{
                "id": "step_for_rows",
                "type": "for",
                "settings": {"source": "table", "table_mode": "row", "table_name": "events", "iterator": "row"},
                "branches": {"body": [{"id": "step_message", "type": "message_box", "settings": {"message": "{row.organizer}"}}]},
            }],
        }
        column_definition = {
            "mode": "headless",
            "steps": [{
                "id": "step_for_column",
                "type": "for",
                "settings": {"source": "table", "table_mode": "column", "table_name": "events", "column_name": "organizer", "iterator": "organizer"},
                "branches": {"body": []},
            }],
        }
        normalized_row = main.normalize_definition(row_definition, "project", "recipe", "recipe", 1, require_first_navigation=False)
        normalized_column = main.normalize_definition(column_definition, "project", "recipe", "recipe", 1, require_first_navigation=False)
        self.assertEqual(normalized_row["steps"][0]["settings"]["table_mode"], "row")
        self.assertEqual(normalized_column["steps"][0]["settings"]["column_name"], "organizer")
        for source in (main.generate_python(normalized_row), main.generate_step_python(normalized_row), main.generate_python(normalized_column)):
            self.assertIn("read_sqlite_table", source)
            self.assertIn("settings.get('source') == 'table'", source)
            self.assertIn("Unknown row column", source)
            self.assertIn("currentRow", source)
            self.assertIn("UPDATE {quoted_table} SET {assignments} WHERE rowid = ?", source)

    def test_full_and_single_step_paths_wait_for_the_same_resolved_loop_targets(self):
        definition = {
            "mode": "headless",
            "variables": [
                {"id": "variable_count", "name": "count", "value": "2"},
                {"id": "variable_value", "name": "value", "value": ""},
            ],
            "steps": [{
                "id": "step_for",
                "type": "for",
                "settings": {"iterator": "i", "start_value": 1, "range_value": "count"},
                "branches": {"body": [
                    {"id": "step_scrape", "type": "scrape", "settings": {"selector": "#item-{i}", "variable": "value"}},
                    {"id": "step_click", "type": "click", "settings": {"selector": "#next-{i}"}},
                ]},
            }],
        }
        normalized = main.normalize_definition(definition, "project", "recipe", "recipe", 1, require_first_navigation=False)
        full_source = main.generate_python(normalized)
        step_source = main.generate_step_python(normalized)
        for source in (full_source, step_source):
            self.assertIn("wait_for_locator", source)
            self.assertIn("resolve(settings.get('selector'), variables)", source)
            self.assertIn("action + ' target not found for selector '", source)
        self.assertIn("for loop_index in range(loop_start, loop_start + loop_count)", full_source)
        self.assertIn("execute_steps(page, (step.get('branches') or {}).get('body', []))", full_source)

    def test_pattern_matcher_normalizes_or_patterns_and_generates_both_runtimes(self):
        definition = {
            "mode": "headless",
            "variables": [{"id": "variable_match", "name": "match", "value": ""}],
            "steps": [{
                "id": "step_pattern",
                "type": "pattern_matcher",
                "settings": {
                    "patterns": [r"Organizer:\s*(.+)", r"Host:\s*(.+)"],
                    "variable": "match",
                },
            }],
        }
        normalized = main.normalize_definition(definition, "project", "recipe", "recipe", 1, require_first_navigation=False)
        self.assertEqual(normalized["steps"][0]["settings"]["patterns"], [r"Organizer:\s*(.+)", r"Host:\s*(.+)"])
        for source in (main.generate_python(normalized), main.generate_step_python(normalized)):
            ast.parse(source)
            self.assertIn("page.locator('body').inner_text()", source)
            self.assertIn("re.finditer(combined_pattern, source, re.IGNORECASE)", source)
            self.assertIn("' | '.join(matches)", source)
            self.assertIn("NO CONTACTS FOUND", source)

        with self.assertRaises(ValueError):
            main.normalize_definition({
                "variables": [{"id": "variable_match", "name": "match", "value": ""}],
                "steps": [{"type": "pattern_matcher", "settings": {
                    "source": "text", "patterns": ["["], "variable": "match",
                }}],
            }, "project", "recipe", "recipe", 1, require_first_navigation=False)

    def test_smartie_normalizes_prompts_schema_and_generates_both_runtimes(self):
        definition = {
            "mode": "headless",
            "variables": [
                {"id": "variable_input", "name": "input", "value": "hello"},
                {"id": "variable_result", "name": "result", "value": ""},
            ],
            "steps": [{
                "id": "step_smartie",
                "type": "smartie",
                "settings": {
                    "system_prompt": "Return only structured data.",
                    "user_prompt": "Classify {input} and {currentRow.name}.",
                    "model": "gemini-2.5-flash",
                    "output_type": "json",
                    "output_variable": "result",
                    "schema": [
                        {"name": "label", "type": "string", "required": True},
                        {"name": "score", "type": "number", "required": False},
                    ],
                },
            }],
        }
        normalized = main.normalize_definition(definition, "project", "recipe", "recipe", 1, require_first_navigation=False)
        settings = normalized["steps"][0]["settings"]
        self.assertEqual(settings["provider"], "google_gemini")
        self.assertEqual(settings["model"], "gemini-2.5-flash")
        self.assertEqual(settings["output_variable"], "result")
        self.assertEqual(settings["schema"][0]["name"], "label")
        for source in (main.generate_python(normalized), main.generate_step_python(normalized)):
            ast.parse(source)
            self.assertIn("GOOGLE_GEMINI_KEY", source)
            self.assertIn("run_smartie", source)
            self.assertIn("gemini-2.5-flash", source)
            self.assertIn("'x-goog-api-key': api_key", source)
            self.assertNotIn("?key=' + quote(api_key", source)
            self.assertIn("resolve(settings.get('user_prompt'), variables)", source)
            self.assertIn("responseSchema", source)

        migrated = main.normalize_definition({
            "variables": [{"name": "result", "value": ""}],
            "steps": [{"type": "smartie", "settings": {
                "user_prompt": "Return text", "output_type": "text", "output_variable": "result",
                "model": "gemini-2.0-flash",
            }}],
        }, "project", "recipe", "recipe", 1, require_first_navigation=False)
        self.assertEqual(migrated["steps"][0]["settings"]["model"], "gemini-2.5-flash")

        with self.assertRaises(ValueError):
            main.normalize_definition({
                "variables": [{"name": "result", "value": ""}],
                "steps": [{"type": "smartie", "settings": {
                    "user_prompt": "Return JSON", "output_type": "json", "output_variable": "result", "schema": [],
                }}],
            }, "project", "recipe", "recipe", 1, require_first_navigation=False)

        with self.assertRaises(ValueError):
            main.normalize_definition({
                "variables": [{"name": "result", "value": ""}],
                "steps": [{"type": "smartie", "settings": {
                    "model": "unsupported-model", "user_prompt": "Return text", "output_variable": "result",
                }}],
            }, "project", "recipe", "recipe", 1, require_first_navigation=False)

        text_definition = main.normalize_definition({
            "variables": [{"name": "result", "value": ""}],
            "steps": [{"type": "smartie", "settings": {
                "user_prompt": "Return text", "output_variable": "result", "schema": [{"name": "", "type": "string"}],
            }}],
        }, "project", "recipe", "recipe", 1, require_first_navigation=False)
        self.assertEqual(text_definition["steps"][0]["settings"]["schema"], [])

    def test_click_recovery_and_command_scoped_failure_logging_exist_in_both_paths(self):
        definition = {
            "mode": "headless",
            "steps": [{
                "id": "step_click",
                "type": "click",
                "settings": {"selector": "#event-link"},
            }],
        }
        normalized = main.normalize_definition(definition, "project", "recipe", "recipe", 1, require_first_navigation=False)
        full_source = main.generate_python(normalized)
        step_source = main.generate_step_python(normalized)
        for source in (full_source, step_source):
            self.assertIn("navigated = page_url(page) != source_url or bool(new_pages(page, before_pages))", source)
            self.assertIn("if not navigated: raise", source)
            self.assertIn("command_id={step.get('command_id', '')}", source)

    def test_frame_transition_retries_locator_and_body_reads_in_both_paths(self):
        normalized = main.normalize_definition({
            "mode": "headless",
            "steps": [{"id": "step_for", "type": "for", "settings": {
                "iterator": "i", "start_value": 0, "range_value": "1",
            }, "branches": {"body": [{
                "id": "step_pattern", "type": "pattern_matcher",
                "settings": {"patterns": [r"\\btest\\b"], "variable": "result"},
            }]}}],
            "variables": [{"name": "result", "value": ""}],
        }, "project", "recipe", "recipe", 1, require_first_navigation=False)
        for source in (main.generate_python(normalized), main.generate_step_python(normalized)):
            ast.parse(source)
            self.assertIn("'frame ownership changed'", source)
            self.assertIn("page_body_text", source)
            self.assertTrue("time.sleep(0.10)" in source or "await asyncio.sleep(0.10)" in source)

    def test_loop_body_continues_after_bounded_load_completion_delay(self):
        definition = {
            "mode": "headful",
            "steps": [{
                "id": "step_for",
                "type": "for",
                "settings": {"iterator": "i", "start_value": 1, "range_value": "2"},
                "branches": {"body": [
                    {"id": "step_delay", "type": "delay", "settings": {"until_load_completes": True}},
                    {"id": "step_message", "type": "message_box", "settings": {"message": "{i}"}},
                    {"id": "step_navigate", "type": "navigate", "settings": {"action": "back"}},
                ]},
            }],
        }
        normalized = main.normalize_definition(definition, "project", "recipe", "recipe", 1, require_first_navigation=False)
        full_source = main.generate_python(normalized)
        step_source = main.generate_step_python(normalized)
        for source in (full_source, step_source):
            self.assertIn("wait_for_load_completion", source)
            self.assertIn("30 second limit; continuing", source)
            self.assertIn("For loop iteration", source)
        self.assertIn("if settings.get('until_load_completes'): wait_for_load_completion(page)", full_source)
        self.assertIn("execute_steps(page, (step.get('branches') or {}).get('body', []))", full_source)

    def test_job_stream_initializes_and_honors_event_cursor(self):
        source = Path(main.__file__).read_text(encoding="utf-8")

        self.assertIn("def stream_job_events(self, job_id: str, start_index: int = 0)", source)
        self.assertIn("index = max(0, int(start_index))", source)
        self.assertIn("return self.stream_job_events(path.split(\"/\")[3], int(query.get(\"after\", [0])[0] or 0))", source)
        self.assertIn("id: {index}\\ndata:", source)

    def test_startup_cleanup_is_scoped_to_python_port_owner(self):
        source = Path(main.__file__).read_text(encoding="utf-8")

        self.assertIn("def stop_existing_python_backend", source)
        self.assertIn('if \"python\" not in Path(image_path).name.lower()', source)
        self.assertIn("Stop-Process -Id", source)

    def test_picker_wire_json_is_ascii_but_round_trips_unicode(self):
        source = main.generate_step_python(self.definition)
        tree = ast.parse(source)
        function = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "inspect_wire_text"
        )
        function_module = ast.Module(
            body=[ast.Import(names=[ast.alias(name="json")]), function],
            type_ignores=[],
        )
        ast.fix_missing_locations(function_module)
        namespace = {
            "json_safe": main._json_safe_value,
        }
        exec(compile(function_module, "generated-picker-test.py", "exec"), namespace)

        original = {"metadata": {"text": "🍌 தமிழ்"}, "selectorCandidates": {"#search"}}
        wire = namespace["inspect_wire_text"](json.dumps(main._json_safe_value(original), ensure_ascii=False))

        self.assertTrue(wire.isascii())
        self.assertEqual(json.loads(wire)["metadata"]["text"], "🍌 தமிழ்")
        self.assertEqual(json.loads(wire)["selectorCandidates"], ["#search"])

    def test_generated_script_execution_preserves_unicode_output(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "unicode-output.py"
            script.write_text("print('🍌 தமிழ்')\n", encoding="utf-8")
            code, stdout, stderr = main.execute_generated(script)

        self.assertEqual(code, 0, stderr)
        self.assertEqual(stdout, ["🍌 தமிழ்"])

    def test_inspect_event_reaches_frontend_payload_unchanged(self):
        source = main.generate_step_python(self.definition)
        tree = ast.parse(source)
        function = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "inspect_wire_text"
        )
        function_module = ast.Module(
            body=[ast.Import(names=[ast.alias(name="json")]), function],
            type_ignores=[],
        )
        ast.fix_missing_locations(function_module)
        namespace = {"json_safe": main._json_safe_value}
        exec(compile(function_module, "generated-picker-event-test.py", "exec"), namespace)

        main.STEP_RUNS.clear()
        main.STEP_RUNS["picker-test"] = {"events": []}
        element = {
            "selector": "button[aria-label='🍌']",
            "selectorCandidates": [{"kind": "ARIA label", "selector": "[aria-label='🍌']", "unique": True}],
            "metadata": {"text": "🍌 தமிழ்", "ariaLabel": "🍌"},
        }
        wire = namespace["inspect_wire_text"](json.dumps(element, ensure_ascii=False))
        main.emit_step_event("picker-test", {"type": "inspect", "element": json.loads(wire)})

        delivered = main.STEP_RUNS["picker-test"]["events"][0]["element"]
        self.assertEqual(delivered, element)


if __name__ == "__main__":
    unittest.main()
