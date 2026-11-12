# Design: keeping the human in the loop

Coding with Claude is hit or miss when the person directing it cannot see what it did to the system. Claude edits
many files at once and prints more than anyone can read, so the person's feedback becomes guesswork: they approve
or object without knowing which parts changed, what now depends on what, or which choices were made for them.
system-diff exists to close that gap. These are the principles it is built on, and the design that follows from them.

## Principles

1. **Show the system, not the text.** Every change is reported at the level a person designs at: parts, entry
   points, connections, data, outside systems. Lines of code are one click away, never the first thing shown.
2. **Ambient and immediate.** The picture follows Claude within about a second of an edit, without anyone asking,
   and it never makes anyone wait. When it is not current (still reading, a slow machine), it says so.
3. **Spend the person's attention on decisions.** Not every change deserves a look. Changes are ranked by
   consequence: a new outside system, a new or changed entry point, a change to stored data, a new dependency
   between parts, a part removed, a boundary crossed. The few that need judgement come first, in plain words, each
   with why it matters.
4. **Intent against effect.** What the person asked for is shown next to what changed, so drift is visible: parts
   changed that the request did not mention, or a request that changed nothing structural.
5. **Observe and understand; the person directs.** system-diff never acts on the person's behalf and puts no
   controls between them and Claude: no approve, ask or undo buttons. It makes the changes plain enough that the
   person can say what they want in the chat, in their own words, knowing what changed and why it matters. The
   loop between the person and Claude stays in Claude Code; the pane, the web page and the terminal view only show.
   (The web page's Send to Claude box is the one exception, and it only types the person's own words into the chat.)
6. **Understanding is tracked.** The person's checkpoint is what they have seen and understood; everything since is
   highlighted until they mark it seen. Nothing changes silently behind them.
7. **One picture everywhere.** The pane beside the chat, the web page and the terminal view show the same live model
   from one resident engine, so they never disagree.
8. **Honest about certainty.** A partial read is labelled partial; a grouping an LLM proposed is labelled as such;
   what the engine could not read is named.

## Design

### One resident engine per repository

The web server (`system-diff watch`) becomes the engine every view talks to. It holds, in memory:

- the facts of every file (cached on disk by content hash, `.git/system-diff/files/`), the blueprint combined from
  them, and the comparisons against the checkpoint and the turn's start;
- the pages already drawn, by view, width, zoom and part, each with the model version it was drawn from;
- a timeline of structural changes: what changed, when, during which request, by which agent, ranked by impact.

It starts with the first view that needs it (pane, web page, terminal view or hook) and stays up while the
repository is in use. A cold start reads the repository in the background, publishing a fuller model as it goes.

### Changes arrive as events, not by re-reading everything

- **Claude's edits:** the plugin's hooks tell the engine the paths Claude edited the moment the tool returns
  (`POST /touch`). The engine reads those files from disk, hashes them, re-parses only the ones whose content
  changed, and recombines.
- **Other edits** (the person's editor, a `git checkout`): a cheap `git status` every second or two, which uses git's
  own file-time cache, names the files that changed; the same path follows.
- Recombining is cheap counting over cached facts. When the structure is unchanged, the previous layout is kept and
  only the change markers move, so boxes do not jump around.

### Views are thin clients

- `system-diff panel` (what the pane runs) asks the engine for the page and waits for the next version (long poll),
  so the pane updates the moment the model changes instead of re-reading every few seconds. With no engine
  running, it starts one and answers from its own read meanwhile, as today.
- The web page and the terminal view use the same pages and the same long poll.

### The person's loop

- **Worth a look first:** the Now view opens with the few changes most worth understanding, ranked by consequence,
  each in plain words with why it matters, what it touches (lit in the diagram) and the request it came from.
- **Intent against effect:** the request is shown with the parts it changed; parts changed that it did not name
  are marked, as a fact to notice, never as a verdict.
- **Short where space is short, full on hover:** the pane and the terminal view keep each change to a line or two;
  the web page shows the whole story on hover: for a box, what it is, what changed in it, why that matters, the
  request and the agent behind it; for an agent, its area, what it edited and read, and what it told the others.
- **Understanding is the checkpoint:** marking the current state as seen (as today) is the person's record of what
  they understand; everything since stays highlighted.

### Objectives: what the person is optimising for

- **Per branch:** each branch keeps its own checkpoint, since branches can hold different designs. A new
  branch starts from what was seen on the branch it came from, or from where it parted from it, so it shows
  what the branch itself changed. The caches are keyed by content, so they serve every branch.
- **Learned from repeated requests.** While the person iterates with Claude they keep asking for the same things
  ("fast on large repositories", "must work on Windows", "only shows, never acts"). An LLM reads their own typed
  requests (system-diff's activity log and Claude Code's transcripts of this project; never tool output, pasted logs
  or code) in the background, at most every ten minutes, and names up to seven objectives, each with the quotes it
  rests on and how a change could break it. A quote that is not in a request is not believed.
- **Shown in a few blocks** beside the diagram, most asked first.
- **Kept in Claude's context at each prompt** in a few lines, so a later iteration does not forget them.
- **Changes that touch one are marked:** a change to a part an objective is about, or to a part, endpoint or outside
  system its words name, is flagged in Worth a look, as a fact to check, never as a verdict.
- **Checked by the agents:** when a team works, every agent's brief carries the objectives; an agent that tests or
  evaluates one reports it (`check_objective`: checking, holds, broken, unclear, with a line of evidence), which is
  kept beside the objective, shown per agent in the Agents tab and published on the bus (`objective:<id>`).
- **The person can clear one** they think is wrong (`x` in the pane, `system-diff objectives clear <id>`). This
  changes system-diff's own notes, not the code, so it is consistent with "only shows". A cleared objective comes
  back only when at least three later requests clearly ask for it again; `restore` takes a clear back.
- Stored in `<git dir>/system-diff/objectives.json`: private, never committed, never sent anywhere but the LLM the
  person already configured. Off with `SYSTEM_DIFF_OBJECTIVES=0`.
