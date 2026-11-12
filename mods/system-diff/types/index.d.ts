// What `system-diff panel --protocol 2` prints: a PAGE of rows of styled runs, the blocks among them a person can
// pick, and what each block lights and shows when picked.
export type Run = [string, string] | [string, string, string] // text, style words, ids (space-separated)
export type Row = Run[]
export type Act = { zoom?: string; learn?: string; view?: string; clear_objective?: string }
export type PageBlock = {
  id: string
  x: number
  y: number
  w: number
  h: number
  enter?: Act // what Enter (or a second click) does
  space?: Act // what space does
  on?: Record<string, Act> // what other keys do on it, by key name ("x": clear an objective)
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
  zoom?: number // how compact it is drawn: 0 full, 1 compact, 2 tiny
  web?: string // the address of the same views in a browser, when that page runs
  send?: { id: string; text: string }[] // typed in the web page's Send to Claude box, for the session
  version?: number // the engine's model version it was drawn from; absent when read without the engine
  // while the system is still being read: how far; with `stage`, nothing is drawn yet (the engine is starting)
  reading?: { done?: number; total?: number; stage?: string; stale?: boolean; at?: number }
}

declare module 'claude-code' {
  interface PluginState {
    'system-diff': {
      page: Shaped<Page | null>
      view: Shaped<string>
      part: Shaped<string>
      back: Shaped<string[]>
      note: Shaped<string>
      zoom: Shaped<number>
    }
  }
}
