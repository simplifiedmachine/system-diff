import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { Diagram, Pick, Row } from '../types'

// System diff, in a pane beside the chat: what Claude is doing to your system as it works, and what it all means.
// Tabs: Now (what Claude is doing, what needs your attention, the part zoomed in on, what just happened, a topic to
// learn), Map, Changes, Topics. The diagram (diagram.tsx) takes the keys once clicked: ← ↑ ↓ → pick a block, Enter
// zooms in on it, b goes back, f follows Claude again. Content: `system-diff panel`.
//
// One meaning per colour: blue is where Claude is and what you picked, yellow what changed and what needs you,
// green what is new, done or to learn, white what to read, gray the background, red only what was taken away.

const PANE = 'system-diff'
const COMMANDS = ['system-diff', 'system-diff-pane'] as const
const VIEWS = [['now', 'Now', 'n'], ['map', 'Map', 'm'], ['changes', 'Changes', 'c'], ['topics', 'Topics', 't']] as const

const head = atom({ plugin: 'system-diff-pane', key: 'head' } as const, [] as Row[], { shape: 'head-1' })
const diagram = atom({ plugin: 'system-diff-pane', key: 'diagram' } as const, null as Diagram | null,
  { shape: 'diagram-1' })
const rows = atom({ plugin: 'system-diff-pane', key: 'rows' } as const, [] as Row[], { shape: 'rows-1' })
const picks = atom({ plugin: 'system-diff-pane', key: 'picks' } as const, [] as Pick[], { shape: 'picks-1' })
const shown = atom({ plugin: 'system-diff-pane', key: 'shown' } as const, '', { shape: 'shown-1' })
const view = atom({ plugin: 'system-diff-pane', key: 'view' } as const, 'now', { shape: 'view-1' })
const part = atom({ plugin: 'system-diff-pane', key: 'part' } as const, '', { shape: 'part-1' })
const back = atom({ plugin: 'system-diff-pane', key: 'back' } as const, [] as string[], { shape: 'back-1' })
const note = atom({ plugin: 'system-diff-pane', key: 'note' } as const, 'Reading the system…', { shape: 'note-1' })

// How to run system-diff: the repository's own virtualenv first, then whatever is on PATH.
const RUNNERS: string[][] = [['.venv/bin/python', '-m', 'system_diff'], ['system-diff'], ['python3', '-m', 'system_diff']]

const COLORS: Record<string, string> = {
  accent: 'blue', focus: 'blue', near: 'blue', data: 'blue',
  attention: 'yellow', changed: 'yellow',
  good: 'green', added: 'green',
  removed: 'red', network: 'magenta',
}

let runner: string[] | undefined
let busy = false
let again = false
let columns = 80 // the pane's width, as last drawn: drawing may not write state, so it is kept here

async function refresh($: EngineInterface): Promise<void> {
  if (busy) {
    again = true // a press while a look runs: look again right after
    return
  }
  busy = true
  try {
    do {
      again = false
      const argv = ['panel', '--width', String(columns), '--view', await read($, view), '--part', await read($, part)]
      let done = false
      for (const run of runner ? [runner] : RUNNERS) {
        try {
          const got = await $.process.run([...run, ...argv], { timeoutMs: 20000 })
          if (got.exitCode !== 0) continue
          const data = JSON.parse(got.stdout) as
            { head?: Row[]; diagram?: Diagram | null; rows: Row[]; picks?: Pick[]; part?: string }
          runner = run
          const choices = data.picks ?? []
          await update($, head, () => data.head ?? [])
          await update($, diagram, () => data.diagram ?? null)
          await update($, rows, () => data.rows)
          await update($, picks, () => choices)
          await update($, shown, () => data.part ?? '')
          await update($, note, () => '')
          done = true
          break
        } catch {
          // not this way: try the next
        }
      }
      if (!done) {
        await update($, note, () =>
          'system-diff is not installed here: pipx install "system-diff[mcp,tui]", then system-diff setup claude')
      }
    } while (again)
  } finally {
    busy = false
  }
}

// A look in the background: one that fails (a session closing, a slow disk) is let go, the next comes soon enough.
function soon($: EngineInterface): void {
  refresh($).catch(() => undefined)
}

async function choose($: EngineInterface, next: string): Promise<void> {
  await update($, view, () => next)
  await refresh($)
}

async function zoom($: EngineInterface, id: string): Promise<void> {
  const from = await read($, shown)
  if (from && from !== id) await update($, back, list => [...list, from].slice(-20))
  await update($, part, () => id)
  await update($, view, () => 'now')
  await refresh($)
}

async function goBack($: EngineInterface): Promise<void> {
  const list = await read($, back)
  if (list.length === 0) return
  await update($, back, () => list.slice(0, -1))
  await update($, part, () => list[list.length - 1] ?? '')
  await refresh($)
}

async function follow($: EngineInterface): Promise<void> {
  await update($, back, () => [])
  await update($, part, () => '')
  await update($, view, () => 'now')
  await refresh($)
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    void $.ui.open({ id: PANE, title: 'System diff' })
    // `/system-diff` opens it; `/system-diff-pane` too, the name it had first. A name someone else holds (an older
    // setup's skill held /system-diff) is skipped: the pane still opens, and the other name still works.
    for (const name of COMMANDS) {
      try {
        await $.command.register({ name, description: 'Show the System diff pane beside the chat' })
      } catch {
        // taken
      }
    }
    soon($)
    $.clock.every(3000, () => soon($))
    return next(e)
  })

  for (const command of COMMANDS) {
    on('command.run', { command }, async $ => {
      await refresh($)
      await $.ui.open({ id: PANE, title: 'System diff' })
      return { text: 'System diff pane opened.' }
    })
  }

  // The engine's own diff of the files sits beside this one: name it for what it is.
  on('ui.open', async ($, e, next) => next(e.title === 'Diff' ? { ...e, title: 'File diff' } : e))

  on('tool.call', async ($, e, next) => {
    const ran = await next(e)
    if (['Edit', 'Write', 'MultiEdit', 'NotebookEdit', 'Read'].includes(e.tool)) soon($)
    return ran
  })

  // What the diagram (diagram.tsx) asks for: zoom in on a block, go back, follow Claude, another tab.
  on('ui.message', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const data = (e.data ?? {}) as { zoom?: string; back?: boolean; follow?: boolean; view?: string }
    if (typeof data.zoom === 'string') await zoom($, data.zoom)
    else if (data.back) await goBack($)
    else if (data.follow) await follow($)
    else if (typeof data.view === 'string') await choose($, data.view)
    return {}
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const elements = $.ui.resolve(e)
    const { Box, Text, Button } = elements
    // A surface that draws a module's region (the terminal) gets the diagram with the keys; any other, its blocks
    // as buttons to press.
    const Client = 'Client' in elements ? elements.Client : undefined
    const picks_ = await read($, picks)
    columns = e.viewport?.columns ?? columns // the next look draws for it
    const top = await read($, head)
    const drawing = await read($, diagram)
    const list = await read($, rows)
    const said = await read($, note)
    const now = await read($, view)
    const picked = await read($, part)
    const behind = await read($, back)

    const line = (row: Row, i: number) =>
      row.length === 0 ? (
        <Text key={`r${i}`}> </Text>
      ) : (
        <Text key={`r${i}`} wrap="truncate-end">
          {row.map(([text, style]) => {
            const words = style.split(' ')
            const color = words.map(w => COLORS[w]).find(Boolean)
            return (
              <Text
                color={color}
                bold={['bold', 'focus', 'heading', 'attention'].some(w => words.includes(w))}
                dimColor={(words.includes('muted') || words.includes('heading')) && !color}
                inverse={words.includes('picked')}
              >
                {text}
              </Text>
            )
          })}
        </Text>
      )

    return (
      <Box flexDirection="column">
        <Box flexDirection="row" gap={2}>
          {VIEWS.map(([id, label, key]) => (
            <Button key={`view-${id}`} hotkey={key} plain onPress={() => void choose($, id)}>
              {now === id ? <Text color="blue" bold underline>{label}</Text> : <Text dimColor>{label}</Text>}
            </Button>
          ))}
          {picked !== '' && (
            <Button key="follow" hotkey="f" plain onPress={() => void follow($)}>
              <Text color="blue">● Follow Claude</Text>
            </Button>
          )}
          {behind.length > 0 && (
            <Button key="back" hotkey="b" plain onPress={() => void goBack($)}><Text dimColor>← Back</Text></Button>
          )}
        </Box>
        {said !== '' && <Text dimColor>{said}</Text>}
        {top.map(line)}
        {drawing !== null && Client !== undefined && (
          <Client key="diagram" module="./diagram.tsx" props={{ diagram: drawing, canGoBack: behind.length > 0 }} />
        )}
        {drawing !== null && Client === undefined && (
          <Box flexDirection="row" flexWrap="wrap" columnGap={2}>
            {picks_.map(c => (
              <Button key={`block-${c.id}`} plain onPress={() => void zoom($, c.id)}>{c.title}</Button>
            ))}
          </Box>
        )}
        {list.map(line)}
      </Box>
    )
  })
}
