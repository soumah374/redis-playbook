# Redis HA avec Sentinel — Playbook Ansible

Automatisation Ansible déployant un cluster Redis en haute disponibilité (3 nœuds) avec
**Redis Sentinel** pour le basculement automatique (failover). Un nœud démarre comme
master, deux comme réplicas ; trois Sentinels (un par nœud, quorum 2) surveillent le
master et orchestrent le failover.

- **OS cible** : RHEL / CentOS / AlmaLinux 9
- **Redis** : installé depuis le module Remi `redis:remi-7.2`
- **Ports** : Redis `6379`, Sentinel `26379`

## Topologie

| Rôle initial | Hôte     | IP              |
|--------------|----------|-----------------|
| master       | master   | 192.168.1.158   |
| replica      | replica1 | 192.168.1.140   |
| replica      | replica2 | 192.168.1.159   |

> ⚠️ Les noms de groupes de l'inventaire reflètent les rôles **initiaux**. Après un
> failover, l'hôte `master` peut tourner en réplica. Découvrez toujours le master
> courant via Sentinel.

## Structure du dépôt

```
redis_sentinel_playbook.yml   Playbook principal (groupe redis_all)
inventory.ini                 Inventaire : redis_master, redis_replicas, redis_all
redis-sentinel.conf.j2        Template Jinja2 → /etc/redis/sentinel.conf
client-python/main.py         Test d'intégration du cluster (9 vérifications)
keygen/*.pem                  Clés SSH privées des nœuds
```

## Prérequis

- Ansible (avec les collections `ansible.posix` pour `firewalld`)
- Accès SSH aux nœuds en tant qu'utilisateur `compute`
- Python 3 + `redis` pour le test d'intégration

## Déploiement

```bash
ansible-playbook -i inventory.ini redis_sentinel_playbook.yml
```

Options utiles :

```bash
# Cibler un sous-ensemble
ansible-playbook -i inventory.ini redis_sentinel_playbook.yml --limit redis_master

# Dry-run avec diff
ansible-playbook -i inventory.ini redis_sentinel_playbook.yml --check --diff

# Verbeux (débogage)
ansible-playbook -i inventory.ini redis_sentinel_playbook.yml -vvv
```

### Étapes du playbook

1. Activation des dépôts EPEL + Remi
2. Installation de `redis` (server + sentinel + cli)
3. Configuration de `/etc/redis/redis.conf` (bind all, désactivation du protected-mode,
   `requirepass` / `masterauth`, `replicaof` sur les réplicas uniquement)
4. Déploiement du template Sentinel
5. Ouverture des ports firewalld (6379, 26379)
6. Démarrage des services `redis` et `redis-sentinel`
7. Vérification via `redis-cli ping` et `info sentinel`

Le rôle (master ou réplica) est déterminé entièrement par l'appartenance aux groupes de
l'inventaire (`when: inventory_hostname in groups[...]`).

## Tests d'intégration

```bash
cd client-python
pip install -r requirements.txt
python3 main.py        # code de sortie 0 uniquement si toutes les vérifications passent
```

Les 9 vérifications s'exécutent séquentiellement : connexions directes, connexions
Sentinel, découverte master/réplicas, écriture/lecture, rôles et état de la réplication.
Il n'y a pas de runner pour un test unique — importez le module et appelez la fonction
`test_*()` voulue pour l'isoler.

## Invariants inter-fichiers

Ces valeurs sont codées en dur à plusieurs endroits (elles ne proviennent pas d'une
source unique). En cas de changement, mettez-les à jour **partout** :

| Valeur          | Playbook                       | Test (`client-python/main.py`) |
|-----------------|--------------------------------|--------------------------------|
| Nom du master   | `sentinel_master_name`         | `MASTER_NAME`                  |
| Mot de passe    | `redis_password`               | `REDIS_PASSWORD`               |
| IP des nœuds    | `inventory.ini` (`ansible_host`)| `SENTINELS` + listes de nœuds |
| Ports           | `redis_port` / `sentinel_port` | `REDIS_PORT` / `26379`         |

> Dans `redis-sentinel.conf.j2`, la ligne `sentinel monitor` **doit** précéder
> `sentinel auth-pass` (sinon Sentinel rejette l'auth pour un master inconnu).

## ⚠️ Sécurité

`keygen/*.pem` sont des **clés SSH privées** versionnées dans le dépôt, et
`redis_password` est un mot de passe en clair dans le playbook. Ce sont de vrais
secrets : à faire tourner / retirer en priorité si le dépôt est partagé.
