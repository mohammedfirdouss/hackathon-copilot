#!/usr/bin/env python3
"""Hackathon co-pilot: what's due, and is a hackathon ready to submit."""

from __future__ import annotations

import argparse
import base64
import json
import ssl
import subprocess
import sys
import tomllib
import urllib.error
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_FILE = Path(__file__).with_name("hackathons.toml")
STATUSES = ("entered", "considering", "dropped")
STATUS_ICONS = {"entered": "🏁", "considering": "🤔", "dropped": "🚫"}
VERDICT_ICONS = {True: "✅", False: "❌", None: "⚠️"}


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Requirement:
    id: str
    text: str
    check: str
    arg: str | None = None
    done: bool = False
    optional: bool = False
    link: str | None = None


@dataclass(frozen=True)
class Hackathon:
    id: str
    name: str
    url: str
    deadline: datetime
    status: str
    requirements: tuple[Requirement, ...]
    repo: str | None = None
    track: str | None = None


@dataclass(frozen=True)
class Config:
    tz: ZoneInfo
    hackathons: tuple[Hackathon, ...]

    def get(self, hackathon_id: str) -> Hackathon | None:
        return next((h for h in self.hackathons if h.id == hackathon_id), None)


@dataclass(frozen=True)
class Result:
    ok: bool | None
    detail: str


@dataclass(frozen=True)
class GhResponse:
    code: int
    out: str
    err: str


Gh = Callable[[list[str]], GhResponse]


def run_gh(args: list[str]) -> GhResponse:
    try:
        p = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as e:
        return GhResponse(127, "", str(e))
    return GhResponse(p.returncode, p.stdout, p.stderr)


def gh_failure(r: GhResponse, not_found: str) -> Result:
    if "HTTP 404" in r.err:
        return Result(False, not_found)
    first = r.err.strip().splitlines()[-1] if r.err.strip() else f"exit {r.code}"
    return Result(None, f"gh failed: {first}")


def needs_repo(fn: Callable[[Requirement, Hackathon, Gh], Result]):
    def wrapper(req: Requirement, h: Hackathon, gh: Gh) -> Result:
        if not h.repo:
            return Result(None, "no repo set")
        return fn(req, h, gh)

    return wrapper


def check_manual(req: Requirement, h: Hackathon, gh: Gh) -> Result:
    return Result(req.done, "done" if req.done else "not done")


@needs_repo
def check_repo_public(req: Requirement, h: Hackathon, gh: Gh) -> Result:
    r = gh(["api", f"repos/{h.repo}", "--jq", ".visibility"])
    if r.code:
        return gh_failure(r, f"{h.repo} not found")
    visibility = r.out.strip()
    return Result(visibility == "public", f"{h.repo} is {visibility}")


@needs_repo
def check_repo_has_license(req: Requirement, h: Hackathon, gh: Gh) -> Result:
    r = gh(["api", f"repos/{h.repo}", "--jq", ".license.spdx_id"])
    if r.code:
        return gh_failure(r, f"{h.repo} not found")
    spdx = r.out.strip()
    if spdx in ("", "null", "NOASSERTION"):
        return Result(False, "no recognised license")
    return Result(True, spdx)


@needs_repo
def check_readme_mentions(req: Requirement, h: Hackathon, gh: Gh) -> Result:
    r = gh(["api", f"repos/{h.repo}/readme", "--jq", ".content"])
    if r.code:
        return gh_failure(r, "no README")
    readme = base64.b64decode(r.out).decode("utf-8", "replace")
    found = req.arg.casefold() in readme.casefold()
    return Result(found, f"{'mentions' if found else 'missing'} \"{req.arg}\"")


def check_url_up(req: Requirement, h: Hackathon, gh: Gh) -> Result:
    last = ""
    # Some hosts reject HEAD (405) while serving GET fine, so fall back to GET.
    for method in ("HEAD", "GET"):
        try:
            with urllib.request.urlopen(
                urllib.request.Request(req.arg, method=method), timeout=10
            ) as resp:
                return Result(True, f"HTTP {resp.status}")
        except urllib.error.HTTPError as e:
            e.close()
            last = f"HTTP {e.code}"
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            reason = getattr(e, "reason", e)
            if isinstance(reason, ssl.SSLCertVerificationError):
                return Result(None, "this Python has no CA certificates (run its Install Certificates.command)")
            last = str(reason)
    return Result(False, f"down: {last}")


def check_starred(req: Requirement, h: Hackathon, gh: Gh) -> Result:
    r = gh(["api", f"user/starred/{req.arg}"])
    if r.code:
        return gh_failure(r, f"{req.arg} not starred")
    return Result(True, f"{req.arg} starred")


@dataclass(frozen=True)
class CheckType:
    run: Callable[[Requirement, Hackathon, Gh], Result]
    needs_arg: bool = False


CHECKS: dict[str, CheckType] = {
    "manual": CheckType(check_manual),
    "repo_public": CheckType(check_repo_public),
    "repo_has_license": CheckType(check_repo_has_license),
    "readme_mentions": CheckType(check_readme_mentions, needs_arg=True),
    "url_up": CheckType(check_url_up, needs_arg=True),
    "starred": CheckType(check_starred, needs_arg=True),
}

HACKATHON_KEYS = {"id", "name", "url", "deadline", "status", "repo", "track", "requirement"}
REQUIREMENT_KEYS = {"id", "text", "check", "arg", "done", "optional", "link"}


def _expect(table: dict, where: str, allowed: set[str], required: set[str]) -> None:
    unknown = set(table) - allowed
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {', '.join(sorted(unknown))}")
    missing = required - set(table)
    if missing:
        raise ConfigError(f"{where}: missing {', '.join(sorted(missing))}")


def parse_requirement(raw: dict, hid: str) -> Requirement:
    where = f"hackathon '{hid}' requirement '{raw.get('id', '?')}'"
    _expect(raw, where, REQUIREMENT_KEYS, {"id", "text", "check"})
    kind = CHECKS.get(raw["check"])
    if kind is None:
        raise ConfigError(
            f"{where}: unknown check '{raw['check']}' (valid: {', '.join(CHECKS)})"
        )
    if kind.needs_arg and not raw.get("arg"):
        raise ConfigError(f"{where}: check '{raw['check']}' needs arg")
    if "done" in raw and raw["check"] != "manual":
        raise ConfigError(f"{where}: done is only for manual checks")
    return Requirement(**raw)


def parse_hackathon(raw: dict) -> Hackathon:
    hid = raw.get("id", "?")
    where = f"hackathon '{hid}'"
    _expect(raw, where, HACKATHON_KEYS, {"id", "name", "url", "deadline", "status"})
    if raw["status"] not in STATUSES:
        raise ConfigError(f"{where}: status must be one of {', '.join(STATUSES)}")
    deadline = raw["deadline"]
    if isinstance(deadline, str):
        try:
            deadline = datetime.fromisoformat(deadline)
        except ValueError as e:
            raise ConfigError(f"{where}: bad deadline: {e}") from None
    if not isinstance(deadline, datetime) or deadline.tzinfo is None:
        raise ConfigError(f"{where}: deadline needs a UTC offset")
    requirements = tuple(parse_requirement(r, hid) for r in raw.get("requirement", []))
    fields = {k: v for k, v in raw.items() if k != "requirement"}
    return Hackathon(**{**fields, "deadline": deadline, "requirements": requirements})


def parse_config(text: str) -> Config:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"bad TOML: {e}") from None
    _expect(data, "top level", {"timezone", "hackathon"}, {"timezone"})
    try:
        tz = ZoneInfo(data["timezone"])
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError(f"unknown timezone '{data['timezone']}'") from None
    hackathons = tuple(parse_hackathon(h) for h in data.get("hackathon", []))
    ids = [h.id for h in hackathons]
    if len(ids) != len(set(ids)):
        raise ConfigError("duplicate hackathon id")
    return Config(tz, hackathons)


def format_deadline(deadline: datetime, tz: ZoneInfo) -> str:
    local = deadline.astimezone(tz)
    return f"{local:%a} {local.day} {local:%b %H:%M %Z}"


def format_countdown(deadline: datetime, now: datetime) -> str:
    seconds = int((deadline - now).total_seconds())
    if seconds <= 0:
        return "CLOSED"
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    if days:
        return f"{days}d {hours}h left"
    return f"{hours}h {rest // 60}m left"


def manual_counts(h: Hackathon) -> tuple[int, int]:
    manual = [r for r in h.requirements if r.check == "manual" and not r.optional]
    return sum(r.done for r in manual), len(manual)


def brief(cfg: Config, now: datetime) -> dict:
    upcoming = sorted(
        (h for h in cfg.hackathons if h.status != "dropped" and h.deadline > now),
        key=lambda h: h.deadline,
    )
    items = []
    for h in upcoming:
        done, total = manual_counts(h)
        items.append(
            {
                "id": h.id,
                "name": h.name,
                "status": h.status,
                "deadline": h.deadline.isoformat(),
                "deadline_local": format_deadline(h.deadline, cfg.tz),
                "countdown": format_countdown(h.deadline, now),
                "manual_done": done,
                "manual_total": total,
            }
        )
    pending = [
        (h, r)
        for h in upcoming
        for r in h.requirements
        if r.check == "manual" and not r.done
    ]
    # Required items beat optional ones even when the optional one is due sooner.
    pending.sort(key=lambda hr: (hr[1].optional, hr[0].deadline))
    nxt = None
    if pending:
        h, r = pending[0]
        nxt = {
            "hackathon": h.id,
            "requirement": r.id,
            "text": r.text,
            "countdown": format_countdown(h.deadline, now),
        }
    return {"hackathons": items, "next": nxt}


def render_brief(data: dict, cfg: Config) -> str:
    if not data["hackathons"]:
        return "No upcoming hackathons."
    lines = []
    for item in data["hackathons"]:
        line = (
            f"{STATUS_ICONS[item['status']]} {item['name']}: "
            f"{item['deadline_local']}, {item['countdown']}"
        )
        if item["manual_total"]:
            line += f", manual: {item['manual_done']}/{item['manual_total']} done"
        lines.append(line)
    nxt = data["next"]
    if nxt:
        name = cfg.get(nxt["hackathon"]).name
        lines.append(f"Most urgent: {nxt['text']} ({name}, {nxt['countdown']})")
    else:
        lines.append("Nothing manual left to do.")
    return "\n".join(lines)


def check(h: Hackathon, cfg: Config, now: datetime, gh: Gh) -> dict:
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(lambda r: CHECKS[r.check].run(r, h, gh), h.requirements)
        )
    reqs = [
        {
            "id": r.id,
            "text": r.text,
            "check": r.check,
            "optional": r.optional,
            "ok": res.ok,
            "detail": res.detail,
            "link": r.link,
        }
        for r, res in zip(h.requirements, results)
    ]
    blocking = sum(1 for r in reqs if not r["optional"] and r["ok"] is not True)
    return {
        "id": h.id,
        "name": h.name,
        "status": h.status,
        "deadline": h.deadline.isoformat(),
        "deadline_local": format_deadline(h.deadline, cfg.tz),
        "countdown": format_countdown(h.deadline, now),
        "requirements": reqs,
        "blocking": blocking,
        "ready": blocking == 0,
    }


def render_check(data: dict) -> str:
    lines = [f"{data['name']}: {data['deadline_local']}, {data['countdown']}"]
    for r in data["requirements"]:
        line = f"{VERDICT_ICONS[r['ok']]} {r['text']}"
        if r["optional"]:
            line += " (optional)"
        line += f": {r['detail']}"
        if r["link"]:
            line += f" {r['link']}"
        lines.append(line)
    n = data["blocking"]
    lines.append("Ready to submit" if n == 0 else f"{n} blocking item{'s' * (n != 1)}")
    return "\n".join(lines)


def main(
    argv: list[str] | None = None,
    *,
    now: datetime | None = None,
    gh: Gh = run_gh,
    out: TextIO = sys.stdout,
    err: TextIO = sys.stderr,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, default=DEFAULT_FILE)
    parser.add_argument("--json", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("brief", help="what's due, soonest first")
    check_p = sub.add_parser("check", help="is a hackathon ready to submit")
    check_p.add_argument("id")
    # Accept --json after the subcommand too, since that is how agents tend to type it.
    for p in (sub.choices["brief"], check_p):
        p.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    try:
        cfg = parse_config(args.file.read_text())
    except (OSError, ConfigError) as e:
        print(f"error: {e}", file=err)
        return 2
    now = now or datetime.now(timezone.utc)

    if args.command == "brief":
        data = brief(cfg, now)
        text = render_brief(data, cfg)
    else:
        h = cfg.get(args.id)
        if h is None:
            valid = ", ".join(x.id for x in cfg.hackathons)
            print(f"error: unknown hackathon '{args.id}'. Valid ids: {valid}", file=err)
            return 2
        data = check(h, cfg, now, gh)
        text = render_check(data)

    print(json.dumps(data, ensure_ascii=False, indent=2) if args.json else text, file=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
