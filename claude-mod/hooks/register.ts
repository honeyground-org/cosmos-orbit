import type { EngineInterface, Register } from 'claude-code'

// Cosmos Orbit's automatic handoff in the background.
//
// Without this mod, Orbit hands a full session to a fresh one by typing `/clear` and one fixed line into the
// terminal (cmux or tmux only). With it, Claude Code does both itself, through its own API: nothing is typed, the
// terminal shows only that a handoff happened and a fresh session took the task over, and it works in any
// terminal. Orbit decides (its prompt hook blocks the request and carries it in the ledger); this mod only acts
// when Orbit's block says so, and only after it took the handoff (`cosmos-orbit handoff-claim`), so exactly one of
// the mod and Orbit's typist acts. Without Orbit installed it does nothing.

const ORBIT = ['/usr/bin/env', 'cosmos-orbit']
const FALLBACK_PREFIX = 'Orbit: handing off to a fresh context'

type OrbitReply = { ok: boolean; out: Record<string, unknown> }

async function orbit($: EngineInterface, args: string[]): Promise<OrbitReply> {
  try {
    const run = await $.process.run([...ORBIT, ...args], { timeoutMs: 10_000 })
    return { ok: run.exitCode === 0, out: JSON.parse(run.stdout || '{}') as Record<string, unknown> }
  } catch {
    return { ok: false, out: {} } // no Orbit on PATH, or it said nothing readable: stay out of the way
  }
}

const TAKEOVER_WAIT_MS = 20_000 // Orbit's typist gives up at 25 s and tells the user; the mod decides before it
const TAKEOVER_POLL_MS = 500

// The fresh session's SessionStart hook takes the task over; only then does the carry line mean anything. Submitted
// before (observed 2026-11-07 16:57: the fresh session started outside any repository and its hooks stayed silent),
// it reached a session holding no task, which then had no request to continue.
async function takenOver($: EngineInterface, task: string): Promise<boolean> {
  for (let waited = 0; waited < TAKEOVER_WAIT_MS; waited += TAKEOVER_POLL_MS) {
    if ((await orbit($, ['handoff-claim', '--session', '-', '--taken-over', task])).ok) return true
    await $.clock.sleep(TAKEOVER_POLL_MS)
  }
  return false
}

async function handOff($: EngineInterface, task: string, carryLine: string): Promise<void> {
  $.ui.toast('Orbit: handing off to a fresh session')
  try {
    // Claude Code puts a prompt a hook blocked back into the input (measured 2.1.318): left there, one Enter would
    // run the carried request a second time. The request is in Orbit's ledger; the input is emptied.
    await $.prompt.fill({ text: '', mode: 'replace' })
    await $.command.run({ command: 'clear', args: '' })
  } catch (error) {
    // Orbit's watcher sees no takeover and tells the user to type /clear; the request stays carried.
    $.ui.toast('Orbit: the handoff did not start (' + String(error).slice(0, 80) + '); type /clear')
    return
  }
  if (!(await takenOver($, task))) {
    // Nothing is submitted: the request stays in Orbit's ledger (`cosmos-orbit brief <task>` shows it).
    $.ui.toast('Orbit: the fresh session did not take the task over; the request stays carried (cosmos-orbit brief ' + task + ')')
    return
  }
  try {
    await $.prompt.submit({ text: carryLine })
    $.ui.toast('Orbit: a fresh session took the task over')
  } catch (error) {
    $.ui.toast('Orbit: the carried request was not submitted (' + String(error).slice(0, 80) + ')')
  }
}

let greeted = ''                 // the session this mod last told Orbit it is loaded in
let prefix = FALLBACK_PREFIX     // how Orbit's block reason starts (Orbit says so at --hello)

export const register: Register = on => {
  on('prompt.submit', async ($, e, next) => {
    const session = await $.session.id()
    if (session && session !== greeted) {
      greeted = session
      const hello = await orbit($, ['handoff-claim', '--hello', '--session', session])
      if (hello.ok && typeof hello.out.handoff_prefix === 'string') prefix = hello.out.handoff_prefix
    }
    const result = await next(e)
    // Claude Code wraps a settings hook's reason: "UserPromptSubmit operation blocked by hook:\n<reason>\n\nOriginal
    // prompt: …" (measured 2.1.318), so the reason is looked for inside the drop, not at its start.
    if (typeof result.drop !== 'string' || !result.drop.includes(prefix) || !session) return result
    const claim = await orbit($, ['handoff-claim', '--session', session])
    if (!claim.ok || typeof claim.out.carry_line !== 'string' || typeof claim.out.task !== 'string') return result // the typist has it, or nothing waits
    const carryLine = claim.out.carry_line
    const task = claim.out.task
    // Not here: a command run inside a hook the turn waits on is refused. Once this hook returned, the session is
    // idle (the request was blocked) and the clear runs.
    $.clock.after(200, () => void handOff($, task, carryLine))
    return result
  })
}
