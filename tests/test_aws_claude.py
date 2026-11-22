"""Tests for aws-claude. Run: uv run python -m unittest discover -s tests -v

No test needs AWS or the real claude. The end-to-end tests use a fake claude.
"""
import contextlib
import http.server
import io
import json
import os
import re
import shutil
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
import aws_claude as app  # noqa: E402

ROLE = "arn:aws:iam::123456789012:role/claude-code"
MODELS = {"haiku": "jp.anthropic.claude-haiku-5-5"}
PROXY_VARS = ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "ALL_PROXY")
# Set AWS_CLAUDE_TEST_EXE to a built aws-claude binary to test it too (see test_built_exe).
EXE = str(Path(os.environ["AWS_CLAUDE_TEST_EXE"]).resolve()) if os.environ.get("AWS_CLAUDE_TEST_EXE") else ""
# Set AWS_CLAUDE_TEST_FIRST_RUN=1 to test a first start: it installs uv, rtk, and graphify
# from the network into a temp home. The workflow does this on Windows and Linux.
FIRST_RUN = os.environ.get("AWS_CLAUDE_TEST_FIRST_RUN") == "1"


def policy(**extra):
    return app.validate_policy(dict({"models": dict(MODELS)}, **extra))


def clean_environ():
    # A copy of the env without AWS_CLAUDE_ keys and proxy vars, so the PC settings do not change the tests.
    return {k: v for k, v in os.environ.items()
            if not k.startswith("AWS_CLAUDE_") and k.upper() not in PROXY_VARS}


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
        text = ("# comment\n\nexport AWS_CLAUDE_REGION=ap-northeast-3\n"
                'AWS_CLAUDE_AUTH_REFRESH="aws sso login --profile x"\n'
                "AWS_CLAUDE_PROXY=off   # a comment after the value\nOTHER='a b'\nEMPTY=\n")
        self.assertEqual(app.parse_dotenv(text), {
            "AWS_CLAUDE_REGION": "ap-northeast-3", "AWS_CLAUDE_AUTH_REFRESH": "aws sso login --profile x",
            "AWS_CLAUDE_PROXY": "off", "OTHER": "a b", "EMPTY": ""})

    def test_parse_bad_line(self):
        with self.assertRaises(app.PolicyError):
            app.parse_dotenv("this is not a pair\n")

    def test_env_file_replaces_policy_values(self):
        paths = self.write(f"AWS_CLAUDE_REGION=ap-northeast-3\nAWS_CLAUDE_ROLE_ARN={ROLE}\nAWS_CLAUDE_EFFORT_LEVEL=\n"
                           "AWS_CLAUDE_AUTO_COMPACT_WINDOW=200000\nAWS_CLAUDE_PROXY_ADD_HOSTS=pypi.org, *.Example.co.jp\n"
                           "AWS_CLAUDE_BLOCK_GREP=yes\nNOT_OURS=1\n")
        p, keys = app.read_policy(*paths)
        self.assertEqual(p["region"], "ap-northeast-3")
        self.assertEqual(p["aws"], {"profile": "aws-claude", "role_arn": ROLE, "source_profile": "default",
                                    "auth_refresh": ""})
        self.assertEqual(p["auto_compact_window"], 200000)
        self.assertEqual(p["proxy"]["allow_hosts"], ["pypi.org", "*.example.co.jp"])
        self.assertIs(p["block_grep"], True)
        self.assertEqual(p["effort_level"], "low")  # An empty value keeps the default.
        self.assertNotIn("AWS_CLAUDE_EFFORT_LEVEL", keys)
        self.assertIn("AWS_CLAUDE_ROLE_ARN", keys)

    def test_add_hosts_and_remove_a_model(self):
        paths = self.write("AWS_CLAUDE_PROXY_ADD_HOSTS=nexus.example.co.jp,pypi.org\nAWS_CLAUDE_MODEL_SONNET=none\n",
                           "models:\n  haiku: jp.anthropic.claude-haiku-5-5\n  sonnet: jp.anthropic.claude-sonnet-4-6\n"
                           "proxy:\n  allow_hosts: [pypi.org, github.com]\n")
        p, _keys = app.read_policy(*paths)
        self.assertEqual(p["proxy"]["allow_hosts"], ["pypi.org", "github.com", "nexus.example.co.jp"])
        self.assertEqual(p["models"], MODELS)
        with self.assertRaises(app.PolicyError):  # The default model can not be removed.
            app.read_policy(*self.write("AWS_CLAUDE_MODEL_HAIKU=none\n"))

    def test_templates_in_the_repo_are_valid(self):
        base, _ = app.read_policy(ROOT / "policy.yaml", self.dir / "no.env")
        p, keys = app.read_policy(ROOT / "policy.yaml", ROOT / ".env.example")
        self.assertEqual(p, base)  # The template shows the defaults. It changes nothing.
        self.assertIn("AWS_CLAUDE_MODEL_HAIKU", keys)
        self.assertEqual((p["region"], p["default_model"]), ("ap-northeast-1", "haiku"))
        self.assertEqual(p["models"]["haiku"], "jp.anthropic.claude-haiku-5-5")
        self.assertTrue(p["proxy"]["enabled"])
        hosts = app.allowed_hosts(p)
        for host in ("github.com", "pypi.org", "registry.npmjs.org", "repo.maven.apache.org"):
            self.assertTrue(app.host_allowed(host, hosts), host)
        for host in ("api.anthropic.com", "claude.ai", "example.com", "sts.amazonaws.com"):
            self.assertFalse(app.host_allowed(host, hosts), host)

    def test_process_env_wins_over_file(self):
        paths = self.write("AWS_CLAUDE_REGION=ap-northeast-3\n")
        with mock.patch.dict(os.environ, {"AWS_CLAUDE_REGION": "ap-northeast-1"}):
            self.assertEqual(app.read_policy(*paths)[0]["region"], "ap-northeast-1")

    def test_no_env_file(self):
        policy_path, env_file = self.write("")
        env_file.unlink()
        p, keys = app.read_policy(policy_path, env_file)
        self.assertEqual((p["region"], keys), ("ap-northeast-1", []))

    def test_bad_values_stop_the_build(self):
        for text in ("AWS_CLAUDE_REGOIN=ap-northeast-1\n",          # typo in the key
                     "AWS_CLAUDE_PROXY=maybe\n",                      # not a bool
                     "AWS_CLAUDE_AUTO_COMPACT_WINDOW=big\n",          # not a number
                     "AWS_CLAUDE_REGION=us-east-1\n",                 # not Japan
                     "AWS_CLAUDE_MODEL_SONNET=global.anthropic.claude-sonnet-5-5\n",
                     "AWS_CLAUDE_ROLE_ARN=arn:aws:iam::1:user/me\n",
                     "AWS_CLAUDE_PROXY_UPSTREAM=socks5://proxy:1080\n",
                     "AWS_CLAUDE_PROXY_ADD_HOSTS=*\n",
                     "AWS_CLAUDE_PROXY_ALLOW_HOSTS=pypi.org\n"):               # old key name
            with self.subTest(text=text), self.assertRaises(app.PolicyError):
                app.read_policy(*self.write(text))

    def test_embed_writes_the_merged_policy(self):
        paths = self.write(f"AWS_CLAUDE_ROLE_ARN={ROLE}\n")
        out = io.StringIO()
        with mock.patch.object(app, "__file__", str(self.dir / "aws_claude.py")), contextlib.redirect_stdout(out):
            app.embed_policy([str(paths[0]), "--env-file", str(paths[1])])
        scope = {}
        exec((self.dir / "aws_claude_policy_embedded.py").read_text(encoding="utf-8"), scope)
        embedded = app.validate_policy(app.yaml.safe_load(scope["POLICY_YAML"]))
        self.assertEqual(embedded, app.read_policy(*paths)[0])
        self.assertEqual(embedded["aws"]["role_arn"], ROLE)
        with self.assertRaises(app.PolicyError):
            app.embed_policy([str(paths[0]), "--env-file", str(self.dir / "missing.env")])


class PolicyTest(TempDirTest):
    def test_defaults(self):
        p = policy()
        self.assertEqual(p["aws"], {"profile": "", "role_arn": "", "source_profile": "", "auth_refresh": ""})
        self.assertEqual(p["proxy"], {"enabled": True, "upstream": "", "allow_hosts": []})
        self.assertEqual(app.validate_policy(p), p)  # The embedded policy is checked again at start.

    def test_bad_aws_and_proxy(self):
        for extra in ({"aws": {"source_profile": "base"}},
                      {"aws": {"role_arn": ROLE, "profile": "x", "source_profile": "x"}},
                      {"aws": {"profile": "has space"}},
                      {"proxy": {"upstream": "proxy:8080"}},
                      {"proxy": {"allow_hosts": ["https://pypi.org"]}},
                      {"proxy": {"allow_hosts": "pypi.org"}}):
            with self.subTest(extra=extra), self.assertRaises(app.PolicyError):
                policy(**extra)

    def test_settings_for_a_profile(self):
        p = policy(aws={"role_arn": ROLE, "auth_refresh": "aws sso login --profile corp"})
        s = app.required_settings(p)
        self.assertEqual(s["env"]["AWS_PROFILE"], "aws-claude")
        self.assertEqual(s["awsAuthRefresh"], "aws sso login --profile corp")
        self.assertEqual(s["env"]["ENABLE_CLAUDEAI_MCP_SERVERS"], "false")
        self.assertIs(s["skipWebFetchPreflight"], True)
        self.assertNotIn("AWS_PROFILE", app.required_settings(policy())["env"])
        self.assertNotIn("awsAuthRefresh", app.required_settings(policy()))

    def test_env_lock(self):
        env = {"AWS_ACCESS_KEY_ID": "a", "AWS_BEARER_TOKEN_BEDROCK": "b", "AWS_PROFILE": "other",
               "CLAUDE_CODE_OAUTH_TOKEN": "c", "ANTHROPIC_API_KEY": "d", "KEEP": "e"}
        with_profile = dict(env)
        removed = app.clean_env(with_profile, policy(aws={"profile": "work"}))
        self.assertEqual(with_profile, {"KEEP": "e"})
        self.assertEqual(len(removed), 5)
        no_profile = dict(env)
        app.clean_env(no_profile, policy())  # Without a profile in the policy, AWS keys are the user's choice.
        self.assertEqual(sorted(no_profile), ["AWS_ACCESS_KEY_ID", "AWS_BEARER_TOKEN_BEDROCK", "AWS_PROFILE", "KEEP"])
        same = {"AWS_PROFILE": "work"}
        self.assertEqual(app.clean_env(same, policy(aws={"profile": "work"})), {})

    def test_bedrock_endpoint_must_be_aws_in_japan(self):
        good = ("https://bedrock-runtime.ap-northeast-1.amazonaws.com",
                "https://bedrock-runtime.ap-northeast-3.amazonaws.com/",
                "https://vpce-0abc-xyz.bedrock-runtime.ap-northeast-1.vpce.amazonaws.com")
        bad = ("https://bedrock-runtime.us-east-1.amazonaws.com",
               "https://x.ap-northeast-1.evil.example",             # the region is only part of the name
               "https://evil.example/.ap-northeast-1.amazonaws.com",  # the region is only in the path
               "https://bedrock-runtime.ap-northeast-1.amazonaws.com.evil.example",
               "http://bedrock-runtime.ap-northeast-1.amazonaws.com",
               "https://gateway.example.co.jp")
        for url in good:
            self.assertIsNone(app.env_violation("ANTHROPIC_BEDROCK_BASE_URL", url, policy()), url)
        for url in bad:
            self.assertIsNotNone(app.env_violation("ANTHROPIC_BEDROCK_BASE_URL", url, policy()), url)

    def test_user_settings(self):
        path = self.dir / "settings.json"
        path.write_text(json.dumps({"awsCredentialExport": "get-keys", "theme": "dark",
                                    "env": {"AWS_SECRET_ACCESS_KEY": "x", "HTTPS_PROXY": "http://corp:8080"}}))
        self.notes(app.ensure_settings, path, policy(aws={"profile": "work"}))
        data = json.loads(path.read_text())
        self.assertNotIn("awsCredentialExport", data)
        self.assertEqual(data["theme"], "dark")
        self.assertEqual(data["env"]["AWS_PROFILE"], "work")
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", data["env"])
        self.assertEqual(data["env"]["HTTPS_PROXY"], "http://corp:8080")  # Kept: it is the upstream proxy.

    def test_iam_account_from_role(self):
        p = policy(aws={"role_arn": ROLE})
        self.assertIn(":123456789012:", json.dumps(app.iam_policy(p, "")))
        with self.assertRaises(app.PolicyError):
            app.iam_policy(policy(), "")


class CfnLoader(app.yaml.SafeLoader):
    # Reads CloudFormation short tags: !Ref X -> {"Ref": X}, !Sub ... -> {"Fn::Sub": ...}.
    pass


def _cfn_tag(loader, suffix, node):
    if isinstance(node, app.yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, app.yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    else:
        value = loader.construct_mapping(node, deep=True)
    if suffix == "Ref":
        return {"Ref": value}
    if suffix == "GetAtt" and isinstance(value, str):
        value = value.split(".", 1)
    return {"Fn::" + suffix: value}


CfnLoader.add_multi_constructor("!", _cfn_tag)
NO_VALUE = object()


class Template:
    # cloudformation/aws-claude.yml with parameter values, resolved like CloudFormation does.
    PATH = ROOT / "cloudformation" / "aws-claude.yml"

    def __init__(self, **params):
        self.doc = app.yaml.load(self.PATH.read_text(encoding="utf-8"), Loader=CfnLoader)
        values = {name: spec.get("Default") for name, spec in self.doc["Parameters"].items()}
        values.update(params)
        for name, spec in self.doc["Parameters"].items():
            if spec["Type"] == "CommaDelimitedList" and isinstance(values[name], str):
                values[name] = values[name].split(",")
        self.values = dict(values, **{"AWS::AccountId": "123456789012", "AWS::Partition": "aws",
                                      "AWS::StackName": "aws-claude", "AWS::NoValue": NO_VALUE})
        self.conditions = {}
        for name, expr in self.doc["Conditions"].items():
            self.conditions[name] = self.resolve(expr)

    def resource(self, name):
        return self.resolve(self.doc["Resources"][name]["Properties"])

    def resolve(self, node):
        if isinstance(node, list):
            items = [self.resolve(x) for x in node]
            return [x for x in items if x is not NO_VALUE]
        if not isinstance(node, dict):
            return node
        if len(node) == 1:
            (key, arg), = node.items()
            if key == "Ref":  # A resource gives a placeholder. Only parameters have values here.
                return self.values[arg] if arg in self.values else f"<{arg}>"
            if key == "Fn::Sub":
                text, extra = (arg[0], self.resolve(arg[1])) if isinstance(arg, list) else (arg, {})
                return re.sub(r"\$\{([^}]+)\}", lambda m: str(extra.get(m[1], self.values.get(m[1]))), text)
            if key == "Fn::If":
                return self.resolve(arg[1] if self.conditions[arg[0]] else arg[2])
            if key == "Fn::Select":
                return self.resolve(arg[1])[int(arg[0])]
            if key == "Fn::Split":
                return self.resolve(arg[1]).split(arg[0])
            if key == "Fn::Join":
                return arg[0].join(self.resolve(arg[1]))
            if key == "Fn::Equals":
                return self.resolve(arg[0]) == self.resolve(arg[1])
            if key == "Fn::Not":
                return not self.resolve(arg[0])
        out = {k: self.resolve(v) for k, v in node.items()}
        return {k: v for k, v in out.items() if v is not NO_VALUE}


class CloudFormationTest(unittest.TestCase):
    def setUp(self):
        self.policy, _ = app.read_policy(ROOT / "policy.yaml", ROOT / "no-such.env")

    def test_defaults_match_policy_yaml(self):
        params = Template().values
        self.assertEqual(params["BedrockRegion"], self.policy["region"])
        self.assertEqual({"haiku": params["HaikuProfileId"], "sonnet": params["SonnetProfileId"]},
                         self.policy["models"], "policy.yaml changed: update the defaults in cloudformation/aws-claude.yml")

    def test_role_policy_is_the_iam_policy_output(self):
        doc = Template().resource("BedrockJapanPolicy")["PolicyDocument"]
        self.assertEqual(doc, app.iam_policy(self.policy, "123456789012"))

    def test_haiku_only_in_osaka(self):
        doc = Template(SonnetProfileId="", BedrockRegion="ap-northeast-3").resource("BedrockJapanPolicy")
        p = app.validate_policy({"region": "ap-northeast-3", "models": {"haiku": self.policy["models"]["haiku"]}})
        self.assertEqual(doc["PolicyDocument"], app.iam_policy(p, "123456789012"))

    def test_trust_policy(self):
        trust = Template().resource("ClaudeCodeRole")["AssumeRolePolicyDocument"]["Statement"][0]
        self.assertEqual(trust["Principal"], {"AWS": "arn:aws:iam::123456789012:root"})
        self.assertNotIn("Condition", trust)
        sso = "arn:aws:iam::123456789012:role/aws-reserved/sso.amazonaws.com/*/AWSReservedSSO_Dev_*"
        trust = Template(TrustedPrincipalArns=sso).resource("ClaudeCodeRole")["AssumeRolePolicyDocument"]
        self.assertEqual(trust["Statement"][0]["Condition"], {"ArnLike": {"aws:PrincipalArn": [sso]}})

    def test_budget_can_be_turned_off(self):
        self.assertTrue(Template().conditions["HasBudget"])
        off = Template(EnableBudget="false")
        self.assertFalse(off.conditions["HasBudget"])
        budget_parts = [name for name, res in off.doc["Resources"].items() if res.get("Condition") == "HasBudget"]
        self.assertEqual(sorted(budget_parts),
                         ["BudgetActionRole", "MonthlyBudget", "StopAtLimit", "StopBedrockPolicy"])


class AwsProfileTest(TempDirTest):
    def setUp(self):
        super().setUp()
        self.config = self.dir / "aws" / "config"
        os.environ["AWS_CONFIG_FILE"] = str(self.config)
        os.environ["AWS_SHARED_CREDENTIALS_FILE"] = str(self.dir / "aws" / "credentials")

    def test_creates_the_role_profile_once(self):
        p = policy(aws={"role_arn": ROLE})
        first = self.notes(app.ensure_aws_profile, p)
        self.assertIn("added the AWS profile 'aws-claude'", first)
        self.assertIn("'default' (the login for the role) was not found", first)
        text = self.config.read_text()
        self.assertEqual(app.aws_profiles(self.config)["aws-claude"],
                         {"role_arn": ROLE, "source_profile": "default", "region": "ap-northeast-1"})
        (self.dir / "aws" / "credentials").write_text("[default]\naws_access_key_id = x\n")
        self.assertEqual(self.notes(app.ensure_aws_profile, p), "")
        self.assertEqual(self.config.read_text(), text)

    def test_keeps_other_profiles_and_crlf(self):
        self.config.parent.mkdir()
        self.config.write_bytes(b"[default]\r\nregion = us-east-1\r\n\r\n[sso-session corp]\r\nsso_region = ap-northeast-1\r\n")
        self.notes(app.ensure_aws_profile, policy(aws={"role_arn": ROLE, "profile": "work"}))
        raw = self.config.read_bytes()
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))
        profiles = app.aws_profiles(self.config)
        self.assertEqual(profiles["default"]["region"], "us-east-1")
        self.assertEqual(profiles["work"]["role_arn"], ROLE)

    def test_another_role_stops_app(self):
        self.config.parent.mkdir()
        self.config.write_text("[profile aws-claude]\nrole_arn = arn:aws:iam::999999999999:role/admin\nsource_profile = default\n")
        with self.assertRaises(SystemExit):
            app.ensure_aws_profile(policy(aws={"role_arn": ROLE}))

    def test_profile_without_role_is_only_checked(self):
        self.assertIn("was not found", self.notes(app.ensure_aws_profile, policy(aws={"profile": "work"})))
        self.assertFalse(self.config.exists())
        self.assertEqual(self.notes(app.ensure_aws_profile, policy()), "")


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
        proxy = app.Proxy(allow, upstream, self.log)
        threading.Thread(target=proxy.serve, daemon=True).start()
        self.addCleanup(proxy.sock.close)
        return proxy

    def test_host_patterns(self):
        allow = ["pypi.org", "*.example.co.jp"]
        for host, ok in (("pypi.org", True), ("PyPI.org.", True), ("evil-pypi.org", False),
                         ("pypi.org.evil.com", False), ("a.b.example.co.jp", True),
                         ("example.co.jp", False), ("badexample.co.jp", False)):
            self.assertEqual(app.host_allowed(host, allow), ok, host)

    def test_default_hosts_are_japan_only(self):
        hosts = app.allowed_hosts(policy(proxy={"allow_hosts": ["pypi.org"]}))
        self.assertIn("bedrock-runtime.ap-northeast-1.amazonaws.com", hosts)
        self.assertIn("sts.ap-northeast-3.amazonaws.com", hosts)
        self.assertEqual([h for h in hosts if "ap-northeast-" not in h], ["pypi.org"])
        for host in ("api.anthropic.com", "claude.ai", "bedrock-runtime.us-east-1.amazonaws.com",
                     "sts.amazonaws.com", "github.com"):
            self.assertFalse(app.host_allowed(host, hosts), host)

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
                head, _rest = app.read_head(conn)
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
        with mock.patch.object(app, "proxy_bypass", return_value=False):
            out = talk(proxy.port, b"CONNECT bedrock-runtime.ap-northeast-1.amazonaws.com:443 HTTP/1.1\r\n\r\n", b"ping")
        self.assertTrue(out.endswith("ping"), out)
        self.assertIn("CONNECT bedrock-runtime.ap-northeast-1.amazonaws.com:443 HTTP/1.1", seen[0])
        self.assertIn("Proxy-Authorization: Basic dXNlcjpwYXNz", seen[0])

    def test_upstream_never_sees_a_blocked_host(self):
        port, seen = self.fake_upstream(b"HTTP/1.1 200 OK\r\n\r\n")
        proxy = self.start(["127.0.0.1"], ("127.0.0.1", port, ""))
        with mock.patch.object(app, "proxy_bypass", return_value=False):
            out = talk(proxy.port, b"CONNECT example.com:443 HTTP/1.1\r\n\r\n")
        self.assertTrue(out.startswith("HTTP/1.1 403 "), out)
        self.assertEqual(seen, [])

    def test_upstream_refuses(self):
        port, _seen = self.fake_upstream(b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n")
        proxy = self.start(["example.com"], ("127.0.0.1", port, ""))
        with mock.patch.object(app, "proxy_bypass", return_value=False):
            out = talk(proxy.port, b"CONNECT example.com:443 HTTP/1.1\r\n\r\n")
        self.assertTrue(out.startswith("HTTP/1.1 502 "), out)
        self.assertIn("407", out)
        self.assertIn("FAIL example.com:443", self.log.read_text())

    def test_find_upstream_order(self):
        settings = {"env": {"HTTPS_PROXY": "http://from-settings:3128"}}
        with mock.patch.object(app.urllib.request, "getproxies", return_value={}):
            self.assertIsNone(app.find_upstream(policy(), {}))
            self.assertEqual(app.find_upstream(policy(), settings), ("from-settings", 3128, ""))
            os.environ["HTTP_PROXY"] = "http://from-env:8080"
            self.assertEqual(app.find_upstream(policy(), settings), ("from-env", 8080, ""))
            p = policy(proxy={"upstream": "http://user:p%40ss@from-policy:8888"})
            self.assertEqual(app.find_upstream(p, settings), ("from-policy", 8888, "dXNlcjpwQHNz"))
            os.environ["HTTPS_PROXY"] = "socks5://nope:1080"  # Not http: ignored with a note.
            self.assertIn("socks5", self.notes(lambda: self.assertEqual(
                app.find_upstream(policy(), settings), ("from-env", 8080, ""))))


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
    # Start aws_claude.py as a user would, with a fake claude and a fake home directory.
    def setUp(self):
        super().setUp()
        script = self.dir / "fake_claude.py"
        script.write_text(FAKE_CLAUDE, encoding="utf-8")
        if app.IS_WIN:
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

    def run_app(self, env_text, extra_env=None, exe="", cwd=None, timeout=120):
        (self.dir / ".env").write_text(env_text, encoding="utf-8")
        env = dict(clean_environ(), **{
            "HOME": str(self.home), "USERPROFILE": str(self.home), "LOCALAPPDATA": str(self.home / "local"),
            "XDG_CACHE_HOME": str(self.home / "cache"), "CLAUDE_CONFIG_DIR": str(self.home / ".claude"),
            "AWS_CONFIG_FILE": str(self.home / ".aws" / "config"),
            "AWS_SHARED_CREDENTIALS_FILE": str(self.home / ".aws" / "credentials"),
            "AWS_CLAUDE_ENV_FILE": str(self.dir / ".env"), "AWS_CLAUDE_SKIP_SETUP": "1", "CLAUDE_REAL_BIN": str(self.claude),
            "FAKE_OUT": str(self.out), "FAKE_TARGET": f"127.0.0.1:{self.target.server_address[1]}",
        })
        env.update(extra_env or {})
        cmd = [exe] if exe else [sys.executable, str(ROOT / "aws_claude.py")]
        done = subprocess.run(cmd + ["-p", "hi"], env=env, cwd=cwd or self.home, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        return done, json.loads(self.out.read_text(encoding="utf-8")) if self.out.exists() else None

    def test_proxy_and_role(self):
        done, out = self.run_app(
            f"AWS_CLAUDE_ROLE_ARN={ROLE}\nAWS_CLAUDE_PROXY_ADD_HOSTS=127.0.0.1\n",
            {"AWS_ACCESS_KEY_ID": "AKIAEXAMPLE", "AWS_SECRET_ACCESS_KEY": "x", "CLAUDE_CODE_OAUTH_TOKEN": "x",
             "ANTHROPIC_API_KEY": "x", "ALL_PROXY": "socks5://corp:1080"})
        self.assertEqual(done.returncode, 7, done.stderr)  # The exit code of claude comes back.
        self.assertEqual(out["argv"], ["--settings", out["flag_file"], "-p", "hi"])
        env = out["env"]
        proxy_url = env["HTTPS_PROXY"]
        self.assertRegex(proxy_url, r"^http://127\.0\.0\.1:\d+$")
        self.assertEqual(env["HTTP_PROXY"], proxy_url)
        self.assertEqual(env["NO_PROXY"], app.LOOPBACK)
        for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
            self.assertEqual(out["flag"]["env"][name], proxy_url)
        self.assertTrue(out["blocked"].startswith("HTTP/1.1 403"), out["blocked"])
        self.assertIn(" 200 ", out["allowed"])
        self.assertEqual(env["AWS_PROFILE"], "aws-claude")
        self.assertEqual(env["CLAUDE_CODE_USE_BEDROCK"], "1")
        for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "CLAUDE_CODE_OAUTH_TOKEN",
                     "ANTHROPIC_API_KEY", "ALL_PROXY"):
            self.assertNotIn(name, env)
        self.assertEqual(app.aws_profiles(self.home / ".aws" / "config")["aws-claude"]["role_arn"], ROLE)
        settings = json.loads((self.home / ".claude" / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(settings["env"]["AWS_PROFILE"], "aws-claude")
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

    @unittest.skipUnless(EXE, "set AWS_CLAUDE_TEST_EXE to a built aws-claude binary")
    def test_built_exe(self):
        # The exe has its policy inside. A .env file and AWS_CLAUDE_ env vars must not change it.
        done, out = self.run_app("AWS_CLAUDE_PROXY=false\n", {"AWS_CLAUDE_PROXY": "false", "ANTHROPIC_API_KEY": "x"}, exe=EXE)
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

    @unittest.skipUnless(FIRST_RUN, "set AWS_CLAUDE_TEST_FIRST_RUN=1 (installs tools from the network)")
    def test_first_run_installs_the_tools(self):
        repo = self.dir / "repo"
        (repo / "src").mkdir(parents=True)
        (repo / "src" / "Hello.java").write_text(
            "public class Hello {\n    void run() { helper(); }\n    void helper() {}\n}\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        # Hide tools that are already on this PC, so aws-claude must install them.
        tools = [name + ext for name in ("uv", "rtk", "graphify") for ext in ("", ".exe")]
        keep = [d for d in os.environ["PATH"].split(os.pathsep)
                if d and (shutil.which("git", path=d) or not any((Path(d) / t).exists() for t in tools))]
        done, out = self.run_app("", {"AWS_CLAUDE_SKIP_SETUP": "", "PATH": os.pathsep.join(keep)},
                                 exe=EXE, cwd=repo, timeout=1500)
        print(done.stderr, file=sys.stderr)  # The notes of aws-claude: what it installed, and what failed.
        self.assertEqual(done.returncode, 7, done.stderr)
        self.assertEqual(out["env"]["GRAPHIFY_HOOK_STRICT"], "1")
        bin_dir = str(self.home / ".local" / "bin")
        problems = [p.name for p in self.home.rglob("*.failed")]
        for tool in ("uv", "rtk", "graphify"):
            if not (shutil.which(tool, path=bin_dir) or shutil.which(tool, path=os.pathsep.join(keep))):
                problems.append(f"{tool} is not installed")
        settings = json.loads((self.home / ".claude" / "settings.json").read_text(encoding="utf-8"))
        if "rtk" not in json.dumps(settings.get("hooks", {})):
            problems.append("no rtk hook in settings.json")
        if not (self.home / ".claude" / "skills" / "graphify").exists():
            problems.append("no graphify skill")
        claude_md = app.read_text(repo / "CLAUDE.md")
        if app.CLAUDE_MD_MARKER not in claude_md:
            problems.append("no data file rules in CLAUDE.md")
        if "graphify" not in claude_md.lower():
            problems.append("no graphify rules in CLAUDE.md")
        if "graphify-out/" not in app.read_text(repo / ".git" / "info" / "exclude"):
            problems.append("graphify-out/ is not in .git/info/exclude")
        graph = repo / "graphify-out" / "graph.json"
        for _ in range(180):  # The graph is built in the background.
            if graph.exists():
                break
            time.sleep(1)
        else:
            logs = "".join(p.read_text(encoding="utf-8", errors="replace") for p in self.home.rglob("graph-*.log"))
            problems.append(f"no code graph after 180 s. Log:\n{logs[-2000:]}")
        self.assertEqual(problems, [], done.stderr)

    def test_proxy_off(self):
        done, out = self.run_app("AWS_CLAUDE_PROXY=false\n", {"AWS_ACCESS_KEY_ID": "AKIAEXAMPLE"})
        self.assertEqual(done.returncode, 7, done.stderr)
        self.assertEqual(out["argv"], ["-p", "hi"])
        self.assertNotIn("HTTPS_PROXY", out["env"])
        self.assertEqual(out["env"]["AWS_ACCESS_KEY_ID"], "AKIAEXAMPLE")  # No profile in the policy.
        self.assertFalse((self.home / ".aws").exists())

    def test_wrong_role_in_aws_config_stops_app(self):
        (self.home / ".aws").mkdir()
        (self.home / ".aws" / "config").write_text("[profile aws-claude]\nrole_arn = arn:aws:iam::999999999999:role/admin\n")
        done, out = self.run_app(f"AWS_CLAUDE_ROLE_ARN={ROLE}\n")
        self.assertNotEqual(done.returncode, 0)
        self.assertIsNone(out)  # claude did not start.
        self.assertIn("does not use the role of the policy", done.stderr)


if __name__ == "__main__":
    unittest.main()
