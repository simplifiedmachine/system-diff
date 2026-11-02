"""system-diff: the system's structure at a git ref, the typed diff between two refs, and pictures of both.

    system-diff map  [--ref REF | --dir DIR] [--out DIR]            -> system.json, system.svg
    system-diff diff --base A [--head B|.] [--out DIR] [--narrate]  -> diff.json, diff.svg, plan.txt, headline.txt
    system-diff readme [--json system.json] [--stale PNGS]          -> the README's generated system map
    system-diff draw SPEC.json...                                   -> a hand-made picture beside each spec
    system-diff png [--host HOST] FILE.svg...                       -> FILE.png beside each SVG
    system-diff summarize --diff diff.json                          -> the plain-words summary alone

Deterministic: everything is read from git objects (no checkout) or a directory, with the standard library only.
`--head .` diffs against the working tree, uncommitted changes included.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

from . import __version__, config, graph, narrate, pictures, png, readme
from .diff import diff, numstat
from .extract import Code, extract
from .sources import DirSource, GitSource, git, repo_root


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
    worktree = args.head in (".", "worktree")
    if worktree:
        on_disk = config.load(root=root, path=args.config)
        head_src = DirSource(root, config.segment_rx(on_disk["code"]["skip"]))
    else:
        head_src = GitSource(args.head, root)
    cfg = config.load(head_src, root, args.config)  # one vocabulary for both sides
    a, b = extract(GitSource(args.base, root), cfg), extract(head_src, cfg)
    stat = numstat(root, args.base, None if worktree else args.head, cfg)
    d = diff(a, b, stat, cfg.get("deploy", []), Code(cfg))
    d["commits"] = git("log", "--format=%s%n%n%b", f"{args.base}..HEAD" if worktree else f"{args.base}..{args.head}",
                       cwd=root)[:6000]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "diff.json").write_text(json.dumps(d, indent=1, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    (out / "plan.txt").write_text(d["plan"] + "\n", encoding="utf-8", newline="\n")
    (out / "headline.txt").write_text(d["headline"] + "\n", encoding="utf-8", newline="\n")
    svg = graph.diff_svg(d, a, b, cfg)
    if svg:
        pictures.write_if_changed(out / "diff.svg", svg)
    elif (out / "diff.svg").is_file():
        (out / "diff.svg").unlink()  # an older run's picture must not pass for this one
    story = narrate.narrate(d, d["commits"], root, cfg) if args.narrate else None
    (out / "narrative.md").write_text((story or "") + "\n", encoding="utf-8", newline="\n")
    print(d["headline"])
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
    d.add_argument("--base", required=True)
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
    if args.cmd == "draw":
        for out in pictures.draw_files([Path(x) for x in args.specs]):
            print(f"drew {out}")
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
