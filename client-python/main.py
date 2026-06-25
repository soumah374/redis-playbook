#!/usr/bin/env python3
# =============================================================================
# Test Redis HA avec Sentinel — AlmaLinux 9
# Usage : python3 test_redis_sentinel.py
# Prérequis : pip install redis
# =============================================================================

import sys
import time
import redis
from redis.sentinel import Sentinel

# ─── Configuration ────────────────────────────────────────────────────────────
SENTINELS = [
    ("192.168.1.158", 26379),  # master
    ("192.168.1.140", 26379),  # replica1
    ("192.168.1.159", 26379),  # replica2
]
MASTER_NAME   = "computemaster"
REDIS_PASSWORD = "Securep@55Here"
REDIS_PORT     = 6379

# ─── Couleurs terminal ────────────────────────────────────────────────────────
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
RESET  = "\033[0m"

passed = 0
failed = 0

def ok(msg):
    global passed
    passed += 1
    print(f"  {GREEN}[✔] PASS{RESET} — {msg}")

def ko(msg, err=""):
    global failed
    failed += 1
    detail = f" ({err})" if err else ""
    print(f"  {RED}[✘] FAIL{RESET} — {msg}{detail}")

def section(title):
    print(f"\n{CYAN}{'═'*55}{RESET}")
    print(f"{CYAN}  {title}{RESET}")
    print(f"{CYAN}{'═'*55}{RESET}")

# ─── Test 1 : Connexion directe à chaque nœud Redis ──────────────────────────
def test_direct_connections():
    section("Test 1 : Connexion directe à chaque nœud Redis")
    nodes = [
        ("192.168.1.158", "master"),
        ("192.168.1.140", "replica1"),
        ("192.168.1.159", "replica2"),
    ]
    for ip, role in nodes:
        try:
            r = redis.Redis(host=ip, port=REDIS_PORT, password=REDIS_PASSWORD, socket_timeout=3)
            pong = r.ping()
            ok(f"PING {role} ({ip}) → PONG")
        except Exception as e:
            ko(f"PING {role} ({ip})", str(e))

# ─── Test 2 : Connexion aux Sentinels ─────────────────────────────────────────
def test_sentinel_connections():
    section("Test 2 : Connexion aux Sentinels (port 26379)")
    for ip, port in SENTINELS:
        try:
            r = redis.Redis(host=ip, port=port, socket_timeout=3)
            pong = r.ping()
            ok(f"Sentinel {ip}:{port} → PONG")
        except Exception as e:
            ko(f"Sentinel {ip}:{port}", str(e))

# ─── Test 3 : Détection du master via Sentinel ────────────────────────────────
def test_sentinel_master_discovery():
    section("Test 3 : Détection du master via Sentinel")
    try:
        sentinel = Sentinel(SENTINELS, socket_timeout=3, password=REDIS_PASSWORD)
        master = sentinel.discover_master(MASTER_NAME)
        ok(f"Master détecté : {master[0]}:{master[1]}")
        return sentinel
    except Exception as e:
        ko("Détection du master via Sentinel", str(e))
        return None

# ─── Test 4 : Détection des réplicas via Sentinel ────────────────────────────
def test_sentinel_replica_discovery():
    section("Test 4 : Détection des réplicas via Sentinel")
    try:
        sentinel = Sentinel(SENTINELS, socket_timeout=3, password=REDIS_PASSWORD)
        replicas = sentinel.discover_slaves(MASTER_NAME)
        if replicas:
            for r in replicas:
                ok(f"Réplica détecté : {r[0]}:{r[1]}")
        else:
            ko("Aucun réplica détecté par Sentinel")
    except Exception as e:
        ko("Détection des réplicas", str(e))

# ─── Test 5 : Écriture et lecture sur le master ───────────────────────────────
def test_write_read():
    section("Test 5 : Écriture / Lecture sur le master")
    try:
        sentinel = Sentinel(SENTINELS, socket_timeout=3, password=REDIS_PASSWORD)
        master = sentinel.master_for(MASTER_NAME, socket_timeout=3)

        # Écriture
        master.set("redis_ha_test", "hello_sentinel", ex=60)
        ok("Écriture clé 'redis_ha_test' sur le master")

        # Lecture depuis le master
        val = master.get("redis_ha_test").decode()
        assert val == "hello_sentinel"
        ok(f"Lecture depuis le master → '{val}'")

    except Exception as e:
        ko("Écriture/Lecture", str(e))

# ─── Test 6 : Lecture sur les réplicas ───────────────────────────────────────
def test_read_from_replicas():
    section("Test 6 : Lecture depuis les réplicas")
    try:
        sentinel = Sentinel(SENTINELS, socket_timeout=3, password=REDIS_PASSWORD)
        slave = sentinel.slave_for(MASTER_NAME, socket_timeout=3)
        time.sleep(0.5)  # laisser le temps à la réplication
        val = slave.get("redis_ha_test")
        if val:
            ok(f"Lecture depuis réplica → '{val.decode()}'")
        else:
            ko("Clé non trouvée sur le réplica (réplication en retard ?)")
    except Exception as e:
        ko("Lecture depuis réplica", str(e))

# ─── Test 7 : Vérification du rôle de chaque nœud ───────────────────────────
def test_roles():
    section("Test 7 : Vérification du rôle de chaque nœud")
    nodes = [
        ("192.168.1.158", "master"),
        ("192.168.1.140", "replica1"),
        ("192.168.1.159", "replica2"),
    ]
    for ip, name in nodes:
        try:
            r = redis.Redis(host=ip, port=REDIS_PORT, password=REDIS_PASSWORD, socket_timeout=3)
            info = r.info("replication")
            role = info.get("role", "unknown")
            ok(f"{name} ({ip}) → rôle : {role}")
        except Exception as e:
            ko(f"Rôle de {name} ({ip})", str(e))

# ─── Test 8 : Vérification de la réplication ─────────────────────────────────
def test_replication():
    section("Test 8 : État de la réplication")
    try:
        r = redis.Redis(host="192.168.1.158", port=REDIS_PORT, password=REDIS_PASSWORD, socket_timeout=3)
        info = r.info("replication")
        nb_replicas = info.get("connected_slaves", 0)
        if nb_replicas >= 1:
            ok(f"{nb_replicas} réplica(s) connecté(s) au master")
            for i in range(nb_replicas):
                slave_info = info.get(f"slave{i}", {})
                ok(f"  Réplica {i} : {slave_info}")
        else:
            ko("Aucun réplica connecté au master")
    except Exception as e:
        ko("Vérification réplication", str(e))

# ─── Test 9 : Nettoyage ───────────────────────────────────────────────────────
def test_cleanup():
    section("Test 9 : Nettoyage des clés de test")
    try:
        sentinel = Sentinel(SENTINELS, socket_timeout=3, password=REDIS_PASSWORD)
        master = sentinel.master_for(MASTER_NAME, socket_timeout=3)
        master.delete("redis_ha_test")
        ok("Clé 'redis_ha_test' supprimée")
    except Exception as e:
        ko("Nettoyage", str(e))

# ─── Résumé ───────────────────────────────────────────────────────────────────
def print_summary():
    total = passed + failed
    print(f"\n{CYAN}{'═'*55}{RESET}")
    print(f"{CYAN}  Résumé des tests{RESET}")
    print(f"{CYAN}{'═'*55}{RESET}")
    print(f"  Total   : {total}")
    print(f"  {GREEN}Réussis : {passed}{RESET}")
    print(f"  {RED}Échoués : {failed}{RESET}")
    if failed == 0:
        print(f"\n  {GREEN}✔ Cluster Redis HA opérationnel !{RESET}\n")
    else:
        print(f"\n  {YELLOW}⚠ {failed} test(s) échoué(s) — vérifiez la configuration.{RESET}\n")
    return 0 if failed == 0 else 1

# ─── MAIN ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"\n{CYAN}╔{'═'*53}╗{RESET}")
    print(f"{CYAN}║     Test Redis HA + Sentinel — AlmaLinux 9      ║{RESET}")
    print(f"{CYAN}╚{'═'*53}╝{RESET}")

    test_direct_connections()
    test_sentinel_connections()
    test_sentinel_master_discovery()
    test_sentinel_replica_discovery()
    test_write_read()
    test_read_from_replicas()
    test_roles()
    test_replication()
    test_cleanup()

    sys.exit(print_summary())