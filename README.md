# shared-lab-iag-assets

Assets for the Forcepoint shared-lab IAG5 server. Edit [`import.yml`](import.yml), merge to `main`, the pipeline reconciles IAG5 to match.

## How it works

```
edit import.yml → PR → merge to main
        │
        ▼
GitHub Actions (.github/workflows/deploy-iag5.yml)
        │  OIDC → assume gha-deploy-iag5 in account 623933009299
        ▼
AWS SSM send-command → IAG5 EC2 (i-0dcf9db60fabecc0d)
        │
        ▼
/opt/gateway/deploy.sh fetches import.yml at the merged commit
and replays repositories/services into iagctl (client mode).
```

No inbound port on the IAG5 box. iagctl auth and certs stay on the host.

## Layout

```
ansible-playbooks/    Ansible playbook services (one subdir per service)
python-scripts/       Python script services (one subdir per service)
terraform-plans/      OpenTofu/Terraform plan services (one subdir per service)
import.yml            Declares the repository + every service the pipeline manages
```

Each top-level dir has a `README.md` with a copy-pasteable `import.yml` snippet for that type.

## Adding an asset

1. Drop files in the matching dir, e.g. `ansible-playbooks/my-feature/site.yml`.
2. Add the service to `import.yml`:
   ```yaml
   services:
     - name: my-feature
       type: ansible-playbook
       repository: shared-lab-iag-assets
       working-directory: ansible-playbooks/my-feature
       playbooks: [site.yml]
   ```
3. Open a PR. Merge to `main` → IAG5 updates within ~30s.

Supported `type` values: `ansible-playbook`, `python-script`, `opentofu-plan`, `executable`. See `iagctl create service <type> --help` on the box for the full flag set.

## Files

- [`import.yml`](import.yml) — declarative state, the only file most changes touch
- [`.github/workflows/deploy-iag5.yml`](.github/workflows/deploy-iag5.yml) — CI pipeline
- [`scripts/deploy.sh`](scripts/deploy.sh) — what runs on the IAG5 box (also installed at `/opt/gateway/deploy.sh`)
- [`scripts/aws-oidc-bootstrap.sh`](scripts/aws-oidc-bootstrap.sh) — one-time AWS setup (already run)
- [`DEPLOY.md`](DEPLOY.md) — full runbook

## Reset notes

If iagctl client falls over: `sudo -u itential iagctl-client login admin` regenerates `/etc/gateway/api.key`. To inspect state from the box: `iagctl-client get repositories` / `iagctl-client get services`.

The pipeline only creates/replaces — it doesn't delete orphans. To remove an old service, run `iagctl-client delete service <name>` on the box.
