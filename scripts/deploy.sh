#!/usr/bin/env bash
# Idempotently sync IAG5 server state to match import.yml.
#
# Contract (called from GitHub Actions via SSM as `sudo -u itential`):
#   $1 = repo full name      (e.g. "michaelelrom/shared-lab-iag-assets")
#   $2 = commit sha          (40-char)
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

# Convert YAML → compact JSON via python so jq can drive it.
python3 - <<'PY' > import.json
import json, sys, yaml
with open('import.yml') as f:
    data = yaml.safe_load(f) or {}
json.dump(data, sys.stdout)
PY

# Strip ANSI from iagctl output so logs are readable.
strip_ansi() { sed 's/\x1b\[[0-9;]*[a-zA-Z]//g'; }

# Run iagctl, strip color. Returns iagctl's exit code via PIPESTATUS.
iag() {
    "${IAGCTL}" "$@" 2>&1 | strip_ansi
    return "${PIPESTATUS[0]}"
}

# Idempotent create: try create; on "already exists", delete then create.
# Args: <resource-kind-singular-for-delete> <name> <create-args...>
upsert() {
    local kind="$1" name="$2"; shift 2
    local out
    if out=$("${IAGCTL}" "$@" 2>&1); then
        echo "${out}" | strip_ansi
        return 0
    fi
    if grep -qiE 'already exists|object already exists' <<<"${out}"; then
        echo "  ${name}: exists, replacing"
        iag delete "${kind}" "${name}" >/dev/null || true
        iag "$@"
    else
        echo "${out}" | strip_ansi >&2
        return 1
    fi
}

# ---------- repositories ----------
echo
echo "==> Syncing repositories"
jq -c '.repositories[]? // empty' import.json | while read -r repo; do
    name=$(jq -r '.name'      <<<"$repo")
    url=$(jq  -r '.url'       <<<"$repo")
    ref=$(jq  -r '.reference // "main"' <<<"$repo")
    desc=$(jq -r '.description // ""'   <<<"$repo")

    args=(create repository "${name}" --url "${url}" --reference "${ref}")
    [ -n "${desc}" ] && args+=(--description "${desc}")
    for tag in $(jq -r '.tags[]? // empty' <<<"$repo"); do
        args+=(--tag "${tag}")
    done

    echo "  - ${name}"
    upsert repository "${name}" "${args[@]}"
done

# ---------- services ----------
echo
echo "==> Syncing services"
jq -c '.services[]? // empty' import.json | while read -r svc; do
    name=$(jq -r '.name' <<<"$svc")
    type=$(jq -r '.type' <<<"$svc")
    repo=$(jq -r '.repository' <<<"$svc")
    desc=$(jq -r '.description // ""' <<<"$svc")
    wdir=$(jq -r '."working-directory" // ""' <<<"$svc")

    case "${type}" in
        ansible-playbook|python-script|opentofu-plan|executable) subcmd="${type}" ;;
        *) echo "    ! unknown service type '${type}' for '${name}' — skipping" >&2; continue ;;
    esac

    args=(create service "${subcmd}" "${name}" --repository "${repo}")
    [ -n "${desc}" ] && args+=(--description "${desc}")
    [ -n "${wdir}" ] && args+=(--working-dir "${wdir}")

    case "${type}" in
        ansible-playbook)
            for pb in $(jq -r '.playbooks[]? // empty' <<<"$svc"); do
                args+=(--playbook "${pb}")
            done
            ;;
        python-script)
            fn=$(jq -r '.filename // ""' <<<"$svc")
            [ -n "${fn}" ] && args+=(--filename "${fn}")
            ;;
    esac

    for tag in $(jq -r '.tags[]? // empty' <<<"$svc"); do
        args+=(--tag "${tag}")
    done

    echo "  - ${name} (${type})"
    upsert service "${name}" "${args[@]}"
done

# ---------- prune orphans ----------
# import.yml is source-of-truth for services in any repository it lists.
# Services in IAG5 whose repository.name is in our managed-repos set but whose
# service name is NOT in our wanted-services set are deleted. Services in
# unmanaged repos are left alone.
echo
echo "==> Pruning orphan services"

MANAGED_REPOS=$(jq -r '[.repositories[]?.name] | join(" ")' import.json)
WANTED_SERVICES=$(jq -r '[.services[]?.name] | join(" ")' import.json)

# Read each service in IAG5 + its repo. `describe service` returns the repo.name.
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
