import { expect, mock, test } from 'claude-code/testing'

const PANE = {
  plugin: 'system-diff',
  component: 'Pane',
  requestId: 'system-diff',
  props: {
    title: 'System diff',
    isFocused: true,
    bodyColumns: 110,
    placement: 'dock',
    scroll: { offset: 0, bodyRows: 40 },
    view: {},
  },
} as const

// What the person typing /system-diff sends.
const RUN = {
  command: 'system-diff',
  args: '',
  origin: { kind: 'composer' },
  presentation: { isFullscreen: false, columns: 120 },
} as const

// What `system-diff panel --protocol 2` prints: Developer → API → Core, Core in a container with Store access.
const page = (part: string, width: number) => ({
  protocol: 2, view: 'now', part, width, wide: width, pick: 'b:api',
  rows: [
    [['● Claude is working  ', 'accent'], ['✎ Editing ', 'accent'], ['API', 'accent bold']],
    [['┌─────────┐   ┌─────┐   ┌──────┐', 'muted', 'b:dev b:api b:g:Core']],
    [['│Developer├─▸', 'muted', 'b:dev'], ['│ API ├', 'title bold accent', 'b:api'], ['─▸', 'muted', 'e:api>g:Core'],
     ['│ Core │', 'title', 'b:core']],
    [],
    [],
    [['WHAT JUST HAPPENED', 'heading']],
    [['API now answers GET /health', 'attention']],
  ],
  blocks: [
    { id: 'b:dev', x: 0, y: 1, w: 11, h: 3, enter: { zoom: 'a:dev' }, slot: [3, 2] },
    { id: 'b:api', x: 14, y: 1, w: 7, h: 3, enter: { zoom: 'api' }, slot: [3, 2] },
    { id: 'b:core', x: 24, y: 2, w: 8, h: 1, enter: { zoom: 'core' }, slot: [3, 2] },
  ],
  lights: { 'b:api': ['b:core', 'e:api>g:Core'] },
  details: { 'b:api': [[['API  ', 'bold'], ['uses Core · used by Developer', 'muted']]] },
})

function answer(on: any, asked: string[][], exitCode = 0, stderr = '', opened: Array<{ columns?: number }> = [],
                more: Record<string, unknown> = {}) {
  on('command.register', () => ({ value: { command: 'system-diff-pane' } }))
  on('ui.open', (_: unknown, e: { columns?: number }) => {
    opened.push(e)
    return { value: { isPlaced: true } }
  })
  on('ui.scroll', () => ({}))
  on('command.run', () => ({ text: '' }))
  on('process.run', (_: unknown, e: { argv: string[] }) => {
    const argv = [...e.argv]
    if (argv.includes('progress')) { // what a long read is doing: nothing, here
      return { value: { exitCode: 0, stdout: '', stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
    }
    asked.push(argv)
    const at = (flag: string) => argv[argv.indexOf(flag) + 1] ?? ''
    const out = argv.includes('panel') ? JSON.stringify({ ...page(at('--part'), Number(at('--width')) || 100), ...more }) : 'ok'
    return { value: { exitCode, stdout: exitCode ? '' : out, stderr, isStdoutTruncated: false,
                      isStderrTruncated: false } }
  })
}

test('the pane paints the page in its own region, and its rows, on every surface', async ($, on) => {
  const asked: string[][] = []
  answer(on, asked)
  await $.command.run(RUN)
  expect(asked[0]).toContain('panel')
  expect(asked[0]).toContain('--protocol')
  for (const surface of ['terminal', 'desktop', 'vscode'] as const) {
    const ui = await $.ui.mount({ ...PANE, surface })
    if (surface === 'vscode') expect(await ui.find({ type: 'Text', text: /WHAT JUST HAPPENED/ })).toBeDefined()
    await ui.unmount()
  }
  const terminal = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect(await terminal.find({ type: 'Text', text: /^v\d+\.\d+\.\d+$/ })).toBeDefined() // the version, bottom right
  await terminal.key({ key: 'down', in: 'page' }) // the page's own region, taking the keys (throws if absent)
  await terminal.unmount()
})

test('without system-diff the pane says how to get it; with an old one, how to upgrade', async ($, on) => {
  answer(on, [], 127)
  await $.command.run(RUN)
  let ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect(await ui.find({ type: 'Text', text: /system-diff is not installed/ })).toBeDefined()
  await ui.unmount()
})

test('an old system-diff gets the upgrade line', async ($, on) => {
  answer(on, [], 2, 'system-diff: error: unrecognized arguments: --protocol 2')
  await $.command.run(RUN)
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect(await ui.find({ type: 'Text', text: /pipx upgrade system-diff/ })).toBeDefined()
  await ui.unmount()
})

test('a resized split is laid out again at once, for the width the page reports', async ($, on) => {
  const asked: string[][] = []
  answer(on, asked)
  await $.command.run(RUN)
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  await ui.resize({ columns: 90, rows: 30, in: 'page' }) // a drag passes through 90 on its way
  await ui.resize({ columns: 72, rows: 30, in: 'page' })
  await ui.advance(400)                                  // and settles at 72: that one width is laid out
  const widths = asked.map(a => a[a.indexOf('--width') + 1])
  expect(widths[widths.length - 1]).toBe('72')
  expect(widths).not.toContain('90')
  await ui.unmount()
})

test('the arrows pick a block, Enter zooms in, b goes back', async ($, on) => {
  const asked: string[][] = []
  answer(on, asked)
  await $.command.run(RUN)
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  const last = () => (asked[asked.length - 1] ?? []).join(' ')

  // The pick starts on the API (Claude's); right is Core.
  await ui.key({ key: 'right', in: 'page' })
  await ui.key({ key: 'return', in: 'page' })
  expect(last()).toContain('--view now --part core')
  expect(await ui.find({ key: 'back' })).toBeDefined()
  await ui.key({ key: 'b', in: 'page' })
  expect(last()).toContain('--part ')
  expect(last()).not.toContain('--part core')

  await ui.key({ key: 'return', in: 'page' })           // zoomed in again: Follow Claude shows
  await ui.key({ key: 'r', in: 'page' })
  expect(last()).toContain('--view review')
  await ui.press({ key: 'view-agents' })
  expect(last()).toContain('--view agents')
  await ui.press({ key: 'follow' })
  expect(last().endsWith('--view now --part ')).toBe(true)
  await ui.unmount()
})

test('space on a technology marks it new to you', async ($, on) => {
  const asked: string[][] = []
  answer(on, asked)
  await $.command.run(RUN)
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  await ui.post({ learn: 'fastapi' }, { in: 'page' })
  expect(asked.some(a => a.join(' ').endsWith('profile --learn fastapi'))).toBe(true)
  await ui.unmount()
})

test('/system-diff and /system-diff-pane both open it, as wide as a dock goes', async ($, on) => {
  const registered: string[] = []
  const opened: Array<{ columns?: number }> = []
  on('ui.open', (_: unknown, e: { columns?: number }) => {
    opened.push(e)
    return { value: { isPlaced: true } }
  })
  on('command.run', () => ({ text: '' }))
  on('process.run', () => ({ value: { exitCode: 0, stdout: JSON.stringify(page('', 100)), stderr: '',
                                      isStdoutTruncated: false, isStderrTruncated: false } }))
  on('session.start', (_: unknown, e: { cwd: string }) => ({ cwd: e.cwd }))
  on('clock.every', () => ({ value: undefined }))
  on('command.register', (_: unknown, e: { name: string }) => {
    registered.push(e.name)
    return { value: { command: e.name } }
  })
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true } as any)
  expect(registered).toEqual(['system-diff', 'system-diff-pane'])
  for (const command of ['system-diff', 'system-diff-pane']) {
    const got = await $.command.run({ ...RUN, command })
    expect((got as { text?: string }).text).toBe('System diff pane opened.')
  }
  expect(opened.every(o => (o.columns ?? 0) >= 200)).toBe(true) // as wide as a dock goes
})

test('/system-diff close closes the pane, and so does its close button', async ($, on) => {
  const closed: string[] = []
  answer(on, [])
  on('ui.close', (_: unknown, e: { id: string }) => {
    closed.push(e.id)
    return { value: undefined }
  })
  const got = await $.command.run({ ...RUN, args: 'close' })
  expect((got as { text?: string }).text).toContain('closed')
  await $.command.run(RUN)
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  await ui.press({ key: 'close' })
  expect(closed).toEqual(['system-diff', 'system-diff'])
  await ui.unmount()
})

test('zoom out draws the diagrams compact, zoom in full again', async ($, on) => {
  const asked: string[][] = []
  answer(on, asked)
  await $.command.run(RUN)
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  await ui.press({ key: 'zoom-out' })
  expect(asked[asked.length - 1]?.join(' ')).toContain('--zoom 1')
  await ui.key({ key: '=', in: 'page' })
  expect(asked[asked.length - 1]?.join(' ')).toContain('--zoom 0')
  await ui.unmount()
})

test('the tab row shows the address of the web page when one runs', async ($, on) => {
  answer(on, [], 0, '', [], { web: 'http://127.0.0.1:8765/' })
  await $.command.run(RUN)
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect(await ui.find({ type: 'Text', text: '🌐 http://127.0.0.1:8765/' })).toBeDefined()
  await ui.unmount()
})

test('what the web page sends is submitted as the person\'s words, once', async ($, on) => {
  const submitted: Array<{ text: string; asUser?: boolean }> = []
  on('prompt.submit', (_: unknown, e: { text: string; origin?: { asUser?: boolean } }) => {
    submitted.push({ text: e.text, asUser: e.origin?.asUser })
    return { text: e.text }
  })
  answer(on, [], 0, '', [], { send: [{ id: '1', text: 'hi' }] })
  await $.command.run(RUN)
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  await ui.advance(3500)                 // the look on the clock delivers it (not the command's own hook)
  await ui.key({ key: 'r', in: 'page' }) // more looks, the same page: not sent again
  await ui.advance(7000)
  expect(submitted).toEqual([{ text: 'hi', asUser: true }])
  await ui.unmount()
})

test('a pane opens at once on the page last seen in this project, until the fresh one is read', async ($, on) => {
  answer(on, [], 127) // nothing reads the system this time: what shows is the kept page
  on('session.root', () => ({ value: '/work/shop' }))
  on('store.get', (_: unknown, e: { key: string }) =>
    ({ value: e.key === 'page:/work/shop' ? JSON.stringify(page('', 110)) : undefined }))
  await $.command.run(RUN)
  const ui = await $.ui.mount({ ...PANE, surface: 'vscode' }) // the surface that draws the rows as text
  expect(await ui.find({ type: 'Text', text: /API now answers GET \/health/ })).toBeDefined()
  await ui.unmount()
})

test('above the chat, the pane says how to have it beside the chat', async ($, on) => {
  answer(on, [])
  await $.command.run(RUN)
  const inline = await $.ui.mount({ ...PANE, props: { ...PANE.props, placement: 'inline' }, surface: 'terminal' })
  expect(await inline.find({ type: 'Text', text: /CLAUDE_CODE_NO_FLICKER/ })).toBeDefined()
  await inline.unmount()
  const docked = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect(await docked.find({ type: 'Text', text: /CLAUDE_CODE_NO_FLICKER/ })).toBeUndefined()
  await docked.unmount()
})

test('/system-diff tui opens a terminal running the terminal view, and always says the command', async ($, on) => {
  const tried: string[][] = []
  let opens = true
  on('session.root', () => ({ value: '/work/shop' }))
  on('process.run', (_: unknown, e: { argv: string[] }) => {
    const argv = [...e.argv]
    const ok = (stdout: string, exitCode = 0) =>
      ({ value: { exitCode, stdout, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } })
    if (argv.includes('--command')) return ok(JSON.stringify({ argv: ['system-diff', 'tui'], line: 'system-diff tui' }))
    tried.push(argv)
    return ok('', opens && argv[0] === 'wt' ? 0 : 1) // Windows Terminal is there; nothing else is
  })
  let got = await $.command.run({ ...RUN, args: 'tui' })
  expect(tried.some(a => a.join(' ') === 'wt -w 0 new-tab -d /work/shop system-diff tui')).toBe(true)
  expect((got as { text?: string }).text).toContain('Opened the System diff in a new Windows Terminal tab')
  expect((got as { text?: string }).text).toContain('system-diff tui')

  opens = false // no terminal opens: the command to run is the answer
  got = await $.command.run({ ...RUN, args: 'tui' })
  expect((got as { text?: string }).text).toContain('Run this in a new terminal tab or window, in /work/shop:\n  system-diff tui')
})

// A resident engine: every page comes with the model's version; `--since V` is held until the version passes V
// (here: until the test moves the clock past `held` ms, as Claude's edit lands), as `system-diff panel` does with
// `system-diff watch` running.
function engine(on: any, clock: { sleep: (ms: number) => Promise<void> }, asked: string[][], held = 5000) {
  let version = 1
  on('command.register', () => ({ value: { command: 'system-diff-pane' } }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('ui.scroll', () => ({}))
  on('command.run', () => ({ text: '' }))
  on('process.run', async (_: unknown, e: { argv: string[] }) => {
    const argv = [...e.argv]
    const ok = (stdout: string) =>
      ({ value: { exitCode: 0, stdout, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } })
    if (argv.includes('progress')) return ok('')
    asked.push(argv)
    const at = (flag: string) => argv[argv.indexOf(flag) + 1] ?? ''
    if (argv.includes('--since')) {
      await clock.sleep(held)
      version += 1 // an edit landed meanwhile
    }
    const base = page(at('--part'), Number(at('--width')) || 100)
    return ok(JSON.stringify({ ...base, view: at('--view'), version,
                               rows: [[[`${at('--view')} at version ${version}`, 'bold']], ...base.rows] }))
  })
}

test('the long poll redraws the pane as soon as the engine has a new version, with no looks between', async ($, on) => {
  const clock = mock.clock(on)
  const asked: string[][] = []
  engine(on, clock, asked)
  const ui = await $.ui.mount({ ...PANE, surface: 'vscode' })        // drawn as rows of text, 110 columns wide
  await $.command.run(RUN)
  await clock.settle()
  expect(asked[0]).not.toContain('--since')                          // the first look answers at once
  expect(asked[1]?.join(' ')).toContain('--since 1')                 // then the long poll waits for what is next
  expect(await ui.find({ type: 'Text', text: /now at version 1/ })).toBeDefined()
  await clock.advance(5000)                                          // Claude's edit: the engine's version 2
  expect(await ui.find({ type: 'Text', text: /now at version 2/ })).toBeDefined()
  await clock.settle()
  expect(asked[asked.length - 1]?.join(' ')).toContain('--since 2')  // and waits for the next one
  await ui.unmount()
  expect(asked.filter(a => !a.includes('--since')).length).toBe(1)  // no looks on a clock meanwhile
})

test('a key asks at once, never behind the long poll, and a long poll for what no longer shows is dropped', async ($, on) => {
  const clock = mock.clock(on)
  const asked: string[][] = []
  engine(on, clock, asked, 20000)
  await $.command.run(RUN)
  await clock.settle()
  expect(asked.some(a => a.includes('--since'))).toBe(true)          // a long poll is waiting
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  await ui.key({ key: 'r', in: 'page' })
  const look = asked[asked.length - 1] ?? []
  expect(look.join(' ')).toContain('--view review')                  // asked at once, while the long poll waits
  expect(look).not.toContain('--since')
  await ui.unmount()
  let text = await $.ui.mount({ ...PANE, surface: 'vscode' })
  expect(await text.find({ type: 'Text', text: /review at version 1/ })).toBeDefined()
  await text.unmount()
  await clock.advance(20000)                                         // the old long poll (for Now) answers: dropped
  text = await $.ui.mount({ ...PANE, surface: 'vscode' })
  expect(await text.find({ type: 'Text', text: /now at version/ })).toBeUndefined()
  await text.unmount()
  await clock.settle()
  const next = asked[asked.length - 1]?.join(' ') ?? ''
  expect(next).toContain('--since')                                  // and the next one asks for what shows
  expect(next).toContain('--view review')
})

test('x on an objective clears it (system-diff\'s own notes, never the code), and the hint says so', async ($, on) => {
  const asked: string[][] = []
  const objective = { id: 'o:fast', x: 0, y: 6, w: 20, h: 1, on: { x: { clear_objective: 'fast' } } }
  answer(on, asked, 0, '', [], { pick: 'o:fast', blocks: [...page('', 110).blocks, objective] })
  await $.command.run(RUN)
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect(await ui.find({ type: 'Text', text: /x clear/, in: 'page' })).toBeDefined()
  const before = asked.length
  await ui.key({ key: 'x', in: 'page' })
  const after = asked.slice(before).map(a => a.join(' '))
  expect(after.some(a => a.endsWith('objectives clear fast'))).toBe(true)
  expect(after[after.length - 1]).toContain('panel')              // and the page is read again without it
  await ui.key({ key: 'up', in: 'page' })                          // on another block, x does nothing
  const more = asked.length
  await ui.key({ key: 'x', in: 'page' })
  expect(asked.slice(more).some(a => a.includes('objectives'))).toBe(false)
  await ui.unmount()
})

test('a page that only says the system is being read never blanks a drawn diagram', async ($, on) => {
  const more: Record<string, unknown> = {} // what the fake engine answers on top of the page: below, "reading"
  answer(on, [], 0, '', [], more)
  await $.command.run(RUN)
  Object.assign(more, { rows: [[['◐ Reading the system… 3 of 430 files', 'attention']]], blocks: [],
                        reading: { done: 3, total: 430, stage: 'Reading the code' } })
  await $.command.run(RUN)
  const ui = await $.ui.mount({ ...PANE, surface: 'vscode' })
  expect(await ui.find({ type: 'Text', text: /API now answers GET \/health/ })).toBeDefined()   // the diagram stays
  expect(await ui.find({ type: 'Text', text: /3 of 430 files/ })).toBeDefined()               // the progress shows
  await ui.unmount()
})

test('the History tab shows how the system evolved; Enter on an entry shows it, b goes back', async ($, on) => {
  const asked: string[][] = []
  const entry = { id: 'hy:t1', x: 0, y: 6, w: 60, h: 2, enter: { entry: 't1' }, slot: [9, 5] }
  answer(on, asked, 0, '', [], { blocks: [...page('', 110).blocks, entry] })
  await $.command.run(RUN)
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  const last = () => (asked[asked.length - 1] ?? []).join(' ')
  expect(await ui.find({ key: 'view-history' })).toBeDefined()
  await ui.press({ key: 'view-history' })
  expect(last()).toContain('--view history --part ')
  expect(last().endsWith('--part ')).toBe(true)                  // the whole system's
  await ui.post({ entry: 't1' }, { in: 'page' })                // ↵ on an entry: that change, on the diagram
  expect(last()).toContain('--view history --part #t1')
  await ui.key({ key: 'b', in: 'page' })                        // back to the timeline
  expect(last()).toContain('--view history')
  expect(last()).not.toContain('#t1')
  await ui.unmount()
})

test('h on a picked box opens that box\'s own history; b goes back to where it was', async ($, on) => {
  const asked: string[][] = []
  answer(on, asked)
  await $.command.run(RUN)
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  const last = () => (asked[asked.length - 1] ?? []).join(' ')
  expect(await ui.find({ type: 'Text', text: /h its history/, in: 'page' })).toBeDefined()
  await ui.key({ key: 'right', in: 'page' })                    // the API, then Core
  await ui.key({ key: 'h', in: 'page' })
  expect(last()).toContain('--view history --part core')
  await ui.key({ key: 'b', in: 'page' })
  expect(last()).toContain('--view now')
  expect(last()).not.toContain('--part core')
  await ui.post({ history: 'api' }, { in: 'page' })            // ↵ on a box of an entry's diagram: its history
  expect(last()).toContain('--view history --part api')
  await ui.press({ key: 'view-now' })                           // leaving History: no block carried into Now
  expect(last().endsWith('--view now --part ')).toBe(true)
  await ui.unmount()
})
