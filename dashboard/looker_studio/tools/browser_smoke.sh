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
#   browser_smoke.sh --self-test  run the negative fixtures and require each
#                                 to fail: an immediate console error, an
#                                 occupied port, a failing browser binary, a
#                                 browser that writes healthy DOM then exits
#                                 nonzero, a console error delayed past the
#                                 marker's creation, a page that never
#                                 writes the app-initialized marker, and a
#                                 page whose live field value is mutated
#                                 without a serialized value attribute, a
#                                 delayed live-value mutation after marker
#                                 creation, and a decoy zero-error element
#                                 beside a marker recording a real error
#
# Every fixture except the missing-initialization one satisfies the full
# healthy baseline (#448): the runtime data-bqaa-app-initialized marker (set
# by script, never static), an aria-disabled action, and no aria-invalid
# anywhere — so each fixture's injected fault is the sole reason it fails.
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

# Extract THE instrumentation marker first and scope every marker-borne
# assertion to that one tag: an unrelated element carrying data-errors="0"
# must never mask a nonzero count on the real marker (#449 review).
FLAT_DOM="$(tr '\n' ' ' < "$OUT_DIR/dom.html")"
MARKER_TAG="$(printf '%s' "$FLAT_DOM" | grep -o '<div[^>]*id="smoke-result"[^>]*>' | head -1)"
[ -n "$MARKER_TAG" ] \
  || fail "instrumentation marker missing — the page never finished loading"
case "$MARKER_TAG" in
  *'data-errors="0"'*) : ;;
  *)
    DETAIL="$(printf '%s' "$MARKER_TAG" | grep -o 'data-detail="[^"]*"' | head -1)"
    fail "page-level errors recorded: ${DETAIL:-unknown}"
    ;;
esac
# The app-initialized marker is written only at runtime by the module, so
# its presence in the live DOM — and its absence from the static source —
# proves app.mjs executed (#448; replaces the pre-#448 initial-error proof).
# Match the marker only as a static tag attribute: a fixture's inline
# script may name it in setAttribute() without shipping it statically.
if grep -Eq '<[A-Za-z!][^>]*data-bqaa-app-initialized' "$DOCS_DIR/index.html"; then
  fail "the app-initialized marker must not appear in static HTML"
fi
grep -q 'data-bqaa-app-initialized="true"' "$OUT_DIR/dom.html" \
  || fail "app-initialized marker missing — app.mjs did not execute in the browser"
# With no query parameters the first load is the pristine state (#448):
# assert the exact LIVE states via the instrumentation snapshot — Chrome's
# dump-dom does not reflect the value property into a value attribute, so
# serialized-markup checks cannot prove the field is empty (#449 review).
case "$MARKER_TAG" in
  *'data-table-value=""'*) : ;;
  *) fail "pristine table-id field must have an empty live value" ;;
esac
case "$MARKER_TAG" in
  *'data-create-aria-disabled="true"'*) : ;;
  *) fail "pristine create link must be aria-disabled" ;;
esac
case "$MARKER_TAG" in
  *'data-create-has-href="false"'*) : ;;
  *) fail "pristine create link must carry no URL" ;;
esac
case "$MARKER_TAG" in
  *'data-copy-disabled="true"'*) : ;;
  *) fail "pristine copy button must be disabled (live property)" ;;
esac
if printf '%s' "$FLAT_DOM" | grep -q 'aria-invalid='; then
  fail "pristine first load must not mark any field invalid"
fi
# Belt and braces: anything Chrome itself logs as an error still fails.
if grep -Eiq 'CONSOLE.*\b(error|blocked|failed|uncaught)\b' "$OUT_DIR/console.log"; then
  fail "browser stderr reported console errors"
fi

echo "browser smoke OK: module initialized, zero page-level errors, pristine first load"
