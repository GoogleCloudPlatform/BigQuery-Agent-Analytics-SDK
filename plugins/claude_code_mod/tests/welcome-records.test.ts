// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

// The welcome card's writes to the store on their own (lib/welcome.ts): one
// at a time, the greeting first, then the landing, each with the card as it
// is by then; the close is not among them (register.ts writes it on its own,
// at once). In a file of its own, so welcome.test.ts also runs against code
// without the queue.

import { expect, test } from 'claude-code/testing'

import { PLUGIN_VERSION } from '../hooks/lib/config.ts'
import { CardRecords, type Welcome } from '../hooks/lib/welcome.ts'

import { SESSION_ID } from './helpers.ts'

const HERE = 'my-gcp-project.agent_analytics.agent_events (US)'
const OWN = `welcome:${PLUGIN_VERSION}:${SESSION_ID}`

// A store whose writes answer when the test says.
function heldStore(): { writes: [string, unknown][]; pending: () => number; answer: () => Promise<void>; save: (key: string, value: unknown) => Promise<void> } {
  const writes: [string, unknown][] = []
  const held: (() => void)[] = []
  return {
    writes,
    pending: () => held.length,
    answer: async () => {
      held.shift()?.()
      for (let i = 0; i < 5; i += 1) await Promise.resolve()
    },
    save: (key, value) =>
      new Promise(resolve =>
        held.push(() => {
          writes.push([key, value])
          resolve()
        }),
      ),
  }
}

const CARD: Welcome = { version: PLUGIN_VERSION, previous: null, sessionId: SESSION_ID, landedAt: null, landedTo: null, closed: false, sent: 0, destination: HERE, owner: 'a-load:1', rank: 1, since: 1, lifetime: 'a-lifetime' }

for (const what of ['greeting', 'landing'] as const) {
  test(`a ${what} whose write fails goes again at the card's next change`, async () => {
    const writes: [string, unknown][] = []
    let refusing = true
    const key = what === 'greeting' ? `welcome:${PLUGIN_VERSION}` : OWN
    const save = async (k: string, value: unknown): Promise<void> => {
      if (refusing && k === key) throw new Error('the store could not be written')
      writes.push([k, value])
    }
    const records = new CardRecords(save, CARD, what === 'greeting')
    records.update({ ...CARD, landedAt: 1, landedTo: HERE, sent: 1 })
    for (let i = 0; i < 10; i += 1) await Promise.resolve()
    expect(writes.some(([k]) => k === key)).toBe(false)
    refusing = false
    // More rows land: the card changes again, its landing as it was.
    records.update({ ...CARD, landedAt: 1, landedTo: HERE, sent: 3 })
    for (let i = 0; i < 10; i += 1) await Promise.resolve()
    expect(writes.filter(([k]) => k === key).length).toBe(1)
    expect(writes.find(([k]) => k === OWN)?.[1]).toEqual({ landedAt: 1, landedTo: HERE })
  })
}

test("a card's greeting and landing go one at a time, each with the card as it is by then; its close is not held behind them", async () => {
  const store = heldStore()
  const records = new CardRecords(store.save, CARD, true)
  // While the greeting is written: rows land, the card is closed (its close
  // is written on its own, by closeWelcome), and more rows land.
  records.update({ ...CARD, landedAt: 5, landedTo: HERE, sent: 1 })
  records.update({ ...CARD, landedAt: 5, landedTo: HERE, sent: 1, closed: true })
  records.update({ ...CARD, landedAt: 5, landedTo: HERE, sent: 3 })
  expect(store.pending()).toBe(1)
  for (let i = 0; i < 3; i += 1) await store.answer()
  expect(store.pending()).toBe(0)
  expect(store.writes).toEqual([
    [`welcome:${PLUGIN_VERSION}`, { sessionId: SESSION_ID, previous: null }],
    [OWN, { landedAt: 5, landedTo: HERE }],
  ])
})

test('a resumed card writes no greeting, and only a landing that changed', async () => {
  const store = heldStore()
  const records = new CardRecords(store.save, { ...CARD, landedAt: 5, landedTo: HERE }, false)
  expect(store.pending()).toBe(0)
  records.update({ ...CARD, landedAt: 5, landedTo: HERE, sent: 2 })
  records.update({ ...CARD, landedAt: 5, landedTo: HERE, sent: 2, closed: true })
  expect(store.pending()).toBe(0)
  records.update({ ...CARD, landedAt: 9, landedTo: HERE, sent: 2 })
  await store.answer()
  expect(store.writes).toEqual([[OWN, { landedAt: 9, landedTo: HERE }]])
})
