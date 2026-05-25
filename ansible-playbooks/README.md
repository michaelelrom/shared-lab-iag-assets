# ansible-playbooks

One subdirectory per Ansible-playbook service. Reference it from the root `../import.yml`:

```yaml
services:
  - name: my-playbook
    type: ansible-playbook
    repository: shared-lab-iag-assets
    working-directory: ansible-playbooks/my-playbook
    playbooks:
      - site.yml
```

Multiple `playbooks:` entries run in sequence. Inventory files / extra-vars files live next to the playbook and are referenced via the service's flags.
