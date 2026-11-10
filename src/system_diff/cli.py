"""system-diff: the system's structure at a git ref, the typed diff between two refs, and pictures of both.

    system-diff map  [--ref REF | --dir DIR] [--out DIR]            -> system.json, system.svg
    system-diff diff --base A [--head B|.] [--out DIR] [--narrate]  -> diff.json, diff.svg, plan.txt, headline.txt
    system-diff readme [--json system.json] [--stale PNGS]          -> the README's generated system map
    system-diff draw SPEC.json...                                   -> a hand-made picture beside each spec
    system-diff png [--host HOST] FILE.svg...                       -> FILE.png beside each SVG
    system-diff summarize --diff diff.json                          -> the plain-words summary alone
    system-diff checkpoint [--ref REF]                              -> mark what you have seen
    system-diff show [PART] [--base checkpoint]                     -> the map, or one part zoomed in, in the terminal
    system-diff tui [--base checkpoint]                             -> a live terminal view that follows the agent
    system-diff watch [--base checkpoint] [--port 8765]             -> a live page of the diff since then
    system-diff setup claude [--scope user|project|local] [--undo]  -> the hooks, status line, a skill, the MCP server
    system-diff mcp                                                 -> the MCP server (stdio)

Deterministic: everything is read from git objects (no checkout) or a directory, with the standard library only.
`--head .` is the working tree, uncommitted and untracked files included; `--base checkpoint` is what you last marked
as seen (HEAD until you mark something); `--base turn` is the working tree when you last sent Claude Code a prompt.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import List, Optional

from . import __version__, activity, compare, config, graph, narrate, pictures, png, readme
from .extract import extract
from .sources import DirSource, GitSource, repo_root


def _write(path: Path, text: str, check: bool) -> bool:
    """Write (or, with --check, only compare); True when the file is or would be different."""
    if check:
        return not path.is_file() or path.read_text(encoding="utf-8") != text
    pictures.write_if_changed(path, text)
    return False


def _map(args, root: Path) -> int:
    if args.dir:
        on_disk = config.load(root=Path(args.dir), path=args.config)
        source = DirSource(Path(args.dir), config.segment_rx(on_disk["code"]["skip"]))
    else:
        source = GitSource(args.ref or "HEAD", root)
    cfg = config.load(source, root, args.config)
    bp = extract(source, cfg)
    out = Path(args.out)
    if not args.check:
        out.mkdir(parents=True, exist_ok=True)
    stale = _write(out / "system.json", json.dumps(bp, indent=1, ensure_ascii=False) + "\n", args.check)
    stale |= _write(out / "system.svg", graph.system_svg(bp, cfg), args.check)
    if args.check:
        print(f"system map {'is out of date' if stale else 'is current'}: {out}/system.svg")
        return 1 if stale else 0
    print(f"system map of {bp['ref']}: {len(bp['components'])} parts, {len(bp['externals'])} outside systems, "
          f"{len(bp['routes'])} routes, {len(bp['tables'])} tables -> {out}/system.svg")
    return 0


def _diff(args, root: Path) -> int:
    c = compare.compare(root, args.base, args.head, args.config)
    d = c.diff
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "diff.json").write_text(json.dumps(d, indent=1, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    (out / "plan.txt").write_text(d["plan"] + "\n", encoding="utf-8", newline="\n")
    (out / "headline.txt").write_text(d["headline"] + "\n", encoding="utf-8", newline="\n")
    svg = graph.diff_svg(d, c.before, c.after, c.cfg)
    if svg:
        pictures.write_if_changed(out / "diff.svg", svg)
    elif (out / "diff.svg").is_file():
        (out / "diff.svg").unlink()  # an older run's picture must not pass for this one
    story = narrate.narrate(d, d["commits"], root, c.cfg) if args.narrate else None
    (out / "narrative.md").write_text((story or "") + "\n", encoding="utf-8", newline="\n")
    print(d["headline"])
    return 0


def _checkpoint(args, root: Path) -> int:
    sha = compare.checkpoint(root, args.ref)
    activity.after_checkpoint(root, args.ref)
    print(f"checkpoint: {args.ref or 'the working tree'} ({sha[:9]}) is what you have seen; "
          "`system-diff diff --base checkpoint --head .` now shows only what comes after it")
    return 0


def _readme(args, root: Path) -> int:
    cfg = config.load(root=root, path=args.config)
    dormant = json.loads(Path(args.json).read_text(encoding="utf-8")).get("dormant", {}) if args.json else {}
    path = Path(args.readme or (root / cfg["readme"]["file"]))
    stale = [x for x in args.stale.split(",") if x]
    if args.check:
        r = cfg["readme"]
        text = path.read_text(encoding="utf-8")
        if r["begin"] not in text or r["end"] not in text:
            print(f"{path} has no system map section")
            return 1
        start, end = text.index(r["begin"]), text.index(r["end"]) + len(r["end"])
        current = text[start:end] == readme.readme_section(dormant, cfg, stale)
        print("README system map " + ("is current" if current else "is out of date"))
        return 0 if current else 1
    changed = readme.update_readme(path, dormant, cfg, stale)
    print("README system map " + ("updated" if changed else "already current"))
    return 0


def _show(args, root: Path) -> int:
    from . import learn, textmap
    c = compare.compare(root, args.base, ".", args.config)
    marks = graph.change_marks(c.diff, c.before, c.after)
    width = args.width or shutil.get_terminal_size((100, 24)).columns
    color = not args.plain and sys.stdout.isatty() and not os.environ.get("NO_COLOR")
    render = textmap.ansi if color else textmap.plain
    if not args.part:
        print(render(textmap.render_map(graph.overview(c.after, c.cfg, marks, c.before), width)))
        print(f"\nSince {c.base}: {c.diff['headline']}")
        return 0
    part = learn.find_part(c, args.part, root)
    if part is None:
        names = ", ".join(sorted(v["label"] for v in c.after["components"].values()))
        print(f"system-diff: no part called {args.part!r}; the parts: {names}", file=sys.stderr)
        return 1
    hood = graph.neighbourhood(c.after, c.cfg, part, marks, c.before)
    print(render(textmap.render_zoom(hood, width)))
    print()
    print(learn.to_markdown(learn.lesson(c, part, GitSource(c.head_sha, root))))
    return 0


def _summarize(args, root: Path) -> int:
    cfg = config.load(root=root, path=args.config)
    d = json.loads(Path(args.diff).read_text(encoding="utf-8"))
    story = narrate.narrate(d, d.get("commits", ""), root, cfg)
    print("BEGIN SUMMARY")
    print(story or "")
    print("END SUMMARY")
    return 0 if story else 1


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="system-diff", description=__doc__.split("\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog="\n".join(__doc__.split("\n")[2:]))
    ap.add_argument("--version", action="version", version=f"system-diff {__version__}")
    ap.add_argument("--repo", help="the repository (default: the one holding the current directory)")
    ap.add_argument("--config", help="a TOML or JSON config (default: system-diff.toml, or pyproject.toml's "
                                     "[tool.system-diff])")
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("map", help="the system as it is: system.json and system.svg")
    m.add_argument("--ref", help="a commit, branch or tag (default HEAD)")
    m.add_argument("--dir", help="a directory instead of a commit, e.g. . for the working tree")
    m.add_argument("--out", default=".")
    m.add_argument("--check", action="store_true", help="write nothing; exit 1 if the files are out of date")
    d = sub.add_parser("diff", help="what a change does to the system")
    d.add_argument("--base", required=True, help="a commit, or checkpoint (what you last marked as seen)")
    d.add_argument("--head", default="HEAD", help="a commit, or . for the working tree (default HEAD)")
    d.add_argument("--out", default=".")
    d.add_argument("--narrate", action="store_true", help="add a plain-words summary from your LLM (narrative.md)")
    r = sub.add_parser("readme", help="rewrite the README's system-map section")
    r.add_argument("--json", default="", help="system.json, for the outside systems that are not drawn")
    r.add_argument("--readme", default="")
    r.add_argument("--stale", default="", help="comma-separated pictures that could not be redrawn")
    r.add_argument("--check", action="store_true", help="write nothing; exit 1 if the section is out of date")
    dr = sub.add_parser("draw", help="draw hand-made pictures from JSON specs")
    dr.add_argument("specs", nargs="+")
    p = sub.add_parser("png", help="SVG to PNG with headless Chromium")
    p.add_argument("--host", help="an SSH host with Docker, when this machine has neither Docker nor Playwright")
    p.add_argument("svgs", nargs="+")
    s = sub.add_parser("summarize", help="the plain-words summary of a diff.json")
    s.add_argument("--diff", required=True)
    c = sub.add_parser("checkpoint", help="mark what you have seen: later diffs from `checkpoint` start here")
    c.add_argument("--ref", help="a commit to mark instead of the working tree")
    sh = sub.add_parser("show", help="the system map, or one part zoomed in with what it is, in the terminal")
    sh.add_argument("part", nargs="?", help="a part's id or name, an outside system's name, or a file in the part")
    sh.add_argument("--base", default="checkpoint", help="marks changes since this point (default checkpoint)")
    sh.add_argument("--width", type=int, default=0, help="columns to fit (default: the terminal's)")
    sh.add_argument("--plain", action="store_true", help="no colours")
    t = sub.add_parser("tui", help="a live terminal view that follows the part being worked on (needs the [tui] extra)")
    t.add_argument("--base", default="checkpoint")
    t.add_argument("--interval", type=float, default=2.0, help="seconds between looks at the files")
    t.add_argument("--pane", action="store_true", help="open it in a new pane beside this terminal instead "
                                                       "(tmux, zellij, WezTerm, kitty, Windows Terminal)")
    w = sub.add_parser("watch", help="a live local page of the diff since the checkpoint")
    w.add_argument("--base", default="checkpoint")
    w.add_argument("--port", type=int, default=8765)
    w.add_argument("--interval", type=float, default=2.0, help="seconds between looks at the files")
    sub.add_parser("mcp", help="run as an MCP server over stdio (needs the [mcp] extra)")
    sub.add_parser("hook", help="what Claude Code's hooks run: the turn's start, the files read and edited, and at "
                                "the end of a turn one line on what it changed in the system")
    pn = sub.add_parser("panel", help="what a pane beside the agent shows, as JSON (for the Claude Code mod)")
    pn.add_argument("--width", type=int, default=80)
    pn.add_argument("--view", default="now", choices=["now", "map", "changes", "topics", "agents"])
    pn.add_argument("--part", default="", help="the part to zoom in on (default: the one the agent works on)")
    pn.add_argument("--protocol", type=int, default=1, help="2: a page of runs and pickable blocks (the mod's painter)")
    pf = sub.add_parser("profile", help="what you want explained: technologies marked new to you")
    pf.add_argument("--learn", metavar="TECH", help="mark a technology new to you, or unmark it")
    sub.add_parser("statusline", help="what Claude Code's status line runs: the part being worked on, the diff in brief")
    st = sub.add_parser("setup", help="wire system-diff into an AI coding tool")
    st.add_argument("tool", choices=["claude"])
    st.add_argument("--scope", default="user", choices=["user", "project", "local"],
                    help="user: every project (default); project: this repository, shared; local: this repository, you")
    st.add_argument("--undo", action="store_true", help="remove what setup added")
    st.add_argument("--no-status-line", action="store_true",
                    help="no status line (and remove system-diff's): for when the pane beside the chat shows it")
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    args = parser().parse_args(argv)
    root = repo_root(Path(args.repo) if args.repo else None)
    if args.cmd == "map":
        return _map(args, root)
    if args.cmd == "diff":
        return _diff(args, root)
    if args.cmd == "readme":
        return _readme(args, root)
    if args.cmd == "summarize":
        return _summarize(args, root)
    if args.cmd == "checkpoint":
        return _checkpoint(args, root)
    if args.cmd == "show":
        try:
            return _show(args, root)
        except ValueError as exc:
            print(f"system-diff: {exc}", file=sys.stderr)
            return 1
    if args.cmd == "tui":
        try:
            from . import tui
        except ImportError as exc:
            print(f'system-diff: the terminal view needs its extra: pip install "system-diff[tui]" ({exc})',
                  file=sys.stderr)
            return 1
        if args.pane:
            from . import companion
            said = companion.open_beside(root)
            vscode = (f'; in VS Code: Tasks: Run Task, then "{companion.TASK}" (setup adds it)'
                      if companion.where() == "vscode" else "")
            print(said or ("system-diff: the terminal view is already open" if companion.running(root) else
                           "system-diff: this terminal cannot open a pane from a command (tmux, zellij, WezTerm, kitty "
                           f"and Windows Terminal can); run `system-diff tui` in a second terminal{vscode}"))
            return 0 if said or companion.running(root) else 1
        return tui.run(root, args.base, args.config, args.interval)
    if args.cmd == "watch":
        from . import watch
        watch.serve(root, args.base, args.port, args.config, args.interval)
        return 0
    if args.cmd == "hook":
        from . import agent
        return agent.run_hook(sys.stdin.read() if not sys.stdin.isatty() else "")
    if args.cmd == "panel":
        from . import panel
        return panel.main(root, args.width, args.view, args.part, args.protocol)
    if args.cmd == "profile":
        from . import profile
        if args.learn:
            on = profile.toggle(args.learn)
            print(f"{args.learn}: {'new to you, explained' if on else 'known, not explained'}")
        else:
            print(json.dumps(profile.load()))
        return 0
    if args.cmd == "statusline":
        from . import statusline
        return statusline.statusline(sys.stdin.read() if not sys.stdin.isatty() else "")
    if args.cmd == "setup":
        from . import agent
        return agent.setup_claude(root, args.scope, args.undo, status_line=not args.no_status_line)
    if args.cmd == "mcp":
        try:
            from . import mcp_server
        except ImportError:
            print('system-diff: the MCP server needs its extra: pip install "system-diff[mcp]"', file=sys.stderr)
            return 1
        mcp_server.main()
        return 0
    if args.cmd == "draw":
        try:
            for out in pictures.draw_files([Path(x) for x in args.specs]):
                print(f"drew {out}")
        except ValueError as exc:
            print(f"system-diff: {exc}", file=sys.stderr)
            return 1
        return 0
    if args.cmd == "png":
        try:
            for out in png.render([Path(x) for x in args.svgs], host=args.host):
                print(f"rendered {out}")
        except (RuntimeError, ValueError) as exc:
            print(f"system-diff: {exc}", file=sys.stderr)
            return 1
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
