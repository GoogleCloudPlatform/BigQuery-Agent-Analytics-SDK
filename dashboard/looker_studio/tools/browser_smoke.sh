#!/usr/bin/env bash
# Loads the configurator in a real headless browser and fails on any
# page-level error or if the page's module never executed. Guards the class
# of failure the Node suite structurally cannot see: specifiers or APIs that
# resolve in Node but not in a browser (issue #404; the `node:path` incident
# on #405).
#
# Detection is instrumentation-based, not keyword-based: a script injected
# ahead of the module records window "error" events (capture phase, so
# failed module/resource loads count), unhandled promise rejections, and
# console.error calls, then stamps the count into the DOM where the dumped
# document can be asserted. Chrome's own exit status and stderr are
# additional failure triggers.
#
# Usage:
#   browser_smoke.sh              run the check against ../docs: index.html
#                                 and, when the docs ship it, the generated
#                                 BQCA deep link bqca/ (a second browser
#                                 pass with the same healthy baseline, plus
#                                 the surface app.mjs must apply on each
#                                 page: adk on index.html, bqca on bqca/)
#   browser_smoke.sh --self-test  run the fixtures and require each stated
#                                 outcome. The negative fixtures must each
#                                 FAIL: an immediate console error, an
#                                 occupied port, a failing browser binary, a
#                                 browser that writes healthy DOM then exits
#                                 nonzero, a console error delayed past the
#                                 marker's creation, a page that never
#                                 writes the app-initialized marker, a page
#                                 whose live field value is mutated without
#                                 a serialized value attribute, a delayed
#                                 live-value mutation after marker creation,
#                                 a decoy zero-error element beside a marker
#                                 recording a real error, a non-bind server
#                                 startup failure, a pinned port under a
#                                 bind conflict, an alive-but-unready
#                                 server, and a BQCA deep link whose module
#                                 path is broken beside a healthy main page.
#                                 The positive fixture must PASS: a
#                                 real bind collision on the first attempt
#                                 is retried on fresh ports to success,
#                                 with two to five server spawns and one
#                                 retry line per spawn past the first.
#
# Every browser-level fixture except the missing-initialization one
# satisfies the full healthy baseline (#448): the runtime
# data-bqaa-app-initialized marker (set by script, never static), an
# aria-disabled action, and no aria-invalid anywhere — so each fixture's
# injected fault is the sole reason it fails. The server-startup fixtures
# inject their fault before any page is loaded. Single-page fixtures ship
# no bqca/ page, so the surface assertions never apply to them.
#
# Env: CHROME_BIN, SMOKE_PORT, SMOKE_DOCS_DIR override discovery. A pinned
# SMOKE_PORT disables the port retry (exactly one attempt on that port),
# so the occupied-port negative fixture keeps failing as it must.
# SMOKE_READY_POLLS is the readiness budget per server attempt: that many
# nonce polls, 0.5 s apart (default 20, about 10 s). The self-test lowers
# it only for the alive-but-unready fixture, whose expected outcome is the
# budget running out.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SCRIPT_PATH="$SCRIPT_DIR/$(basename "$0")"
DOCS_DIR="$(cd "${SMOKE_DOCS_DIR:-$SCRIPT_DIR/../docs}" && pwd)"
OUT_DIR="$(mktemp -d)"
SERVER_PID=""
CHROME_PID=""

cleanup() {
  # Kill AND reap: waiting after the kill collects the child so bash never
  # prints a stray "Terminated" job report at exit, and no zombie is left.
  if [ -n "$SERVER_PID" ]; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
  if [ -n "$CHROME_PID" ]; then
    kill "$CHROME_PID" 2>/dev/null || true
    wait "$CHROME_PID" 2>/dev/null || true
  fi
  rm -rf "$OUT_DIR"
}
trap cleanup EXIT

fail() {
  echo "browser smoke: $*" >&2
  exit 1
}

READY_POLLS="${SMOKE_READY_POLLS:-20}"
case "$READY_POLLS" in
  *[!0-9]* | 0*)
    fail "SMOKE_READY_POLLS must be a positive integer, got '$READY_POLLS'"
    ;;
esac

find_chrome() {
  if [ -n "${CHROME_BIN:-}" ]; then
    echo "$CHROME_BIN"
    return
  fi
  for candidate in google-chrome google-chrome-stable chromium-browser chromium \
      "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"; do
    if command -v "$candidate" >/dev/null 2>&1; then
      echo "$candidate"
      return
    fi
  done
  echo ""
}

# ---------------------------------------------------------------------------
# Self-test: every negative fixture below must make the main check FAIL;
# the positive retry fixture must make it PASS with the first attempt's
# bind collision retried to success.
# ---------------------------------------------------------------------------
if [ "${1:-}" = "--self-test" ]; then
  CHROME="$(find_chrome)"
  [ -n "$CHROME" ] || fail "no Chrome/Chromium binary found (set CHROME_BIN)"

  # 1. A page that reports a generic console error (no keyword the old
  #    grep would have matched) but otherwise satisfies the full healthy
  #    baseline: runtime marker, pristine field, disabled action.
  FIXTURE="$OUT_DIR/fixture-console-error"
  mkdir -p "$FIXTURE"
  cat > "$FIXTURE/index.html" <<'HTML'
<!doctype html>
<html><body>
<input id="table-id">
<a id="create-dashboard" aria-disabled="true"></a>
<button id="copy-link" disabled></button>
<script>
document.documentElement.setAttribute("data-bqaa-app-initialized", "true");
console.error("generic boom");
</script>
</body></html>
HTML
  if SMOKE_DOCS_DIR="$FIXTURE" "$SCRIPT_PATH" >/dev/null 2>&1; then
    fail "self-test 1 FAILED: a page with a console error passed"
  fi
  echo "self-test 1 OK: generic console error is detected"

  # 2. An occupied port serving the WRONG tree: the check must notice it
  #    does not own the port instead of validating a stranger's content.
  #    A pinned port stays single-attempt with the pre-retry diagnostic,
  #    so this fixture also pins that wording byte-for-byte.
  DECOY="$OUT_DIR/decoy"
  mkdir -p "$DECOY"
  cp "$DOCS_DIR/index.html" "$DECOY/index.html" 2>/dev/null || echo "<body></body>" > "$DECOY/index.html"
  BUSY_PORT=$((30000 + RANDOM % 10000))
  python3 -m http.server "$BUSY_PORT" --directory "$DECOY" >/dev/null 2>&1 &
  DECOY_PID=$!
  disown "$DECOY_PID" 2>/dev/null || true
  sleep 1
  PINNED_ERR="$OUT_DIR/fixture2-stderr.txt"
  if SMOKE_PORT="$BUSY_PORT" "$SCRIPT_PATH" >/dev/null 2>"$PINNED_ERR"; then
    kill "$DECOY_PID" 2>/dev/null || true
    fail "self-test 2 FAILED: an occupied port was treated as our server"
  fi
  kill "$DECOY_PID" 2>/dev/null || true
  grep -Fq "server did not become ready on port $BUSY_PORT (occupied by another process, or failed to start)" "$PINNED_ERR" ||
    fail "self-test 2 FAILED: the pinned-port diagnostic text changed"
  echo "self-test 2 OK: occupied/stale port is detected (diagnostic unchanged)"

  # 3. A browser binary that exits nonzero without producing output.
  if CHROME_BIN="/bin/false" "$SCRIPT_PATH" >/dev/null 2>&1; then
    fail "self-test 3 FAILED: a failing browser binary passed"
  fi
  echo "self-test 3 OK: nonzero browser exit is detected"

  # 4. A browser that writes a healthy-looking instrumented DOM (markers
  #    that would satisfy every DOM assertion), lingers briefly, and THEN
  #    exits nonzero. Only honest exit-status reaping catches this one.
  FAKE_CHROME="$OUT_DIR/fake-chrome-slow-nonzero.sh"
  cat > "$FAKE_CHROME" <<'FAKE'
#!/usr/bin/env bash
cat <<'DOM'
<html data-bqaa-app-initialized="true"><body>
<input id="table-id">
<a id="create-dashboard" aria-disabled="true"></a>
<button id="copy-link" disabled></button>
<div id="smoke-result" data-errors="0" data-detail="" data-table-value="" data-create-aria-disabled="true" data-create-has-href="false" data-copy-disabled="true"></div>
</body></html>
DOM
sleep 1
exit 42
FAKE
  chmod +x "$FAKE_CHROME"
  if CHROME_BIN="$FAKE_CHROME" "$SCRIPT_PATH" >/dev/null 2>&1; then
    fail "self-test 4 FAILED: nonzero exit after healthy DOM passed"
  fi
  echo "self-test 4 OK: nonzero exit after healthy DOM is detected"

  # 5. A generic console error that fires AFTER the marker is created
  #    (900 ms past load, inside the virtual-time budget). Only a live
  #    marker — not a one-shot snapshot — catches this one.
  DELAYED="$OUT_DIR/fixture-delayed-error"
  mkdir -p "$DELAYED"
  cat > "$DELAYED/index.html" <<'HTML'
<!doctype html>
<html><body>
<input id="table-id">
<a id="create-dashboard" aria-disabled="true"></a>
<button id="copy-link" disabled></button>
<script>
document.documentElement.setAttribute("data-bqaa-app-initialized", "true");
window.addEventListener("load", function () {
  setTimeout(function () { console.error("generic delayed boom"); }, 900);
});
</script>
</body></html>
HTML
  if SMOKE_DOCS_DIR="$DELAYED" "$SCRIPT_PATH" >/dev/null 2>&1; then
    fail "self-test 5 FAILED: a delayed console error passed"
  fi
  echo "self-test 5 OK: post-snapshot delayed error is detected"

  # 6. A page that looks completely healthy — no errors, pristine field,
  #    disabled action — but never writes the runtime app-initialized
  #    marker. Only the marker assertion catches a module that silently
  #    failed to execute.
  UNINITIALIZED="$OUT_DIR/fixture-missing-initialization"
  mkdir -p "$UNINITIALIZED"
  cat > "$UNINITIALIZED/index.html" <<'HTML'
<!doctype html>
<html><body>
<input id="table-id">
<a id="create-dashboard" aria-disabled="true"></a>
<button id="copy-link" disabled></button>
</body></html>
HTML
  if SMOKE_DOCS_DIR="$UNINITIALIZED" "$SCRIPT_PATH" >/dev/null 2>&1; then
    fail "self-test 6 FAILED: a page without the app-initialized marker passed"
  fi
  echo "self-test 6 OK: missing app initialization is detected"

  # 7. A page whose live table-id value PROPERTY is set to a non-empty
  #    string (no value attribute ever appears in the markup). Serialized
  #    tag checks false-pass this state; only the live-state snapshot on
  #    the instrumentation marker can catch it.
  MUTATED="$OUT_DIR/fixture-live-value"
  mkdir -p "$MUTATED"
  cat > "$MUTATED/index.html" <<'HTML'
<!doctype html>
<html><body>
<input id="table-id">
<a id="create-dashboard" aria-disabled="true"></a>
<button id="copy-link" disabled></button>
<script>
document.documentElement.setAttribute("data-bqaa-app-initialized", "true");
document.getElementById("table-id").value = "not-pristine";
</script>
</body></html>
HTML
  if SMOKE_DOCS_DIR="$MUTATED" "$SCRIPT_PATH" >/dev/null 2>&1; then
    fail "self-test 7 FAILED: a mutated live field value passed as pristine"
  fi
  echo "self-test 7 OK: non-pristine live field value is detected"

  # 8. A page that is pristine at marker creation (400 ms) but mutates the
  #    live field value at 900 ms. Only a periodically restamped snapshot —
  #    not a one-shot at creation — reflects the final state.
  DELAYED_VALUE="$OUT_DIR/fixture-delayed-live-value"
  mkdir -p "$DELAYED_VALUE"
  cat > "$DELAYED_VALUE/index.html" <<'HTML'
<!doctype html>
<html><body>
<input id="table-id">
<a id="create-dashboard" aria-disabled="true"></a>
<button id="copy-link" disabled></button>
<script>
document.documentElement.setAttribute("data-bqaa-app-initialized", "true");
window.addEventListener("load", function () {
  setTimeout(function () {
    document.getElementById("table-id").value = "late-mutation";
  }, 900);
});
</script>
</body></html>
HTML
  if SMOKE_DOCS_DIR="$DELAYED_VALUE" "$SCRIPT_PATH" >/dev/null 2>&1; then
    fail "self-test 8 FAILED: a delayed live-value mutation passed as pristine"
  fi
  echo "self-test 8 OK: delayed live-value mutation is detected"

  # 9. A page carrying an unrelated data-errors="0" element while the real
  #    instrumentation marker records an error. An unscoped whole-DOM grep
  #    would be satisfied by the decoy; only the marker-scoped assertion
  #    catches the real count.
  MASKING="$OUT_DIR/fixture-masking-element"
  mkdir -p "$MASKING"
  cat > "$MASKING/index.html" <<'HTML'
<!doctype html>
<html><body>
<input id="table-id">
<a id="create-dashboard" aria-disabled="true"></a>
<button id="copy-link" disabled></button>
<div data-errors="0" data-detail=""></div>
<script>
document.documentElement.setAttribute("data-bqaa-app-initialized", "true");
console.error("masked boom");
</script>
</body></html>
HTML
  if SMOKE_DOCS_DIR="$MASKING" "$SCRIPT_PATH" >/dev/null 2>&1; then
    fail "self-test 9 FAILED: a decoy data-errors element masked a real error"
  fi
  echo "self-test 9 OK: decoy zero-error element cannot mask the marker"

  # Test-only python3 shim shared by the server-startup fixtures below: it
  # counts http.server spawns (SMOKE_SHIM_COUNTER) and, on the FIRST
  # spawn only, injects the deterministic startup failure named by
  # SMOKE_SHIM_MODE. Every other invocation passes through to the real
  # interpreter untouched, so instrumentation and later attempts run the
  # real code paths — and a retry-happy regression would be fed by the
  # shim's SECOND spawn, which serves normally.
  make_shim() {
    mkdir -p "$1"
    cat > "$1/python3" <<'SHIM'
#!/usr/bin/env bash
# Injects a deterministic first-spawn server failure for the smoke
# self-test; everything else passes through to the real python3.
set -u
REAL="${SMOKE_SHIM_REAL:?SMOKE_SHIM_REAL must name the real python3}"
MODE="${SMOKE_SHIM_MODE:-}"
COUNTER="${SMOKE_SHIM_COUNTER:-}"
if [ "$REAL" = "$0" ]; then
  echo "shim: SMOKE_SHIM_REAL must not name the shim itself" >&2
  exit 9
fi
if [ "${1:-}" = "-m" ] && [ "${2:-}" = "http.server" ] && [ "${4:-}" = "--directory" ]; then
  SPAWNS=1
  if [ -n "$COUNTER" ]; then
    printf 'spawn\n' >> "$COUNTER"
    SPAWNS="$(grep -c '' "$COUNTER" || true)"
  fi
  if [ -n "$MODE" ] && [ "$SPAWNS" = "1" ]; then
    case "$MODE" in
      collide-first)
        # Really occupy the port first, then start the real server on it:
        # the server dies with a genuine EADDRINUSE ("Address already in
        # use") on its own bind attempt — the exact failure the retry loop
        # must positively identify before it may retry.
        "$REAL" - "$3" "$5" <<'PYEOF'
import socket
import subprocess
import sys

port = int(sys.argv[1])
directory = sys.argv[2]
# SO_REUSEADDR lets the holders bind even over a port sitting in TIME_WAIT
# (Linux ephemeral range overlaps the draw range), so the collision is
# always injected; an ACTIVE listener still blocks the real server's bind
# on both Linux and macOS, which is what makes the failure deterministic.
holders = []
for family, address in (
    (socket.AF_INET, "0.0.0.0"),
    (socket.AF_INET6, "::"),
):
  try:
    holder = socket.socket(family, socket.SOCK_STREAM)
    holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if family == socket.AF_INET6 and hasattr(socket, "IPV6_V6ONLY"):
      holder.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
    holder.bind((address, port))
    holder.listen(1)
    holders.append(holder)
  except OSError:
    # Already taken by an active socket: the real server started below
    # hits that live listener and reports the same genuine EADDRINUSE.
    pass
try:
  server = subprocess.run(
      [
          sys.executable,
          "-m",
          "http.server",
          str(port),
          "--directory",
          directory,
      ],
      timeout=30,
  )
except subprocess.TimeoutExpired:
  print("shim: held-port server run timed out", file=sys.stderr)
  sys.exit(9)
sys.exit(server.returncode)
PYEOF
        exit $?
        ;;
      nonbind-first)
        # A real class of intermittent startup failure that a fresh port
        # can never cure: any retry would only mask it.
        echo "shim: synthetic non-bind startup failure (server module import exploded)" >&2
        exit 7
        ;;
      alive-unready-first)
        # Binds nothing, stays alive, never answers the readiness nonce:
        # the readiness-timeout-with-a-live-child case.
        echo "shim: alive but never serving the readiness nonce" >&2
        exec sleep 30
        ;;
      *)
        echo "shim: unknown SMOKE_SHIM_MODE '$MODE'" >&2
        exit 9
        ;;
    esac
  fi
fi
exec "$REAL" "$@"
SHIM
    chmod +x "$1/python3"
  }

  # 10. (positive) A REAL bind collision on the first attempt: the shim
  #     holds the drawn port with genuine listeners, so the first
  #     http.server dies with an authentic "Address already in use". The
  #     check must positively identify the bind conflict, retry on a fresh
  #     port, and still pass. The recovery draws are random too, so a
  #     later attempt may hit a genuine second collision and correctly
  #     retry again within the five-attempt cap: assert 2-5 spawns with
  #     one retry line per spawn past the first — the first attempt
  #     collided, every retry logged its line, the final spawn served —
  #     instead of exact counts that a real environmental collision on a
  #     recovery draw would break.
  #     NOTE: the real interpreter is resolved BEFORE the PATH override
  #     below: bash applies prefix assignments left to right, so expanding
  #     "$(command -v python3)" inside the PATH-prefixed command would
  #     resolve to the shim itself.
  REAL_PY="$(command -v python3)"
  make_shim "$OUT_DIR/shim-collide"
  COLLIDE_COUNT="$OUT_DIR/collide-spawns.txt"
  COLLIDE_ERR="$OUT_DIR/fixture10-stderr.txt"
  : > "$COLLIDE_COUNT"
  if ! PATH="$OUT_DIR/shim-collide:$PATH" \
       SMOKE_SHIM_REAL="$REAL_PY" \
       SMOKE_SHIM_MODE=collide-first \
       SMOKE_SHIM_COUNTER="$COLLIDE_COUNT" \
       SMOKE_DOCS_DIR="$DOCS_DIR" \
       "$SCRIPT_PATH" >/dev/null 2>"$COLLIDE_ERR"; then
    fail "self-test 10 FAILED: a bind collision on the first attempt was not retried to success"
  fi
  COLLIDE_SPAWNS="$(grep -c '' "$COLLIDE_COUNT" || true)"
  [ "$COLLIDE_SPAWNS" -ge 2 ] && [ "$COLLIDE_SPAWNS" -le 5 ] ||
    fail "self-test 10 FAILED: expected 2-5 server spawns (first collision, then recovery draws within the five-attempt cap), got $COLLIDE_SPAWNS"
  RETRY_LINES="$(grep -c 'already in use; retrying on a fresh port' "$COLLIDE_ERR" || true)"
  [ "$RETRY_LINES" -eq "$((COLLIDE_SPAWNS - 1))" ] ||
    fail "self-test 10 FAILED: retry lines must equal server spawns minus one (spawns: $COLLIDE_SPAWNS, retry lines: $RETRY_LINES)"
  echo "self-test 10 OK: bind collision on attempt 1 is retried on a fresh port and passes"

  # 11. A NON-BIND startup failure on the first attempt — the masking
  #     regression this fixture exists for: a retry-everything loop would
  #     swallow the failure and pass on the shim's second, healthy spawn.
  #     The check must fail ON the first attempt (exactly one server
  #     spawn), and the diagnostic must carry the captured server output.
  make_shim "$OUT_DIR/shim-nonbind"
  NONBIND_COUNT="$OUT_DIR/nonbind-spawns.txt"
  NONBIND_ERR="$OUT_DIR/fixture11-stderr.txt"
  : > "$NONBIND_COUNT"
  if PATH="$OUT_DIR/shim-nonbind:$PATH" \
     SMOKE_SHIM_REAL="$REAL_PY" \
     SMOKE_SHIM_MODE=nonbind-first \
     SMOKE_SHIM_COUNTER="$NONBIND_COUNT" \
     SMOKE_DOCS_DIR="$DOCS_DIR" \
     "$SCRIPT_PATH" >/dev/null 2>"$NONBIND_ERR"; then
    fail "self-test 11 FAILED: a non-bind server startup failure was masked by a retry"
  fi
  NONBIND_SPAWNS="$(grep -c '' "$NONBIND_COUNT" || true)"
  [ "$NONBIND_SPAWNS" = "1" ] ||
    fail "self-test 11 FAILED: a non-bind failure must fail on attempt 1 with no retry (server spawns: $NONBIND_SPAWNS)"
  grep -Fq "synthetic non-bind startup failure" "$NONBIND_ERR" ||
    fail "self-test 11 FAILED: the diagnostic must include the captured server output"
  echo "self-test 11 OK: non-bind startup failure fails on attempt 1, no retry, output surfaced"

  # 12. A pinned SMOKE_PORT stays single-attempt even under a positively
  #     identified bind conflict: the shim would happily serve on a second
  #     spawn, so any retry would be caught by the spawn counter, and the
  #     diagnostic must stay byte-identical to the pre-retry wording. The
  #     docs are the console-error fixture so even a buggy retry that
  #     reached the browser would still fail the check.
  make_shim "$OUT_DIR/shim-pinned"
  PIN_PORT=$((30000 + RANDOM % 10000))
  PIN_COUNT="$OUT_DIR/pinned-spawns.txt"
  PIN_ERR="$OUT_DIR/fixture12-stderr.txt"
  : > "$PIN_COUNT"
  if PATH="$OUT_DIR/shim-pinned:$PATH" \
     SMOKE_SHIM_REAL="$REAL_PY" \
     SMOKE_SHIM_MODE=collide-first \
     SMOKE_SHIM_COUNTER="$PIN_COUNT" \
     SMOKE_DOCS_DIR="$FIXTURE" \
     SMOKE_PORT="$PIN_PORT" \
     "$SCRIPT_PATH" >/dev/null 2>"$PIN_ERR"; then
    fail "self-test 12 FAILED: a pinned occupied port passed"
  fi
  PIN_SPAWNS="$(grep -c '' "$PIN_COUNT" || true)"
  [ "$PIN_SPAWNS" = "1" ] ||
    fail "self-test 12 FAILED: a pinned port must stay single-attempt (server spawns: $PIN_SPAWNS)"
  grep -Fq "server did not become ready on port $PIN_PORT (occupied by another process, or failed to start)" "$PIN_ERR" ||
    fail "self-test 12 FAILED: the pinned-port diagnostic text changed"
  echo "self-test 12 OK: pinned port stays single-attempt with the unchanged diagnostic"

  # 13. A server that stays alive but never answers the readiness nonce:
  #     a readiness timeout with a LIVE child must fail immediately — a
  #     retry on a fresh port could only mask a real startup hang. The
  #     shim would serve normally on a second spawn, so the spawn counter
  #     catches any retry. The expected outcome is the budget running
  #     out, and the code after the poll loop is the same for any budget,
  #     so a 4-poll budget (about 2 s) replaces the default 20 (about
  #     10 s). The shim logs its line and execs sleep within milliseconds
  #     of the spawn, well inside 2 s.
  make_shim "$OUT_DIR/shim-alive"
  ALIVE_COUNT="$OUT_DIR/alive-spawns.txt"
  ALIVE_ERR="$OUT_DIR/fixture13-stderr.txt"
  : > "$ALIVE_COUNT"
  if PATH="$OUT_DIR/shim-alive:$PATH" \
     SMOKE_SHIM_REAL="$REAL_PY" \
     SMOKE_SHIM_MODE=alive-unready-first \
     SMOKE_SHIM_COUNTER="$ALIVE_COUNT" \
     SMOKE_DOCS_DIR="$FIXTURE" \
     SMOKE_READY_POLLS=4 \
     "$SCRIPT_PATH" >/dev/null 2>"$ALIVE_ERR"; then
    fail "self-test 13 FAILED: an alive-but-unready server passed"
  fi
  ALIVE_SPAWNS="$(grep -c '' "$ALIVE_COUNT" || true)"
  [ "$ALIVE_SPAWNS" = "1" ] ||
    fail "self-test 13 FAILED: an alive-but-unready child must fail immediately with no retry (server spawns: $ALIVE_SPAWNS)"
  grep -Fq "alive but never serving the readiness nonce" "$ALIVE_ERR" ||
    fail "self-test 13 FAILED: the diagnostic must include the captured server output"
  echo "self-test 13 OK: readiness timeout with a live child fails immediately, no retry"

  # 14. The real site with a BQCA deep link whose module path is broken
  #     (./app.mjs from the bqca/ subdirectory 404s) while the main page
  #     stays healthy: only the second, bqca/ browser pass can catch it,
  #     and the diagnostic must name that page.
  if [ -f "$DOCS_DIR/bqca/index.html" ]; then
    BROKEN_BQCA="$OUT_DIR/fixture-broken-bqca-module"
    BROKEN_BQCA_ERR="$OUT_DIR/fixture14-stderr.txt"
    mkdir -p "$BROKEN_BQCA"
    cp -R "$DOCS_DIR/." "$BROKEN_BQCA/"
    python3 - "$BROKEN_BQCA/bqca/index.html" <<'EOF'
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
source = path.read_text()
good = '<script type="module" src="../app.mjs"></script>'
assert source.count(good) == 1, "bqca/index.html must load ../app.mjs exactly once"
path.write_text(source.replace(good, good.replace("../app.mjs", "./app.mjs")))
EOF
    if SMOKE_DOCS_DIR="$BROKEN_BQCA" "$SCRIPT_PATH" >/dev/null 2>"$BROKEN_BQCA_ERR"; then
      fail "self-test 14 FAILED: a BQCA page whose module never loads passed"
    fi
    grep -Fq "browser smoke: bqca/index.html: " "$BROKEN_BQCA_ERR" ||
      fail "self-test 14 FAILED: the failure must come from the bqca/ pass (stderr: $(tr '\n' ' ' < "$BROKEN_BQCA_ERR"))"
    echo "self-test 14 OK: a broken BQCA deep link fails the bqca/ pass while the main page passes"
  else
    echo "self-test 14 SKIPPED: the docs ship no bqca/index.html"
  fi

  echo "browser smoke self-test OK: every negative fixture fails as required, and the bind-collision retry recovers"
  exit 0
fi

# ---------------------------------------------------------------------------
# Main check.
# ---------------------------------------------------------------------------
CHROME_BIN="$(find_chrome)"
[ -n "$CHROME_BIN" ] || fail "no Chrome/Chromium binary found (set CHROME_BIN)"

# Instrumented copy of the site: the injected script runs before the module
# and records everything the page throws. A site that ships the generated
# BQCA deep link (bqca/index.html) gets the same instrumentation there and a
# second browser pass below; fixtures without it keep the one-page check.
SITE="$OUT_DIR/site"
mkdir -p "$SITE"
cp -R "$DOCS_DIR/." "$SITE/"
PAGES=("$SITE/index.html")
BQCA_PAGE=""
if [ -f "$DOCS_DIR/bqca/index.html" ]; then
  BQCA_PAGE=1
  PAGES+=("$SITE/bqca/index.html")
fi
python3 - "${PAGES[@]}" <<'EOF'
import pathlib
import sys

instrument = """<script>
window.__smokeErrors = [];
(function () {
  // The marker is LIVE: every recorded error re-stamps it, so anything
  // that fires before the DOM dump (the whole virtual-time budget) is
  // reflected, not just errors before a one-shot snapshot.
  var marker = null;
  var stamp = function () {
    if (!marker) {
      return;
    }
    marker.setAttribute("data-errors", String(window.__smokeErrors.length));
    marker.setAttribute("data-detail", window.__smokeErrors.join(" | ").slice(0, 500));
    // Live-state snapshot (#449 review): dump-dom does not reflect the
    // value PROPERTY into a value attribute, so the pristine assertions
    // must read the live properties, not the serialized markup.
    var table = document.querySelector("#table-id");
    var create = document.querySelector("#create-dashboard");
    var copy = document.querySelector("#copy-link");
    marker.setAttribute(
      "data-table-value",
      table ? String(table.value) : "MISSING"
    );
    marker.setAttribute(
      "data-create-aria-disabled",
      create ? String(create.getAttribute("aria-disabled")) : "MISSING"
    );
    marker.setAttribute(
      "data-create-has-href",
      create ? String(create.hasAttribute("href")) : "MISSING"
    );
    marker.setAttribute(
      "data-copy-disabled",
      copy ? String(copy.disabled) : "MISSING"
    );
    // Surface snapshot: the profile app.mjs applied, the placeholder it
    // wrote, and how many profile-only elements disagree with it.
    var active = document.documentElement.getAttribute("data-bqaa-profile");
    var scoped = document.querySelectorAll("[data-profile-only]");
    var mismatches = 0;
    for (var i = 0; i < scoped.length; i += 1) {
      if (scoped[i].hidden !== (scoped[i].getAttribute("data-profile-only") !== active)) {
        mismatches += 1;
      }
    }
    marker.setAttribute("data-profile", String(active));
    marker.setAttribute(
      "data-placeholder",
      table ? String(table.getAttribute("placeholder")) : "MISSING"
    );
    marker.setAttribute("data-profile-mismatches", String(mismatches));
  };
  var record = function (message) {
    window.__smokeErrors.push(String(message));
    stamp();
  };
  window.addEventListener("error", function (event) {
    record(event.message || (event.target && (event.target.src || event.target.href)) || "resource error");
  }, true);
  window.addEventListener("unhandledrejection", function (event) {
    record(event.reason);
  });
  var original = console.error;
  console.error = function () {
    record(Array.prototype.join.call(arguments, " "));
    original.apply(console, arguments);
  };
  window.addEventListener("load", function () {
    setTimeout(function () {
      marker = document.createElement("div");
      marker.id = "smoke-result";
      document.body.appendChild(marker);
      stamp();
      // Restamp periodically until the DOM dump: a one-time snapshot would
      // miss a live-state mutation after marker creation (#449 review), the
      // same way the error count is kept live rather than one-shot.
      setInterval(stamp, 100);
    }, 400);
  });
})();
</script>"""
marker = "<body>"
for name in sys.argv[1:]:
  path = pathlib.Path(name)
  source = path.read_text()
  assert marker in source, f"{name} has no <body> tag to instrument"
  path.write_text(source.replace(marker, marker + instrument, 1))
EOF

# The server must provably be OURS: readiness is a nonce round-trip, not a
# sleep, so an occupied port (our bind fails, a stranger answers) is caught.
# One random draw is not enough: the pick range overlaps the runner's
# ephemeral port span, and a single collision red-fails an otherwise-green
# CI run (main @ 1908eb0, run 35760370644: port 37368). Draw up to five
# candidates — every attempt still proves ownership via the nonce, and an
# explicitly pinned SMOKE_PORT stays single-attempt so the occupied-port
# negative fixture keeps failing exactly as it must.
#
# A retry is admissible for exactly ONE failure: a bind conflict,
# positively identified from the dead child's own captured output
# (EADDRINUSE / "Address already in use") — the only failure a fresh port
# can cure. Every other failure — a non-bind exit, a readiness timeout
# with the child still alive, or anything unidentified — fails the check
# immediately on that attempt with the captured server output in the
# diagnostic, so an intermittent real startup failure can never be masked
# by a later lucky spawn.
NONCE="smoke-nonce-$$-$RANDOM"
echo "$NONCE" > "$SITE/$NONCE.txt"
ATTEMPTS=5
if [ -n "${SMOKE_PORT:-}" ]; then
  ATTEMPTS=1
fi

# Last lines of one attempt's captured server output, flattened, so a
# fail-fast diagnostic carries the child's own error, not a generic one.
server_output_snippet() {
  if [ ! -s "$1" ]; then
    echo "(no server output captured)"
    return
  fi
  tail -n 5 "$1" | tr '\n' ' '
}

READY=""
PORT=""
for ATTEMPT in $(seq 1 "$ATTEMPTS"); do
  PORT="${SMOKE_PORT:-$((20000 + RANDOM % 20000))}"
  SERVER_LOG="$OUT_DIR/server-attempt-$ATTEMPT.log"
  python3 -m http.server "$PORT" --directory "$SITE" >"$SERVER_LOG" 2>&1 &
  SERVER_PID=$!
  for _ in $(seq 1 "$READY_POLLS"); do
    BODY="$(curl -fsS --max-time 2 "http://127.0.0.1:$PORT/$NONCE.txt" 2>/dev/null || true)"
    if [ "$BODY" = "$NONCE" ]; then
      READY=1
      break
    fi
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
      break
    fi
    sleep 0.5
  done
  if [ -n "$READY" ]; then
    break
  fi
  if kill -0 "$SERVER_PID" 2>/dev/null; then
    # Alive but never served the nonce: the port is ours, the server just
    # never answered. A fresh port cannot cure that — fail now, before a
    # retry could mask a real startup hang.
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
    SERVER_PID=""
    fail "server did not become ready on port $PORT (server still running, never answered the readiness nonce); server output: $(server_output_snippet "$SERVER_LOG")"
  fi
  # The child is dead: reap its real exit status and classify it by the
  # captured output of THIS attempt.
  wait "$SERVER_PID" && SERVER_STATUS=0 || SERVER_STATUS=$?
  SERVER_PID=""
  if grep -Eq 'EADDRINUSE|Address already in use' "$SERVER_LOG"; then
    # Positively identified bind conflict: the one retryable failure.
    if [ "$ATTEMPT" -lt "$ATTEMPTS" ]; then
      echo "browser smoke: attempt $ATTEMPT/$ATTEMPTS: port $PORT already in use; retrying on a fresh port" >&2
      continue
    fi
    fail "server did not become ready on port $PORT (occupied by another process, or failed to start)"
  fi
  # Any other death is a real startup failure: surface it from this attempt.
  fail "server exited with status $SERVER_STATUS on port $PORT (attempt $ATTEMPT of $ATTEMPTS); server output: $(server_output_snippet "$SERVER_LOG")"
done
[ -n "$READY" ] || fail "server did not become ready on port $PORT (occupied by another process, or failed to start)"

# Chrome occasionally lingers after dumping the DOM, so it runs in the
# background with a deadline. The child's exit status is ALWAYS reaped and
# honored: a natural nonzero exit fails the check even when it happens
# after a healthy-looking DOM was written. The only exempt exit is the
# deliberate timeout kill below — and only when this script's own kill
# succeeded, so a racing natural exit still surfaces its real status.
#
# dump_dom URL DOM_OUT CONSOLE_OUT USER_DATA_DIR LABEL
dump_dom() {
  "$CHROME_BIN" --headless=new --disable-gpu --no-first-run --no-sandbox \
    --user-data-dir="$4" --enable-logging=stderr \
    --virtual-time-budget=5000 --dump-dom "$1" \
    > "$2" 2> "$3" &
  CHROME_PID=$!

  for _ in $(seq 1 45); do
    if ! kill -0 "$CHROME_PID" 2>/dev/null; then
      break
    fi
    if [ -s "$2" ]; then
      break
    fi
    sleep 1
  done
  # Grace window: let a browser that already produced output finish and
  # report its real status instead of assuming success.
  for _ in $(seq 1 10); do
    if ! kill -0 "$CHROME_PID" 2>/dev/null; then
      break
    fi
    sleep 1
  done
  TIMED_OUT_KILL=""
  if kill -0 "$CHROME_PID" 2>/dev/null; then
    if kill "$CHROME_PID" 2>/dev/null; then
      TIMED_OUT_KILL=1
    fi
  fi
  wait "$CHROME_PID" && CHROME_STATUS=0 || CHROME_STATUS=$?
  CHROME_PID=""
  if [ -z "$TIMED_OUT_KILL" ] && [ "$CHROME_STATUS" -ne 0 ]; then
    fail "${5:-}browser exited with status $CHROME_STATUS"
  fi
}

# assert_live_page DOM CONSOLE STATIC_HTML LABEL [PROFILE DEFAULT_TABLE]
# The healthy baseline every served page must meet; PROFILE additionally
# requires app.mjs to have applied that surface.
assert_live_page() {
  local dom="$1" console_log="$2" static_html="$3" label="$4"
  local profile="${5:-}" default_table="${6:-}"
  local flat_dom marker_tag detail
  # Extract THE instrumentation marker first and scope every marker-borne
  # assertion to that one tag: an unrelated element carrying data-errors="0"
  # must never mask a nonzero count on the real marker (#449 review).
  flat_dom="$(tr '\n' ' ' < "$dom")"
  marker_tag="$(printf '%s' "$flat_dom" | grep -o '<div[^>]*id="smoke-result"[^>]*>' | head -1 || true)"
  [ -n "$marker_tag" ] \
    || fail "${label}instrumentation marker missing — the page never finished loading"
  case "$marker_tag" in
    *'data-errors="0"'*) : ;;
    *)
      detail="$(printf '%s' "$marker_tag" | grep -o 'data-detail="[^"]*"' | head -1 || true)"
      fail "${label}page-level errors recorded: ${detail:-unknown}"
      ;;
  esac
  # The app-initialized marker is written only at runtime by the module, so
  # its presence in the live DOM — and its absence from the static source —
  # proves app.mjs executed (#448; replaces the pre-#448 initial-error proof).
  # Match the marker only as a static tag attribute: a fixture's inline
  # script may name it in setAttribute() without shipping it statically.
  if grep -Eq '<[A-Za-z!][^>]*data-bqaa-app-initialized' "$static_html"; then
    fail "${label}the app-initialized marker must not appear in static HTML"
  fi
  grep -q 'data-bqaa-app-initialized="true"' "$dom" \
    || fail "${label}app-initialized marker missing — app.mjs did not execute in the browser"
  # With no query parameters the first load is the pristine state (#448):
  # assert the exact LIVE states via the instrumentation snapshot — Chrome's
  # dump-dom does not reflect the value property into a value attribute, so
  # serialized-markup checks cannot prove the field is empty (#449 review).
  case "$marker_tag" in
    *'data-table-value=""'*) : ;;
    *) fail "${label}pristine table-id field must have an empty live value" ;;
  esac
  case "$marker_tag" in
    *'data-create-aria-disabled="true"'*) : ;;
    *) fail "${label}pristine create link must be aria-disabled" ;;
  esac
  case "$marker_tag" in
    *'data-create-has-href="false"'*) : ;;
    *) fail "${label}pristine create link must carry no URL" ;;
  esac
  case "$marker_tag" in
    *'data-copy-disabled="true"'*) : ;;
    *) fail "${label}pristine copy button must be disabled (live property)" ;;
  esac
  if printf '%s' "$flat_dom" | grep -q 'aria-invalid='; then
    fail "${label}pristine first load must not mark any field invalid"
  fi
  if [ -n "$profile" ]; then
    case "$marker_tag" in
      *"data-profile=\"$profile\""*) : ;;
      *) fail "${label}app.mjs must apply the $profile surface" ;;
    esac
    case "$marker_tag" in
      *"data-placeholder=\"my-project.my_dataset.$default_table\""*) : ;;
      *) fail "${label}the table-id placeholder must name $default_table" ;;
    esac
    case "$marker_tag" in
      *'data-profile-mismatches="0"'*) : ;;
      *) fail "${label}only the $profile surface copy may be visible" ;;
    esac
  fi
  # Belt and braces: anything Chrome itself logs as an error still fails.
  if grep -Eiq 'CONSOLE.*\b(error|blocked|failed|uncaught)\b' "$console_log"; then
    fail "${label}browser stderr reported console errors"
  fi
}

dump_dom "http://127.0.0.1:$PORT/index.html" \
  "$OUT_DIR/dom.html" "$OUT_DIR/console.log" "$OUT_DIR/profile" ""
if [ -z "$BQCA_PAGE" ]; then
  assert_live_page "$OUT_DIR/dom.html" "$OUT_DIR/console.log" \
    "$DOCS_DIR/index.html" ""
  echo "browser smoke OK: module initialized, zero page-level errors, pristine first load"
  exit 0
fi
assert_live_page "$OUT_DIR/dom.html" "$OUT_DIR/console.log" \
  "$DOCS_DIR/index.html" "" adk agent_events

# The BQCA deep link is served from a subdirectory, so it alone proves its
# ../ module and stylesheet paths resolve in a real browser, and that its
# declared default surface is the one app.mjs applies. Loaded by directory
# URL, exactly as GitHub Pages serves it.
dump_dom "http://127.0.0.1:$PORT/bqca/" \
  "$OUT_DIR/dom-bqca.html" "$OUT_DIR/console-bqca.log" \
  "$OUT_DIR/profile-bqca" "bqca/index.html: "
assert_live_page "$OUT_DIR/dom-bqca.html" "$OUT_DIR/console-bqca.log" \
  "$DOCS_DIR/bqca/index.html" "bqca/index.html: " \
  bqca bqca_prompt_response_logs

echo "browser smoke OK: module initialized, zero page-level errors, pristine first load (index.html: adk surface; bqca/: bqca surface)"
