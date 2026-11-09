export type Run = [string, string]
export type Row = Run[]
export type Pick = { id: string; title: string; status?: string }
export type Block = { id: string; title: string; sub: string; status: string; outside: boolean; agent: boolean }
export type Side = Block & { edge: string; edge_status: string }
export type Diagram =
  | {
      kind: 'zoom'
      heading: string
      part: Block & { lines: [string, string][] }
      callers: Side[]
      callees: Side[]
      more_callers: number
      more_callees: number
      outside_caller: boolean
    }
  | { kind: 'map'; columns: { heading: string; blocks: Block[] }[] }

declare module 'claude-code' {
  interface PluginState {
    'system-diff-pane': {
      head: Shaped<Row[]>
      diagram: Shaped<Diagram | null>
      rows: Shaped<Row[]>
      picks: Shaped<Pick[]>
      shown: Shaped<string>
      view: Shaped<string>
      part: Shaped<string>
      back: Shaped<string[]>
      note: Shaped<string>
    }
  }
}
