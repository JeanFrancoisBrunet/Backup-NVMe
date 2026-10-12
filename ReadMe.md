# Backup_NVMe

Sauvegarde du SSD NVMe d'un Raspberry Pi 5 vers une **Clé USB** et/ou une **Carte SD**, chacune rendue **bootable** en secours du système principal.

Deux façons de lancer la sauvegarde :
- **`Backup_usb_sd.py`** — application graphique (Tkinter), lancement manuel.
- **`Backup_cron.py`** — version en ligne de commande, sans interface, exécutée automatiquement chaque semaine par `cron`.

Les deux scripts partagent la même logique de détection et de synchronisation ; le script cron en est une version simplifiée, pensée pour tourner sans surveillance.

## Contexte matériel
- Raspberry Pi 5 (16 Go RAM)
- SSD NVMe 1 To (disque système)
- Raspberry Pi OS Bookworm, avec `log2ram` (les journaux `/var/log` sont en mémoire, leur copie persistante est `/var/hdd.log`)
- Destinations : clé USB et/ou carte SD (256 Go chacune), formatées en ext4 (partition ROOT) + une partition boot FAT32, table de partitions MBR

## Fonctionnement
Pour chaque support cible (clé USB, carte SD) :

1. **Détection automatique** du support via `lsblk` (déjà monté, ou monté automatiquement si besoin).
2. **Vérification de l'espace disponible** sur le support (marge de sécurité de +5 % par rapport à l'espace utilisé sur le NVMe) — la sauvegarde est annulée pour ce support si l'espace est insuffisant.
3. **Calcul et journalisation du taux d'occupation** (%) du SSD NVMe (source) et du support de destination, avant puis après la synchronisation. Si le support dépasse le seuil `WARN_THRESHOLD_PCT` (80 % par défaut) après la sauvegarde, un avertissement `[WARNING]` est loggé — la sauvegarde reste néanmoins un succès, c'est une alerte préventive pour anticiper un remplacement de support avant qu'il ne soit plein.
4. **Synchronisation ROOT** : `rsync -aAX --delete-after` du système de fichiers racine vers le support, avec exclusions des points de montage système (`/dev`, `/proc`, `/sys`, `/run`, `/tmp`, `/media`, `/mnt`, `/boot`, caches, `/var/log`…).
5. **Montage de la partition BOOT** du support (si nécessaire) puis **synchronisation BOOT** : `rsync -rt --delete-after` du contenu de `/boot/firmware` (ou `/boot`).
6. **Synchronisation des journaux** : `/var/log` est exclu de l'étape 4 (il est en mémoire avec `log2ram`). Le script recopie donc la copie persistante `/var/hdd.log` du NVMe dans le `/var/log` de la copie (`rsync -aAX --delete`), car c'est ce dossier que `log2ram` relit au démarrage de la copie. Sans cette étape, des services comme `exim4` ou `logrotate` échouent au démarrage de la copie. L'étape est ignorée si `log2ram` n'est pas actif.
7. **Correction des identifiants de démarrage** (voir ci-dessous).
8. **Démontage propre** de la partition BOOT et, uniquement si le support a été monté par le script lui-même (point de montage fallback, aucun montage préexistant trouvé), démontage du support lui-même.

Le support n'est jamais démonté s'il était déjà monté avant le lancement du script — que ce soit par un montage système persistant (unité `systemd .mount`, clé USB ou carte SD laissée branchée en permanence) ou par une session graphique. C'est notamment le cas typique d'une clé USB de sauvegarde restant connectée en continu au Raspberry Pi : le script la trouve déjà montée à chaque exécution et ne cherche jamais à la démonter, ce qui évite les conflits avec le montage système existant.

### Bootabilité de la copie : correction des PARTUUID
Le Raspberry Pi démarre sur une partition désignée par son `PARTUUID` (dans `cmdline.txt` : `root=PARTUUID=…`, et dans `/etc/fstab`). Les deux `rsync` recopient ces deux fichiers **tels qu'ils sont sur le NVMe** : une copie pointerait donc vers la partition du NVMe et ne démarrerait pas seule.

Après la synchronisation, chaque script :
- lit les `PARTUUID` du NVMe (source) et ceux du support (destination) ;
- remplace les premiers par les seconds dans `/etc/fstab` de la copie et dans `boot/cmdline.txt` de la copie ;
- journalise `PARTUUID corrigés (ancien -> nouveau)` pour chaque fichier modifié.

Cas particulier : si `lsblk` ne renvoie pas de `PARTUUID` pour la partition ROOT du support (par exemple partition tronquée), le script le **déduit** de celui de la partition BOOT du même disque (table MBR : `<signature du disque>-<n° de partition>`) et l'indique dans le log.

Une erreur à cette étape est signalée par un `[WARN]` sans faire échouer la sauvegarde : dans ce cas, la copie risque de ne pas démarrer seule.

### Application graphique — `Backup_usb_sd.py`
- Interface Tkinter avec sélection du support cible (Clé USB / Carte SD) par bouton radio.
- Barre de progression, journal de synchronisation en temps réel (sortie de `rsync` affichée ligne par ligne).
- Bouton **Annuler** permettant d'interrompre proprement le `rsync` en cours.
- Écran de démarrage (splash screen) avec logos.
- Dépendances : `tkinter`, `Pillow` (`PIL`).

Lancement manuel :
```bash
python3 Backup_usb_sd.py
```

### Version cron — `Backup_cron.py`
- Aucune interface graphique : toute la progression est journalisée via `print()` horodaté, capturée par `cron` dans un fichier de log.
- Sauvegarde **USB puis SD**, l'une après l'autre (une destination en échec n'interrompt pas le traitement de l'autre).
- Code de sortie non nul si au moins une des sauvegardes a échoué (utile pour une alerte cron).

Options en ligne de commande :
```bash
python3 Backup_cron.py              # USB puis SD (comportement par défaut)
python3 Backup_cron.py --target usb # Clé USB uniquement
python3 Backup_cron.py --target sd  # Carte SD uniquement
```

Lancement manuel avec journalisation :
```bash
python3 Backup_cron.py 2>&1 | tee backup_cron.log
```

### Suivi de capacité des supports
Chaque exécution (graphique ou cron) journalise le taux d'occupation (%) :
- du SSD NVMe (source), pour suivre sa croissance dans le temps ;
- du support de destination (clé USB / carte SD), avant et après la sauvegarde.

Le seuil d'alerte est fixé par la constante `WARN_THRESHOLD_PCT` (80 % par défaut) en tête de chaque script. Au-delà de ce seuil, un message `[WARNING]` est ajouté au log (et affiché en orange dans l'interface graphique) pour anticiper un changement de support avant qu'il ne sature — sans faire échouer la sauvegarde en cours.

Tâche planifiée (tous les lundis à 12h00, si le Raspberry Pi est allumé à ce moment-là) :
```cron
0 12 * * 1 /usr/bin/python3 /home/jfbrunet/Projects/Backup_NVMe/Backup_cron.py >> /home/jfbrunet/Projects/Backup_NVMe/backup_cron.log 2>&1
```

> Le Pi5 devant être allumé au moment programmé, la sauvegarde n'a pas de mécanisme de rattrapage si l'appareil est éteint le lundi à midi — elle sera simplement passée cette semaine-là.

> **La sauvegarde doit toujours être lancée depuis le NVMe**, jamais depuis la clé USB ou la carte SD après un démarrage de test : source et destination ne seraient plus les bonnes. Vérifier avec `findmnt /` (attendu : `/dev/nvme0n1p2`).

### Notifications Telegram
Les deux scripts peuvent envoyer un compte rendu sur Telegram, via le **même bot et le même fichier `~/.telegram_config`** que `telegram_bot_ctrl_pi5.py` (clés `token_ctrl` et `chat_id`). Les scripts appellent directement l'API Telegram : le bot de contrôle n'a pas besoin d'être démarré pour qu'ils puissent notifier.

| Situation                                                                                                    | Message                                                                                        |
|---                                                                                                           |---                                                                                             |
| Échec d'un support (rsync, support introuvable, trop petit…)                                                 | ❌ toujours envoyé                                                                             |
| Succès avec anomalie (`[WARN]`, PARTUUID non corrigés, fichiers de démarrage manquants, occupation ≥ seuil…) | ⚠️ toujours envoyé, avec la liste des anomalies (10 maximum)                                   |
| Succès sans anomalie                                                                                         | ✅ envoyé si `NOTIFY_ON_SUCCESS = True` (défaut cron : oui ; défaut interface graphique : non) |

Le compte rendu cron récapitule chaque support (USB, SD), la durée et l'occupation. Après écriture de `fstab` et `cmdline.txt`, le script relit les fichiers pour vérifier qu'il ne reste plus de `PARTUUID` du NVMe, et contrôle la présence de `kernel_2712.img`, `config.txt` et `cmdline.txt` dans la copie.

Tester la configuration :
```bash
python3 /home/jfbrunet/Projects/Backup_NVMe/Backup_cron.py --test-telegram
```
Pour désactiver les notifications : `NOTIFY_TELEGRAM = False` en tête du script. Le jeton étant dans `~/.telegram_config`, s'assurer que le fichier n'est lisible que par son propriétaire : `chmod 600 ~/.telegram_config`.

> Si le Pi est éteint ou sans réseau le lundi à midi, aucun message n'arrive : l'absence de message de succès ne prouve donc pas que tout va bien (d'où `NOTIFY_ON_SUCCESS = True` pour le cron).

### Commande `/info_backup` du bot de contrôle
Dans `telegram_bot_ctrl_pi5.py`, la commande **`/info_backup`** (ajoutée au menu `/aide`) répond à la demande « où en est ma sauvegarde ? » sans ouvrir de terminal. Elle lit `~/Projects/Backup_NVMe/backup_cron.log` (constante `BACKUP_LOG`), isole le **dernier** passage (le log est en mode ajout) et affiche :
- le verdict : ✅ OK, ⚠️ avec anomalies, ❌ échec, ou ⏳ en cours / interrompue (pas de ligne « Fin Sauvegarde ») ;
- la date de la sauvegarde et son ancienneté, avec un avertissement si elle date de plus de `BACKUP_MAX_DAYS` jours (8 par défaut : sauvegarde hebdomadaire en retard, Pi éteint le lundi…) ;
- le résultat par support (USB, SD), l'occupation après sauvegarde, la durée totale ;
- les anomalies relevées (8 maximum).

Elle complète les notifications : celles-ci arrivent d'elles-mêmes à la fin d'une sauvegarde, `/info_backup` permet de vérifier à la demande, et notamment de détecter une sauvegarde qui n'a pas eu lieu. Après modification du bot, le redémarrer pour prendre en compte la commande.

## Exemple de log

Sauvegarde vers la carte SD (le `PARTUUID` de la racine est ici déduit de celui du BOOT) :

```
[10/10/2026 19:59:25] ########## Début Sauvegarde Disk NVMe vers (SD) ##########
[10/10/2026 19:59:25] === Début sauvegarde vers SD ===
[10/10/2026 19:59:25] Carte SD trouvée : /dev/mmcblk0p2 déjà montée sur /media/jfbrunet/rootfs
[10/10/2026 19:59:25] Support    : Carte SD
[10/10/2026 19:59:25] Montage    : /media/jfbrunet/rootfs
[10/10/2026 19:59:25] Espace lib : 155.14 Go
[10/10/2026 19:59:25] Occupation SSD NVMe (source)      : 6.6% (60.1 Go / 915.3 Go)
[10/10/2026 19:59:25] Occupation Carte SD (avant backup) : 28.3% (66.1 Go / 233.1 Go)
[10/10/2026 19:59:25] BOOT source : /boot/firmware/
[10/10/2026 19:59:25] Synchronisation ROOT...
[10/10/2026 19:59:49] Rsync ROOT : OK
[10/10/2026 19:59:49] Montage de /dev/mmcblk0p1 -> /media/jfbrunet/rootfs/boot
[10/10/2026 19:59:49] Synchronisation BOOT...
[10/10/2026 19:59:49] Rsync BOOT : OK
[10/10/2026 19:59:49] Synchronisation JOURNAUX...
[10/10/2026 19:59:49] Journaux : /var/hdd.log -> /media/jfbrunet/rootfs/var/log : OK
[10/10/2026 19:59:49] Correction des identifiants de démarrage (fstab, cmdline.txt)...
[10/10/2026 19:59:49] PARTUUID racine déduit de celui du BOOT : 2e767340-02
[10/10/2026 19:59:49] /media/jfbrunet/rootfs/etc/fstab : PARTUUID corrigés (cea73a38-02 -> 2e767340-02)
[10/10/2026 19:59:50] /media/jfbrunet/rootfs/boot/cmdline.txt : PARTUUID corrigés (cea73a38-02 -> 2e767340-02)
[10/10/2026 19:59:50] Occupation Carte SD (après backup)  : 28.3% (65.9 Go / 233.1 Go)
[10/10/2026 19:59:50] === Sauvegarde Carte SD terminée avec succès (durée : 0m 25s) ===
[10/10/2026 19:59:50] Démontage BOOT...
[10/10/2026 19:59:50] ---------- Résumé ----------
[10/10/2026 19:59:50]   SD   : OK
[10/10/2026 19:59:50] Durée totale : 0m 25s
[10/10/2026 19:59:50] ########## Fin Sauvegarde ##########
```

> Les lignes « Synchronisation JOURNAUX » / « Journaux : … » ont été ajoutées après cette exécution ; elles reproduisent les messages émis par le script.

## Tester le démarrage d'une copie
Un démarrage de test ne modifie ni le NVMe ni l'autre copie : en cas d'échec, il suffit de retirer le support testé.

L'ordre de démarrage du Pi 5 est dans l'EEPROM (`sudo rpi-eeprom-config --edit`, ligne `BOOT_ORDER`). La valeur se lit **de droite à gauche** : `1` = carte SD, `4` = USB, `6` = NVMe, `f` = recommencer.

| `BOOT_ORDER` | Ordre d'essai           | Usage              |
|---           |---                      |---                 |
| `0xf416`     | NVMe, puis SD, puis USB | **réglage normal** |
| `0xf614`     | USB, puis SD, puis NVMe | tester la clé USB  |
| `0xf641`     | SD, puis USB, puis NVMe | tester la carte SD |

Procédure :
1. Vérifier les identifiants de la copie : `grep -o 'root=[^ ]*' <montage>/boot/cmdline.txt` (clé : `PARTUUID` de la clé, carte : `PARTUUID` de la carte).
2. `sudo rpi-eeprom-config --edit`, régler la valeur voulue, enregistrer, puis `sudo reboot`.
3. Vérifier le support de démarrage : `findmnt /` (`/dev/sda2` = clé USB, `/dev/mmcblk0p2` = carte SD, `/dev/nvme0n1p2` = NVMe) et `systemctl --failed` (liste attendue vide).
4. **Remettre `0xf416`**, redémarrer, et vérifier `findmnt /` → `/dev/nvme0n1p2`.

Si l'écran reste figé plus de deux minutes, couper l'alimentation, retirer le support testé et rallumer : le Pi démarre sur le support suivant.

## Dépannage

### La carte SD « tourne en rond » au démarrage
Le noyau n'arrive pas à retrouver la partition racine par son `PARTUUID`. Cause rencontrée : la table de partitions déclarait une partition ROOT plus grande que la carte (`mmcblk0: p2 size … extends beyond EOD, truncated` dans `dmesg`), la partition n'avait alors plus de `PARTUUID` visible.

Diagnostic (lecture seule) :
```bash
ls /dev/disk/by-partuuid/                      # le PARTUUID de la partition 2 est-il présent ?
sudo fdisk -l /dev/mmcblk0                     # fin de la partition 2 vs taille du disque
sudo dumpe2fs -h /dev/mmcblk0p2 | grep -E 'Block count|Block size'
```
Avant de réduire la partition, vérifier que `Block count × Block size` tient dans la taille réelle de la carte.

Réparation (ramener la partition 2 à la fin réelle de la carte, sans toucher aux données) :
```bash
sudo sfdisk -d /dev/mmcblk0 > ~/table.sauvegarde        # sauvegarde de l'ancienne table
sudo umount /dev/mmcblk0p1 /dev/mmcblk0p2
# taille = (secteurs du disque) - (début de la partition 2)
echo '<début>,<taille>,83' | sudo sfdisk -N 2 /dev/mmcblk0
sudo e2fsck -fn /dev/mmcblk0p2                           # contrôle, sans modification
```
Relancer ensuite une sauvegarde vers la carte (le script re-corrige `fstab` et `cmdline.txt`).

### `exim4` / `logrotate` en échec au démarrage de la copie
Les journaux de la copie sont incomplets (dossier `/var/log/exim4` manquant, ancien dossier aux mauvais droits). Relancer une sauvegarde avec la version à jour des scripts : l'étape « Journaux » reconstruit le `/var/log` de la copie à partir de `/var/hdd.log` du NVMe. Un message `fail2ban` éventuel dans `logrotate` au démarrage peut venir de la lenteur de la carte SD et est sans conséquence.

## Prérequis
- Raspberry Pi OS (ou toute distribution Linux basée sur Debian)
- `rsync`, `lsblk`, `findmnt`
- Droits `sudo` sans mot de passe pour les commandes `mount`/`umount`/`rsync`/`tee` utilisées par les scripts (`sudo -n`), à configurer via `visudo` pour un fonctionnement non interactif (indispensable pour le cron)
- Python 3
- Pour les notifications Telegram et `/info_backup` : `~/.telegram_config` avec `token_ctrl` et `chat_id`, et un accès Internet (bibliothèque standard uniquement pour les scripts de sauvegarde, rien à installer)
- Pour l'application graphique : `python3-tk`, `Pillow`
- Optionnel : `log2ram` (si absent, l'étape « Journaux » est ignorée)

## Structure du dépôt
```
Backup_NVMe/
├── Backup_usb_sd.py     # Application graphique (Tkinter) - lancement manuel
├── Backup_cron.py       # Version ligne de commande - lancement automatique via cron
├── ReadMe.md            # Ce fichier
├── backup_cron.log      # Exemple de fichier de log généré
└── icons/               # Images utilisées par l'interface graphique (non incluses ici)
```

## Limites connues
- La destination doit déjà contenir un système de fichiers ext4/ext3/ext2 préparé au préalable (le script ne partitionne ni ne formate le support).
- La détection de la partition BOOT suppose une disposition classique (partition FAT32/vfat sur le même disque physique que la partition ROOT).
- La correction des `PARTUUID` suppose une table de partitions MBR (identifiant `<signature>-<n°>`).
- `/info_backup` s'appuie sur le format des lignes de `backup_cron.log` : si les messages des scripts changent, adapter `_resume_derniere_sauvegarde()` dans le bot.
- Les journaux de la copie sont ceux du NVMe au moment de la sauvegarde (copie de `/var/hdd.log`) : ils ne reflètent pas ceux de la copie elle-même.
- L'alerte de capacité (`WARN_THRESHOLD_PCT`) est purement informative : elle ne bloque pas la sauvegarde ; elle est signalée dans le compte rendu Telegram.

## Auteur
Jean-François BRUNET - JFBConseils - Septembre / Octobre 2026
