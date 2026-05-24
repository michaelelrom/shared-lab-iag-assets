# IAG5 auto-deploy

On every push to `main` (i.e. PR merge), GitHub Actions assumes an AWS role via OIDC and runs `/opt/gateway/deploy.sh` on the shared-lab IAG5 box via **SSM** — no inbound port required. iagctl client config, certs, and api.key all live on the host.

## Architecture

```
git push main
   │
   ▼
GitHub Actions runner (ubuntu-latest)
   │  OIDC → sts:AssumeRoleWithWebIdentity
   ▼
gha-deploy-iag5 role  (ssm:SendCommand on i-0dcf9db60fabecc0d only)
   │  aws ssm send-command AWS-RunShellScript
   ▼
amazon-ssm-agent on IAG5 EC2 (Rocky 9)  — runs as root
   │
   ▼
sudo -u itential /opt/gateway/deploy.sh <repo> <sha> <import.yml URL>
   │
   ▼
iagctl (mode=client, gRPC to 127.0.0.1:50051)
   – parses import.yml with python+jq
   – idempotent upsert of each repository / service
```

## Status (shared lab, account 623933009299, us-east-1)

| Step                                          | Status |
|-----------------------------------------------|--------|
| GitHub OIDC provider + `gha-deploy-iag5` role | ✅ |
| `iag5-ssm-profile` attached to `i-0dcf9db60fabecc0d` | ✅ |
| `amazon-ssm-agent` Online (3.3.4364.0)        | ✅ |
| `iagctl` + `iagctl-client` wrappers in PATH   | ✅ |
| `/etc/gateway/api.key` (admin login complete) | ✅ |
| `/opt/gateway/deploy.sh` installed + smoke-tested | ✅ |
| `iag5-shared-lab` environment in GitHub       | needed |
| Repo secrets + variables in GitHub            | needed |
| Merge PR #1                                   | needed |

## GitHub repo secrets/variables

```bash
gh secret   set AWS_DEPLOY_ROLE_ARN --body "arn:aws:iam::623933009299:role/gha-deploy-iag5"
gh variable set AWS_REGION          --body "us-east-1"
gh variable set IAG5_INSTANCE_ID    --body "i-0dcf9db60fabecc0d"
gh variable set DEPLOY_SCRIPT       --body "/opt/gateway/deploy.sh"
```

| Kind     | Name                  | Value                                            |
|----------|-----------------------|--------------------------------------------------|
| Secret   | `AWS_DEPLOY_ROLE_ARN` | `arn:aws:iam::623933009299:role/gha-deploy-iag5` |
| Variable | `AWS_REGION`          | `us-east-1`                                      |
| Variable | `IAG5_INSTANCE_ID`    | `i-0dcf9db60fabecc0d`                            |
| Variable | `DEPLOY_SCRIPT`       | `/opt/gateway/deploy.sh`                         |

You also need an environment named `iag5-shared-lab` in repo Settings → Environments (or remove the `environment:` line in the workflow). The environment is useful for adding a required reviewer or wait-timer.

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
