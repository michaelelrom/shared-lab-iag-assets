#!/usr/bin/env bash
# Sync IAG5 server state to match import.yml.
#
# Contract (called from GitHub Actions via SSM as `sudo -u itential`):
#   $1 = repo full name      (e.g. "michaelelrom/shared-lab-iag-assets")
#   $2 = commit sha
#   $3 = raw URL of import.yml at that commit
#
# Exit 0 on success, non-zero on any failure. Stdout/stderr are captured by
# SSM and shown in the GitHub Actions job summary.

set -euo pipefail

REPO="${1:?repo required}"
SHA="${2:?sha required}"
IMPORT_URL="${3:?import.yml URL required}"

IAGCTL=/opt/gateway/iagctl
WORK_DIR=/var/lib/itential/deploy
export GATEWAY_APPLICATION_MODE=client
export TERM=dumb
export NO_COLOR=1

mkdir -p "${WORK_DIR}"
cd "${WORK_DIR}"

echo "==> Deploy ${REPO}@${SHA:0:7}"
echo "==> Fetching ${IMPORT_URL}"
curl -fsSL "${IMPORT_URL}" -o import.yml
echo "    $(wc -l < import.yml) lines"

strip_ansi() { sed 's/\x1b\[[0-9;]*[a-zA-Z]//g'; }
iag() { "${IAGCTL}" "$@" 2>&1 | strip_ansi; return "${PIPESTATUS[0]}"; }

# ---------- validate + import ----------
echo
echo "==> Validating"
iag db import import.yml --validate

echo
echo "==> Planned changes (dry-run)"
iag db import import.yml --check --force

echo
echo "==> Applying"
iag db import import.yml --force

# ---------- prune orphans ----------
# import.yml is the source of truth for services in repositories it lists.
# Services in IAG5 whose repository.name is in our managed-repos set but whose
# service name is NOT in our wanted-services set are deleted. Services in
# unmanaged repos are left alone, so the same IAG5 host can serve multiple
# independent asset repos.
echo
echo "==> Pruning orphan services"

# python keeps the YAML parser; jq is overkill here.
python3 - <<'PY' > /tmp/managed.json
import json, yaml
with open('import.yml') as f:
    data = yaml.safe_load(f) or {}
print(json.dumps({
    'repos':    [r['name'] for r in (data.get('repositories') or [])],
    'services': [s['name'] for s in (data.get('services')     or [])],
}))
PY

MANAGED_REPOS=$(jq -r '.repos[]' /tmp/managed.json)
WANTED_SERVICES=$(jq -r '.services[]' /tmp/managed.json)

"${IAGCTL}" get services --raw 2>/dev/null \
  | jq -r '.services[]?.name' \
  | while read -r svc_name; do
      [ -z "${svc_name}" ] && continue
      svc_repo=$("${IAGCTL}" describe service "${svc_name}" --raw 2>/dev/null \
                  | jq -r '.metadata.repository.name // ""')
      managed=0; wanted=0
      for r in ${MANAGED_REPOS}; do [ "${r}" = "${svc_repo}" ] && managed=1; done
      for w in ${WANTED_SERVICES}; do [ "${w}" = "${svc_name}" ] && wanted=1; done
      if [ "${managed}" = "1" ] && [ "${wanted}" = "0" ]; then
          echo "  - ${svc_name} (in repo ${svc_repo}): orphan, deleting"
          iag delete service "${svc_name}" >/dev/null || true
      fi
  done

echo
echo "==> Done"
echo
echo "--- repositories now in IAG5 ---"
iag get repositories
echo
echo "--- services now in IAG5 ---"
iag get services
