#!/usr/bin/env python3
# =============================================================================
# Test de montée en charge — Redis HA + Sentinel — AlmaLinux 9
# Usage : python3 loadtest.py [options]
# Prérequis : pip install redis
#
# Génère une charge concurrente (lectures/écritures) sur le cluster Redis via
# Sentinel et mesure le débit (ops/s) et les latences (p50/p95/p99).
# Les écritures vont sur le master, les lectures sont réparties sur les réplicas.
# =============================================================================

import argparse
import random
import statistics
import string
import sys
import threading
import time

import redis
from redis.sentinel import Sentinel

# ─── Configuration (alignée sur main.py / inventory.ini) ──────────────────────
SENTINELS = [
    ("192.168.1.158", 26379),  # master
    ("192.168.1.140", 26379),  # replica1
    ("192.168.1.159", 26379),  # replica2
]
MASTER_NAME    = "computemaster"
REDIS_PASSWORD = "Securep@55Here"

KEY_PREFIX = "loadtest:"

# ─── Couleurs terminal ────────────────────────────────────────────────────────
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
RESET  = "\033[0m"


def section(title):
    print(f"\n{CYAN}{'═'*55}{RESET}")
    print(f"{CYAN}  {title}{RESET}")
    print(f"{CYAN}{'═'*55}{RESET}")


# ─── Statistiques partagées entre workers ─────────────────────────────────────
class Stats:
    def __init__(self):
        self.lock = threading.Lock()
        self.latencies = []      # en millisecondes
        self.reads = 0
        self.writes = 0
        self.errors = 0

    def record(self, latency_ms, op):
        with self.lock:
            self.latencies.append(latency_ms)
            if op == "read":
                self.reads += 1
            else:
                self.writes += 1

    def record_error(self):
        with self.lock:
            self.errors += 1


def make_payload(size):
    """Génère une valeur aléatoire de `size` octets."""
    return "".join(random.choices(string.ascii_letters + string.digits, k=size))


def worker(worker_id, args, stats, deadline, stop_event, payload):
    """Boucle de charge exécutée par chaque thread."""
    # Pools de connexion gérés par Sentinel : un master (write) + un slave (read).
    sentinel = Sentinel(SENTINELS, socket_timeout=args.timeout, password=REDIS_PASSWORD)
    master = sentinel.master_for(MASTER_NAME, socket_timeout=args.timeout, password=REDIS_PASSWORD)
    slave = sentinel.slave_for(MASTER_NAME, socket_timeout=args.timeout, password=REDIS_PASSWORD)

    n = 0
    while not stop_event.is_set() and time.monotonic() < deadline:
        key = f"{KEY_PREFIX}{worker_id}:{n % args.keyspace}"
        is_write = random.random() < args.write_ratio
        t0 = time.perf_counter()
        try:
            if is_write:
                master.set(key, payload, ex=args.ttl)
                op = "write"
            else:
                slave.get(key)
                op = "read"
            stats.record((time.perf_counter() - t0) * 1000.0, op)
        except Exception:
            stats.record_error()
        n += 1


def reporter(stats, deadline, stop_event, interval):
    """Affiche un point de situation périodique (débit instantané)."""
    last_total = 0
    last_t = time.monotonic()
    while not stop_event.is_set() and time.monotonic() < deadline:
        time.sleep(interval)
        with stats.lock:
            total = stats.reads + stats.writes
            errors = stats.errors
        now = time.monotonic()
        rate = (total - last_total) / (now - last_t) if now > last_t else 0
        print(f"  {YELLOW}…{RESET} {total:>8} ops  |  {rate:>10.0f} ops/s  |  {errors} erreurs")
        last_total, last_t = total, now


def percentile(sorted_data, pct):
    if not sorted_data:
        return 0.0
    k = (len(sorted_data) - 1) * (pct / 100.0)
    f = int(k)
    c = min(f + 1, len(sorted_data) - 1)
    if f == c:
        return sorted_data[f]
    return sorted_data[f] + (sorted_data[c] - sorted_data[f]) * (k - f)


def preflight():
    """Vérifie l'accès au cluster avant de lancer la charge."""
    section("Pré-vérification du cluster")
    try:
        sentinel = Sentinel(SENTINELS, socket_timeout=3, password=REDIS_PASSWORD)
        master = sentinel.discover_master(MASTER_NAME)
        replicas = sentinel.discover_slaves(MASTER_NAME)
        print(f"  {GREEN}[✔]{RESET} Master détecté : {master[0]}:{master[1]}")
        print(f"  {GREEN}[✔]{RESET} {len(replicas)} réplica(s) détecté(s)")
        return True
    except Exception as e:
        print(f"  {RED}[✘]{RESET} Cluster inaccessible : {e}")
        return False


def cleanup(args):
    """Supprime les clés de test générées."""
    try:
        sentinel = Sentinel(SENTINELS, socket_timeout=5, password=REDIS_PASSWORD)
        master = sentinel.master_for(MASTER_NAME, socket_timeout=5, password=REDIS_PASSWORD)
        deleted = 0
        for key in master.scan_iter(match=f"{KEY_PREFIX}*", count=500):
            master.delete(key)
            deleted += 1
        print(f"  {GREEN}[✔]{RESET} {deleted} clé(s) de test supprimée(s)")
    except Exception as e:
        print(f"  {RED}[✘]{RESET} Nettoyage : {e}")


def run(args):
    stats = Stats()
    stop_event = threading.Event()
    payload = make_payload(args.value_size)
    deadline = time.monotonic() + args.duration

    section(f"Montée en charge — {args.workers} workers × {args.duration}s")
    print(f"  Ratio écriture/lecture : {int(args.write_ratio*100)}% / {int((1-args.write_ratio)*100)}%")
    print(f"  Taille des valeurs     : {args.value_size} octets")
    print(f"  Keyspace par worker    : {args.keyspace} clés\n")

    threads = []
    for i in range(args.workers):
        t = threading.Thread(target=worker, args=(i, args, stats, deadline, stop_event, payload), daemon=True)
        t.start()
        threads.append(t)

    rep = threading.Thread(target=reporter, args=(stats, deadline, stop_event, args.report_interval), daemon=True)
    rep.start()

    t_start = time.monotonic()
    try:
        for t in threads:
            t.join(timeout=args.duration + args.timeout + 5)
    except KeyboardInterrupt:
        print(f"\n  {YELLOW}Interruption — arrêt des workers…{RESET}")
        stop_event.set()
        for t in threads:
            t.join(timeout=5)
    elapsed = time.monotonic() - t_start
    stop_event.set()

    print_summary(stats, elapsed)

    if not args.no_cleanup:
        section("Nettoyage")
        cleanup(args)

    return 0 if stats.errors == 0 else 1


def print_summary(stats, elapsed):
    section("Résultats")
    latencies = sorted(stats.latencies)
    total = stats.reads + stats.writes
    throughput = total / elapsed if elapsed > 0 else 0

    print(f"  Durée réelle      : {elapsed:.1f} s")
    print(f"  Opérations totales: {total}  ({stats.writes} écritures, {stats.reads} lectures)")
    print(f"  Erreurs           : {stats.errors}")
    print(f"  Débit             : {GREEN}{throughput:.0f} ops/s{RESET}")
    if latencies:
        print(f"\n  Latences (ms) :")
        print(f"    min    : {latencies[0]:.2f}")
        print(f"    moyenne: {statistics.mean(latencies):.2f}")
        print(f"    p50    : {percentile(latencies, 50):.2f}")
        print(f"    p95    : {percentile(latencies, 95):.2f}")
        print(f"    p99    : {percentile(latencies, 99):.2f}")
        print(f"    max    : {latencies[-1]:.2f}")

    if stats.errors == 0:
        print(f"\n  {GREEN}✔ Aucune erreur — cluster stable sous charge.{RESET}\n")
    else:
        rate = stats.errors / (total + stats.errors) * 100 if (total + stats.errors) else 0
        print(f"\n  {YELLOW}⚠ {stats.errors} erreur(s) ({rate:.1f}%) sous charge.{RESET}\n")


def parse_args():
    p = argparse.ArgumentParser(description="Test de montée en charge Redis HA + Sentinel")
    p.add_argument("-w", "--workers", type=int, default=50, help="Nombre de threads concurrents (def: 50)")
    p.add_argument("-d", "--duration", type=float, default=30, help="Durée du test en secondes (def: 30)")
    p.add_argument("-r", "--write-ratio", type=float, default=0.2,
                   help="Proportion d'écritures entre 0.0 et 1.0 (def: 0.2)")
    p.add_argument("-s", "--value-size", type=int, default=128, help="Taille des valeurs en octets (def: 128)")
    p.add_argument("-k", "--keyspace", type=int, default=1000, help="Nombre de clés distinctes par worker (def: 1000)")
    p.add_argument("--ttl", type=int, default=300, help="TTL des clés écrites en secondes (def: 300)")
    p.add_argument("--timeout", type=float, default=3, help="Socket timeout en secondes (def: 3)")
    p.add_argument("--report-interval", type=float, default=2, help="Intervalle des points de situation (def: 2s)")
    p.add_argument("--no-cleanup", action="store_true", help="Ne pas supprimer les clés de test à la fin")
    args = p.parse_args()
    if not 0.0 <= args.write_ratio <= 1.0:
        p.error("--write-ratio doit être compris entre 0.0 et 1.0")
    return args


if __name__ == "__main__":
    args = parse_args()
    print(f"\n{CYAN}╔{'═'*53}╗{RESET}")
    print(f"{CYAN}║   Montée en charge Redis HA + Sentinel — AlmaLinux  ║{RESET}")
    print(f"{CYAN}╚{'═'*53}╝{RESET}")

    if not preflight():
        sys.exit(1)

    sys.exit(run(args))
