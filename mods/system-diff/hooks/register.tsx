import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { Act, Page, Row } from '../types'

// System diff, in a pane beside the chat: the system as a system designer draws it (who uses it, its entry points,
// its containers and their components, its data and the systems outside it, joined by named flows) with Claude's
// component lit and this turn's changes coloured; what needs your attention; what the turn did to the design; how
// the part is designed; what just happened. Tabs: Now, Map, Changes, Topics. The page (page.tsx) takes the keys once
// clicked: ← ↑ ↓ → pick a block and light its flows, Enter zooms in, b back, f follows Claude. Content: `system-diff
// panel --protocol 2`, laid out for the pane's own width.
//
// One meaning per colour: blue is where Claude is and what you picked, yellow what changed and what needs you,
// green what is new, done or to learn, white what to read, gray the background, red only what was taken away.

const PANE = 'system-diff'
const VERSION = '0.5.3' // the plugin's own, as .claude-plugin/plugin.json says (a test keeps them one)
const WIDEST = 999 // as wide as Claude Code docks a pane: 45% of the terminal
const COMMANDS = ['system-diff', 'system-diff-pane'] as const
const VIEWS = [['now', 'Now', 'n'], ['review', 'Review', 'r'], ['agents', 'Agents', 'a'], ['topics', 'Topics', 't']] as const

const page = atom({ plugin: 'system-diff', key: 'page' } as const, null as Page | null, { shape: 'page-2' })
const view = atom({ plugin: 'system-diff', key: 'view' } as const, 'now', { shape: 'view-1' })
const part = atom({ plugin: 'system-diff', key: 'part' } as const, '', { shape: 'part-1' })
const back = atom({ plugin: 'system-diff', key: 'back' } as const, [] as string[], { shape: 'back-2' })
const note = atom({ plugin: 'system-diff', key: 'note' } as const, 'Reading the system…', { shape: 'note-1' })
// How compact the diagrams are: -1 by the pane's width (compact below 110 columns), 0 full, 1 compact, 2 tiny.
const zoomLevel = atom({ plugin: 'system-diff', key: 'zoom' } as const, -1, { shape: 'zoom-1' })
const ZOOM_WORD = ['full', 'compact', 'tiny']

// How to run system-diff: the repository's own virtualenv first, then whatever is on PATH.
// First the plugin's own launcher (scripts/run.sh): it finds system-diff, or installs the plugin's own copy of it the
// first time. Then, for a plugin loaded some other way (--plugin-dir), the places system-diff usually is.
const LAUNCHER = 'exec sh "${CLAUDE_PLUGIN_ROOT:-$HOME/.claude/plugins/cache/system-diff/system-diff/' + VERSION +
  '}/scripts/run.sh" "$@"'
const RUNNERS: string[][] = [
  ['sh', '-c', LAUNCHER, 'system-diff'],
  ['.venv/bin/python', '-m', 'system_diff'], ['.venv/Scripts/python.exe', '-m', 'system_diff'], ['system-diff'],
  ['python3', '-m', 'system_diff'], ['python', '-m', 'system_diff'],
]
const COLORS: Record<string, string> = {
  accent: 'blue', focus: 'blue', attention: 'yellow', changed: 'yellow', good: 'green', added: 'green',
  removed: 'red', network: 'magenta',
}

let runner: string[] | undefined
let busy = false
let again = false
let columns = 100 // the pane's body width, as last drawn or reported by the page: drawing may not write state
let last = '' // the last page drawn, so an unchanged look redraws nothing
// What the web page's Send to Claude box asked for, by id: waiting to be taken by the session, being submitted now,
// or taken. The page hands each item over once (the outbox is emptied as it is read), so one the session refuses
// waits here for the next look.
const waiting = new Map<string, string>()
const trying = new Set<string>()
const sent = new Set<string>()
let regionTop = 1 // rows above the page in the pane's body: the tabs, and a note while there is one
let windowAt = 0 // the pane's scroll: its first row showing, and how many show
let windowRows = 40
let wantOffset: number | null = null

async function run($: EngineInterface, argv: string[]): Promise<{ out: string; old: boolean; installing?: boolean } | null> {
  for (const how of runner ? [runner] : RUNNERS) {
    try {
      const got = await $.process.run([...how, ...argv], { timeoutMs: 20000 })
      if (got.exitCode === 0) {
        runner = how
        return { out: got.stdout, old: false }
      }
      if (/unrecognized arguments|invalid choice/.test(got.stderr)) return { out: '', old: true }
      if (/system-diff: installing/.test(got.stderr)) return { out: '', old: false, installing: true }
    } catch {
      // not this way: try the next
    }
  }
  return null
}

// Submit what was typed in the web page as the person's own words, each id once. The call resolves only as its turn
// starts (once the session is idle), so the look does not wait for it; and one made where the session will not take it
// (inside the /system-diff command's own hook) is tried again at the next look, the one on the clock.
function deliver($: EngineInterface, send: Page['send']): void {
  for (const item of Array.isArray(send) ? send : []) {
    if (!item || typeof item.id !== 'string' || typeof item.text !== 'string' || !item.text) continue
    if (!sent.has(item.id)) waiting.set(item.id, item.text)
  }
  for (const [id, text] of waiting) {
    if (trying.has(id)) continue
    trying.add(id)
    const done = (ok: boolean) => {
      trying.delete(id)
      if (!ok) return
      waiting.delete(id)
      sent.add(id)
    }
    try {
      $.prompt.submit({ text, asUser: true }).then(() => done(true), () => done(false))
    } catch {
      done(false)
    }
  }
}

async function refresh($: EngineInterface): Promise<void> {
  if (busy) {
    again = true // asked while a look runs: look again right after
    return
  }
  busy = true
  try {
    do {
      again = false
      const argv = ['panel', '--protocol', '2', '--width', String(columns), '--view', await read($, view),
                    '--part', await read($, part)]
      const level = await read($, zoomLevel)
      if (level >= 0) argv.push('--zoom', String(level)) // left out, the engine picks by the width (older ones too)
      const got = await run($, argv)
      if (got && !got.old && !got.installing) {
        const data = JSON.parse(got.out) as Page
        deliver($, data.send)
        if (got.out !== last) {
          last = got.out
          await update($, page, () => data)
        }
        if ((await read($, note)) !== '') await update($, note, () => '')
      } else {
        await update($, note, () => got?.installing
          ? 'Installing system-diff for this plugin (first use, about a minute)…'
          : got?.old
          ? 'This pane needs a newer system-diff: pipx upgrade system-diff'
          : 'system-diff could not be installed here (it needs Python 3.11 or newer): pipx install "system-diff[mcp]"')
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
  const from = await read($, part)
  if (from !== id) await update($, back, list => [...list, from].slice(-20))
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

async function learn($: EngineInterface, key: string): Promise<void> {
  await run($, ['profile', '--learn', key])
  await refresh($)
}

// Bring a row of the page into view when the keys pick a block below or above the pane's window.
async function reveal($: EngineInterface, y: number): Promise<void> {
  const row = regionTop + y
  if (row >= windowAt && row < windowAt + windowRows - 1) return
  wantOffset = Math.max(0, row - Math.floor(windowRows / 3))
  try {
    await $.ui.scroll({ to: 'start', in: PANE })
  } catch {
    wantOffset = null
  }
}

// Scroll the pane by rows, for the keys of a page that has them (shift ↑ ↓, Page Up and Down).
async function scrollBy($: EngineInterface, rows: number): Promise<void> {
  const by = Math.abs(rows) >= 1000 ? Math.sign(rows) * Math.max(1, windowRows - 2) : rows
  wantOffset = Math.max(0, windowAt + by)
  try {
    await $.ui.scroll({ to: 'start', in: PANE })
  } catch {
    wantOffset = null
  }
}

// As wide as Claude Code docks a pane, the first time: a width the person set (dragged, or with the pane's keys:
// ctrl+x ← →) is theirs from then on.
// More of the system in the pane (zoom out, +1) or bigger boxes (zoom in, -1), from where the page is drawn now.
async function zoomBy($: EngineInterface, by: number): Promise<void> {
  const drawnAt = (await read($, page))?.zoom ?? 0
  await update($, zoomLevel, () => Math.max(0, Math.min(2, drawnAt + by)))
  await refresh($)
}

function open($: EngineInterface) {
  return $.ui.open({ id: PANE, title: 'System diff', columns: WIDEST })
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    void open($)
    // `/system-diff` opens it; `/system-diff-pane` too, the name it had first. A name someone else holds is skipped.
    for (const name of COMMANDS) {
      try {
        await $.command.register({ name, description: 'Show the System diff pane beside the chat (close: hide it)',
                                   argumentHint: '[close]' })
      } catch {
        // taken
      }
    }
    soon($)
    $.clock.every(3000, () => soon($))
    return next(e)
  })

  for (const command of COMMANDS) {
    on('command.run', { command }, async ($, e) => {
      if (e.args.trim().toLowerCase() === 'close') {
        await $.ui.close({ id: PANE })
        return { text: 'System diff pane closed. /system-diff opens it again.' }
      }
      await refresh($)
      await open($)
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

  // The pane's own scroll: kept, and moved where a reveal asked.
  on('ui.scroll', { component: 'Pane', requestId: PANE }, async ($, e, next) => {
    windowRows = e.bodyRows
    if (wantOffset !== null && e.origin.kind === 'plugin') {
      const offset = wantOffset
      wantOffset = null
      windowAt = offset
      return next({ ...e, offset })
    }
    windowAt = e.offset
    return next(e)
  }).catch(($, e, next) => next(e)) // a failure here must never hold the person's own scrolling

  // What the page (page.tsx) asks for.
  on('ui.message', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const data = (e.data ?? {}) as Act & { back?: boolean; follow?: boolean; width?: number; reveal?: number;
                                             scrollBy?: number; zoomBy?: number }
    if (typeof data.width === 'number' && data.width > 0 && data.width !== columns) {
      columns = data.width
      await refresh($)
    } else if (typeof data.reveal === 'number') await reveal($, data.reveal)
    else if (typeof data.scrollBy === 'number') await scrollBy($, data.scrollBy)
    else if (typeof data.zoom === 'string') await zoom($, data.zoom)
    else if (typeof data.learn === 'string') await learn($, data.learn)
    else if (data.back) await goBack($)
    else if (data.follow) await follow($)
    else if (typeof data.zoomBy === 'number') await zoomBy($, data.zoomBy)
    else if (typeof data.view === 'string') await choose($, data.view)
    return {}
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const elements = $.ui.resolve(e)
    const { Box, Text, Button } = elements
    // A surface that draws a module's region (the terminal, desktop) gets the page with keys and clicks; any other
    // its rows as text and its blocks as buttons.
    const Client = (e.surface === 'terminal' || e.surface === 'desktop') && 'Client' in elements
      ? elements.Client : undefined
    columns = e.props.bodyColumns || columns // the next look is laid out for it
    windowAt = e.props.scroll.offset
    windowRows = e.props.scroll.bodyRows
    const drawn = await read($, page)
    const said = await read($, note)
    const now = await read($, view)
    const zoomed = await read($, part)
    const behind = await read($, back)
    regionTop = 1 + (said !== '' ? 1 : 0)

    const line = (row: Row, i: number) =>
      row.length === 0 ? (
        <Text key={`r${i}`}> </Text>
      ) : (
        <Text key={`r${i}`} wrap="truncate-end">
          {row.map(([text, style]) => {
            const words = style.split(' ')
            const color = words.map(w => COLORS[w]).find(Boolean)
            return (
              <Text color={color} bold={['bold', 'heading', 'attention'].some(w => words.includes(w))}
                    dimColor={(words.includes('muted') || words.includes('heading')) && !color}>
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
          {zoomed !== '' && (
            <Button key="follow" hotkey="f" plain onPress={() => void follow($)}>
              <Text color="blue">● Follow Claude</Text>
            </Button>
          )}
          {behind.length > 0 && (
            <Button key="back" hotkey="b" plain onPress={() => void goBack($)}><Text dimColor>← Back</Text></Button>
          )}
          {drawn?.web ? <Text key="web" dimColor>{`🌐 ${drawn.web}`}</Text> : null}
          <Button key="zoom-out" plain onPress={() => void zoomBy($, 1)}><Text dimColor>⊖</Text></Button>
          <Text dimColor>{ZOOM_WORD[drawn?.zoom ?? 0] ?? 'full'}</Text>
          <Button key="zoom-in" plain onPress={() => void zoomBy($, -1)}><Text dimColor>⊕</Text></Button>
          <Button key="close" plain onPress={() => void $.ui.close({ id: PANE })}><Text dimColor>× close</Text></Button>
        </Box>
        {said !== '' && <Text dimColor>{said}</Text>}
        {drawn !== null && Client !== undefined && (
          <Client key="page" module="./page.tsx" width="100%" props={{ page: drawn, canGoBack: behind.length > 0 }} />
        )}
        {drawn !== null && Client === undefined && drawn.rows.map(line)}
        {drawn !== null && Client === undefined && (
          <Box flexDirection="row" flexWrap="wrap" columnGap={2}>
            {drawn.blocks.filter(b => b.enter?.zoom).slice(0, 40).map(b => (
              <Button key={`block-${b.id}`} plain onPress={() => void zoom($, b.enter?.zoom ?? '')}>
                {b.id.replace(/^b:(g:)?/, '')}
              </Button>
            ))}
          </Box>
        )}
        <Box flexDirection="row" justifyContent="flex-end"><Text dimColor>{`v${VERSION}`}</Text></Box>
      </Box>
    )
  })
}
