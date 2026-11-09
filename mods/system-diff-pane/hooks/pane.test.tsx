import { expect, test } from 'claude-code/testing'

const PANE = {
  plugin: 'system-diff-pane',
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

const block = (id: string, title: string, status = 'same') =>
  ({ id, title, sub: '', status, outside: false, agent: false, edge: 'call', edge_status: 'same' })

// What `system-diff panel` prints for the API zoomed in on: Web app calls it, it calls Core and Store.
const panel = (part: string) => ({
  part,
  head: [[['● Claude is working  ', 'accent'], ['✎ Editing ', 'accent'], ['API', 'accent bold']]],
  diagram: {
    kind: 'zoom', heading: 'Entry points', more_callers: 0, more_callees: 0, outside_caller: false,
    part: { ...block(part, part === 'api' ? 'API' : part, 'changed'), agent: true, lines: [['GET /health', 'added']] },
    callers: [block('web', 'Web app')],
    callees: [block('core', 'Core', 'changed'), block('store', 'Store')],
  },
  rows: [[['WHAT JUST HAPPENED', 'heading']], [['API now answers GET /health', 'attention']]],
  picks: [{ id: 'web', title: 'Web app' }, { id: 'core', title: 'Core' }, { id: 'store', title: 'Store' }],
})

function answer(on: any, asked: string[][], exitCode = 0) {
  on('command.register', () => ({ value: { command: 'system-diff-pane' } }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('command.run', () => ({ text: '' }))
  on('process.run', (_: unknown, e: { argv: string[] }) => {
    const argv = [...e.argv]
    asked.push(argv)
    const part = argv[argv.indexOf('--part') + 1] || 'api'
    return { value: { exitCode, stdout: exitCode ? '' : JSON.stringify(panel(part)), stderr: '',
                      isStdoutTruncated: false, isStderrTruncated: false } }
  })
}

test('the pane draws the diagram in its own region, and what happened, on every surface', async ($, on) => {
  const asked: string[][] = []
  answer(on, asked)
  await $.command.run(RUN)
  expect(asked[0]).toContain('panel')
  for (const surface of ['terminal', 'desktop', 'vscode'] as const) {
    const ui = await $.ui.mount({ ...PANE, surface })
    expect(await ui.find({ type: 'Text', text: /WHAT JUST HAPPENED/ })).toBeDefined()
    await ui.unmount()
  }
  const terminal = await $.ui.mount({ ...PANE, surface: 'terminal' })
  await terminal.key({ key: 'down', in: 'diagram' })   // the diagram's own region, taking the keys (throws if absent)
  await terminal.unmount()
})

test('without system-diff the pane says how to get it', async ($, on) => {
  answer(on, [], 127)
  await $.command.run(RUN)
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect(await ui.find({ type: 'Text', text: /not installed/ })).toBeDefined()
  await ui.unmount()
})

test('a block pressed zooms in on it, Back returns, the tabs switch views, Follow goes back to Claude', async ($, on) => {
  const asked: string[][] = []
  answer(on, asked)
  await $.command.run(RUN)
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  const last = () => (asked[asked.length - 1] ?? []).join(' ')

  await ui.press({ key: 'block-core' })
  expect(last()).toContain('--view now --part core')
  expect(await ui.find({ key: 'back' })).toBeDefined()

  await ui.press({ key: 'back' })
  expect(last()).toContain('--part api')

  await ui.press({ key: 'view-map' })
  expect(last()).toContain('--view map')
  await ui.press({ key: 'follow' })
  expect(last().endsWith('--view now --part ')).toBe(true)
  await ui.unmount()
})

test('in the diagram, the arrows pick a block and Enter zooms in on it, as in the terminal view', async ($, on) => {
  const asked: string[][] = []
  answer(on, asked)
  await $.command.run(RUN)
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  const last = () => (asked[asked.length - 1] ?? []).join(' ')

  // The zoom's lanes: Web app (calls it) | API | Core, Store (it calls). The pick starts on Core.
  await ui.key({ key: 'down', in: 'diagram' })
  await ui.key({ key: 'return', in: 'diagram' })
  expect(last()).toContain('--part store')
  await ui.key({ key: 'left', in: 'diagram' })      // across, to the part: Enter does nothing there
  await ui.key({ key: 'left', in: 'diagram' })      // to what calls it
  await ui.key({ key: 'return', in: 'diagram' })
  expect(last()).toContain('--part web')
  await ui.key({ key: 'b', in: 'diagram' })
  expect(last()).toContain('--part store')
  await ui.key({ key: 'm', in: 'diagram' })
  expect(last()).toContain('--view map')
  await ui.unmount()
})

test('/system-diff and /system-diff-pane both open it', async ($, on) => {
  const registered: string[] = []
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('command.run', () => ({ text: '' }))
  on('process.run', () => ({ value: { exitCode: 0, stdout: JSON.stringify(panel('api')), stderr: '',
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
})
