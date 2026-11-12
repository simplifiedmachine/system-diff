# Contributing to system-diff

Thank you for wanting to help. system-diff exists so that people can keep up with what AI agents do to their
systems; every contribution that makes that clearer, faster or available to more people is welcome.

## Reporting a bug

Open an issue with the **Bug report** template. The most useful things to include:

- what you did, what you expected, and what happened (screenshots of the pane or the web page help a lot);
- your OS, Python version, `system-diff --version`, and Claude Code's version;
- the output of `python -m system_diff doctor`, run in the repository where it happened: it times a look, counts git
  calls, and checks the hooks, the engine and the web page;
- if you can, a small repository (or the shape of yours: languages, frameworks, size) where it happens.

Security problems: please do not open a public issue; use GitHub's private vulnerability reporting on this repository.

## Suggesting a feature

Open an issue with the **Feature request** template and describe the problem it solves for you, before the solution.
The roadmap in the README lists what is planned; comment on an item if you want it or want to build it.

## Setting up

```sh
git clone https://github.com/simplifiedmachine/system-diff
cd system-diff
python -m venv .venv && . .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
python -m pytest -q                                  # the engine's tests
```

The Claude Code plugin lives in `mods/system-diff/` (TypeScript, run by Claude Code itself):

```sh
claude plugin validate mods/system-diff
claude plugin test mods/system-diff                  # the pane's tests
claude --plugin-dir mods/system-diff                 # try your changes in a real session
```

A project's own `.venv` with system-diff in it is used before any installed copy, so the pane in this repository runs
your working tree.

## How the code is laid out

system-diff draws its own architecture: run `python -m system_diff panel --protocol 2 --width 120 --view now`, open
the web page (`python -m system_diff watch`), or look at the map at the bottom of the README. In short:

| Area | Where |
|---|---|
| Reading code into a blueprint (parts, routes, settings, tables, outside systems) | `extract.py`, `config.py`, `sources.py`, `gitfs.py` |
| Comparing two states, checkpoints, the live engine | `compare.py`, `diff.py`, `live.py` |
| Turning diffs into plain words, ranking, names, history, objectives | `design.py`, `human.py`, `names.py`, `history.py`, `objectives.py` |
| The diagram model and drawing | `architecture.py`, `canvas.py`, `pictures.py`, `inside.py` |
| Pages for the pane, the web page and the terminal view | `pane.py`, `panel.py`, `watch.py`, `tui.py` |
| Claude Code: hooks, MCP server, agents | `agent.py`, `mcp_server.py`, `agents.py`, `mods/system-diff/` |

A new language is mostly a new set of per-file facts in `extract.py` (imports, routes, settings reads): see how
Python and TypeScript are done, and add a fixture repository in `tests/`.

## Making a pull request

1. Fork, branch from `main`, and keep the change focused: one fix or feature per PR.
2. Add or update tests with it. Bugs get a test that fails without the fix.
3. Run the checks CI runs:
   ```sh
   python -m pytest -q
   claude plugin test mods/system-diff               # if you touched the plugin
   system-diff map --dir . --out docs/diagrams         # the system map, if the structure changed
   system-diff readme --json docs/diagrams/system.json
   ```
   CI runs the tests on Linux and Windows (Python 3.11 to 3.13) and checks that the system map is current.
4. Write the PR description as the template asks: what and why, how you tested it, and what it changes in the
   system's structure (the system-diff comment on your PR shows it).
5. Keep the principles in [docs/design.md](docs/design.md) in mind: show the system, not the text; never make
   anyone wait; plain words; only show, never act for the person; honest about what is certain.

Maintainers review as soon as they can; small PRs are reviewed fastest. Versions and releases are done by the
maintainers.

## Style

- Python: standard library first; new dependencies need a good reason (and an optional extra where they can be).
- Names and messages in plain words a person who did not write the code understands.
- Comments say why, not what.
- No network calls or git processes on a look's path: the pane asks every second.

## Code of conduct

Be kind and assume good intent. Harassment or discrimination of any kind is not tolerated; maintainers may remove
comments or contributors that break this.
