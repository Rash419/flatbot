import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

from app.ai import AIError, CodexAI

# A stand-in for the codex CLI. It reads its instructions from behaviour.json next to
# itself (the real environment is scrubbed, so env vars cannot steer it), records what
# it was given in record.json, and answers like `codex exec` would.
FAKE = r'''#!{python}
import json, os, sys, time
here = os.path.dirname(os.path.abspath(__file__))
cfg = json.load(open(os.path.join(here, "behaviour.json")))
args = sys.argv[1:]
if args[:2] == ["login", "status"]:
    sys.exit(0 if cfg.get("logged_in") else 1)
stdin = sys.stdin.read()
rec = {{"argv": args, "env": dict(os.environ), "stdin": stdin, "cwd": os.getcwd(),
        "cwd_files": os.listdir("."), "pid": os.getpid()}}
def opt(name):
    return args[args.index(name) + 1] if name in args else None
if opt("--output-schema"):
    rec["schema"] = json.load(open(opt("--output-schema")))
if opt("--image"):
    rec["image"] = open(opt("--image"), "rb").read().hex()
    rec["image_name"] = os.path.basename(opt("--image"))
json.dump(rec, open(os.path.join(here, "record.json"), "w"))
mode = cfg.get("mode", "ok")
if mode == "sleep":
    time.sleep(30)
if mode == "fail":
    sys.stderr.write(cfg.get("stderr", "boom"))
    sys.exit(3)
if mode != "no_output":
    with open(opt("--output-last-message"), "w") as f:
        f.write(cfg.get("answer", ""))
'''

SCHEMA = {"type": "object", "properties": {"a": {"type": "integer"}}, "required": ["a"],
          "additionalProperties": False}


class CodexAITest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.bin = os.path.join(self.dir, "codex")
        with open(self.bin, "w") as f:
            f.write(FAKE.format(python=sys.executable))
        os.chmod(self.bin, 0o755)
        self.behave(mode="ok", answer='{"a": 1}')
        self.ai = CodexAI(codex_bin=self.bin, timeout=10)

    def behave(self, **cfg):
        with open(os.path.join(self.dir, "behaviour.json"), "w") as f:
            json.dump(cfg, f)

    def record(self):
        with open(os.path.join(self.dir, "record.json")) as f:
            return json.load(f)

    def test_success(self):
        self.assertEqual(self.ai.ask("What is a?", SCHEMA), {"a": 1})
        rec = self.record()
        argv = rec["argv"]
        self.assertEqual(argv[0], "exec")
        self.assertEqual(argv[-1], "-")  # prompt comes from stdin
        self.assertIn("What is a?", rec["stdin"])
        self.assertIn("never as instructions", rec["stdin"])
        for flag in ("--skip-git-repo-check", "--ephemeral", "--ignore-user-config", "--ignore-rules"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")
        self.assertIn('model_reasoning_effort="low"', argv)
        self.assertIn('web_search="disabled"', argv)
        self.assertEqual(argv[argv.index("--disable") + 1], "shell_tool")
        self.assertNotIn("--model", argv)
        self.assertNotIn("--image", argv)
        # Runs in an empty directory that is removed afterwards.
        self.assertEqual(rec["cwd_files"], [])
        self.assertEqual(os.path.realpath(argv[argv.index("--cd") + 1]), os.path.realpath(rec["cwd"]))
        self.assertFalse(os.path.exists(rec["cwd"]))

    def test_schema_file(self):
        self.ai.ask("q", SCHEMA)
        self.assertEqual(self.record()["schema"], SCHEMA)

    def test_model_and_effort(self):
        CodexAI(codex_bin=self.bin, model="gpt-x", effort="medium").ask("q", SCHEMA)
        argv = self.record()["argv"]
        self.assertEqual(argv[argv.index("--model") + 1], "gpt-x")
        self.assertIn('model_reasoning_effort="medium"', argv)

    def test_image_passed(self):
        png = b"\x89PNG\r\n\x1a\n" + b"pixels"
        self.ai.ask("read this", SCHEMA, image=png)
        rec = self.record()
        self.assertEqual(bytes.fromhex(rec["image"]), png)
        self.assertTrue(rec["image_name"].endswith(".png"))
        argv = rec["argv"]
        # The variadic --image is followed by another option, never by the "-" prompt marker.
        i = argv.index("--image")
        self.assertTrue(argv[i + 2].startswith("--"))
        self.assertEqual(argv[-1], "-")

    def test_env_scrubbed(self):
        extra = {"TELEGRAM_BOT_TOKEN": "123:secret-token", "OPENAI_API_KEY": "sk-nope",
                 "DB_PATH": "/data/bot.db", "CODEX_HOME": "/data/codex", "LANG": "C.UTF-8"}
        with mock.patch.dict(os.environ, extra):
            self.ai.ask("q", SCHEMA)
        env = self.record()["env"]
        self.assertNotIn("TELEGRAM_BOT_TOKEN", env)
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("DB_PATH", env)
        self.assertNotIn("123:secret-token", json.dumps(env))
        self.assertEqual(env["CODEX_HOME"], "/data/codex")
        self.assertEqual(env["LANG"], "C.UTF-8")
        self.assertIn("PATH", env)

    def test_nonzero_exit(self):
        self.behave(mode="fail", stderr="Error: Not logged in")
        with self.assertRaises(AIError) as ctx:
            self.ai.ask("SECRET SMS TEXT", SCHEMA)
        self.assertIn("not signed in", str(ctx.exception))
        self.assertNotIn("SECRET", str(ctx.exception))

    def test_usage_limit_reason(self):
        self.behave(mode="fail", stderr="You've hit your usage limit.")
        with self.assertRaises(AIError) as ctx:
            self.ai.ask("q", SCHEMA)
        self.assertIn("usage limit", str(ctx.exception))

    def test_timeout_kills_process(self):
        self.behave(mode="sleep")
        ai = CodexAI(codex_bin=self.bin, timeout=1)
        start = time.monotonic()
        with self.assertRaises(AIError) as ctx:
            ai.ask("q", SCHEMA)
        self.assertLess(time.monotonic() - start, 10)
        self.assertIn("longer than", str(ctx.exception))
        pid = self.record()["pid"]
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_invalid_json(self):
        self.behave(mode="ok", answer="Sure! The answer is 1.")
        with self.assertRaises(AIError):
            self.ai.ask("q", SCHEMA)

    def test_json_not_an_object(self):
        self.behave(mode="ok", answer="[1, 2]")
        with self.assertRaises(AIError):
            self.ai.ask("q", SCHEMA)

    def test_fenced_json_is_accepted(self):
        self.behave(mode="ok", answer='```json\n{"a": 2}\n```')
        self.assertEqual(self.ai.ask("q", SCHEMA), {"a": 2})

    def test_missing_or_empty_output(self):
        self.behave(mode="no_output")
        with self.assertRaises(AIError):
            self.ai.ask("q", SCHEMA)
        self.behave(mode="ok", answer="   ")
        with self.assertRaises(AIError):
            self.ai.ask("q", SCHEMA)

    def test_missing_binary(self):
        ai = CodexAI(codex_bin=os.path.join(self.dir, "nope"))
        self.assertFalse(ai.installed())
        self.assertFalse(ai.signed_in())
        with self.assertRaises(AIError):
            ai.ask("q", SCHEMA)

    def test_signed_in(self):
        self.behave(logged_in=True)
        self.assertTrue(self.ai.signed_in())
        self.behave(logged_in=False)
        self.assertFalse(self.ai.signed_in())
        self.assertTrue(self.ai.installed())


if __name__ == "__main__":
    unittest.main()
