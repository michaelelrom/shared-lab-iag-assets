# terraform-plans

One subdirectory per OpenTofu/Terraform plan service. IAG5 runs OpenTofu (Terraform-compatible) — `.tf` files work as-is. Reference from the root `../import.yml`:

```yaml
services:
  - name: my-plan
    type: opentofu-plan
    repository: shared-lab-iag-assets
    working-directory: terraform-plans/my-plan
```

`vars:` / `var-files:` / `backend-config:` are passed via service flags; consult `iagctl create service opentofu-plan --help` on the IAG5 box for the full set.
