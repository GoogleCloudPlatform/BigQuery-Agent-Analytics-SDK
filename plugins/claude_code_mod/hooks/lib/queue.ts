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

// The in-memory buffer between the hooks and the background writer: bounded
// by rows and by bytes, and when full it drops its oldest rows.

import { type BqaaRow, rowBytes } from './rows.ts'

// `configuredUserId`: the user_id the row was built with, kept once the row
// has been given the credential's account (userIdFromCredentials), so the
// row can fall back to it when the current credential's account is unknown.
export type Queued = { row: BqaaRow; bytes: number; attempts: number; configuredUserId?: string | null }

export class BoundedQueue {
  private items: Queued[] = []
  private maxRows: number
  private maxBytes: number
  bytes = 0
  dropped = 0

  constructor(maxRows: number, maxBytes: number) {
    this.maxRows = maxRows
    this.maxBytes = maxBytes
  }

  get length(): number {
    return this.items.length
  }

  push(row: BqaaRow): void {
    const item: Queued = { row, bytes: rowBytes(row), attempts: 0 }
    this.items.push(item)
    this.bytes += item.bytes
    this.enforce()
  }

  // Puts rows back at the front, ahead of newer ones (a batch to retry).
  unshift(items: Queued[]): void {
    if (items.length === 0) return
    this.items.unshift(...items)
    for (const item of items) this.bytes += item.bytes
    this.enforce()
  }

  // Puts rows at the back, behind every row queued (another queue's rows,
  // newer than these).
  append(items: Queued[]): void {
    if (items.length === 0) return
    this.items.push(...items)
    for (const item of items) this.bytes += item.bytes
    this.enforce()
  }

  // Takes up to `maxRows` rows and `maxBytes` bytes from the front; always at
  // least one row when any is queued.
  take(maxRows: number, maxBytes: number): Queued[] {
    let count = 0
    let bytes = 0
    while (count < this.items.length && count < maxRows) {
      const next = this.items[count] as Queued
      if (count > 0 && bytes + next.bytes > maxBytes) break
      bytes += next.bytes
      count += 1
    }
    const taken = this.items.splice(0, count)
    this.bytes -= bytes
    return taken
  }

  takeAll(): Queued[] {
    const all = this.items
    this.items = []
    this.bytes = 0
    return all
  }

  // Holds the queue to new bounds (a configuration that changed), the oldest
  // rows dropped past them.
  limit(maxRows: number, maxBytes: number): void {
    this.maxRows = maxRows
    this.maxBytes = maxBytes
    this.enforce()
  }

  isUnderPressure(): boolean {
    return this.items.length > this.maxRows * 0.8 || this.bytes > this.maxBytes * 0.8
  }

  private enforce(): void {
    while (this.items.length > 0 && (this.items.length > this.maxRows || this.bytes > this.maxBytes)) {
      const oldest = this.items.shift() as Queued
      this.bytes -= oldest.bytes
      this.dropped += 1
    }
  }
}
