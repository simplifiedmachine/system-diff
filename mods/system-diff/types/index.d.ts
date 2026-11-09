// What `system-diff panel --protocol 2` prints: a PAGE of rows of styled runs, the blocks among them a person can
// pick, and what each block lights and shows when picked.
export type Run = [string, string] | [string, string, string] // text, style words, ids (space-separated)
export type Row = Run[]
export type Act = { zoom?: string; learn?: string; view?: string }
export type PageBlock = {
  id: string
  x: number
  y: number
  w: number
  h: number
  enter?: Act // what Enter (or a second click) does
  space?: Act // what space does
  slot?: [number, number] // the rows under its picture where its detail shows: first row, how many
}
export type Page = {
  protocol: 2
  view: string
  part: string
  width: number // laid out for this many columns
  wide: number // the widest row: past `width`, the pane scrolls across
  rows: Row[]
  blocks: PageBlock[]
  lights: Record<string, string[]>
  details: Record<string, Row[]>
  pick: string
}

declare module 'claude-code' {
  interface PluginState {
    'system-diff': {
      page: Shaped<Page | null>
      view: Shaped<string>
      part: Shaped<string>
      back: Shaped<string[]>
      note: Shaped<string>
    }
  }
}
