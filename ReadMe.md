# Backup_NVMe

Sauvegarde du SSD NVMe d'un Raspberry Pi 5 vers une **Clé USB** et/ou une **Carte SD**, chacune rendue bootable en secours du système principal.

Deux façons de lancer la sauvegarde :
- **`Backup_usb_sd.py`** — application graphique (Tkinter), lancement manuel.
- **`Backup_cron.py`** — version en ligne de commande, sans interface, exécutée automatiquement chaque semaine par `cron`.

Les deux scripts partagent la même logique de détection et de synchronisation ; le script cron en est une version simplifiée, pensée pour tourner sans surveillance.

## Contexte matériel
- Raspberry Pi 5 (16 Go RAM)
- SSD NVMe 1 To (disque système)
- Raspberry Pi OS Bookworm
- Destinations : clé USB et/ou carte SD, formatées en ext4 (partition ROOT) + une partition boot FAT32

## Fonctionnement
Pour chaque support cible (clé USB, carte SD) :

1. **Détection automatique** du support via `lsblk` (déjà monté, ou monté automatiquement si besoin).
2. **Vérification de l'espace disponible** sur le support (marge de sécurité de +5 % par rapport à l'espace utilisé sur le NVMe) — la sauvegarde est annulée pour ce support si l'espace est insuffisant.
3. **Calcul et journalisation du taux d'occupation** (%) du SSD NVMe (source) et du support de destination, avant puis après la synchronisation. Si le support dépasse le seuil `WARN_THRESHOLD_PCT` (80 % par défaut) après la sauvegarde, un avertissement `[WARNING]` est loggé — la sauvegarde reste néanmoins un succès, c'est une alerte préventive pour anticiper un remplacement de support avant qu'il ne soit plein.
4. **Synchronisation ROOT** : `rsync -aAX --delete-after` du système de fichiers racine vers le support, avec exclusions des points de montage système (`/dev`, `/proc`, `/sys`, `/run`, `/tmp`, `/media`, `/mnt`, `/boot`, caches, logs…).
5. **Montage de la partition BOOT** du support (si nécessaire) puis **synchronisation BOOT** : `rsync -rt --delete-after` du contenu de `/boot/firmware` (ou `/boot`).
6. **Démontage propre** de la partition BOOT et, uniquement si le support a été monté par le script lui-même (point de montage fallback, aucun montage préexistant trouvé), démontage du support lui-même.

Le support n'est jamais démonté s'il était déjà monté avant le lancement du script — que ce soit par un montage système persistant (unité `systemd .mount`, clé USB ou carte SD laissée branchée en permanence) ou par une session graphique. C'est notamment le cas typique d'une clé USB de sauvegarde restant connectée en continu au Raspberry Pi : le script la trouve déjà montée à chaque exécution et ne cherche jamais à la démonter, ce qui évite les conflits avec le montage système existant.

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

## Exemple de log

```
[24/07/2026 16:26:03] ########## Début Sauvegarde Disk NVMe vers (USB+SD) ##########
[24/07/2026 16:26:03] === Début sauvegarde vers USB ===
[24/07/2026 16:26:03] Clé USB trouvée : /dev/sda2 déjà montée sur /media/usb
[24/07/2026 16:26:03] Support    : Clé USB
[24/07/2026 16:26:03] Espace lib : 164.48 Go
[24/07/2026 16:26:03] Occupation SSD NVMe (source)      : 7.5% (77.3 Go / 1024.0 Go)
[24/07/2026 16:26:03] Occupation Clé USB (avant backup) : 28.9% (68.1 Go / 236.0 Go)
[24/07/2026 16:26:14] Rsync ROOT : OK
[24/07/2026 16:26:14] Rsync BOOT : OK
[24/07/2026 16:26:14] Occupation Clé USB (après backup)  : 32.7% (77.3 Go / 236.0 Go)
[24/07/2026 16:26:14] === Sauvegarde Clé USB terminée avec succès (durée : 0m 11s) ===
[24/07/2026 16:26:14] === Début sauvegarde vers SD ===
[24/07/2026 16:26:26] Rsync ROOT : OK
[24/07/2026 16:26:26] Rsync BOOT : OK
[24/07/2026 16:26:26] Occupation Carte SD (après backup)  : 32.7% (77.3 Go / 236.0 Go)
[24/07/2026 16:26:26] === Sauvegarde Carte SD terminée avec succès (durée : 0m 11s) ===
[24/07/2026 16:26:26] ---------- Résumé ----------
[24/07/2026 16:26:26]   USB  : OK
[24/07/2026 16:26:26]   SD   : OK
[24/07/2026 16:26:26] Durée totale : 0m 23s
[24/07/2026 16:26:26] ########## Fin Sauvegarde ##########
```

## Prérequis
- Raspberry Pi OS (ou toute distribution Linux basée sur Debian)
- `rsync`, `lsblk`
- Droits `sudo` sans mot de passe pour les commandes `mount`/`umount`/`rsync` utilisées par les scripts (`sudo -n`), à configurer via `visudo` pour un fonctionnement non interactif (indispensable pour le cron)
- Python 3
- Pour l'application graphique : `python3-tk`, `Pillow`

## Structure du dépôt
```
Backup_NVMe/
├── Backup_usb_sd.py     # Application graphique (Tkinter) - lancement manuel
├── Backup_cron.py       # Version ligne de commande - lancement automatique via cron
├── backup_cron.log      # Exemple de fichier de log généré
└── icons/               # Images utilisées par l'interface graphique (non incluses ici)
```

## Limites connues
- La destination doit déjà contenir un système de fichiers ext4/ext3/ext2 préparé au préalable (le script ne partitionne ni ne formate pas le support).
- La détection de la partition BOOT suppose une disposition classique (partition FAT32/vfat sur le même disque physique que la partition ROOT).
- L'alerte de capacité (`WARN_THRESHOLD_PCT`) est purement informative : elle ne bloque pas la sauvegarde et ne déclenche aucune notification externe (à ajouter séparément si besoin, ex. email ou Telegram).

## Auteur
Jean-François BRUNET - JFBConseils - Septembre 2026
