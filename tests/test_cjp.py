"""Tests for cjp. Run: uv run python -m unittest discover -s tests -v

No test needs AWS or the real claude. The end-to-end tests use a fake claude.
"""
import contextlib
import http.server
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import cjp  # noqa: E402

ROLE = "arn:aws:iam::123456789012:role/claude-code"
MODELS = {"haiku": "jp.anthropic.claude-haiku-5-5"}
PROXY_VARS = ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "ALL_PROXY")
# Set CJP_TEST_EXE to a built cjp binary to test it too (see test_built_exe).
EXE = str(Path(os.environ["CJP_TEST_EXE"]).resolve()) if os.environ.get("CJP_TEST_EXE") else ""


def policy(**extra):
    return cjp.validate_policy(dict({"models": dict(MODELS)}, **extra))


def clean_environ():
    # A copy of the env without CJP_ keys and proxy vars, so the PC settings do not change the tests.
    return {k: v for k, v in os.environ.items()
            if not k.startswith("CJP_") and k.upper() not in PROXY_VARS}


class TempDirTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        patch = mock.patch.dict(os.environ, clean_environ(), clear=True)
        patch.start()
        self.addCleanup(patch.stop)

    def notes(self, fn, *args):
        # Run fn and return what it printed to stderr.
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            fn(*args)
        return err.getvalue()


class DotenvTest(TempDirTest):
    def write(self, env_text, policy_text="models:\n  haiku: jp.anthropic.claude-haiku-5-5\n"):
        (self.dir / "policy.yaml").write_text(policy_text, encoding="utf-8")
        (self.dir / ".env").write_text(env_text, encoding="utf-8")
        return self.dir / "policy.yaml", self.dir / ".env"

    def test_parse(self):
        text = ("# comment\n\nexport CJP_REGION=ap-northeast-3\n"
                'CJP_AWS_AUTH_REFRESH="aws sso login --profile x"\n'
                "CJP_PROXY=off   # a comment after the value\nOTHER='a b'\nEMPTY=\n")
        self.assertEqual(cjp.parse_dotenv(text), {
            "CJP_REGION": "ap-northeast-3", "CJP_AWS_AUTH_REFRESH": "aws sso login --profile x",
            "CJP_PROXY": "off", "OTHER": "a b", "EMPTY": ""})

    def test_parse_bad_line(self):
        with self.assertRaises(cjp.PolicyError):
            cjp.parse_dotenv("this is not a pair\n")

    def test_env_file_replaces_policy_values(self):
        paths = self.write(f"CJP_REGION=ap-northeast-3\nCJP_AWS_ROLE_ARN={ROLE}\nCJP_EFFORT_LEVEL=\n"
                           "CJP_AUTO_COMPACT_WINDOW=200000\nCJP_PROXY_ADD_HOSTS=pypi.org, *.Example.co.jp\n"
                           "CJP_BLOCK_GREP=yes\nNOT_OURS=1\n")
        p, keys = cjp.read_policy(*paths)
        self.assertEqual(p["region"], "ap-northeast-3")
        self.assertEqual(p["aws"], {"profile": "cjp", "role_arn": ROLE, "source_profile": "default",
                                    "auth_refresh": ""})
        self.assertEqual(p["auto_compact_window"], 200000)
        self.assertEqual(p["proxy"]["allow_hosts"], ["pypi.org", "*.example.co.jp"])
        self.assertIs(p["block_grep"], True)
        self.assertEqual(p["effort_level"], "low")  # An empty value keeps the default.
        self.assertNotIn("CJP_EFFORT_LEVEL", keys)
        self.assertIn("CJP_AWS_ROLE_ARN", keys)

    def test_add_hosts_and_remove_a_model(self):
        paths = self.write("CJP_PROXY_ADD_HOSTS=nexus.example.co.jp,pypi.org\nCJP_MODEL_SONNET=none\n",
                           "models:\n  haiku: jp.anthropic.claude-haiku-5-5\n  sonnet: jp.anthropic.claude-sonnet-4-6\n"
                           "proxy:\n  allow_hosts: [pypi.org, github.com]\n")
        p, _keys = cjp.read_policy(*paths)
        self.assertEqual(p["proxy"]["allow_hosts"], ["pypi.org", "github.com", "nexus.example.co.jp"])
        self.assertEqual(p["models"], MODELS)
        with self.assertRaises(cjp.PolicyError):  # The default model can not be removed.
            cjp.read_policy(*self.write("CJP_MODEL_HAIKU=none\n"))

    def test_templates_in_the_repo_are_valid(self):
        base, _ = cjp.read_policy(ROOT / "policy.yaml", self.dir / "no.env")
        p, keys = cjp.read_policy(ROOT / "policy.yaml", ROOT / ".env.example")
        self.assertEqual(p, base)  # The template shows the defaults. It changes nothing.
        self.assertIn("CJP_MODEL_HAIKU", keys)
        self.assertEqual((p["region"], p["default_model"]), ("ap-northeast-1", "haiku"))
        self.assertEqual(p["models"]["haiku"], "jp.anthropic.claude-haiku-5-5")
        self.assertTrue(p["proxy"]["enabled"])
        hosts = cjp.allowed_hosts(p)
        for host in ("github.com", "pypi.org", "registry.npmjs.org", "repo.maven.apache.org"):
            self.assertTrue(cjp.host_allowed(host, hosts), host)
        for host in ("api.anthropic.com", "claude.ai", "example.com", "sts.amazonaws.com"):
            self.assertFalse(cjp.host_allowed(host, hosts), host)

    def test_process_env_wins_over_file(self):
        paths = self.write("CJP_REGION=ap-northeast-3\n")
        with mock.patch.dict(os.environ, {"CJP_REGION": "ap-northeast-1"}):
            self.assertEqual(cjp.read_policy(*paths)[0]["region"], "ap-northeast-1")

    def test_no_env_file(self):
        policy_path, env_file = self.write("")
        env_file.unlink()
        p, keys = cjp.read_policy(policy_path, env_file)
        self.assertEqual((p["region"], keys), ("ap-northeast-1", []))

    def test_bad_values_stop_the_build(self):
        for text in ("CJP_REGOIN=ap-northeast-1\n",          # typo in the key
                     "CJP_PROXY=maybe\n",                      # not a bool
                     "CJP_AUTO_COMPACT_WINDOW=big\n",          # not a number
                     "CJP_REGION=us-east-1\n",                 # not Japan
                     "CJP_MODEL_SONNET=global.anthropic.claude-sonnet-5-5\n",
                     "CJP_AWS_ROLE_ARN=arn:aws:iam::1:user/me\n",
                     "CJP_PROXY_UPSTREAM=socks5://proxy:1080\n",
                     "CJP_PROXY_ADD_HOSTS=*\n",
                     "CJP_PROXY_ALLOW_HOSTS=pypi.org\n"):               # old key name
            with self.subTest(text=text), self.assertRaises(cjp.PolicyError):
                cjp.read_policy(*self.write(text))

    def test_embed_writes_the_merged_policy(self):
        paths = self.write(f"CJP_AWS_ROLE_ARN={ROLE}\n")
        out = io.StringIO()
        with mock.patch.object(cjp, "__file__", str(self.dir / "cjp.py")), contextlib.redirect_stdout(out):
            cjp.embed_policy([str(paths[0]), "--env-file", str(paths[1])])
        scope = {}
        exec((self.dir / "cjp_policy_embedded.py").read_text(encoding="utf-8"), scope)
        embedded = cjp.validate_policy(cjp.yaml.safe_load(scope["POLICY_YAML"]))
        self.assertEqual(embedded, cjp.read_policy(*paths)[0])
        self.assertEqual(embedded["aws"]["role_arn"], ROLE)
        with self.assertRaises(cjp.PolicyError):
            cjp.embed_policy([str(paths[0]), "--env-file", str(self.dir / "missing.env")])


class PolicyTest(TempDirTest):
    def test_defaults(self):
        p = policy()
        self.assertEqual(p["aws"], {"profile": "", "role_arn": "", "source_profile": "", "auth_refresh": ""})
        self.assertEqual(p["proxy"], {"enabled": True, "upstream": "", "allow_hosts": []})
        self.assertEqual(cjp.validate_policy(p), p)  # The embedded policy is checked again at start.

    def test_bad_aws_and_proxy(self):
        for extra in ({"aws": {"source_profile": "base"}},
                      {"aws": {"role_arn": ROLE, "profile": "x", "source_profile": "x"}},
                      {"aws": {"profile": "has space"}},
                      {"proxy": {"upstream": "proxy:8080"}},
                      {"proxy": {"allow_hosts": ["https://pypi.org"]}},
                      {"proxy": {"allow_hosts": "pypi.org"}}):
            with self.subTest(extra=extra), self.assertRaises(cjp.PolicyError):
                policy(**extra)

    def test_settings_for_a_profile(self):
        p = policy(aws={"role_arn": ROLE, "auth_refresh": "aws sso login --profile corp"})
        s = cjp.required_settings(p)
        self.assertEqual(s["env"]["AWS_PROFILE"], "cjp")
        self.assertEqual(s["awsAuthRefresh"], "aws sso login --profile corp")
        self.assertEqual(s["env"]["ENABLE_CLAUDEAI_MCP_SERVERS"], "false")
        self.assertIs(s["skipWebFetchPreflight"], True)
        self.assertNotIn("AWS_PROFILE", cjp.required_settings(policy())["env"])
        self.assertNotIn("awsAuthRefresh", cjp.required_settings(policy()))

    def test_env_lock(self):
        env = {"AWS_ACCESS_KEY_ID": "a", "AWS_BEARER_TOKEN_BEDROCK": "b", "AWS_PROFILE": "other",
               "CLAUDE_CODE_OAUTH_TOKEN": "c", "ANTHROPIC_API_KEY": "d", "KEEP": "e"}
        with_profile = dict(env)
        removed = cjp.clean_env(with_profile, policy(aws={"profile": "work"}))
        self.assertEqual(with_profile, {"KEEP": "e"})
        self.assertEqual(len(removed), 5)
        no_profile = dict(env)
        cjp.clean_env(no_profile, policy())  # Without a profile in the policy, AWS keys are the user's choice.
        self.assertEqual(sorted(no_profile), ["AWS_ACCESS_KEY_ID", "AWS_BEARER_TOKEN_BEDROCK", "AWS_PROFILE", "KEEP"])
        same = {"AWS_PROFILE": "work"}
        self.assertEqual(cjp.clean_env(same, policy(aws={"profile": "work"})), {})

    def test_user_settings(self):
        path = self.dir / "settings.json"
        path.write_text(json.dumps({"awsCredentialExport": "get-keys", "theme": "dark",
                                    "env": {"AWS_SECRET_ACCESS_KEY": "x", "HTTPS_PROXY": "http://corp:8080"}}))
        self.notes(cjp.ensure_settings, path, policy(aws={"profile": "work"}))
        data = json.loads(path.read_text())
        self.assertNotIn("awsCredentialExport", data)
        self.assertEqual(data["theme"], "dark")
        self.assertEqual(data["env"]["AWS_PROFILE"], "work")
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", data["env"])
        self.assertEqual(data["env"]["HTTPS_PROXY"], "http://corp:8080")  # Kept: it is the upstream proxy.

    def test_iam_account_from_role(self):
        p = policy(aws={"role_arn": ROLE})
        self.assertIn(":123456789012:", json.dumps(cjp.iam_policy(p, "")))
        with self.assertRaises(cjp.PolicyError):
            cjp.iam_policy(policy(), "")


class AwsProfileTest(TempDirTest):
    def setUp(self):
        super().setUp()
        self.config = self.dir / "aws" / "config"
        os.environ["AWS_CONFIG_FILE"] = str(self.config)
        os.environ["AWS_SHARED_CREDENTIALS_FILE"] = str(self.dir / "aws" / "credentials")

    def test_creates_the_role_profile_once(self):
        p = policy(aws={"role_arn": ROLE})
        first = self.notes(cjp.ensure_aws_profile, p)
        self.assertIn("added the AWS profile 'cjp'", first)
        self.assertIn("'default' (the login for the role) was not found", first)
        text = self.config.read_text()
        self.assertEqual(cjp.aws_profiles(self.config)["cjp"],
                         {"role_arn": ROLE, "source_profile": "default", "region": "ap-northeast-1"})
        (self.dir / "aws" / "credentials").write_text("[default]\naws_access_key_id = x\n")
        self.assertEqual(self.notes(cjp.ensure_aws_profile, p), "")
        self.assertEqual(self.config.read_text(), text)

    def test_keeps_other_profiles_and_crlf(self):
        self.config.parent.mkdir()
        self.config.write_bytes(b"[default]\r\nregion = us-east-1\r\n\r\n[sso-session corp]\r\nsso_region = ap-northeast-1\r\n")
        self.notes(cjp.ensure_aws_profile, policy(aws={"role_arn": ROLE, "profile": "work"}))
        raw = self.config.read_bytes()
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))
        profiles = cjp.aws_profiles(self.config)
        self.assertEqual(profiles["default"]["region"], "us-east-1")
        self.assertEqual(profiles["work"]["role_arn"], ROLE)

    def test_another_role_stops_cjp(self):
        self.config.parent.mkdir()
        self.config.write_text("[profile cjp]\nrole_arn = arn:aws:iam::999999999999:role/admin\nsource_profile = default\n")
        with self.assertRaises(SystemExit):
            cjp.ensure_aws_profile(policy(aws={"role_arn": ROLE}))

    def test_profile_without_role_is_only_checked(self):
        self.assertIn("was not found", self.notes(cjp.ensure_aws_profile, policy(aws={"profile": "work"})))
        self.assertFalse(self.config.exists())
        self.assertEqual(self.notes(cjp.ensure_aws_profile, policy()), "")


class Target(http.server.BaseHTTPRequestHandler):
    # The "internet" for the proxy tests. It answers with the request it saw.
    protocol_version = "HTTP/1.1"
    seen = []

    def answer(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        Target.seen.append(self.path)
        text = f"{self.command} {self.path} {body.decode()}".encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(text)))
        self.end_headers()
        self.wfile.write(text)

    do_GET = do_POST = answer

    def log_message(self, *args):
        pass


def start_target():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Target)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def stop_target(server):
    server.shutdown()
    server.server_close()


def talk(port, data, more=b""):
    # Send bytes to a local port. Return everything that comes back until the other side closes.
    with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
        s.sendall(data)
        if more:
            head = s.recv(4096)  # The answer to CONNECT.
            s.sendall(more)
        else:
            head = b""
        s.shutdown(socket.SHUT_WR)
        chunks = [head]
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    return b"".join(chunks).decode("latin-1")


class ProxyTest(TempDirTest):
    # 127.0.0.1 is the allowed host. "localhost" and all other names are not allowed.
    def setUp(self):
        super().setUp()
        Target.seen.clear()
        self.target = start_target()
        self.addCleanup(stop_target, self.target)
        self.tport = self.target.server_address[1]
        self.log = self.dir / "proxy.log"
        self.proxy = self.start(["127.0.0.1"], None)

    def start(self, allow, upstream):
        proxy = cjp.Proxy(allow, upstream, self.log)
        threading.Thread(target=proxy.serve, daemon=True).start()
        self.addCleanup(proxy.sock.close)
        return proxy

    def test_host_patterns(self):
        allow = ["pypi.org", "*.example.co.jp"]
        for host, ok in (("pypi.org", True), ("PyPI.org.", True), ("evil-pypi.org", False),
                         ("pypi.org.evil.com", False), ("a.b.example.co.jp", True),
                         ("example.co.jp", False), ("badexample.co.jp", False)):
            self.assertEqual(cjp.host_allowed(host, allow), ok, host)

    def test_default_hosts_are_japan_only(self):
        hosts = cjp.allowed_hosts(policy(proxy={"allow_hosts": ["pypi.org"]}))
        self.assertIn("bedrock-runtime.ap-northeast-1.amazonaws.com", hosts)
        self.assertIn("sts.ap-northeast-3.amazonaws.com", hosts)
        self.assertEqual([h for h in hosts if "ap-northeast-" not in h], ["pypi.org"])
        for host in ("api.anthropic.com", "claude.ai", "bedrock-runtime.us-east-1.amazonaws.com",
                     "sts.amazonaws.com", "github.com"):
            self.assertFalse(cjp.host_allowed(host, hosts), host)

    def test_connect_allowed(self):
        out = talk(self.proxy.port, f"CONNECT 127.0.0.1:{self.tport} HTTP/1.1\r\n\r\n".encode(),
                   b"GET /in-tunnel HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        self.assertTrue(out.startswith("HTTP/1.1 200 Connection established"), out)
        self.assertIn("GET /in-tunnel", out)

    def test_connect_blocked_and_logged(self):
        for target in (f"localhost:{self.tport}", "api.anthropic.com:443", "claude.ai:443"):
            out = talk(self.proxy.port, f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
            self.assertTrue(out.startswith("HTTP/1.1 403 "), out)
            self.assertIn("not in the allowed hosts", out)
        self.assertEqual(Target.seen, [])
        log = self.log.read_text()
        self.assertIn("BLOCK api.anthropic.com:443", log)
        talk(self.proxy.port, b"CONNECT api.anthropic.com:443 HTTP/1.1\r\n\r\n")
        self.assertEqual(self.log.read_text(), log)  # The same event is logged once.

    def test_plain_http(self):
        url = f"http://127.0.0.1:{self.tport}/page?x=1"
        out = talk(self.proxy.port, f"GET {url} HTTP/1.1\r\nHost: x\r\nProxy-Connection: keep-alive\r\n\r\n".encode())
        self.assertIn("200 OK", out)
        self.assertTrue(out.endswith("GET /page?x=1 "), out)
        out = talk(self.proxy.port, f"POST {url} HTTP/1.1\r\nHost: x\r\nContent-Length: 5\r\n\r\nhello".encode())
        self.assertTrue(out.endswith("POST /page?x=1 hello"), out)
        out = talk(self.proxy.port, f"GET http://localhost:{self.tport}/ HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        self.assertTrue(out.startswith("HTTP/1.1 403 "), out)
        out = talk(self.proxy.port, b"GET /no-host HTTP/1.1\r\nHost: x\r\n\r\n")
        self.assertTrue(out.startswith("HTTP/1.1 400 "), out)

    def test_second_request_on_one_connection_is_dropped(self):
        first = f"GET http://127.0.0.1:{self.tport}/first HTTP/1.1\r\nHost: x\r\n\r\n"
        second = f"GET http://127.0.0.1:{self.tport}/second HTTP/1.1\r\nHost: x\r\n\r\n"
        out = talk(self.proxy.port, (first + second).encode())
        self.assertEqual(out.count("HTTP/1.0 200") + out.count("HTTP/1.1 200"), 1, out)
        self.assertEqual(Target.seen, ["/first"])

    def fake_upstream(self, answer):
        # A corporate proxy: it records the request, sends the answer, then echoes.
        seen = []
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(5)
        self.addCleanup(server.close)

        def run():
            try:
                conn, _ = server.accept()
            except OSError:
                return
            with conn:
                head, _rest = cjp.read_head(conn)
                seen.append(head)
                conn.sendall(answer)
                while True:
                    data = conn.recv(4096)
                    if not data:
                        break
                    conn.sendall(data)

        threading.Thread(target=run, daemon=True).start()
        return server.getsockname()[1], seen

    def test_goes_through_the_upstream_proxy(self):
        port, seen = self.fake_upstream(b"HTTP/1.1 200 OK\r\n\r\n")
        proxy = self.start(["bedrock-runtime.ap-northeast-1.amazonaws.com"], ("127.0.0.1", port, "dXNlcjpwYXNz"))
        with mock.patch.object(cjp, "proxy_bypass", return_value=False):
            out = talk(proxy.port, b"CONNECT bedrock-runtime.ap-northeast-1.amazonaws.com:443 HTTP/1.1\r\n\r\n", b"ping")
        self.assertTrue(out.endswith("ping"), out)
        self.assertIn("CONNECT bedrock-runtime.ap-northeast-1.amazonaws.com:443 HTTP/1.1", seen[0])
        self.assertIn("Proxy-Authorization: Basic dXNlcjpwYXNz", seen[0])

    def test_upstream_never_sees_a_blocked_host(self):
        port, seen = self.fake_upstream(b"HTTP/1.1 200 OK\r\n\r\n")
        proxy = self.start(["127.0.0.1"], ("127.0.0.1", port, ""))
        with mock.patch.object(cjp, "proxy_bypass", return_value=False):
            out = talk(proxy.port, b"CONNECT example.com:443 HTTP/1.1\r\n\r\n")
        self.assertTrue(out.startswith("HTTP/1.1 403 "), out)
        self.assertEqual(seen, [])

    def test_upstream_refuses(self):
        port, _seen = self.fake_upstream(b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n")
        proxy = self.start(["example.com"], ("127.0.0.1", port, ""))
        with mock.patch.object(cjp, "proxy_bypass", return_value=False):
            out = talk(proxy.port, b"CONNECT example.com:443 HTTP/1.1\r\n\r\n")
        self.assertTrue(out.startswith("HTTP/1.1 502 "), out)
        self.assertIn("407", out)
        self.assertIn("FAIL example.com:443", self.log.read_text())

    def test_find_upstream_order(self):
        settings = {"env": {"HTTPS_PROXY": "http://from-settings:3128"}}
        with mock.patch.object(cjp.urllib.request, "getproxies", return_value={}):
            self.assertIsNone(cjp.find_upstream(policy(), {}))
            self.assertEqual(cjp.find_upstream(policy(), settings), ("from-settings", 3128, ""))
            os.environ["HTTP_PROXY"] = "http://from-env:8080"
            self.assertEqual(cjp.find_upstream(policy(), settings), ("from-env", 8080, ""))
            p = policy(proxy={"upstream": "http://user:p%40ss@from-policy:8888"})
            self.assertEqual(cjp.find_upstream(p, settings), ("from-policy", 8888, "dXNlcjpwQHNz"))
            os.environ["HTTPS_PROXY"] = "socks5://nope:1080"  # Not http: ignored with a note.
            self.assertIn("socks5", self.notes(lambda: self.assertEqual(
                cjp.find_upstream(policy(), settings), ("from-env", 8080, ""))))


FAKE_CLAUDE = r'''
import json, os, socket, sys

def ask(port, request):
    with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
        s.sendall(request.encode())
        return s.recv(4096).decode("latin-1").split("\r\n")[0]

out = {"argv": sys.argv[1:], "env": dict(os.environ)}
if "--settings" in sys.argv:
    out["flag_file"] = sys.argv[sys.argv.index("--settings") + 1]
    out["flag"] = json.load(open(out["flag_file"], encoding="utf-8"))
    port = int(os.environ["HTTPS_PROXY"].rsplit(":", 1)[1])
    target = os.environ["FAKE_TARGET"]
    out["blocked"] = ask(port, "CONNECT api.anthropic.com:443 HTTP/1.1\r\n\r\n")
    out["allowed"] = ask(port, f"GET http://{target}/ok HTTP/1.1\r\nHost: x\r\n\r\n")
json.dump(out, open(os.environ["FAKE_OUT"], "w", encoding="utf-8"))
sys.exit(7)
'''


class EndToEndTest(TempDirTest):
    # Start cjp.py as a user would, with a fake claude and a fake home directory.
    def setUp(self):
        super().setUp()
        script = self.dir / "fake_claude.py"
        script.write_text(FAKE_CLAUDE, encoding="utf-8")
        if cjp.IS_WIN:
            self.claude = self.dir / "claude.cmd"
            self.claude.write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
        else:
            self.claude = self.dir / "claude"
            self.claude.write_text(f"#!/bin/sh\nexec '{sys.executable}' '{script}' \"$@\"\n", encoding="utf-8")
            self.claude.chmod(0o755)
        self.target = start_target()
        self.addCleanup(stop_target, self.target)
        self.home = self.dir / "home"
        self.home.mkdir()
        self.out = self.dir / "out.json"

    def run_cjp(self, env_text, extra_env=None, exe=""):
        (self.dir / ".env").write_text(env_text, encoding="utf-8")
        env = dict(clean_environ(), **{
            "HOME": str(self.home), "USERPROFILE": str(self.home), "LOCALAPPDATA": str(self.home / "local"),
            "XDG_CACHE_HOME": str(self.home / "cache"), "CLAUDE_CONFIG_DIR": str(self.home / ".claude"),
            "AWS_CONFIG_FILE": str(self.home / ".aws" / "config"),
            "AWS_SHARED_CREDENTIALS_FILE": str(self.home / ".aws" / "credentials"),
            "CJP_ENV_FILE": str(self.dir / ".env"), "CJP_SKIP_SETUP": "1", "CLAUDE_REAL_BIN": str(self.claude),
            "FAKE_OUT": str(self.out), "FAKE_TARGET": f"127.0.0.1:{self.target.server_address[1]}",
        }, **(extra_env or {}))
        cmd = [exe] if exe else [sys.executable, str(ROOT / "cjp.py")]
        done = subprocess.run(cmd + ["-p", "hi"], env=env, cwd=self.home,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)
        return done, json.loads(self.out.read_text(encoding="utf-8")) if self.out.exists() else None

    def test_proxy_and_role(self):
        done, out = self.run_cjp(
            f"CJP_AWS_ROLE_ARN={ROLE}\nCJP_PROXY_ADD_HOSTS=127.0.0.1\n",
            {"AWS_ACCESS_KEY_ID": "AKIAEXAMPLE", "AWS_SECRET_ACCESS_KEY": "x", "CLAUDE_CODE_OAUTH_TOKEN": "x",
             "ANTHROPIC_API_KEY": "x", "ALL_PROXY": "socks5://corp:1080"})
        self.assertEqual(done.returncode, 7, done.stderr)  # The exit code of claude comes back.
        self.assertEqual(out["argv"], ["--settings", out["flag_file"], "-p", "hi"])
        env = out["env"]
        proxy_url = env["HTTPS_PROXY"]
        self.assertRegex(proxy_url, r"^http://127\.0\.0\.1:\d+$")
        self.assertEqual(env["HTTP_PROXY"], proxy_url)
        self.assertEqual(env["NO_PROXY"], cjp.LOOPBACK)
        for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
            self.assertEqual(out["flag"]["env"][name], proxy_url)
        self.assertTrue(out["blocked"].startswith("HTTP/1.1 403"), out["blocked"])
        self.assertIn(" 200 ", out["allowed"])
        self.assertEqual(env["AWS_PROFILE"], "cjp")
        self.assertEqual(env["CLAUDE_CODE_USE_BEDROCK"], "1")
        for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "CLAUDE_CODE_OAUTH_TOKEN",
                     "ANTHROPIC_API_KEY", "ALL_PROXY"):
            self.assertNotIn(name, env)
        self.assertEqual(cjp.aws_profiles(self.home / ".aws" / "config")["cjp"]["role_arn"], ROLE)
        settings = json.loads((self.home / ".claude" / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(settings["env"]["AWS_PROFILE"], "cjp")
        self.assertNotIn("HTTPS_PROXY", settings["env"])  # The proxy port changes on each start.
        logs = list(self.home.rglob("proxy.log"))
        self.assertEqual(len(logs), 1)
        self.assertIn("BLOCK api.anthropic.com:443", logs[0].read_text())
        flag = Path(out["flag_file"])
        for _ in range(100):  # On POSIX the proxy process removes the file after claude ends.
            if not flag.exists():
                break
            time.sleep(0.1)
        self.assertFalse(flag.exists())

    @unittest.skipUnless(EXE, "set CJP_TEST_EXE to a built cjp binary")
    def test_built_exe(self):
        # The exe has its policy inside. A .env file and CJP_ env vars must not change it.
        done, out = self.run_cjp("CJP_PROXY=false\n", {"CJP_PROXY": "false", "ANTHROPIC_API_KEY": "x"}, exe=EXE)
        self.assertEqual(done.returncode, 7, done.stderr)
        self.assertEqual(out["argv"], ["--settings", out["flag_file"], "-p", "hi"])
        self.assertRegex(out["env"]["HTTPS_PROXY"], r"^http://127\.0\.0\.1:\d+$")
        self.assertTrue(out["blocked"].startswith("HTTP/1.1 403"), out["blocked"])
        self.assertEqual(out["env"]["CLAUDE_CODE_USE_BEDROCK"], "1")
        self.assertNotIn("ANTHROPIC_API_KEY", out["env"])
        flag = Path(out["flag_file"])
        for _ in range(100):
            if not flag.exists():
                break
            time.sleep(0.1)
        self.assertFalse(flag.exists())

    def test_proxy_off(self):
        done, out = self.run_cjp("CJP_PROXY=false\n", {"AWS_ACCESS_KEY_ID": "AKIAEXAMPLE"})
        self.assertEqual(done.returncode, 7, done.stderr)
        self.assertEqual(out["argv"], ["-p", "hi"])
        self.assertNotIn("HTTPS_PROXY", out["env"])
        self.assertEqual(out["env"]["AWS_ACCESS_KEY_ID"], "AKIAEXAMPLE")  # No profile in the policy.
        self.assertFalse((self.home / ".aws").exists())

    def test_wrong_role_in_aws_config_stops_cjp(self):
        (self.home / ".aws").mkdir()
        (self.home / ".aws" / "config").write_text("[profile cjp]\nrole_arn = arn:aws:iam::999999999999:role/admin\n")
        done, out = self.run_cjp(f"CJP_AWS_ROLE_ARN={ROLE}\n")
        self.assertNotEqual(done.returncode, 0)
        self.assertIsNone(out)  # claude did not start.
        self.assertIn("does not use the role of the policy", done.stderr)


if __name__ == "__main__":
    unittest.main()
