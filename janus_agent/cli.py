"""janus-agent command line: scan, plan, check, learn."""

import argparse
import json
import sys
from pathlib import Path

from janus_agent import __version__, providers
from janus_agent.config import ConfigError, load_config
from janus_agent.learner import LearnError, run_learn
from janus_agent.llm.base import ProviderError
from janus_agent.llm.structured import StructuredError
from janus_agent.orchestrator import Checker
from janus_agent.planner import run_plan
from janus_agent.scanners import is_blocking, run_scan, write_report

EXIT_OK, EXIT_PROBLEMS, EXIT_USAGE, EXIT_NOT_APPLIED = 0, 1, 2, 3


def ask_confirmation(diff: str) -> bool:
    if not sys.stdin.isatty():
        print("No terminal to confirm: nothing written (use --apply to write).")
        return False
    try:
        return input("Write these changes? [y/N] ").strip().lower() in ("y", "yes", "o", "oui")
    except EOFError:
        return False


def _target(path: str) -> Path:
    target = Path(path).resolve()
    if not target.is_dir():
        raise SystemExit(f"Not a directory: {target}")
    return target


def cmd_scan(args) -> int:
    target = _target(args.path)
    report = run_scan(target)
    report_path = write_report(target, report)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"Janus scan: {target.name}")
        if not report["findings"]:
            print("  No issues found.")
        for f in report["findings"]:
            loc = f"{f['file']}:{f['line']}" if f.get("line") else f["file"]
            print(f"  {f['severity'].upper():<9} {f['detail']}  ({loc})")
        print(f"\n{report['count']} issue(s). Report: {report_path.relative_to(target)}")
    return EXIT_PROBLEMS if is_blocking(report) else EXIT_OK


def _model(args):
    config = load_config(args.config, {
        "provider": args.provider, "model": args.model, "base_url": args.base_url,
        "tool_calling": args.tool_calling,
    })
    if getattr(args, "max_rounds", None):
        config.check.max_rounds = args.max_rounds
    provider = providers.create(config.model)
    where = config.model.base_url or "provider default address"
    print(f"Model: {config.model.provider} / {config.model.model} ({where}), "
          f"tool calling: {config.model.tool_calling}")
    return config, provider


def cmd_check(args) -> int:
    target = _target(args.path)
    config, provider = _model(args)
    result = Checker(target, config, provider, confirm=ask_confirmation, apply=args.apply).run()
    if result.changes and not result.applied:
        return EXIT_NOT_APPLIED
    return EXIT_OK if result.success else EXIT_PROBLEMS


def cmd_plan(args) -> int:
    target = _target(args.path)
    config, provider = _model(args)
    run_plan(target, args.description, config, provider)
    return EXIT_OK


def cmd_learn(args) -> int:
    target = _target(args.path)
    config, provider = _model(args)
    written = run_learn(target, config, provider, apply=args.apply, confirm=ask_confirmation)
    return EXIT_OK if written else EXIT_NOT_APPLIED


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="janus-agent",
                                     description="Looks before. Checks after. Never forgets.")
    parser.add_argument("--version", action="version", version=f"janus-agent {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def model_options(p):
        p.add_argument("--config", type=Path, help="config file (default: ./janus-agent.toml)")
        p.add_argument("--provider", help="provider name (openai_compat, anthropic, ...)")
        p.add_argument("--model", help="model name")
        p.add_argument("--base-url", help="server address")
        p.add_argument("--tool-calling", choices=["auto", "native", "json"])

    scan = sub.add_parser("scan", help="run the free scanners only (no model)")
    scan.add_argument("path", nargs="?", default=".")
    scan.add_argument("--json", action="store_true", help="print the JSON report")
    scan.set_defaults(func=cmd_scan)

    plan = sub.add_parser("plan", help="before a change: list what it touches, write a contract")
    plan.add_argument("description", help="the change you want to make")
    plan.add_argument("--path", default=".", help="project (default: current directory)")
    model_options(plan)
    plan.set_defaults(func=cmd_plan)

    check = sub.add_parser("check", help="scan, fix with parallel sub-agents, verify")
    check.add_argument("path", nargs="?", default=".")
    check.add_argument("--apply", action="store_true", help="write the changes without asking")
    check.add_argument("--max-rounds", type=int, help="maximum fix rounds")
    model_options(check)
    check.set_defaults(func=cmd_check)

    learn = sub.add_parser("learn", help="turn the last check's fixes into rules and decision lines")
    learn.add_argument("path", nargs="?", default=".")
    learn.add_argument("--apply", action="store_true", help="write without asking")
    model_options(learn)
    learn.set_defaults(func=cmd_learn)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ConfigError, providers.UnknownProvider, LearnError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except (ProviderError, StructuredError) as exc:
        print(f"Model error: {exc}", file=sys.stderr)
        return EXIT_PROBLEMS
    except KeyboardInterrupt:
        print("Interrupted: nothing was written.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
