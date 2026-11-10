---
name: system-diff-explain
description: Shows the person what a code change does to the system's structure (parts, endpoints, connections, settings, tables, outside systems) and teaches them the parts and technologies involved. Use after changing code that may change the structure, before asking them to review, when they ask what changed, or when they ask what a part of the system is or does.
---

# Showing the system diff

After a change that may touch the system's structure:

1. Call the `system_diff` MCP tool, or run `system-diff diff --base checkpoint --head . --out .git/system-diff/latest`.
   With base `turn`, it shows only what this turn changed.
2. Tell the person, in two or three plain sentences built only from its plan, what changed in the system and why it
   matters: an endpoint added or removed, a new connection between parts, a new outside system, changed stored data,
   or code changed without tests.
3. Point them at the picture: the path the tool returns, the System diff pane beside the chat (`/system-diff`), or
   its web page (`system-diff watch`), which redraw as you work.
4. When they say they have seen it, call the `checkpoint` tool (or run `system-diff checkpoint`), so the next diff
   shows only what is new to them.

# Teaching the system

When the person asks what a part is or does, how it fits, or what a technology in it is, call the `explain` tool with
the part (empty for the one you are working on) and answer from what it returns: what the part is, what is changing in
it and why that matters, and the topics worth knowing, with their links. To show them, paste its `diagram` in a code
block. Keep it short and plain.

Never mention a part, endpoint, table or system that is not in the plan or in what `explain` returns. For a picture of
something else (a process, a sequence of calls), use the `draw` tool.
