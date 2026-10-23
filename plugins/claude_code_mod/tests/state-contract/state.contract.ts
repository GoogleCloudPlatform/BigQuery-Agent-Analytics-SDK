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

// Run by tests/state-contract.test.sh as this mod's tests/state.test.ts (a
// name the main suite would pick up, so it is kept under this one): what the
// reload test's stand-in models of the host's `$.state`, checked against the
// host's own test runtime. One dispatch reads one moment: another's write is
// not seen; a conditional write lands only at the version read; landed or
// missed, it brings the whole moment up to date.

import { expect, test } from 'claude-code/testing'

const args = { origin: { kind: 'composer' }, presentation: { isFullscreen: false, columns: 100 } } as const

for (const write of ['both', 'b'] as const) {
  test(`one dispatch reads one moment, which its conditional write (${write === 'both' ? 'missed' : 'landed'}) brings up to date whole`, async ($, on) => {
    let release = (): void => undefined
    let entered = (): void => undefined
    const waiting = new Promise<void>(resolve => (entered = resolve))
    on('store.get', () =>
      new Promise<{ value: unknown }>(resolve => {
        release = () => resolve({ value: undefined })
        entered()
      }),
    )
    const read = $.command.run({ command: 'probe-read', args: '', ...args })
    await waiting
    await $.command.run({ command: 'probe-write', args: write, ...args })
    release()
    const seen = JSON.parse((await read).text ?? '{}') as Record<string, { value?: unknown; version?: number; isSet?: boolean }>
    expect(seen.first).toEqual({ version: 0 })
    expect(seen.staleB).toEqual({ version: 0 })
    expect(seen.set?.isSet).toBe(write === 'b')
    expect(seen.afterA?.value).toBe(write === 'both' ? 1 : 99)
    expect(seen.afterB?.value).toBe(2)
  })
}
