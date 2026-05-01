#!/usr/bin/env python3
"""Unit tests for Agent Relay (relay-msg)."""

import importlib
import importlib.util
import json
import os
import sys
import unittest
from io import StringIO
from unittest.mock import patch, MagicMock, call

# Load relay-msg as a module (it has no .py extension).
_relay_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "relay-msg")
loader = importlib.machinery.SourceFileLoader("relay_msg", _relay_path)
spec = importlib.util.spec_from_loader("relay_msg", loader, origin=_relay_path)
relay = importlib.util.module_from_spec(spec)

relay.__file__ = _relay_path

# Prevent _load_env_file from running during import — we control env in tests.
with patch.dict(os.environ, {"RELAY_IDENTITY": "test-agent", "RELAY_DB_NAME": "test_db"}):
    spec.loader.exec_module(relay)

# Register in sys.modules so patch("relay_msg.X") works.
sys.modules["relay_msg"] = relay


def _mock_subprocess(stdout="", returncode=0, stderr=""):
    """Create a mock subprocess.run result."""
    mock = MagicMock()
    mock.stdout = stdout
    mock.stderr = stderr
    mock.returncode = returncode
    return mock


class TestEsc(unittest.TestCase):
    """Test SQL escaping."""

    def test_escapes_single_quotes(self):
        self.assertEqual(relay._esc("it's"), "it\\'s")

    def test_escapes_backslashes(self):
        self.assertEqual(relay._esc("a\\b"), "a\\\\b")

    def test_no_change_for_safe_strings(self):
        self.assertEqual(relay._esc("hello world"), "hello world")

    def test_empty_string(self):
        self.assertEqual(relay._esc(""), "")

    def test_both_quotes_and_backslashes(self):
        self.assertEqual(relay._esc("it's a\\path"), "it\\'s a\\\\path")


class TestDetectSelf(unittest.TestCase):
    """Test agent identity detection."""

    def test_env_var_wins(self):
        with patch.dict(os.environ, {"RELAY_IDENTITY": "alice"}):
            self.assertEqual(relay.detect_self(), "alice")

    def test_cwd_pattern_match(self):
        with patch.dict(os.environ, {}, clear=False):
            # Remove RELAY_IDENTITY to test CWD fallback.
            env = os.environ.copy()
            env.pop("RELAY_IDENTITY", None)
            with patch.dict(os.environ, env, clear=True):
                with patch("relay_msg.run_sql_raw", return_value=["bob\tmy-project-bob"]):
                    with patch("os.getcwd", return_value="/home/user/my-project-bob"):
                        self.assertEqual(relay.detect_self(), "bob")

    def test_cwd_no_match_falls_back_to_default(self):
        env = os.environ.copy()
        env.pop("RELAY_IDENTITY", None)
        with patch.dict(os.environ, env, clear=True):
            with patch("relay_msg.run_sql_raw", return_value=["bob\tmy-project-bob"]):
                with patch("os.getcwd", return_value="/some/other/path"):
                    self.assertEqual(relay.detect_self(), "default")

    def test_db_failure_falls_back_to_default(self):
        env = os.environ.copy()
        env.pop("RELAY_IDENTITY", None)
        with patch.dict(os.environ, env, clear=True):
            with patch("relay_msg.run_sql_raw", side_effect=SystemExit(2)):
                self.assertEqual(relay.detect_self(), "default")


class TestGetTransport(unittest.TestCase):
    """Test transport detection."""

    def test_returns_transport_from_db(self):
        with patch("relay_msg.detect_self", return_value="alice"):
            with patch("relay_msg.run_sql_raw", return_value=["remote"]):
                self.assertEqual(relay._get_transport(), "remote")

    def test_defaults_to_local_when_not_registered(self):
        with patch("relay_msg.detect_self", return_value="unknown"):
            with patch("relay_msg.run_sql_raw", return_value=[]):
                self.assertEqual(relay._get_transport(), "local")

    def test_defaults_to_local_on_db_error(self):
        with patch("relay_msg.detect_self", return_value="alice"):
            with patch("relay_msg.run_sql_raw", side_effect=Exception("connection refused")):
                self.assertEqual(relay._get_transport(), "local")


class TestRunSqlRaw(unittest.TestCase):
    """Test raw SQL execution."""

    def test_local_transport_calls_mysql_directly(self):
        with patch.dict(os.environ, {"RELAY_TRANSPORT": "local"}):
            with patch("subprocess.run", return_value=_mock_subprocess("row1\nrow2")) as mock_run:
                result = relay.run_sql_raw("SELECT 1", fetch=True)
                self.assertEqual(result, ["row1", "row2"])
                mock_run.assert_called_once()
                args = mock_run.call_args[0][0]
                self.assertEqual(args[0], "mysql")

    def test_local_transport_returns_empty_on_failure(self):
        with patch.dict(os.environ, {"RELAY_TRANSPORT": "local"}):
            with patch("subprocess.run", return_value=_mock_subprocess(returncode=1)):
                result = relay.run_sql_raw("BAD SQL", fetch=True)
                self.assertEqual(result, [])

    def test_remote_transport_uses_proxy(self):
        with patch.dict(os.environ, {"RELAY_TRANSPORT": "remote", "RELAY_PROXY_CMD": "my-proxy"}):
            with patch("subprocess.run", return_value=_mock_subprocess("row1")) as mock_run:
                result = relay.run_sql_raw("SELECT 1", fetch=True)
                self.assertEqual(result, ["row1"])
                mock_run.assert_called_once()
                # Should use shell=True for proxy commands.
                self.assertTrue(mock_run.call_args[1].get("shell"))

    def test_remote_transport_no_proxy_returns_empty(self):
        env = os.environ.copy()
        env["RELAY_TRANSPORT"] = "remote"
        env.pop("RELAY_PROXY_CMD", None)
        with patch.dict(os.environ, env, clear=True):
            with patch("relay_msg._get_proxy_cmd", return_value=None):
                result = relay.run_sql_raw("SELECT 1", fetch=True)
                self.assertEqual(result, [])


class TestRunSql(unittest.TestCase):
    """Test SQL execution with transport routing."""

    def test_local_calls_mysql(self):
        with patch("relay_msg._get_transport", return_value="local"):
            with patch("subprocess.run", return_value=_mock_subprocess("ok")) as mock_run:
                result = relay.run_sql("SELECT 1", fetch=True)
                self.assertEqual(result, ["ok"])

    def test_remote_user_proxy_uses_shell(self):
        """User-supplied RELAY_PROXY_CMD goes through the shell=True path so
        custom proxy commands (ssh, docker exec, etc.) keep working."""
        with patch("relay_msg._get_transport", return_value="remote"), \
             patch.dict(os.environ, {"RELAY_PROXY_CMD": "my-proxy"}), \
             patch("subprocess.run", return_value=_mock_subprocess("ok")) as mock_run:
            result = relay.run_sql("SELECT 1", fetch=True)
            self.assertEqual(result, ["ok"])
            self.assertTrue(mock_run.call_args[1].get("shell"))

    def test_remote_auto_run_cmd_uses_direct_invocation(self):
        """Without RELAY_PROXY_CMD, fall back to direct python3 run-cmd invocation
        (no shell) to bypass multi-layer shell-quoting corruption."""
        env_without_proxy = {k: v for k, v in os.environ.items() if k != "RELAY_PROXY_CMD"}
        with patch("relay_msg._get_transport", return_value="remote"), \
             patch.dict(os.environ, env_without_proxy, clear=True), \
             patch("relay_msg._find_run_cmd", return_value="/fake/bin/run-cmd"), \
             patch("subprocess.run", return_value=_mock_subprocess("ok")) as mock_run:
            result = relay.run_sql("SELECT 1", fetch=True)
            self.assertEqual(result, ["ok"])
            # First positional arg must be a list starting with python3 + run-cmd path.
            called_args = mock_run.call_args[0][0]
            self.assertEqual(called_args[:3], ["python3", "/fake/bin/run-cmd", "local"])
            self.assertFalse(mock_run.call_args[1].get("shell", False))

    def test_remote_no_proxy_no_run_cmd_exits(self):
        env_without_proxy = {k: v for k, v in os.environ.items() if k != "RELAY_PROXY_CMD"}
        with patch("relay_msg._get_transport", return_value="remote"), \
             patch.dict(os.environ, env_without_proxy, clear=True), \
             patch("relay_msg._find_run_cmd", return_value=None):
            with self.assertRaises(SystemExit) as ctx:
                relay.run_sql("SELECT 1")
            self.assertEqual(ctx.exception.code, 2)

    def test_local_mysql_error_exits(self):
        with patch("relay_msg._get_transport", return_value="local"):
            with patch("subprocess.run", return_value=_mock_subprocess(returncode=1, stderr="access denied")):
                with self.assertRaises(SystemExit) as ctx:
                    relay.run_sql("SELECT 1")
                self.assertEqual(ctx.exception.code, 2)


class TestCmdSend(unittest.TestCase):
    """Test sending messages."""

    def test_send_to_agent(self):
        with patch("relay_msg.ensure_schema"):
            with patch("relay_msg.detect_self", return_value="alice"):
                with patch("relay_msg._get_all_agents", return_value=["alice", "bob"]):
                    with patch("relay_msg._get_all_groups", return_value=[]):
                        with patch("relay_msg._resolve_alias", return_value="bob"):
                            with patch("relay_msg.run_sql") as mock_sql:
                                with patch("sys.stdout", new_callable=StringIO) as mock_out:
                                    relay.cmd_send("bob", "hello")
                                    sql = mock_sql.call_args[0][0]
                                    self.assertIn("bob", sql)
                                    self.assertIn("hello", sql)
                                    self.assertIn("alice", sql)  # sender
                                    self.assertIn("Message sent to bob", mock_out.getvalue())

    def test_send_to_group(self):
        with patch("relay_msg.ensure_schema"):
            with patch("relay_msg.detect_self", return_value="alice"):
                with patch("relay_msg._get_all_agents", return_value=["alice", "bob", "carol"]):
                    with patch("relay_msg._get_all_groups", return_value=["backend"]):
                        with patch("relay_msg._get_group_members", return_value=["alice", "bob", "carol"]):
                            with patch("relay_msg.run_sql") as mock_sql:
                                with patch("sys.stdout", new_callable=StringIO) as mock_out:
                                    relay.cmd_send("backend", "deploy done")
                                    sql = mock_sql.call_args[0][0]
                                    # Should send to bob and carol, not alice (sender).
                                    self.assertIn("bob", sql)
                                    self.assertIn("carol", sql)
                                    self.assertIn("[broadcast:backend]", sql)
                                    output = mock_out.getvalue()
                                    self.assertIn("Broadcast (backend)", output)

    def test_send_to_all(self):
        with patch("relay_msg.ensure_schema"):
            with patch("relay_msg.detect_self", return_value="alice"):
                with patch("relay_msg._get_all_agents", return_value=["alice", "bob", "carol"]):
                    with patch("relay_msg._get_all_groups", return_value=[]):
                        with patch("relay_msg.run_sql") as mock_sql:
                            with patch("sys.stdout", new_callable=StringIO):
                                relay.cmd_send("all", "maintenance")
                                sql = mock_sql.call_args[0][0]
                                self.assertIn("bob", sql)
                                self.assertIn("carol", sql)
                                # alice appears as sender but NOT as a target (recipient).
                                # Targets are the first field in each tuple.
                                values_part = sql.split("VALUES")[1]
                                self.assertNotIn("('alice'", values_part)  # sender excluded from recipients

    def test_send_all_no_peers_exits(self):
        with patch("relay_msg.ensure_schema"):
            with patch("relay_msg.detect_self", return_value="alice"):
                with patch("relay_msg._get_all_agents", return_value=["alice"]):
                    with patch("relay_msg._get_all_groups", return_value=[]):
                        with self.assertRaises(SystemExit):
                            relay.cmd_send("all", "hello?")

    def test_send_to_alias(self):
        with patch("relay_msg.ensure_schema"):
            with patch("relay_msg.detect_self", return_value="alice"):
                with patch("relay_msg._get_all_agents", return_value=["alice", "bob"]):
                    with patch("relay_msg._get_all_groups", return_value=[]):
                        with patch("relay_msg._resolve_alias", return_value="bob"):
                            with patch("relay_msg.run_sql") as mock_sql:
                                with patch("sys.stdout", new_callable=StringIO):
                                    relay.cmd_send("robert", "hey")
                                    sql = mock_sql.call_args[0][0]
                                    self.assertIn("bob", sql)

    def test_send_to_unknown_still_delivers(self):
        """Sending to an unknown target resolves via alias and delivers anyway."""
        with patch("relay_msg.ensure_schema"):
            with patch("relay_msg.detect_self", return_value="alice"):
                with patch("relay_msg._get_all_agents", return_value=["alice"]):
                    with patch("relay_msg._get_all_groups", return_value=[]):
                        with patch("relay_msg._resolve_alias", return_value="unknown"):
                            with patch("relay_msg.run_sql") as mock_sql:
                                with patch("sys.stdout", new_callable=StringIO) as mock_out:
                                    relay.cmd_send("unknown", "hello")
                                    sql = mock_sql.call_args[0][0]
                                    self.assertIn("unknown", sql)
                                    self.assertIn("Message sent to unknown", mock_out.getvalue())


class TestCmdCheck(unittest.TestCase):
    """Test checking messages."""

    def test_check_prints_unread(self):
        with patch("relay_msg.ensure_schema"):
            with patch("relay_msg.detect_self", return_value="bob"):
                with patch("relay_msg._get_aliases", return_value=[]):
                    with patch("relay_msg.run_sql") as mock_sql:
                        mock_sql.side_effect = [
                            # First call: SELECT messages.
                            ["1\talice\t2026-04-14 12:00:00\thello bob\tNULL"],
                            # Second call: UPDATE read_at.
                            [],
                        ]
                        with patch("sys.stdout", new_callable=StringIO) as mock_out:
                            relay.cmd_check()
                            output = mock_out.getvalue()
                            self.assertIn("[alice]", output)
                            self.assertIn("hello bob", output)
                            # Should have called UPDATE to mark as read.
                            self.assertEqual(mock_sql.call_count, 2)

    def test_check_peek_does_not_mark_read(self):
        with patch("relay_msg.ensure_schema"):
            with patch("relay_msg.detect_self", return_value="bob"):
                with patch("relay_msg._get_aliases", return_value=[]):
                    with patch("relay_msg.run_sql") as mock_sql:
                        mock_sql.return_value = ["1\talice\t2026-04-14 12:00:00\thello\tNULL"]
                        with patch("sys.stdout", new_callable=StringIO):
                            relay.cmd_check(peek=True)
                            # Only the SELECT, no UPDATE.
                            self.assertEqual(mock_sql.call_count, 1)

    def test_check_no_messages_silent(self):
        with patch("relay_msg.ensure_schema"):
            with patch("relay_msg.detect_self", return_value="bob"):
                with patch("relay_msg._get_aliases", return_value=[]):
                    with patch("relay_msg.run_sql", return_value=[]):
                        with patch("sys.stdout", new_callable=StringIO) as mock_out:
                            relay.cmd_check()
                            self.assertEqual(mock_out.getvalue(), "")

    def test_check_history_shows_read_messages(self):
        with patch("relay_msg.ensure_schema"):
            with patch("relay_msg.detect_self", return_value="bob"):
                with patch("relay_msg._get_aliases", return_value=[]):
                    with patch("relay_msg.run_sql") as mock_sql:
                        mock_sql.return_value = [
                            "1\talice\t2026-04-14 12:00:00\told msg\t2026-04-14 12:01:00"
                        ]
                        with patch("sys.stdout", new_callable=StringIO) as mock_out:
                            relay.cmd_check(history=True)
                            output = mock_out.getvalue()
                            self.assertIn("[read]", output)
                            self.assertIn("old msg", output)

    def test_check_includes_aliases(self):
        with patch("relay_msg.ensure_schema"):
            with patch("relay_msg.detect_self", return_value="bob"):
                with patch("relay_msg._get_aliases", return_value=["robert"]):
                    with patch("relay_msg.run_sql") as mock_sql:
                        mock_sql.return_value = []
                        relay.cmd_check()
                        sql = mock_sql.call_args[0][0]
                        self.assertIn("'bob'", sql)
                        self.assertIn("'robert'", sql)


class TestCmdRegister(unittest.TestCase):
    """Test agent registration."""

    def test_register_basic(self):
        with patch("relay_msg.ensure_schema"):
            with patch("relay_msg.run_sql") as mock_sql:
                with patch("sys.stdout", new_callable=StringIO) as mock_out:
                    relay.cmd_register("alice")
                    output = mock_out.getvalue()
                    self.assertIn("Registered: alice", output)
                    # INSERT + DELETE groups + DELETE aliases = 3 calls.
                    self.assertEqual(mock_sql.call_count, 3)

    def test_register_with_groups(self):
        with patch("relay_msg.ensure_schema"):
            with patch("relay_msg.run_sql") as mock_sql:
                with patch("sys.stdout", new_callable=StringIO) as mock_out:
                    relay.cmd_register("alice", groups=["backend", "infra"])
                    output = mock_out.getvalue()
                    self.assertIn("groups: backend, infra", output)
                    # INSERT + DELETE groups + 2 group INSERTs + DELETE aliases = 5.
                    self.assertEqual(mock_sql.call_count, 5)

    def test_register_with_aliases(self):
        with patch("relay_msg.ensure_schema"):
            with patch("relay_msg.run_sql") as mock_sql:
                with patch("sys.stdout", new_callable=StringIO) as mock_out:
                    relay.cmd_register("alice", aliases=["al", "a1"])
                    output = mock_out.getvalue()
                    self.assertIn("aliases: al, a1", output)

    def test_register_with_cwd_and_transport(self):
        with patch("relay_msg.ensure_schema"):
            with patch("relay_msg.run_sql") as mock_sql:
                with patch("sys.stdout", new_callable=StringIO):
                    relay.cmd_register("alice", cwd_pattern="my-proj", transport="remote")
                    insert_sql = mock_sql.call_args_list[0][0][0]
                    self.assertIn("remote", insert_sql)
                    self.assertIn("my-proj", insert_sql)


class TestCmdUnregister(unittest.TestCase):
    """Test agent removal."""

    def test_unregister(self):
        with patch("relay_msg.ensure_schema"):
            with patch("relay_msg.run_sql") as mock_sql:
                with patch("sys.stdout", new_callable=StringIO) as mock_out:
                    relay.cmd_unregister("alice")
                    self.assertIn("Unregistered: alice", mock_out.getvalue())
                    sql = mock_sql.call_args[0][0]
                    self.assertIn("DELETE FROM relay_agents", sql)
                    self.assertIn("alice", sql)


class TestCmdList(unittest.TestCase):
    """Test agent listing."""

    def test_list_agents(self):
        with patch("relay_msg.ensure_schema"):
            with patch("relay_msg.run_sql", return_value=[
                "alice\tlocal\tmy-proj\tbackend\tal"
            ]):
                with patch("sys.stdout", new_callable=StringIO) as mock_out:
                    relay.cmd_list()
                    output = mock_out.getvalue()
                    self.assertIn("alice", output)
                    self.assertIn("local", output)
                    self.assertIn("backend", output)

    def test_list_empty(self):
        with patch("relay_msg.ensure_schema"):
            with patch("relay_msg.run_sql", return_value=[]):
                with patch("sys.stdout", new_callable=StringIO) as mock_out:
                    relay.cmd_list()
                    self.assertIn("No agents registered", mock_out.getvalue())


class TestLoadEnvFile(unittest.TestCase):
    """Test .relay-env file loading."""

    def _parse_env_line(self, value_str):
        """Apply the same parsing logic as _load_env_file to a value."""
        value = value_str.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        return value

    def test_loads_key_value_pairs(self):
        self.assertEqual(self._parse_env_line("my_test_value"), "my_test_value")
        self.assertEqual(self._parse_env_line("'quoted_value'"), "quoted_value")
        self.assertEqual(self._parse_env_line('"double_quoted"'), "double_quoted")

    def test_symmetric_quotes_stripped(self):
        self.assertEqual(self._parse_env_line("'hello world'"), "hello world")
        self.assertEqual(self._parse_env_line('"hello world"'), "hello world")

    def test_asymmetric_quotes_preserved(self):
        """Trailing quote without matching opening quote is kept (Cowork bug fix)."""
        self.assertEqual(self._parse_env_line('bar"'), 'bar"')
        self.assertEqual(self._parse_env_line("bar'"), "bar'")
        self.assertEqual(self._parse_env_line('"bar'), '"bar')

    def test_value_with_inner_quotes_preserved(self):
        """Values like python3 cmd local "{}" keep internal quotes."""
        self.assertEqual(
            self._parse_env_line('python3 /path/to/cmd local "{}"'),
            'python3 /path/to/cmd local "{}"',
        )

    def test_env_vars_take_precedence(self):
        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            f.write("_TEST_EXISTING_KEY=from_file\n")
            path = f.name
        try:
            with patch.dict(os.environ, {"_TEST_EXISTING_KEY": "from_env"}):
                # Simulate: file says from_file but env already has from_env.
                with open(path) as fh:
                    for line in fh:
                        line = line.strip()
                        if not line or line.startswith("#"):
                            continue
                        if "=" in line:
                            key, _, value = line.partition("=")
                            key = key.strip()
                            value = self._parse_env_line(value)
                            if key and key not in os.environ:
                                os.environ[key] = value
                self.assertEqual(os.environ["_TEST_EXISTING_KEY"], "from_env")
        finally:
            os.unlink(path)


class TestMain(unittest.TestCase):
    """Test CLI argument parsing."""

    def test_no_args_prints_help(self):
        with patch("sys.argv", ["relay-msg"]):
            with self.assertRaises(SystemExit) as ctx:
                relay.main()
            self.assertEqual(ctx.exception.code, 1)

    def test_unknown_command_exits(self):
        with patch("sys.argv", ["relay-msg", "bogus"]):
            with self.assertRaises(SystemExit) as ctx:
                with patch("sys.stderr", new_callable=StringIO):
                    relay.main()
            self.assertEqual(ctx.exception.code, 1)

    def test_send_missing_args_exits(self):
        with patch("sys.argv", ["relay-msg", "send", "alice"]):
            with self.assertRaises(SystemExit) as ctx:
                with patch("sys.stderr", new_callable=StringIO):
                    relay.main()
            self.assertEqual(ctx.exception.code, 1)

    def test_register_missing_name_exits(self):
        with patch("sys.argv", ["relay-msg", "register"]):
            with self.assertRaises(SystemExit) as ctx:
                with patch("sys.stderr", new_callable=StringIO):
                    relay.main()
            self.assertEqual(ctx.exception.code, 1)

    def test_register_parses_all_flags(self):
        with patch("sys.argv", [
            "relay-msg", "register", "alice",
            "--group", "backend",
            "--group", "infra",
            "--cwd", "my-proj",
            "--transport", "remote",
            "--alias", "al",
        ]):
            with patch("relay_msg.cmd_register") as mock_reg:
                relay.main()
                mock_reg.assert_called_once_with(
                    "alice",
                    groups=["backend", "infra"],
                    cwd_pattern="my-proj",
                    transport="remote",
                    aliases=["al"],
                )


class TestDecodeMysqlField(unittest.TestCase):
    """Reverse of `mysql -N -B`'s output escape encoding. Without this,
    JSON payloads containing internal escape sequences (any string with
    quotes inside, which is most JSON) come back from SELECT with doubled
    backslashes and json.loads barfs."""

    def test_plain_string_unchanged(self):
        self.assertEqual(relay._decode_mysql_field("hello world"), "hello world")

    def test_doubled_backslash_collapses(self):
        # mysql -B encodes a single '\' as '\\' on output.
        self.assertEqual(relay._decode_mysql_field("a\\\\b"), "a\\b")

    def test_escaped_quote_in_json_round_trips(self):
        # The exact failure mode dogfooding caught: payload contains \" as
        # the JSON escape for a literal quote inside a string. mysql -B
        # turns the \ into \\, giving us \\" which json.loads can't parse.
        encoded = '{"a":"\\\\"b"}'  # what mysql -B outputs
        decoded = relay._decode_mysql_field(encoded)
        self.assertEqual(decoded, '{"a":"\\"b"}')
        # And critically, json.loads now succeeds on the decoded form.
        # Note: the JSON has an escaped-quote-then-literal-content shape,
        # so we just check it parses without exception.
        parsed = json.loads(decoded)
        self.assertEqual(parsed["a"], '"b')

    def test_tab_newline_cr_decoded(self):
        self.assertEqual(relay._decode_mysql_field("a\\tb"), "a\tb")
        self.assertEqual(relay._decode_mysql_field("a\\nb"), "a\nb")
        self.assertEqual(relay._decode_mysql_field("a\\rb"), "a\rb")

    def test_unknown_escape_passes_through(self):
        # \q is not an escape mysql -B produces; we leave it alone rather
        # than silently corrupting data.
        self.assertEqual(relay._decode_mysql_field("a\\qb"), "a\\qb")

    def test_trailing_backslash_passes_through(self):
        # No char follows — emit the backslash as-is.
        self.assertEqual(relay._decode_mysql_field("a\\"), "a\\")

    def test_mysql_output_form_of_typed_payload(self):
        """End-to-end shape of the bug Conor hit: take a real JSON payload,
        apply the encoding mysql -B does on output, then verify our decoder
        produces something json.loads can parse."""
        original = {"action": "review_pr", "pr": 45, "saw": '"quoted"'}
        original_json = json.dumps(original)
        # Simulate mysql -B doubling backslashes on output.
        mysql_encoded = original_json.replace("\\", "\\\\")
        decoded = relay._decode_mysql_field(mysql_encoded)
        self.assertEqual(json.loads(decoded), original)


class TestRenderTypedBody(unittest.TestCase):
    """Display-time rendering of typed-message JSON to one-line text.
    No LLM is involved — this is the cost-saving substitute for prose."""

    def test_simple_payload(self):
        out = relay._render_typed_body(
            "task", '{"action":"review_pr","pr":45}', "pr", "45", None,
        )
        self.assertIn("[task\u2192pr:45]", out)
        self.assertIn("action=review_pr", out)
        self.assertIn("pr=45", out)

    def test_thread_id_appended(self):
        out = relay._render_typed_body(
            "task", '{"a":1}', None, None, "review-45",
        )
        self.assertIn("[task]", out)
        self.assertIn("#review-45", out)

    def test_null_payload_renders_label_only(self):
        out = relay._render_typed_body("ack", "NULL", None, None, None)
        self.assertEqual(out, "[ack]")

    def test_null_ref_fields_omitted(self):
        out = relay._render_typed_body(
            "result", '{"status":"ok"}', "NULL", "NULL", "NULL",
        )
        self.assertNotIn("\u2192", out)
        self.assertNotIn("#", out)
        self.assertIn("status=ok", out)

    def test_invalid_json_falls_back_to_raw(self):
        out = relay._render_typed_body("task", "not-json", None, None, None)
        self.assertIn("[task]", out)
        self.assertIn("not-json", out)

    def test_nested_values_serialized(self):
        out = relay._render_typed_body(
            "task", '{"params":{"k":1},"items":[1,2]}', None, None, None,
        )
        self.assertIn('params={"k":1}', out)
        self.assertIn("items=[1,2]", out)


class TestCmdSendTyped(unittest.TestCase):
    """Validation and SQL shape for typed sends."""

    def setUp(self):
        # Each patch is started here and stopped via addCleanup so the
        # exact same patcher instance is unwound — re-creating patchers
        # in a teardown helper would leak the started ones into later tests.
        patchers = [
            patch("relay_msg.ensure_schema"),
            patch("relay_msg.detect_self", return_value="alice"),
            patch("relay_msg._get_all_agents", return_value=["alice", "bob"]),
            patch("relay_msg._get_all_groups", return_value=[]),
            patch("relay_msg._resolve_alias", return_value="bob"),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)

    def test_invalid_msg_type_exits(self):
        with patch("sys.stderr", new_callable=StringIO):
            with self.assertRaises(SystemExit):
                relay.cmd_send("bob", "x", msg_type="bogus")

    def test_typed_without_payload_exits(self):
        with patch("sys.stderr", new_callable=StringIO):
            with self.assertRaises(SystemExit):
                relay.cmd_send("bob", "", msg_type="task")

    def test_invalid_json_payload_exits(self):
        with patch("sys.stderr", new_callable=StringIO):
            with self.assertRaises(SystemExit):
                relay.cmd_send("bob", "", msg_type="task", payload="not-json")

    def test_note_without_message_exits(self):
        with patch("sys.stderr", new_callable=StringIO):
            with self.assertRaises(SystemExit):
                relay.cmd_send("bob", "", msg_type="note")

    def test_typed_send_drops_message_text(self):
        """Typed sends must not store prose alongside JSON — that would
        double the sender's output tokens."""
        with patch("relay_msg.run_sql") as mock_sql:
            with patch("sys.stdout", new_callable=StringIO):
                relay.cmd_send(
                    "bob", "this prose should be dropped",
                    msg_type="task",
                    payload='{"action":"x"}',
                )
                sql = mock_sql.call_args[0][0]
                self.assertNotIn("this prose should be dropped", sql)
                self.assertIn("'task'", sql)
                self.assertIn('{"action":"x"}', sql)

    def test_typed_send_includes_refs_and_thread(self):
        with patch("relay_msg.run_sql") as mock_sql:
            with patch("sys.stdout", new_callable=StringIO):
                relay.cmd_send(
                    "bob", "",
                    msg_type="task",
                    payload='{"action":"x"}',
                    ref_type="pr",
                    ref_id="45",
                    thread_id="review-45",
                )
                sql = mock_sql.call_args[0][0]
                self.assertIn("'pr'", sql)
                self.assertIn("'45'", sql)
                self.assertIn("'review-45'", sql)

    def test_broadcast_prefix_skipped_for_typed(self):
        """[broadcast:group] prose only attaches to note sends — typed
        messages already convey routing via target + thread_id."""
        with patch("relay_msg.ensure_schema"), \
             patch("relay_msg.detect_self", return_value="alice"), \
             patch("relay_msg._get_all_agents", return_value=["alice", "bob"]), \
             patch("relay_msg._get_all_groups", return_value=["dev"]), \
             patch("relay_msg._get_group_members", return_value=["alice", "bob"]), \
             patch("relay_msg.run_sql") as mock_sql, \
             patch("sys.stdout", new_callable=StringIO):
            relay.cmd_send("dev", "", msg_type="task", payload='{"a":1}')
            sql = mock_sql.call_args[0][0]
            self.assertNotIn("[broadcast", sql)


class TestMigration(unittest.TestCase):
    """Schema migration helpers."""

    def test_existing_columns_sqlite_parses_pragma(self):
        with patch("relay_msg._is_sqlite", return_value=True):
            with patch("relay_msg.run_sql_raw", return_value=[
                "0\tid\tINTEGER\t0\t\t1",
                "1\ttarget\tTEXT\t1\t\t0",
                "2\tmessage\tTEXT\t1\t\t0",
            ]):
                cols = relay._existing_message_columns()
                self.assertEqual(cols, {"id", "target", "message"})

    def test_existing_columns_mysql_parses_information_schema(self):
        with patch("relay_msg._is_sqlite", return_value=False):
            with patch("relay_msg.run_sql_raw", return_value=[
                "id", "target", "sender", "message",
            ]):
                cols = relay._existing_message_columns()
                self.assertEqual(cols, {"id", "target", "sender", "message"})

    def test_migrate_skips_when_table_missing(self):
        """If introspection returns empty, CREATE TABLE has handled it —
        no ALTER calls."""
        with patch("relay_msg._existing_message_columns", return_value=set()):
            with patch("relay_msg.run_sql_raw") as mock_raw:
                relay._migrate_messages_table()
                mock_raw.assert_not_called()

    def test_migrate_adds_only_missing_columns(self):
        existing = {"id", "target", "sender", "message", "created_at", "read_at"}
        with patch("relay_msg._is_sqlite", return_value=True):
            with patch("relay_msg._existing_message_columns", return_value=existing):
                with patch("relay_msg.run_sql_raw") as mock_raw:
                    relay._migrate_messages_table()
                    altered = [
                        c for c in mock_raw.call_args_list
                        if c.args and "ALTER TABLE" in c.args[0]
                    ]
                    self.assertEqual(len(altered), 6)  # six new columns
                    sqls = " ".join(c.args[0] for c in altered)
                    for col in ("msg_type", "payload", "ref_type",
                                "ref_id", "thread_id", "parent_id"):
                        self.assertIn(col, sqls)

    def test_migrate_no_alters_when_all_present(self):
        existing = {
            "id", "target", "sender", "message", "created_at", "read_at",
            "msg_type", "payload", "ref_type", "ref_id", "thread_id", "parent_id",
        }
        with patch("relay_msg._is_sqlite", return_value=True):
            with patch("relay_msg._existing_message_columns", return_value=existing):
                with patch("relay_msg.run_sql_raw") as mock_raw:
                    relay._migrate_messages_table()
                    altered = [
                        c for c in mock_raw.call_args_list
                        if c.args and "ALTER TABLE" in c.args[0]
                    ]
                    self.assertEqual(altered, [])


class TestMainSendFlags(unittest.TestCase):
    """CLI parsing of typed-message flags."""

    def test_send_with_type_and_payload(self):
        with patch("sys.argv", [
            "relay-msg", "send", "bob",
            "--type", "task",
            "--payload", '{"action":"x"}',
            "--ref-type", "pr",
            "--ref-id", "45",
            "--thread-id", "review-45",
            "--parent-id", "12",
        ]):
            with patch("relay_msg.cmd_send") as mock_send:
                relay.main()
                mock_send.assert_called_once_with(
                    "bob", "",
                    msg_type="task",
                    payload='{"action":"x"}',
                    ref_type="pr",
                    ref_id="45",
                    thread_id="review-45",
                    parent_id="12",
                )

    def test_send_default_note_with_positional_message(self):
        with patch("sys.argv", ["relay-msg", "send", "bob", "hello", "world"]):
            with patch("relay_msg.cmd_send") as mock_send:
                relay.main()
                mock_send.assert_called_once_with(
                    "bob", "hello world",
                    msg_type="note",
                    payload=None,
                    ref_type=None,
                    ref_id=None,
                    thread_id=None,
                    parent_id=None,
                )


if __name__ == "__main__":
    unittest.main()
