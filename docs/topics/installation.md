# Installation

Generally, extensions need to be installed into the same Python environment Salt uses.

:::{tab} State
```yaml
Install Salt Incus extension:
  pip.installed:
    - name: incus
```
:::

:::{tab} Onedir installation
```bash
salt-pip install incus
```
:::

:::{tab} Regular installation
```bash
pip install incus
```
:::

:::{hint}
Saltexts are not distributed automatically via the fileserver like custom modules, they need to be installed
on each node you want them to be available on.
:::
