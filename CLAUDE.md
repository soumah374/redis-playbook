# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Ansible automation that deploys a 3-node Redis high-availability cluster with Redis
Sentinel for automatic failover. One node starts as master, two as replicas; three
Sentinels (one per node, quorum 2) monitor the master and orchestrate failover. Target
OS is RHEL/CentOS/AlmaLinux 9 (Redis installed from the Remi `redis:remi-7.2` module).

`client-python/` holds a standalone integration test that connects to the live cluster
through Sentinel and verifies replication, discovery, and read/write paths.

## Commands

Deploy the cluster:
```bash
ansible-playbook -i inventory.ini redis_sentinel_playbook.yml
```

Run against a subset / dry-run / verbose:
```bash
ansible-playbook -i inventory.ini redis_sentinel_playbook.yml --limit redis_master
ansible-playbook -i inventory.ini redis_sentinel_playbook.yml --check --diff
ansible-playbook -i inventory.ini redis_sentinel_playbook.yml -vvv
```

Deploy the monitoring stack (run AFTER the main playbook):
```bash
ansible-playbook -i inventory.ini monitoring_playbook.yml
```

Run the cluster integration test (requires network reach to the nodes):
```bash
cd client-python
pip install -r requirements.txt
python3 main.py        # exits 0 only if all checks pass
```

There is no single-test runner — `client-python/main.py` runs all 9 checks
sequentially from `__main__`. To run one in isolation, import the module and call the
individual `test_*()` function.

## Architecture

- **`redis_sentinel_playbook.yml`** — single playbook, runs on the `redis_all` group.
  Flow: enable EPEL + Remi repos → install `redis` → edit `/etc/redis/redis.conf`
  (bind all, disable protected-mode, set `requirepass`/`masterauth`, and `replicaof`
  only on replicas) → template Sentinel config → open firewalld ports → start
  `redis` and `redis-sentinel` services → verify with `redis-cli ping` and
  `info sentinel`. Role (master vs replica) is decided entirely by inventory group
  membership via `when: inventory_hostname in groups[...]`.

- **`inventory.ini`** — defines the topology. `redis_master` (one host) and
  `redis_replicas` (two hosts) roll up into `redis_all:children`. `master_ip` in the
  playbook is derived from `hostvars[groups['redis_master'][0]]` — i.e. whoever is in
  the `redis_master` group is the initial master, and replicas point `replicaof` at it.

- **`redis-sentinel.conf.j2`** — Jinja2 template rendered to `/etc/redis/sentinel.conf`
  on every node. Order matters: `sentinel monitor` MUST precede `sentinel auth-pass`
  (Sentinel rejects auth-pass for an unknown master otherwise).

- **`monitoring_playbook.yml`** — two plays. Play 1 (`redis_all`) installs
  `redis_exporter` (binary + systemd, port 9121) on every Redis node, authenticating to
  local Redis via `redis_password`. Play 2 (`monitoring` group) installs Prometheus
  (binary + systemd, port 9090) and Grafana (RPM repo, port 3000). Prometheus scrape
  targets are generated from `groups['redis_all']` in `templates/prometheus.yml.j2`, so
  adding a Redis node to the inventory is automatically picked up. Grafana's datasource
  and the Redis dashboard (`files/redis-dashboard.json`) are provisioned on disk — no
  manual UI setup. Templated configs live in `templates/`, static provisioning in
  `files/`.

- **`client-python/main.py`** — operational smoke test. Connection details
  (`SENTINELS`, `MASTER_NAME`, `REDIS_PASSWORD`, node IPs) are hardcoded near the top
  and must be kept in sync with `inventory.ini` and the playbook vars.

## Key cross-file invariants

When changing any of these, update **all** the places that hardcode them — they are not
shared from a single source:

- **Master name** — `sentinel_master_name: computemaster` (playbook) must equal
  `MASTER_NAME` in `client-python/main.py`.
- **Password** — `redis_password` (playbook) must equal `REDIS_PASSWORD` (test). Note
  the Sentinel ports (26379) are *not* password-protected in this config; the test
  pings them without auth.
- **Node IPs** — `inventory.ini` `ansible_host` values must match the IPs hardcoded in
  `client-python/main.py` (`SENTINELS` and the per-test node lists).
- **Ports** — `redis_port: 6379`, `sentinel_port: 26379`.

## After a failover the "master" is no longer the master

Group names in `inventory.ini` reflect the *initial* roles only. Once Sentinel performs
a failover, the host in `redis_master` may actually be running as a replica (the test
output and `info replication` will show this). Always discover the current master via
Sentinel (`sentinel.discover_master(MASTER_NAME)`) rather than assuming the inventory
master host is authoritative.

## Security note

`keygen/*.pem` are **OpenSSH private keys** committed to the repo, and `redis_password`
is a plaintext default in the playbook. These are real secrets — do not propagate them
to new files, and treat rotating/removing them as a priority if this repo is shared.
