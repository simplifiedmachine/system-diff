import type { ClientKeyEvent, ClientModule, JsonValue } from 'claude-code'

import type { Page, PageBlock, Row, Run } from '../types'

// The page `system-diff panel` lays out, painted cell for cell, in its own region of the pane so it gets the keys and
// the clicks: ← ↑ ↓ → pick a block (a box of the system diagram, a component in a container, a technology), Enter
// or a second click zooms in on it, space marks a technology new to you, [ and ] scroll a picture wider than the
// pane, shift ↑ ↓ and Page Up/Down scroll a tall one (as the wheel does), b goes back, f follows Claude, n m c t
// switch tabs, Esc hands the keys back. Shift ← → are left to the terminal.
//
// A picked block is blue; what it connects to (its lines, in and out, and the blocks at their other ends) is lit;
// the rest keeps its colours: yellow what changed, green what is new, red what went, gray the background.

type Props = { page: Page; canGoBack: boolean }
type State = { sel: string; sx: number; mine: boolean; ticking?: boolean; free?: boolean } // free: scrolled by hand

const COLORS: Record<string, string> = {
  accent: 'blue', focus: 'blue', near: 'blue', data: 'blue',
  attention: 'yellow', changed: 'yellow',
  good: 'green', added: 'green',
  removed: 'red', network: 'magenta',
  agent0: 'cyan', agent1: 'magenta', agent2: 'green', agent3: 'yellow', agent4: 'blue',
}
const BOLD = ['bold', 'focus', 'heading', 'attention']
const STEP = 8 // columns a scroll moves

let askedWidth = 0 // the width last reported to the hooks module, so a resize is told once
let pendingWidth = 0 // a width the region took, told once it has held for a moment (a drag passes many)
let pendingTicks = 0

// What the picked block's own keys do, for the hint line: " · x clear".
function keysOf(b: PageBlock | undefined): string {
  return Object.entries(b?.on ?? {}).map(([key, what]) =>
    ` · ${key} ${what.clear_objective !== undefined ? 'clear' : Object.keys(what)[0] ?? ''}`).join('')
}

function centre(b: PageBlock): [number, number] {
  return [b.x + b.w / 2, b.y + b.h / 2]
}

// The nearest block that way, along the key's axis first: the next box down a column, the next column across.
function move(blocks: PageBlock[], from: PageBlock | undefined, key: string): PageBlock | undefined {
  if (!from) return blocks[0]
  const [cx, cy] = centre(from)
  let best: PageBlock | undefined
  let score = Infinity
  for (const b of blocks) {
    if (b.id === from.id) continue
    const [x, y] = centre(b)
    const dx = x - cx
    const dy = y - cy
    const ahead = key === 'down' ? dy : key === 'up' ? -dy : key === 'right' ? dx : -dx
    const aside = key === 'down' || key === 'up' ? Math.abs(dx) : Math.abs(dy)
    if (ahead < 0.5) continue
    const s = key === 'down' || key === 'up' ? ahead + aside * 0.6 : ahead + aside * 4
    if (s < score) {
      score = s
      best = b
    }
  }
  return best ?? from
}

// The visible window across a wide page: as it was, moved just enough to show the picked block whole.
function across(page: Page, cols: number, sx: number, b: PageBlock | undefined): number {
  const most = Math.max(0, page.wide - cols)
  let x = Math.min(Math.max(0, sx), most)
  if (b && cols > 0) {
    if (b.x < x) x = Math.max(0, b.x - 2)
    else if (b.x + b.w > x + cols) x = Math.min(most, b.x + b.w - cols + 2)
  }
  return x
}

// One row cut to the window [sx, sx + cols): runs are split where the window edges fall.
function slice(row: Row, sx: number, cols: number): Run[] {
  const out: Run[] = []
  let at = 0
  for (const run of row) {
    const text = run[0]
    const from = Math.max(sx, at)
    const to = Math.min(sx + cols, at + text.length)
    if (to > from) out.push([text.slice(from - at, to - at), run[1], run[2] ?? ''] as Run)
    at += text.length
    if (at >= sx + cols) break
  }
  return out
}

const PageView: ClientModule<JsonValue, State> = (raw, surface) => {
  const { Box, Text } = surface.elements
  const { page, canGoBack } = raw as unknown as Props
  const cols = surface.columns || page.width
  const byId = new Map(page.blocks.map(b => [b.id, b]))
  const state = surface.state
  const sel = state?.mine && byId.has(state.sel) ? state.sel : byId.has(page.pick) ? page.pick : (page.blocks[0]?.id ?? '')
  const picked = byId.get(sel)
  const sx = across(page, cols, state?.mine ? state.sx : 0, state?.free ? undefined : picked)

  // The pane was laid out for another width (the split moved): ask for this one once it holds, not for every
  // width a drag passes on its way.
  if (surface.columns > 0 && surface.columns !== page.width && surface.columns !== askedWidth) {
    if (pendingWidth !== surface.columns) pendingTicks = 0
    pendingWidth = surface.columns
  }
  if (!state?.ticking) {
    surface.every(150, () => {
      if (pendingWidth > 0 && ++pendingTicks >= 2) {
        askedWidth = pendingWidth
        pendingWidth = 0
        surface.post({ width: askedWidth })
      }
    })
    surface.setState({ sel: state?.sel ?? '', sx: state?.sx ?? 0, mine: state?.mine ?? false, ticking: true,
                       free: state?.free })
  }

  const pick = (b: PageBlock | undefined, x = sx) => {
    if (!b) return
    surface.setState({ sel: b.id, sx: across(page, cols, x, b), mine: true, ticking: true })
    surface.post({ reveal: b.y })
  }
  const act = (b: PageBlock | undefined, which: 'enter' | 'space') => {
    const what = b?.[which]
    if (what) surface.post(what)
  }

  surface.onKey((e: ClientKeyEvent) => {
    const own = !e.shift && !e.ctrl && !e.meta ? picked?.on?.[e.key] : undefined
    if (own) {
      surface.post(own) // a key the picked block names (x on an objective: clear it)
    } else if ((e.key === 'up' || e.key === 'down') && e.shift) {
      surface.post({ scrollBy: e.key === 'up' ? -5 : 5 }) // the pane scrolls; the pick stays
    } else if (e.key === 'pageup' || e.key === 'pagedown') {
      surface.post({ scrollBy: e.key === 'pageup' ? -1000 : 1000 }) // a window's worth
    } else if ((e.key === 'left' || e.key === 'right') && e.shift) {
      // shift+← → belong to the terminal (its own splits): nothing here
    } else if (['up', 'down', 'left', 'right'].includes(e.key)) {
      pick(move(page.blocks, picked, e.key))
    } else if (e.key === '[' || e.key === ']') {
      surface.setState({ sel, sx: across(page, cols, sx + (e.key === '[' ? -STEP : STEP), undefined), mine: true, free: true,
                         ticking: true })
    } else if (e.key === '-' || e.key === '=' || e.key === '+') {
      surface.post({ zoomBy: e.key === '-' ? 1 : -1 }) // - shows more of the system, = bigger boxes
    } else if (e.key === 'return') {
      act(picked, 'enter')
    } else if (e.key === ' ') {
      act(picked, 'space')
    } else if (e.key === 'backspace' || e.key === 'b') {
      surface.post({ back: true })
    } else if (e.key === 'f') {
      surface.post({ follow: true })
    } else if (['n', 'r', 'm', 'c', 'a', 't'].includes(e.key)) { // m and c: what Map and Changes were
      surface.post({ view: { n: 'now', r: 'review', m: 'review', c: 'review', a: 'agents', t: 'topics' }[e.key] ?? 'now' })
    }
  })
  // The detail of the picked block, in the slot under its picture.
  const rows: Row[] = page.rows.slice()
  if (picked?.slot) {
    const [at, n] = picked.slot
    const detail = page.details[sel] ?? []
    for (let i = 0; i < n; i++) rows[at + i] = detail[i] ?? []
  }
  const lit = new Set(page.lights[sel] ?? [])

  type Look = { text: string; color?: string; bold: boolean; dim: boolean; inverse: boolean }
  const look = (run: Run): Look => {
    const words = run[1].split(' ')
    const ids = (run[2] ?? '').split(' ').filter(Boolean)
    const isPicked = sel !== '' && ids.includes(sel)
    const isLit = !isPicked && ids.some(id => lit.has(id))
    const color = isPicked ? 'blue' : words.map(w => COLORS[w]).find(Boolean)
    const muted = words.includes('muted') || (words.includes('heading') && !color)
    return { text: run[0], color, bold: isPicked || isLit || BOLD.some(w => words.includes(w)),
             dim: muted && !isPicked && !isLit, inverse: isPicked && words.includes('title') }
  }
  const merged = (runs: Run[]): Look[] => {
    const out: Look[] = []
    for (const run of runs) {
      const l = look(run)
      const prev = out[out.length - 1]
      if (prev && prev.color === l.color && prev.bold === l.bold && prev.dim === l.dim && prev.inverse === l.inverse) {
        prev.text += l.text
      } else {
        out.push(l)
      }
    }
    return out
  }
  const cell = (l: Look, i: number) => (
    <Text key={`c${i}`} color={l.color} bold={l.bold} dimColor={l.dim} inverse={l.inverse}>{l.text}</Text>
  )

  const wide = page.wide > cols
  return (
    <Box flexDirection="column">
      {wide && (
        <Text dimColor wrap="truncate-end">
          {`${sx > 0 ? '◂ ' : '  '}columns ${sx + 1}–${Math.min(page.wide, sx + cols)} of ${page.wide}` +
            ` · [ ] scroll across${sx + cols < page.wide ? ' ▸' : ''}`}
        </Text>
      )}
      {rows.map((row, y) => {
        const runs = wide ? slice(row, sx, cols) : row
        return runs.length === 0 ? (
          <Text key={`r${y}`}> </Text>
        ) : (
          <Text key={`r${y}`} wrap="truncate-end">{merged(runs).map(cell)}</Text>
        )
      })}
      <Text dimColor wrap="truncate-end">
        {'click here, then ← ↑ ↓ → pick · ↵ zoom in' + keysOf(picked) + ' · [ ] shift+↑ ↓ scroll' +
          (canGoBack ? ' · b back' : '') + ' · f follow · Esc leaves'}
      </Text>
    </Box>
  )
}

export default PageView
