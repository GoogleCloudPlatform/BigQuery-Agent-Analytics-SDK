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

// A mod of its own, for tests/state-contract.test.sh: two commands, each its
// own dispatch, on two values of the session's state. `probe-read` reads `a`,
// waits on a store read the test holds, then reads `b`, writes `a` at the
// version it read, and reads both again; `probe-write` writes `b` (and `a`
// too, for `both`) meanwhile.

import type { Register } from 'claude-code'

const A = { plugin: 'state-contract', key: 'a' } as const
const B = { plugin: 'state-contract', key: 'b' } as const

export const register: Register = on => {
  on('command.run', { command: 'probe-read' }, async $ => {
    const first = await $.state.get(A)
    await $.store.get('pause')
    const staleB = await $.state.get(B)
    const set = await $.state.set(A, 99, { ifVersion: first.version })
    const afterA = await $.state.get(A)
    const afterB = await $.state.get(B)
    return { text: JSON.stringify({ first, staleB, set, afterA, afterB }) }
  })
  on('command.run', { command: 'probe-write' }, async ($, e) => {
    if (e.args === 'both') await $.state.set(A, 1)
    await $.state.set(B, 2)
    return { text: 'written' }
  })
}
