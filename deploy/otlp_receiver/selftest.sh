#!/usr/bin/env bash
# Container self-test (issue #349 / #356 round 4) — checked in so it runs
# in PR CI, not only at tag time. Usage: selftest.sh <image> <version>
#
# Proves, hermetically:
#   1. packaged version == expected release version
#   2. both Cloud Run entrypoint factories import
#   3. the receiver serves real HTTP: bad token -> 401
#   4. a REAL authenticated protobuf export -> 200 (decode + publish
#      through a digest-pinned Pub/Sub emulator on a private bridge
#      network — the emulator cannot spoof the receiver port, and no
#      host code is installed between test and push: the payload is
#      generated INSIDE the image under test)
#   5. prints the local image ID so the caller can assert it is
#      unchanged immediately before push.
set -euo pipefail

IMAGE="${1:?usage: selftest.sh <image> <version>}"
VERSION="${2:?usage: selftest.sh <image> <version>}"
# Digest-pinned: a mutable emulator tag must not become the release oracle.
EMULATOR_IMAGE="gcr.io/google.com/cloudsdktool/google-cloud-cli@sha256:38132a268745db5a1dc2ebfecfe6f935d75de281dddc6922f0fe3780c5552b81"
NET="bqaa-selftest-$$"

cleanup() {
  docker rm -f bqaa-selftest-recv bqaa-selftest-emu > /dev/null 2>&1 || true
  docker network rm "$NET" > /dev/null 2>&1 || true
}
trap cleanup EXIT

echo "==> metadata + factory assertions (inside the image)"
docker run --rm "$IMAGE" python -c "
import importlib.metadata as m
v = m.version('bigquery-agent-analytics-tracing')
assert v == '${VERSION}', f'image has {v}, want ${VERSION}'
from bigquery_agent_analytics_tracing.otlp import app, consumer
assert callable(app.make_app)
assert callable(consumer.make_push_app_from_env)
print('version + factories ok:', v)
"

echo "==> private bridge network + pinned emulator"
docker network create "$NET" > /dev/null
docker run -d --name bqaa-selftest-emu --network "$NET" \
  "$EMULATOR_IMAGE" \
  gcloud beta emulators pubsub start --host-port=0.0.0.0:8085 > /dev/null

echo "==> receiver on the same network; ONLY its port reaches the host"
docker run -d --name bqaa-selftest-recv --network "$NET" \
  -p 127.0.0.1:18080:8080 \
  -e BQAA_OTLP_TOKEN=selftest-token \
  -e BQAA_OTLP_MAIN_TOPIC=projects/selftest/topics/selftest-topic \
  -e PUBSUB_EMULATOR_HOST=bqaa-selftest-emu:8085 \
  "$IMAGE" > /dev/null

echo "==> bounded readiness + topic creation (through the receiver's network)"
for i in $(seq 1 30); do
  if docker exec bqaa-selftest-recv python -c "
import urllib.request
urllib.request.urlopen('http://bqaa-selftest-emu:8085', timeout=2)
" 2>/dev/null; then
    break
  fi
  [ "$i" = 30 ] && { echo "emulator never became ready"; docker logs bqaa-selftest-emu | tail -20; exit 1; }
  sleep 2
done
docker exec bqaa-selftest-recv python -c "
import urllib.request
req = urllib.request.Request(
    'http://bqaa-selftest-emu:8085/v1/projects/selftest/topics/selftest-topic',
    method='PUT')
print(urllib.request.urlopen(req, timeout=5).status)
"

echo "==> bad token must be rejected (401)"
CODE=""
for i in $(seq 1 30); do
  CODE=$(curl -s --connect-timeout 2 --max-time 5 -o /dev/null -w '%{http_code}' \
    -X POST http://127.0.0.1:18080/v1/logs \
    -H 'Content-Type: application/x-protobuf' \
    -H 'Authorization: Bearer wrong-token' --data-binary '' || true)
  [ "$CODE" = "401" ] && break
  [ "$i" = 30 ] && { echo "receiver never answered (last: ${CODE:-none})"; docker logs bqaa-selftest-recv | tail -20; exit 1; }
  sleep 2
done
echo "bad token: 401"

echo "==> real authenticated protobuf export (payload generated IN the image)"
docker run --rm "$IMAGE" python -c "
import sys
from opentelemetry.proto.collector.logs.v1 import logs_service_pb2
req = logs_service_pb2.ExportLogsServiceRequest()
rec = req.resource_logs.add().scope_logs.add().log_records.add()
rec.body.string_value = 'release-selftest'
sys.stdout.buffer.write(req.SerializeToString())
" > "${TMPDIR:-/tmp}/bqaa-selftest-payload.bin"
RESPONSE_FILE="${TMPDIR:-/tmp}/bqaa-selftest-response.json"
GOOD=$(curl -s --connect-timeout 2 --max-time 10 -o "$RESPONSE_FILE" -w '%{http_code}' \
  -X POST http://127.0.0.1:18080/v1/logs \
  -H 'Content-Type: application/x-protobuf' \
  -H 'Authorization: Bearer selftest-token' \
  --data-binary @"${TMPDIR:-/tmp}/bqaa-selftest-payload.bin")
test "$GOOD" = "200" || { echo "authenticated export got ${GOOD}, want 200"; docker logs bqaa-selftest-recv | tail -20; exit 1; }
# 200 alone is NOT a decoded export: the receiver can answer 200 with
# published=0, and per-record decode failures return 200 with
# dead_lettered>0. Require actual publication and zero dead letters.
python3 - "$RESPONSE_FILE" << 'CHECK_EOF'
import json, sys
body = json.load(open(sys.argv[1]))
assert body.get("published", 0) > 0, f"nothing published: {body}"
assert body.get("dead_lettered", 0) == 0, f"records dead-lettered: {body}"
print(f"decoded + published: {body['published']} record(s), 0 dead-lettered")
CHECK_EOF
echo "authenticated protobuf export: 200 with proven publication"

IMAGE_ID=$(docker inspect "$IMAGE" --format '{{.Id}}')
echo "SELFTEST_IMAGE_ID=${IMAGE_ID}"
echo "self-test passed"
