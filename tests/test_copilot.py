import base64
import io
import json
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import copilot  # noqa: E402

NOW = datetime(2026, 11, 6, 8, 0, tzinfo=timezone.utc)

CONFIG = """
timezone = "Africa/Lagos"

[[hackathon]]
id = "later"
name = "Later Hack"
url = "https://example.com/later"
deadline = "2026-11-08T07:00:00+00:00"
status = "entered"
repo = "me/later"

  [[hackathon.requirement]]
  id = "video"
  text = "Video"
  check = "manual"
  done = true
  link = "https://youtu.be/abc"

  [[hackathon.requirement]]
  id = "form"
  text = "Form sent"
  check = "manual"

[[hackathon]]
id = "soon"
name = "Soon Hack"
url = "https://example.com/soon"
deadline = "2026-11-06T12:10:00+00:00"
status = "considering"

  [[hackathon.requirement]]
  id = "blog"
  text = "Blog post"
  check = "manual"
  optional = true

  [[hackathon.requirement]]
  id = "register"
  text = "Registered"
  check = "manual"

[[hackathon]]
id = "past"
name = "Past Hack"
url = "https://example.com/past"
deadline = "2026-11-01T00:00:00+00:00"
status = "entered"

[[hackathon]]
id = "gone"
name = "Dropped Hack"
url = "https://example.com/gone"
deadline = "2026-11-09T00:00:00+00:00"
status = "dropped"
"""


def hackathon_with(requirements: str, repo: str | None = "me/app") -> str:
    repo_line = f'repo = "{repo}"' if repo else ""
    return f"""
timezone = "Africa/Lagos"
[[hackathon]]
id = "h"
name = "H"
url = "https://example.com"
deadline = "2026-11-10T00:00:00+00:00"
status = "entered"
{repo_line}
{requirements}
"""


def requirement(check: str, arg: str | None = None, optional: bool = False) -> str:
    lines = ["[[hackathon.requirement]]", 'id = "r"', 'text = "Thing"', f'check = "{check}"']
    if arg:
        lines.append(f'arg = "{arg}"')
    if optional:
        lines.append("optional = true")
    return "\n".join(lines)


class FakeGh:
    def __init__(self, responses: dict[str, copilot.GhResponse]):
        self.responses = responses
        self.calls: list[list[str]] = []

    def __call__(self, args):
        self.calls.append(args)
        return self.responses[args[1]]


def ok(out: str) -> copilot.GhResponse:
    return copilot.GhResponse(0, out, "")


NOT_FOUND = copilot.GhResponse(1, "", "gh: Not Found (HTTP 404)\n")
GH_DOWN = copilot.GhResponse(1, "", "error connecting to api.github.com\n")


def run(config: str, argv: list[str], gh=None) -> tuple[int, str, str]:
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "hackathons.toml"
        path.write_text(config)
        out, err = io.StringIO(), io.StringIO()
        code = copilot.main(
            ["--file", str(path), *argv], now=NOW, gh=gh or FakeGh({}), out=out, err=err
        )
        return code, out.getvalue(), err.getvalue()


class BriefTest(unittest.TestCase):
    def test_sorted_by_deadline_in_configured_timezone_skipping_past_and_dropped(self):
        code, out, _ = run(CONFIG, ["brief"])
        self.assertEqual(code, 0)
        self.assertEqual(
            out.splitlines(),
            [
                "**What's due**",
                "- 🤔 **Soon Hack**: Fri 6 Nov 13:10 WAT, 4h 10m left, 0/1 manual done",
                "- 🏁 **Later Hack**: Sun 8 Nov 08:00 WAT, 1d 23h left, 1/2 manual done",
                "",
                "**Do first**: Registered (Soon Hack, 4h 10m left)",
            ],
        )

    def test_brief_makes_no_github_calls(self):
        gh = FakeGh({})
        run(CONFIG, ["brief"], gh)
        self.assertEqual(gh.calls, [])

    def test_json(self):
        _, out, _ = run(CONFIG, ["brief", "--json"])
        data = json.loads(out)
        self.assertEqual([h["id"] for h in data["hackathons"]], ["soon", "later"])
        self.assertEqual(data["next"]["requirement"], "register")


class CountdownTest(unittest.TestCase):
    def test_formats(self):
        def at(iso):
            return datetime.fromisoformat(iso)

        self.assertEqual(copilot.format_countdown(at("2026-11-07T19:00:00+00:00"), NOW), "1d 11h left")
        self.assertEqual(copilot.format_countdown(at("2026-11-06T12:10:00+00:00"), NOW), "4h 10m left")
        self.assertEqual(copilot.format_countdown(NOW, NOW), "CLOSED")

    def test_check_on_past_hackathon_says_closed(self):
        _, out, _ = run(CONFIG, ["check", "past"])
        self.assertEqual(out.splitlines()[0], "**Past Hack**: Sun 1 Nov 01:00 WAT, CLOSED")


class CheckTest(unittest.TestCase):
    def verdict(self, req: str, gh=None, repo="me/app") -> str:
        code, out, _ = run(hackathon_with(req, repo), ["check", "h"], gh)
        self.assertEqual(code, 0)
        return out.splitlines()[1].removeprefix("- ")

    def test_manual_with_link_and_optional(self):
        code, out, _ = run(CONFIG, ["check", "later"])
        self.assertEqual(
            out.splitlines()[1:],
            ["- ✅ Video: done https://youtu.be/abc", "- ❌ Form sent: not done", "", "**1 blocking item**"],
        )
        _, out, _ = run(CONFIG, ["check", "soon"])
        self.assertIn("❌ Blog post (optional): not done", out)
        self.assertTrue(out.endswith("**1 blocking item**\n"))

    def test_dropped_hackathon_can_still_be_checked(self):
        code, out, _ = run(CONFIG, ["check", "gone"])
        self.assertEqual(code, 0)
        self.assertTrue(out.endswith("**Ready to submit**\n"))

    def test_repo_public(self):
        line = self.verdict(requirement("repo_public"), FakeGh({"repos/me/app": ok("public\n")}))
        self.assertTrue(line.startswith("✅"))
        line = self.verdict(requirement("repo_public"), FakeGh({"repos/me/app": ok("private\n")}))
        self.assertTrue(line.startswith("❌"))

    def test_repo_has_license(self):
        line = self.verdict(requirement("repo_has_license"), FakeGh({"repos/me/app": ok("MIT\n")}))
        self.assertEqual(line, "✅ Thing: MIT")
        for spdx in ("null\n", "NOASSERTION\n"):
            line = self.verdict(requirement("repo_has_license"), FakeGh({"repos/me/app": ok(spdx)}))
            self.assertTrue(line.startswith("❌"), spdx)

    def test_readme_mentions_is_case_insensitive(self):
        readme = base64.encodebytes(b"# App\nBuilt with the switch console.\n").decode()
        gh = FakeGh({"repos/me/app/readme": ok(readme)})
        self.assertTrue(self.verdict(requirement("readme_mentions", "Switch Console"), gh).startswith("✅"))
        self.assertTrue(self.verdict(requirement("readme_mentions", "Lightning"), gh).startswith("❌"))

    def test_starred(self):
        req = requirement("starred", "mozilla-ai/otari")
        self.assertTrue(self.verdict(req, FakeGh({"user/starred/mozilla-ai/otari": ok("")})).startswith("✅"))
        self.assertTrue(self.verdict(req, FakeGh({"user/starred/mozilla-ai/otari": NOT_FOUND})).startswith("❌"))

    def test_gh_failure_and_missing_repo_are_couldnt_check(self):
        line = self.verdict(requirement("repo_public"), FakeGh({"repos/me/app": GH_DOWN}))
        self.assertTrue(line.startswith("⚠️"), line)
        _, out, _ = run(hackathon_with(requirement("repo_public"), None), ["check", "h"])
        self.assertIn("⚠️ Thing: no repo set", out)
        self.assertTrue(out.endswith("**1 blocking item**\n"))

    def test_url_up(self):
        class Handler(BaseHTTPRequestHandler):
            def do_HEAD(self):
                self.send_response(405 if self.path == "/no-head" else 200 if self.path != "/broken" else 500)
                self.end_headers()

            def do_GET(self):
                self.send_response(500 if self.path == "/broken" else 200)
                self.end_headers()

            def log_message(self, *a):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"
        self.assertTrue(self.verdict(requirement("url_up", f"{base}/")).startswith("✅"))
        self.assertTrue(self.verdict(requirement("url_up", f"{base}/no-head")).startswith("✅"))
        self.assertTrue(self.verdict(requirement("url_up", f"{base}/broken")).startswith("❌"))

    def test_ready_when_all_required_pass(self):
        req = requirement("repo_public") + "\n" + requirement("manual", optional=True)
        _, out, _ = run(hackathon_with(req), ["check", "h"], FakeGh({"repos/me/app": ok("public\n")}))
        self.assertTrue(out.endswith("**Ready to submit**\n"))

    def test_json(self):
        _, out, _ = run(CONFIG, ["check", "later", "--json"])
        data = json.loads(out)
        self.assertEqual(data["blocking"], 1)
        self.assertFalse(data["ready"])
        self.assertEqual(data["requirements"][0]["link"], "https://youtu.be/abc")
        self.assertIs(data["requirements"][1]["ok"], False)


class InputTest(unittest.TestCase):
    def test_unknown_id_exits_2_and_lists_ids(self):
        code, out, err = run(CONFIG, ["check", "nope"])
        self.assertEqual(code, 2)
        self.assertIn("later, soon, past, gone", err)

    def assert_rejected(self, config: str, *needles: str):
        code, _, err = run(config, ["brief"])
        self.assertEqual(code, 2)
        for n in needles:
            self.assertIn(n, err)

    def test_unknown_check_rejected_naming_hackathon_and_requirement(self):
        self.assert_rejected(hackathon_with(requirement("vibes")), "'h'", "'r'", "vibes")

    def test_unknown_key_and_bad_status_rejected(self):
        self.assert_rejected(hackathon_with(requirement("manual") + '\nlnk = "x"'), "lnk")
        self.assert_rejected(hackathon_with("").replace('"entered"', '"maybe"'), "status")

    def test_real_file_parses(self):
        cfg = copilot.parse_config((ROOT / "hackathons.toml").read_text())
        self.assertIn("switch", [h.id for h in cfg.hackathons])


if __name__ == "__main__":
    unittest.main()
