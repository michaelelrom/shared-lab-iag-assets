# IAG5 auto-deploy

On every push to `main` (i.e. PR merge), GitHub Actions calls the Itential Platform's `GatewayManager` API directly — Platform pulls `import.yml` straight from this repo over its existing connection to the gateway. No AWS, no SSM, no inbound port, nothing running on the gateway host itself.

## Architecture

```
git push main
   │
   ▼
GitHub Actions runner (ubuntu-latest)
   │  POST /oauth/token (client_credentials)
   ▼
Itential Platform
   │  POST /gateway_manager/v1/gateways/{clusterId}/configuration/import
   │    { options: { source: "git", git: { url, file: "import.yml", reference: <sha> }, ... } }
   │  Platform clones the repo itself and pushes the parsed config to the
   │  gateway over its existing mTLS/WebSocket connection.
   ▼
Gateway (cluster_id matches GATEWAY_CLUSTER_ID) — applies the config
```

Three calls per deploy, same semantics as `iagctl db import`: `validate: true` (parse-only), `check: true` (dry-run diff, printed to the job summary), then `force: true` (apply).

**Known trade-off vs. the old pipeline:** this API does not delete orphaned resources — same as bare `iagctl db import` (adds/replaces only). The old `deploy.sh` had a custom loop that diffed live services against `import.yml` and deleted anything no longer listed. That's gone. Removing a service from `import.yml` now requires a manual `iagctl-client delete service <name>` on the box (see "Manual recovery" below) until/unless a bulk-delete endpoint is confirmed to exist.

**Important — this pipeline is Platform-instance-specific.** The physical shared-lab gateway gets re-paired between different Itential Platform instances over time (see the pairing-toggle setup in `gateway.conf`). The `PLATFORM_URL`/`PLATFORM_CLIENT_ID`/`PLATFORM_CLIENT_SECRET`/`GATEWAY_CLUSTER_ID` values below must always point at whichever Platform instance the gateway is *currently* paired with — update them as part of any re-pairing, or this pipeline will silently deploy to the wrong (or an unreachable) Platform.

## GitHub repo secrets/variables

```bash
gh secret   set PLATFORM_CLIENT_ID     --body "<oauth client id>"
gh secret   set PLATFORM_CLIENT_SECRET --body "<oauth client secret>"
gh variable set PLATFORM_URL           --body "https://<your-instance>.itential.io"
gh variable set GATEWAY_CLUSTER_ID     --body "<cluster id, e.g. cluster-itential>"
```

| Kind     | Name                      | Notes                                              |
|----------|---------------------------|-----------------------------------------------------|
| Secret   | `PLATFORM_CLIENT_ID`      | OAuth client_credentials client ID                  |
| Secret   | `PLATFORM_CLIENT_SECRET`  | OAuth client_credentials client secret              |
| Variable | `PLATFORM_URL`            | Base URL of the currently-paired Platform instance  |
| Variable | `GATEWAY_CLUSTER_ID`      | `cluster_id` from `GET /gateway_manager/v1/gateways/` on that instance |

You also need an environment named `iag5-shared-lab` in repo Settings → Environments (or remove the `environment:` line in the workflow). The environment is useful for adding a required reviewer or wait-timer.

The old AWS-based secrets/variables (`AWS_DEPLOY_ROLE_ARN`, `AWS_REGION`, `IAG5_INSTANCE_ID`, `DEPLOY_SCRIPT`) are no longer used by the automated pipeline. They're left alone for now since the AWS OIDC role and SSM access are still useful for the manual recovery path below.

## Branch protection (recommended)

```bash
gh api -X PUT repos/michaelelrom/shared-lab-iag-assets/branches/main/protection \
  -F required_pull_request_reviews.required_approving_review_count=1 \
  -F enforce_admins=false \
  -F required_status_checks=null \
  -F restrictions=null
```

## Adding/removing assets

Edit `import.yml`. The pipeline creates and replaces but **does not delete orphans** — if you remove a service entry, the service stays on the IAG5 server until you `iagctl-client delete service <name>` on the box.

## Manual recovery on the box

```bash
ssh -i ~/.ssh/aws-shared-lab-us-east-1.pem rocky@52.204.154.11
sudo -u itential iagctl-client get repositories
sudo -u itential iagctl-client get services

# Re-login if api.key expires (default 24h, configurable via GATEWAY_SERVER_API_KEY_EXPIRATION):
sudo -u itential iagctl-client login admin

# Run the deploy script manually (e.g. against a branch):
sudo -u itential /opt/gateway/deploy.sh michaelelrom/shared-lab-iag-assets HEAD \
  https://raw.githubusercontent.com/michaelelrom/shared-lab-iag-assets/main/import.yml
```

## Re-running the AWS bootstrap

`scripts/aws-oidc-bootstrap.sh` is idempotent. Re-run if you change region, role name, or want to scope to a different branch.

```bash
AWS_PROFILE=poc-team-sbx ./scripts/aws-oidc-bootstrap.sh
```
