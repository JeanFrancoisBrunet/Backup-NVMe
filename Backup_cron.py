#!/usr/bin/env python3
# =============================================================================
#  Sauvegarde CRON du SSD NVMe -> Clé USB & Carte SD (Raspberry Pi 5)
#  Version simplifiée sans interface graphique, basée sur Backup_usb_sd.py
#  Auteur : Jean-François BRUNET - JFBConseils - Juillet 2026
# =============================================================================
#
#  Utilisation :
#     python3 Backup_cron.py              -> sauvegarde USB puis SD (défaut)
#     python3 Backup_cron.py --target usb -> sauvegarde uniquement la Clé USB
#     python3 Backup_cron.py --target sd  -> sauvegarde uniquement la Carte SD
#
#  Cron hebdomadaire, tous les lundis à 12h00 (Backup NVMe vers USB puis SD, l'un après l'autre) :
#     0 12 * * 1 /usr/bin/python3 /home/jfbrunet/Projects/Backup_NVMe/Backup_cron.py >> /home/jfbrunet/Projects/Backup_NVMe/backup_cron.log 2>&1
# 
#  Lancement manuel par :    python3 /home/jfbrunet/Projects/Backup_NVMe/Backup_cron.py 2>&1 | tee /home/jfbrunet/Projects/Backup_NVMe/backup_cron.log
#  Lecture du fichier log :  cat /home/jfbrunet/Projects/Backup_NVMe/backup_cron.log
#
# =============================================================================

import subprocess
import os
import shutil
import sys
import argparse
from datetime import datetime

# ---------------------------------------------------------------------- #
# Configuration                                                          #
# ---------------------------------------------------------------------- #
DEST_FALLBACK    = "/media/usb"   # point de montage fallback clé USB
DEST_FALLBACK_SD = "/media/sd"    # point de montage fallback carte SD
EXCLUDE_HOME     = "/home/jfbrunet/.cache/*"  # à adapter si besoin

EXT_FS = ("ext4", "ext3", "ext2")

# Seuil d'alerte : si le support de sauvegarde dépasse ce % d'occupation
# après la sauvegarde, un avertissement est loggé (sans faire échouer le script)
WARN_THRESHOLD_PCT = 80

def log(msg):
    """Affiche un message horodaté (capturé par cron dans le fichier log)."""
    print(f"[{datetime.now().strftime('%d/%m/%Y %H:%M:%S')}] {msg}", flush=True)

def usage_pct(path):
    """Renvoie (used_go, total_go, pct_occupation) pour le point de montage donné."""
    total, used, _ = shutil.disk_usage(path)
    pct = (used / total * 100) if total else 0.0
    return used / (1024 ** 3), total / (1024 ** 3), pct

# ---------------------------------------------------------------------- #
# lsblk                                                                  #
# ---------------------------------------------------------------------- #
def get_lsblk_rows():
    try:
        out = subprocess.check_output(
            ["lsblk", "--output", "NAME,MOUNTPOINT,FSTYPE,TYPE,PKNAME,TRAN",
             "--pairs", "--noheadings"],
            text=True
        )
    except Exception as e:
        log(f"[WARN] lsblk : {e}")
        return []

    rows = []
    for line in out.strip().splitlines():
        d = {}
        for token in line.split():
            if '=' in token:
                k, v = token.split('=', 1)
                d[k] = v.strip('"')
        rows.append((
            d.get('NAME', ''),
            d.get('MOUNTPOINT', ''),
            d.get('FSTYPE', ''),
            d.get('TYPE', ''),
            d.get('PKNAME', ''),
            d.get('TRAN', ''),
        ))
    return rows

def detect_boot_source():
    if os.path.ismount("/boot/firmware"):
        return "/boot/firmware/"
    if os.path.ismount("/boot"):
        return "/boot/"
    if os.path.isdir("/boot/firmware"):
        return "/boot/firmware/"
    return "/boot/"

# ---------------------------------------------------------------------- #
# Détection du support (USB ou SD)                                      #
# ---------------------------------------------------------------------- #
def find_disks(rows, prefix):
    return {name for (name, mnt, fstype, typ, pk, tran) in rows
            if typ == "disk" and name.startswith(prefix)}

def find_already_mounted(rows, disks):
    for (name, mnt, fstype, typ, pk, tran) in rows:
        if typ != "part" or pk not in disks:
            continue
        if fstype.lower() not in EXT_FS:
            continue
        if mnt:
            return f"/dev/{name}", mnt
    return "", ""

def find_root_partition(rows, disks):
    system_mounts = ("/", "/boot", "/boot/firmware")
    candidates = []
    for (name, mnt, fstype, typ, pk, tran) in rows:
        if typ != "part" or pk not in disks:
            continue
        if fstype.lower() not in EXT_FS:
            continue
        if mnt not in system_mounts:
            candidates.append(name)
    return f"/dev/{candidates[0]}" if candidates else ""

def find_boot_partition(rows, dest_mnt):
    root_part = None
    for row in rows:
        name, mnt, fstype, typ, pk, tran = row
        if mnt == dest_mnt and typ == "part":
            root_part = row
            break
    if not root_part:
        return ""
    parent = root_part[4]
    candidates = [n for (n, m, fs, t, pk, tr) in rows
                  if t == "part" and pk == parent
                  and fs.lower() in ("vfat", "fat32")]
    if not candidates:
        return ""
    candidates.sort(key=lambda n: (not (n.endswith("1") or n.endswith("p1")), n))
    return f"/dev/{candidates[0]}"

def resolve_dest(target):
    rows = get_lsblk_rows()

    if target == "usb":
        label    = "Clé USB"
        fallback = DEST_FALLBACK
        prefix   = "sd"
    else:
        label    = "Carte SD"
        fallback = DEST_FALLBACK_SD
        prefix   = "mmcblk"

    disks = find_disks(rows, prefix)
    if not disks:
        return "", label, False

    dev, mnt = find_already_mounted(rows, disks)
    if mnt:
        log(f"{label} trouvée : {dev} déjà montée sur {mnt}")
        return mnt, label, False

    root_dev = find_root_partition(rows, disks)
    if not root_dev:
        return "", label, False

    os.makedirs(fallback, exist_ok=True)
    log(f"{label} détectée : {root_dev} -> montage sur {fallback}")
    proc = subprocess.run(["sudo", "-n", "mount", root_dev, fallback],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if proc.returncode == 0:
        log(f"-> {label} montée automatiquement.")
        return fallback, label, True

    log(f"[WARN] Impossible de monter {root_dev} : {proc.stderr.strip()}")
    return "", label, False

def run_cmd(cmd, phase):
    """Exécute une commande, log la sortie, renvoie le code retour."""
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if proc.stdout:
        for line in proc.stdout.splitlines():
            log(f"  {line}")
    code = proc.returncode
    if code == 0:
        log(f"{phase} : OK")
    elif code in (23, 24):
        log(f"{phase} : Terminé avec fichiers ignorés (code {code}) - normal")
    else:
        log(f"{phase} : ECHEC (code {code})")
    return code

# ---------------------------------------------------------------------- #
# Sauvegarde d'un seul support (USB ou SD)                               #
# Renvoie True si succès, False si échec (n'interrompt jamais le reste)  #
# ---------------------------------------------------------------------- #
def backup_one(target):
    start_time = datetime.now()
    log(f"=== Début sauvegarde vers {target.upper()} ===")

    dest, label, mounted_by_script = resolve_dest(target)
    if not dest:
        log(f"ERREUR : {label} introuvable. Sauvegarde ignorée pour ce support.")
        return False

    # Vérification de l'espace disponible (marge +5 %)
    _, src_used, _ = shutil.disk_usage("/")
    _, _, free     = shutil.disk_usage(dest)
    needed         = src_used * 1.05

    if free < needed:
        log(f"ERREUR : {label} trop petite "
            f"({free / (1024**3):.2f} Go libres < {needed / (1024**3):.2f} Go nécessaires)")
        return False

    src_used_go, src_total_go, src_pct = usage_pct("/")
    dst_used_go, dst_total_go, dst_pct = usage_pct(dest)

    log(f"Support    : {label}")
    log(f"Montage    : {dest}")
    log(f"Espace lib : {free / (1024**3):.2f} Go")
    log(f"Occupation SSD NVMe (source)      : {src_pct:.1f}% ({src_used_go:.1f} Go / {src_total_go:.1f} Go)")
    log(f"Occupation {label} (avant backup) : {dst_pct:.1f}% ({dst_used_go:.1f} Go / {dst_total_go:.1f} Go)")

    mounted_boot = False

    try:
        # ---- Rsync ROOT ----
        boot_src = detect_boot_source()
        log(f"BOOT source : {boot_src}")

        rsync_root = [
            "sudo", "rsync", "-aAX", "--delete-after",
            f"--exclude={dest}/*",
            "--exclude=/boot/*",
            "--exclude=/boot/firmware/*",
            "--exclude=/dev/*",
            "--exclude=/proc/*",
            "--exclude=/sys/*",
            "--exclude=/run/*",
            "--exclude=/tmp/*",
            "--exclude=/media/*",
            "--exclude=/mnt/*",
            "--exclude=/lost+found",
            f"--exclude={EXCLUDE_HOME}",
            "--exclude=/var/log/*",
            "/", dest + "/"
        ]
        log("Synchronisation ROOT...")
        ret1 = run_cmd(rsync_root, "Rsync ROOT")
        if ret1 not in (0, 23, 24):
            log(f"ERREUR : Rsync ROOT a échoué pour {label}.")
            return False

        # ---- Montage BOOT ----
        dest_boot_path = os.path.join(dest, "boot")
        os.makedirs(dest_boot_path, exist_ok=True)
        if not os.path.ismount(dest_boot_path):
            rows = get_lsblk_rows()
            boot_dev = find_boot_partition(rows, dest)
            if boot_dev:
                log(f"Montage de {boot_dev} -> {dest_boot_path}")
                proc = subprocess.run(["sudo", "-n", "mount", boot_dev, dest_boot_path],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                if proc.returncode == 0:
                    mounted_boot = True
                else:
                    log(f"[WARN] Impossible de monter BOOT : {proc.stderr.strip()}")
            else:
                log("[WARN] Partition BOOT introuvable.")

        # ---- Rsync BOOT ----
        rsync_boot = [
            "sudo", "rsync", "-rt", "--delete-after",
            "--no-perms", "--no-owner", "--no-group",
            boot_src, dest_boot_path + "/"
        ]
        log("Synchronisation BOOT...")
        ret2 = run_cmd(rsync_boot, "Rsync BOOT")
        if ret2 not in (0, 23, 24):
            log(f"ERREUR : Rsync BOOT a échoué pour {label}.")
            return False

        # Occupation du support après la sauvegarde
        dst_used_go, dst_total_go, dst_pct = usage_pct(dest)
        log(f"Occupation {label} (après backup)  : {dst_pct:.1f}% ({dst_used_go:.1f} Go / {dst_total_go:.1f} Go)")
        if dst_pct >= WARN_THRESHOLD_PCT:
            log(f"[WARNING] {label} occupée à {dst_pct:.1f}% (seuil d'alerte : {WARN_THRESHOLD_PCT}%). "
                f"Pensez à surveiller l'espace disponible ou à prévoir un support plus grand.")

        elapsed = datetime.now() - start_time
        minutes, seconds = divmod(int(elapsed.total_seconds()), 60)
        log(f"=== Sauvegarde {label} terminée avec succès (durée : {minutes}m {seconds:02d}s) ===")
        return True

    except Exception as e:
        log(f"ERREUR INATTENDUE sur {label} : {e}")
        return False

    finally:
        # Démontage BOOT
        dest_boot_path = os.path.join(dest, "boot")
        if mounted_boot and os.path.ismount(dest_boot_path):
            log("Démontage BOOT...")
            subprocess.run(["sudo", "-n", "umount", dest_boot_path])

        # Démontage support (seulement si c'est CE script qui l'a monté ;
        # un support déjà monté par le système - systemd, session graphique,
        # branchement permanent - n'est jamais démonté par ce script)
        if mounted_by_script and os.path.ismount(dest):
            log(f"Démontage {label}...")
            subprocess.run(["sudo", "-n", "umount", dest])

# ---------------------------------------------------------------------- #
# Programme principal : USB puis SD, l'un après l'autre                  #
# ---------------------------------------------------------------------- #
def main():
    parser = argparse.ArgumentParser(
        description="Sauvegarde NVMe -> Clé USB puis Carte SD (cron)")
    parser.add_argument("--target", choices=["usb", "sd", "both"], default="both",
                        help="Support(s) de destination (défaut : both = usb puis sd)")
    args = parser.parse_args()

    targets = ["usb", "sd"] if args.target == "both" else [args.target]

    global_start = datetime.now()
    log(f"########## Début Sauvegarde Disk NVMe vers ({'+'.join(t.upper() for t in targets)}) ##########")

    results = {}
    for t in targets:
        results[t] = backup_one(t)

    elapsed = datetime.now() - global_start
    minutes, seconds = divmod(int(elapsed.total_seconds()), 60)

    log("---------- Résumé ----------")
    for t, ok in results.items():
        log(f"  {t.upper():4s} : {'OK' if ok else 'ECHEC'}")
    log(f"Durée totale : {minutes}m {seconds:02d}s")
    log("########## Fin Sauvegarde ##########")

    # Code retour non nul si au moins un support a échoué (utile pour cron / alerting)
    if not all(results.values()):
        sys.exit(1)

if __name__ == "__main__":
    main()
