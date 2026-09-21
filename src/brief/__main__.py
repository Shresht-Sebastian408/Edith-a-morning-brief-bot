"""CLI entrypoint.

Every stage is runnable on its own, so failures are isolated to one layer
instead of surfacing as "the brief didn't arrive":

    python -m brief --agent calendar     # connector only, no LLM, no send
    python -m brief --dry-run            # full pipeline, printed not sent
    python -m brief                      # for real
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from brief.config import get_settings
from brief.contracts import Brief
from brief.orchestrator import AGENTS, build_brief, collect_signals, gather_reports

log = logging.getLogger("brief")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="morning-brief", description=__doc__)
    p.add_argument("--dry-run", action="store_true",
                   help="Print the brief instead of sending it.")
    p.add_argument("--agent", metavar="NAME",
                   help="Run a single agent and dump its report as JSON. Skips synthesis.")
    p.add_argument("--no-voice", action="store_true",
                   help="Skip text-to-speech. Useful when iterating on wording.")
    p.add_argument("--explain", action="store_true",
                   help="Show why each email was kept or dropped by the rules.")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()

    if args.agent:
        agent = next((a for a in AGENTS if a.name == args.agent), None)
        if agent is None:
            names = ", ".join(a.name for a in AGENTS)
            print(f"Unknown agent {args.agent!r}. Available: {names}", file=sys.stderr)
            return 2
        from brief.agents.base import run_safely

        report = await run_safely(agent, settings)
        print(report.model_dump_json(indent=2))
        return 0 if report.ok else 1

    reports = await gather_reports(settings)
    signals = collect_signals(reports)

    errors = [(r.agent, e) for r in reports for e in r.errors]
    for agent, error in errors:
        print(f"[warn] {agent}: {error}", file=sys.stderr)

    print(
        f"[info] {len(signals)} signals from "
        f"{sum(r.scanned for r in reports)} raw items",
        file=sys.stderr,
    )

    brief = await build_brief(settings, reports)

    if args.dry_run:
        _print_brief(brief)
        return 0

    audio = None
    if not args.no_voice:
        from brief.delivery.tts import synthesize

        try:
            audio = await synthesize(brief.voice_script)
        except Exception as exc:  # noqa: BLE001
            # A missing voice note is a downgrade, not a reason to skip the brief.
            log.warning("voice synthesis failed, sending text only: %s", exc)
            print(f"[warn] voice failed: {exc}", file=sys.stderr)

    from brief.delivery.telegram import TelegramDelivery

    await TelegramDelivery(settings).send(brief, audio)
    print("[ok] brief delivered", file=sys.stderr)

    # Agent failures shouldn't block delivery, but they should colour the exit
    # code so a scheduled run surfaces as degraded rather than silently fine.
    return 1 if errors else 0


def _print_brief(brief: Brief) -> None:
    from brief.delivery.telegram import render_html

    bar = "=" * 68
    print(f"\n{bar}\nVOICE SCRIPT ({len(brief.voice_script.split())} words)\n{bar}")
    print(brief.voice_script)
    print(f"\n{bar}\nTEXT BRIEF\n{bar}")
    print(render_html(brief))
    print()


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if (args.verbose or args.explain) else logging.INFO,
        format="%(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    if not args.verbose:
        logging.getLogger("googleapiclient").setLevel(logging.ERROR)
        logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001
        log.error("%s", exc)
        if args.verbose:
            raise
        print(f"\n[fail] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
