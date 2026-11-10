import { expect, test } from 'claude-code/testing'

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
  expect(await ui.find({ type: 'Text', text: /needs uv or Python 3.11/ })).toBeDefined()
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
