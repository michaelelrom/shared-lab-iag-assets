# python-scripts

One subdirectory per Python script service. Reference it from the root `../import.yml`:

```yaml
services:
  - name: my-script
    type: python-script
    repository: shared-lab-iag-assets
    working-directory: python-scripts/my-script
    filename: main.py
```

Optional alongside `main.py`: `pyproject.toml` or `requirements.txt` (referenced via `--req-file` on `iagctl create service python-script`).
