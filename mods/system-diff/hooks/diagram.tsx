import type { ClientModule, ClientKeyEvent, JsonValue } from 'claude-code'

import type { Block, Diagram, Side } from '../types'

// The system diagram, drawn in its own region of the pane so it gets the keys, as the terminal view's does: click it
// once, then ← ↑ ↓ → pick a block, Enter zooms in on it, b goes back, f follows Claude, n m c t switch tabs, Esc
// hands the keys back. A press goes to the hooks module as a post; it does the zooming.
//
// Colours as in the rest of the pane: blue is what you picked and where Claude is, yellow what changed, green what
// is new, gray the rest, red only what was taken away.

type Props = { diagram: Diagram; canGoBack: boolean }
type State = { id: string }

const STATUS_COLOR: Record<string, string | undefined> = { added: 'green', changed: 'yellow', removed: 'red' }
const SIGN: Record<string, string> = { added: '+ ', changed: '~ ', removed: '− ' }

// The blocks in lanes: a zoom's are columns (what calls it, the part, what it calls), walked down by ↑ ↓ and across
// by ← →; a map's are rows (one per column of the system), walked across by ← → and between by ↑ ↓.
function lanes(d: Diagram): string[][] {
  if (d.kind === 'zoom') {
    return [d.callers.map(b => b.id), [d.part.id], d.callees.map(b => b.id)].filter(lane => lane.length > 0)
  }
  return d.columns.map(col => col.blocks.map(b => b.id))
}

function where(all: string[][], id: string): [number, number] {
  for (let l = 0; l < all.length; l++) {
    const i = (all[l] ?? []).indexOf(id)
    if (i >= 0) return [l, i]
  }
  return [0, 0]
}

function move(d: Diagram, id: string, key: string): string {
  const all = lanes(d)
  if (all.length === 0) return id
  let [l, i] = where(all, id)
  const along = d.kind === 'zoom' ? ['up', 'down'] : ['left', 'right'] // within a lane
  const across = d.kind === 'zoom' ? ['left', 'right'] : ['up', 'down'] // between lanes
  if (key === along[0]) i -= 1
  else if (key === along[1]) i += 1
  else if (key === across[0]) l -= 1
  else if (key === across[1]) l += 1
  l = Math.max(0, Math.min(all.length - 1, l))
  const lane = all[l] ?? []
  i = Math.max(0, Math.min(lane.length - 1, i))
  return lane[i] ?? id
}

function firstPick(d: Diagram): string {
  if (d.kind === 'zoom') return (d.callees[0] ?? d.callers[0] ?? d.part).id
  return d.columns[0]?.blocks[0]?.id ?? ''
}

const DiagramView: ClientModule<JsonValue, State> = (raw, surface) => {
  const { Box, Text, Button } = surface.elements
  const { diagram: d, canGoBack } = raw as unknown as Props
  const all = lanes(d).flat()
  const sel = surface.state && all.includes(surface.state.id) ? surface.state.id : firstPick(d)
  const columns = surface.columns || 80

  surface.onKey((e: ClientKeyEvent) => {
    if (['up', 'down', 'left', 'right'].includes(e.key)) {
      surface.setState({ id: move(d, sel, e.key) })
    } else if (e.key === 'return') {
      if (d.kind === 'map' || sel !== d.part.id) surface.post({ zoom: sel })
    } else if (e.key === 'backspace' || e.key === 'b') {
      surface.post({ back: true })
    } else if (e.key === 'f') {
      surface.post({ follow: true })
    } else if (['n', 'm', 'c', 't'].includes(e.key)) {
      surface.post({ view: { n: 'now', m: 'map', c: 'changes', t: 'topics' }[e.key] ?? 'now' })
    }
  })

  const block = (b: Block, width: number | undefined, withSub: boolean, isPart = false) => {
    const isPicked = b.id === sel
    const color = isPicked || isPart ? 'blue' : STATUS_COLOR[b.status]
    return (
      <Box key={`box-${b.id}`} borderStyle={isPicked || isPart ? 'bold' : b.outside ? 'round' : 'single'}
           borderColor={color} borderDimColor={!color} paddingX={1} width={width} flexDirection="column">
        <Button key={`block-${b.id}`} plain onPress={() => (isPart ? undefined : surface.post({ zoom: b.id }))}>
          <Text bold color={isPicked || isPart ? 'blue' : STATUS_COLOR[b.status]} inverse={isPicked}
                wrap="truncate-end">
            {b.agent ? '● ' : ''}{SIGN[b.status] ?? ''}{b.title}
          </Text>
        </Button>
        {withSub && b.sub !== '' && <Text dimColor wrap="truncate-end">{b.sub}</Text>}
      </Box>
    )
  }

  const arrow = (s: Side) => {
    const color = STATUS_COLOR[s.edge_status] ?? (s.edge === 'data' ? 'blue' : s.edge === 'network' ? 'magenta' : undefined)
    const glyph = s.edge === 'network' || s.edge_status === 'removed' ? '╌╌▸' : '──▸'
    return <Text color={color} dimColor={!color}>{glyph}</Text>
  }
  const label = (text: string) => <Text dimColor bold>{text}</Text>
  const more = (n: number) => (n > 0 ? <Text dimColor>{`+${n} more`}</Text> : null)
  const hint = (
    <Text dimColor>
      {'click here, then ← ↑ ↓ → pick a block · Enter zooms in' + (canGoBack ? ' · b back' : '') + ' · Esc leaves'}
    </Text>
  )

  if (d.kind === 'map') {
    const perRow = Math.max(1, Math.floor(columns / 30))
    const width = Math.max(20, Math.floor(columns / perRow) - 1)
    return (
      <Box flexDirection="column">
        {hint}
        {d.columns.map(col => (
          <Box key={`col-${col.heading}`} flexDirection="column">
            {label(col.heading.toUpperCase())}
            <Box flexDirection="row" flexWrap="wrap" columnGap={1}>{col.blocks.map(b => block(b, width, true))}</Box>
          </Box>
        ))}
      </Box>
    )
  }

  const part = { ...d.part }
  const centre = (
    <Box borderStyle="bold" borderColor="blue" paddingX={1} flexDirection="column"
         width={columns >= 96 ? 34 : undefined}>
      <Text color="blue" bold inverse={sel === part.id} wrap="truncate-end">
        {part.agent ? '● ' : ''}{SIGN[part.status] ?? ''}{part.title}
      </Text>
      {part.sub !== '' && <Text dimColor wrap="truncate-end">{part.sub}</Text>}
      {part.lines.map(([text, status], i) => (
        <Text key={`l${i}`} color={STATUS_COLOR[status]} dimColor={!STATUS_COLOR[status]} wrap="truncate-end">
          {(SIGN[status] ?? '· ') + text}
        </Text>
      ))}
    </Box>
  )
  const callersTitle = d.outside_caller ? 'USED BY' : 'CALLED BY'
  if (columns >= 96) {
    const side = Math.max(18, Math.min(30, Math.floor((columns - 44) / 2)))
    return (
      <Box flexDirection="column">
        {hint}
        <Box flexDirection="row" alignItems="flex-start">
          <Box flexDirection="column">
            {d.callers.length > 0 && label(callersTitle)}
            {d.callers.map(s => (
              <Box key={`c-${s.id}`} flexDirection="row" alignItems="center">{block(s, side, false)}{arrow(s)}</Box>
            ))}
            {more(d.more_callers)}
          </Box>
          <Box flexDirection="column">{label(d.heading.toUpperCase())}{centre}</Box>
          <Box flexDirection="column">
            {d.callees.length > 0 && label('CALLS AND USES')}
            {d.callees.map(s => (
              <Box key={`e-${s.id}`} flexDirection="row" alignItems="center">{arrow(s)}{block(s, side, false)}</Box>
            ))}
            {more(d.more_callees)}
          </Box>
        </Box>
      </Box>
    )
  }
  const width = Math.max(18, columns - 2)
  return (
    <Box flexDirection="column">
      {hint}
      {d.callers.length > 0 && label(callersTitle)}
      {d.callers.map(s => block(s, width, false))}
      {more(d.more_callers)}
      {d.callers.length > 0 && <Text dimColor>  ▼</Text>}
      {label(d.heading.toUpperCase())}
      {centre}
      {d.callees.length > 0 && <Text dimColor>  ▼</Text>}
      {d.callees.length > 0 && label('CALLS AND USES')}
      {d.callees.map(s => block(s, width, false))}
      {more(d.more_callees)}
    </Box>
  )
}

export default DiagramView
