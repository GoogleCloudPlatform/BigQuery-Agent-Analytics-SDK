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

// Ids in the BQAA formats: 32-hex trace and event ids, 16-hex span ids
// (OpenTelemetry widths). `crypto.getRandomValues` is one of the web APIs a
// hooks module has.

const HEX: string[] = Array.from({ length: 256 }, (_, i) => i.toString(16).padStart(2, '0'))

export function randomHex(bytes: number): string {
  const buf = new Uint8Array(bytes)
  crypto.getRandomValues(buf)
  let out = ''
  for (const b of buf) out += HEX[b]
  return out
}

export type IdSource = {
  traceId: () => string
  spanId: () => string
  eventId: () => string
}

export const randomIds: IdSource = {
  traceId: () => randomHex(16),
  spanId: () => randomHex(8),
  eventId: () => randomHex(16),
}
