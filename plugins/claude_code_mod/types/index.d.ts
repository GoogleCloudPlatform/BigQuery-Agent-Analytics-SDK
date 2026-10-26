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

// The values this mod keeps in the session's `$.state`, which the host keeps
// across a reload of the mod's code (the code's own variables start over):
// the welcome card (lib/welcome.ts), its one copy, which every load of the
// code reads and the band draws from.
export type WelcomeState = {
  version: string
  previous: string | null
  sessionId: string | null
  landedAt: number | null
  landedTo: string | null
  closed: boolean
  // Rows landed where the card says rows go, by the run it counts from
  // (`since`) and every later run: the sum of their totals in `delivered`.
  sent: number
  // Where it counts rows: the `destinationKey` of the load that showed it.
  destination: string | null
  // The load of the code (and its session.start run) that showed it: the
  // one that speaks for it (a toast); rows an earlier load still sends only
  // count.
  owner: string
  // The rank of that load's run (`loads` when it ran), so a load whose reads
  // answered late never takes the card from a later one.
  rank: number
  // The rank of the run that opened the card, or last moved it where rows go
  // now: the card counts that run's rows and those of every run ranked above
  // it there, and no run's ranked below it, whenever they land.
  since: number
  // The lifetime of the count that rank is of (`loads`): runs of another,
  // from before the session's state was reset, never count on the card.
  lifetime: string
}

// The session's count of session.start runs, each run's rank, and the
// lifetime it counts in: a name the first run to count gives it, so a count
// that starts over (/clear, /resume and /branch reset every value of the
// session's state, and fire no session.start) is told from the one before.
export type LoadsState = {
  count: number
  lifetime: string
}

// A session.start run, as `loads` numbered it: its rank and the lifetime it
// took it in.
export type Run = {
  rank: number
  lifetime: string
}

// What one session.start run has landed in one destination so far: the
// run's rank and lifetime, the destination (`destinationKey`), and its rows
// in all.
export type DeliveredRows = {
  rank: number
  lifetime: string
  to: string
  rows: number
}

// Each run's rows landed so far in the session, by run and destination
// (register.ts `countKey`), whatever the card shows (none, closed, or
// counting elsewhere).
export type DeliveredState = Record<string, DeliveredRows>

declare module 'claude-code' {
  interface PluginState {
    'bigquery-agent-analytics-mod': {
      welcome: WelcomeState | null
      // How many session.start runs, of every load of the code, the session
      // has seen, and in what lifetime: each run's rank (register.ts
      // `nextLoad`).
      loads: LoadsState
      // Each run's rows landed so far in the session (DeliveredState).
      delivered: DeliveredState
    }
  }
}
