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
#   browser_smoke.sh              run the check against ../docs
#   browser_smoke.sh --self-test  run the four negative fixtures (a page
#                                 with a console error, an occupied port, a
#                                 failing browser binary, and a browser that
#                                 writes healthy DOM then exits nonzero) and
#                                 require each to fail
#
# Env: CHROME_BIN, SMOKE_PORT, SMOKE_DOCS_DIR override discovery.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SCRIPT_PATH="$SCRIPT_DIR/$(basename "$0")"
DOCS_DIR="$(cd "${SMOKE_DOCS_DIR:-$SCRIPT_DIR/../docs}" && pwd)"
OUT_DIR="$(mktemp -d)"
SERVER_PID=""
CHROME_PID=""

cleanup() {
  if [ -n "$SERVER_PID" ]; then kill "$SERVER_PID" 2>/dev/null || true; fi
  if [ -n "$CHROME_PID" ]; then kill "$CHROME_PID" 2>/dev/null || true; fi
  rm -rf "$OUT_DIR"
}
trap cleanup EXIT

fail() {
  echo "browser smoke: $*" >&2
  exit 1
}

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
# Self-test: every fixture below must make the main check FAIL.
# ---------------------------------------------------------------------------
if [ "${1:-}" = "--self-test" ]; then
  CHROME="$(find_chrome)"
  [ -n "$CHROME" ] || fail "no Chrome/Chromium binary found (set CHROME_BIN)"

  # 1. A page that reports a generic console error (no keyword the old
  #    grep would have matched) but otherwise looks healthy, including the
  #    aria-invalid marker.
  FIXTURE="$OUT_DIR/fixture-console-error"
  mkdir -p "$FIXTURE"
  cat > "$FIXTURE/index.html" <<'HTML'
<!doctype html>
<html><body>
<input aria-invalid="true">
<script>console.error("generic boom");</script>
</body></html>
HTML
  if SMOKE_DOCS_DIR="$FIXTURE" "$SCRIPT_PATH" >/dev/null 2>&1; then
    fail "self-test 1 FAILED: a page with a console error passed"
  fi
  echo "self-test 1 OK: generic console error is detected"

  # 2. An occupied port serving the WRONG tree: the check must notice it
  #    does not own the port instead of validating a stranger's content.
  DECOY="$OUT_DIR/decoy"
  mkdir -p "$DECOY"
  cp "$DOCS_DIR/index.html" "$DECOY/index.html" 2>/dev/null || echo "<body></body>" > "$DECOY/index.html"
  BUSY_PORT=$((30000 + RANDOM % 10000))
  python3 -m http.server "$BUSY_PORT" --directory "$DECOY" >/dev/null 2>&1 &
  DECOY_PID=$!
  disown "$DECOY_PID" 2>/dev/null || true
  sleep 1
  if SMOKE_PORT="$BUSY_PORT" "$SCRIPT_PATH" >/dev/null 2>&1; then
    kill "$DECOY_PID" 2>/dev/null || true
    fail "self-test 2 FAILED: an occupied port was treated as our server"
  fi
  kill "$DECOY_PID" 2>/dev/null || true
  echo "self-test 2 OK: occupied/stale port is detected"

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
<html><body>
<input aria-invalid="true">
<div id="smoke-result" data-errors="0" data-detail=""></div>
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

  echo "browser smoke self-test OK: all negative fixtures fail as required"
  exit 0
fi

# ---------------------------------------------------------------------------
# Main check.
# ---------------------------------------------------------------------------
CHROME_BIN="$(find_chrome)"
[ -n "$CHROME_BIN" ] || fail "no Chrome/Chromium binary found (set CHROME_BIN)"

# Instrumented copy of the site: the injected script runs before the module
# and records everything the page throws.
SITE="$OUT_DIR/site"
mkdir -p "$SITE"
cp -R "$DOCS_DIR/." "$SITE/"
python3 - "$SITE/index.html" <<'EOF'
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
source = path.read_text()
instrument = """<script>
window.__smokeErrors = [];
(function () {
  var record = function (message) {
    window.__smokeErrors.push(String(message));
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
      var el = document.createElement("div");
      el.id = "smoke-result";
      el.setAttribute("data-errors", String(window.__smokeErrors.length));
      el.setAttribute("data-detail", window.__smokeErrors.join(" | ").slice(0, 500));
      document.body.appendChild(el);
    }, 400);
  });
})();
</script>"""
marker = "<body>"
assert marker in source, "index.html has no <body> tag to instrument"
path.write_text(source.replace(marker, marker + instrument, 1))
EOF

# The server must provably be OURS: readiness is a nonce round-trip, not a
# sleep, so an occupied port (our bind fails, a stranger answers) is caught.
NONCE="smoke-nonce-$$-$RANDOM"
echo "$NONCE" > "$SITE/$NONCE.txt"
PORT="${SMOKE_PORT:-$((20000 + RANDOM % 20000))}"
python3 -m http.server "$PORT" --directory "$SITE" >/dev/null 2>&1 &
SERVER_PID=$!
disown "$SERVER_PID" 2>/dev/null || true

READY=""
for _ in $(seq 1 20); do
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
[ -n "$READY" ] || fail "server did not become ready on port $PORT (occupied by another process, or failed to start)"

# Chrome occasionally lingers after dumping the DOM, so it runs in the
# background with a deadline. The child's exit status is ALWAYS reaped and
# honored: a natural nonzero exit fails the check even when it happens
# after a healthy-looking DOM was written. The only exempt exit is the
# deliberate timeout kill below — and only when this script's own kill
# succeeded, so a racing natural exit still surfaces its real status.
"$CHROME_BIN" --headless=new --disable-gpu --no-first-run --no-sandbox \
  --user-data-dir="$OUT_DIR/profile" --enable-logging=stderr \
  --virtual-time-budget=5000 --dump-dom "http://127.0.0.1:$PORT/index.html" \
  > "$OUT_DIR/dom.html" 2> "$OUT_DIR/console.log" &
CHROME_PID=$!

for _ in $(seq 1 45); do
  if ! kill -0 "$CHROME_PID" 2>/dev/null; then
    break
  fi
  if [ -s "$OUT_DIR/dom.html" ]; then
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
  fail "browser exited with status $CHROME_STATUS"
fi

grep -q 'id="smoke-result"' "$OUT_DIR/dom.html" \
  || fail "instrumentation marker missing — the page never finished loading"
if ! grep -q 'data-errors="0"' "$OUT_DIR/dom.html"; then
  DETAIL="$(grep -o 'data-detail="[^"]*"' "$OUT_DIR/dom.html" | head -1)"
  fail "page-level errors recorded: ${DETAIL:-unknown}"
fi
# With no query parameters the project field is empty, so a successfully
# loaded module runs refresh() and marks the field invalid. Static HTML
# never contains this attribute; its presence proves app.mjs executed.
grep -q 'aria-invalid="true"' "$OUT_DIR/dom.html" \
  || fail "validation never ran — app.mjs did not execute in the browser"
# Belt and braces: anything Chrome itself logs as an error still fails.
if grep -Eiq 'CONSOLE.*\b(error|blocked|failed|uncaught)\b' "$OUT_DIR/console.log"; then
  fail "browser stderr reported console errors"
fi

echo "browser smoke OK: module loaded, zero page-level errors, validation ran"
