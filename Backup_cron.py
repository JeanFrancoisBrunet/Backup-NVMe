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
#     python3 Backup_cron.py --test-telegram -> envoie un message de test Telegram
#
#  Cron hebdomadaire, tous les lundis à 12h00 (Backup NVMe vers USB puis SD, l'un après l'autre) :
#     0 12 * * 1 /usr/bin/python3 /home/jfbrunet/Projects/Backup_NVMe/Backup_cron.py >> /home/jfbrunet/Projects/Backup_NVMe/backup_cron.log 2>&1
# 
#  Lancement manuel par :    python3 /home/jfbrunet/Projects/Backup_NVMe/Backup_cron.py 2>&1 | tee /home/jfbrunet/Projects/Backup_NVMe/backup_cron.log
#  Lecture du fichier log :  cat /home/jfbrunet/Projects/Backup_NVMe/backup_cron.log
#
#  Bootabilité : après les rsync, le script réécrit les PARTUUID du NVMe par ceux
#  du support dans /etc/fstab et boot/cmdline.txt de la copie (sinon la copie
#  pointerait vers la partition du NVMe et ne démarrerait pas seule).
#
#  Notifications Telegram : à la fin, un compte rendu (✅ / ⚠️ / ❌) est envoyé via
#  le même bot et le même fichier ~/.telegram_config que telegram_bot_ctrl_pi5.py
#  (clés token_ctrl et chat_id). Les échecs et anomalies sont toujours envoyés ;
#  le message de succès dépend de NOTIFY_ON_SUCCESS. Le bot n'a pas besoin de
#  tourner : le script appelle directement l'API Telegram.
#
# =============================================================================

import subprocess
import os
import re
import shutil
import sys
import argparse
import urllib.request
import urllib.parse
from pathlib import Path
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

# ---- Notifications Telegram ----
NOTIFY_TELEGRAM   = True    # False = aucune notification
NOTIFY_ON_SUCCESS = True    # True = message aussi quand tout va bien (utile pour savoir que la sauvegarde a tourné)
CONFIG_FILE       = Path.home() / ".telegram_config"
TOKEN_KEY         = "token_ctrl"   # même clé que telegram_bot_ctrl_pi5.py

ANOMALIES       = []   # messages [WARN] / ERREUR / ECHEC collectés pendant l'exécution
STATS           = {}   # résultats par support : durée, occupation...
_current_target = ""   # support en cours de traitement (pour étiqueter les anomalies)

def log(msg):
    """Affiche un message horodaté (capturé par cron dans le fichier log)
    et mémorise les anomalies pour le compte rendu Telegram."""
    print(f"[{datetime.now().strftime('%d/%m/%Y %H:%M:%S')}] {msg}", flush=True)
    if msg.startswith("[WARN") or msg.startswith("ERREUR") or "ECHEC (code" in msg:
        ANOMALIES.append(f"[{_current_target.upper() or 'GLOBAL'}] {msg.strip()}")

def _read_telegram_config():
    """Lit token et chat_id dans ~/.telegram_config (lignes clé=valeur).
    Renvoie (token, chat_id, erreur)."""
    token = chat_id = ""
    try:
        for line in CONFIG_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k == TOKEN_KEY:
                token = v
            elif k == "chat_id":
                chat_id = v
    except Exception as e:
        return "", "", f"lecture de {CONFIG_FILE} impossible : {e}"
    if not token or not chat_id:
        return "", "", f"{TOKEN_KEY} ou chat_id absent de {CONFIG_FILE}"
    return token, chat_id, ""

def send_telegram(text):
    """Envoie 'text' au chat Telegram. Renvoie (ok, info). Ne lève jamais d'exception
    et n'affiche jamais le token."""
    token, chat_id, err = _read_telegram_config()
    if err:
        return False, err
    try:
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": text[:4000]}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
        with urllib.request.urlopen(req, timeout=15) as r:
            return (r.status == 200), f"HTTP {r.status}"
    except Exception as e:
        # on n'affiche que le type d'erreur : l'URL contient le token
        code = getattr(e, "code", "")
        return False, f"{type(e).__name__} {code}".strip()

def verify_boot_files(dest_boot_path):
    """Vérifie que la partition BOOT de la copie contient bien les fichiers indispensables.
    Renvoie la liste des fichiers manquants."""
    needed = ("kernel_2712.img", "config.txt", "cmdline.txt")
    return [f for f in needed if not os.path.isfile(os.path.join(dest_boot_path, f))]

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
# Journaux (log2ram) : copie du disque du NVMe vers /var/log de la copie #
# ---------------------------------------------------------------------- #
LOG_DISK_SRC = "/var/hdd.log"   # copie sur disque des journaux (log2ram)

def sync_logs(dest):
    """
    Avec log2ram, /var/log est en mémoire et sa copie persistante est /var/hdd.log.
    Au démarrage de la copie, log2ram relit le /var/log DISQUE de la copie : on y
    recopie donc /var/hdd.log du NVMe (sinon exim4, logrotate... échouent).
    Renvoie True si OK ou sans objet.
    """
    if subprocess.run(["findmnt", "-n", LOG_DISK_SRC],
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0:
        log(f"Journaux : {LOG_DISK_SRC} absent (log2ram inactif), étape ignorée.")
        return True
    dst = os.path.join(dest, "var", "log")
    if not os.path.isdir(dst):
        log(f"[WARN] Journaux : {dst} introuvable, étape ignorée.")
        return False
    proc = subprocess.run(["sudo", "rsync", "-aAX", "--delete", LOG_DISK_SRC + "/", dst + "/"],
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if proc.returncode in (0, 23, 24):
        log(f"Journaux : {LOG_DISK_SRC} -> {dst} : OK")
        return True
    log(f"[WARN] Journaux : échec (code {proc.returncode}) {proc.stdout.strip()}")
    return False

# ---------------------------------------------------------------------- #
# Bootabilité : PARTUUID de la copie                                     #
# ---------------------------------------------------------------------- #
def _findmnt_source(path):
    """Périphérique (ex. /dev/sda2) monté sur 'path' ou contenant 'path', sinon ''."""
    try:
        return subprocess.check_output(
            ["findmnt", "-n", "-o", "SOURCE", "--target", path], text=True).strip()
    except Exception:
        return ""

def _partuuid(dev):
    """PARTUUID d'une partition (via lsblk), sinon ''."""
    if not dev:
        return ""
    try:
        return subprocess.check_output(
            ["lsblk", "-no", "PARTUUID", dev], text=True).strip()
    except Exception:
        return ""

def _partnum(dev):
    """Numéro de partition (/dev/sda2 -> 2, /dev/mmcblk0p2 -> 2)."""
    m = re.search(r"(\d+)$", dev or "")
    return int(m.group(1)) if m else None

def fix_boot_identifiers(dest, dest_boot_path, boot_src):
    """
    Après les rsync, /etc/fstab et boot/cmdline.txt de la copie contiennent les
    PARTUUID du NVMe. On les remplace par ceux du support (clé USB / carte SD)
    pour que la copie puisse démarrer seule. Renvoie True si tout est correct.
    """
    if not os.path.ismount(dest_boot_path):
        log("[WARN] Partition BOOT non montée : correction des PARTUUID impossible.")
        return False

    # PARTUUID de la source (NVMe en cours d'utilisation)
    old_root = _partuuid(_findmnt_source("/"))
    old_boot = _partuuid(_findmnt_source(boot_src.rstrip("/") or "/"))

    # PARTUUID de la destination
    dst_root_dev = _findmnt_source(dest)
    dst_boot_dev = _findmnt_source(dest_boot_path)
    new_boot = _partuuid(dst_boot_dev)
    new_root = _partuuid(dst_root_dev)

    # Table MBR : PARTUUID = "<signature disque>-<n° partition>". Si lsblk n'en
    # renvoie pas pour la racine (ex. partition tronquée de la carte SD), on le
    # déduit de celui de la partition BOOT du même disque.
    if not new_root and new_boot and "-" in new_boot:
        n = _partnum(dst_root_dev)
        if n:
            new_root = f"{new_boot.rsplit('-', 1)[0]}-{n:02d}"
            log(f"PARTUUID racine déduit de celui du BOOT : {new_root}")

    if not (old_root and old_boot and new_root and new_boot):
        log(f"[WARN] PARTUUID introuvables (source {old_root or '?'}/{old_boot or '?'}, "
            f"destination {new_root or '?'}/{new_boot or '?'}) : fstab et cmdline.txt non modifiés.")
        return False

    mapping = {
        f"PARTUUID={old_root}": f"PARTUUID={new_root}",
        f"PARTUUID={old_boot}": f"PARTUUID={new_boot}",
    }
    targets = [os.path.join(dest, "etc", "fstab"),
               os.path.join(dest_boot_path, "cmdline.txt")]

    ok = True
    for path in targets:
        try:
            with open(path, encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            log(f"[WARN] Lecture de {path} impossible : {e}")
            ok = False
            continue

        new_content = content
        for old, new in mapping.items():
            new_content = new_content.replace(old, new)

        if new_content == content:
            log(f"{path} : identifiants déjà corrects")
            continue

        proc = subprocess.run(["sudo", "-n", "tee", path], input=new_content,
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        if proc.returncode == 0:
            log(f"{path} : PARTUUID corrigés ({old_root} -> {new_root})")
        else:
            log(f"[WARN] Écriture de {path} impossible : {proc.stderr.strip()}")
            ok = False

    # Vérification après écriture : plus aucun PARTUUID du NVMe, et celui de la copie présent
    for path in targets:
        try:
            with open(path, encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            log(f"[WARN] Vérification de {path} impossible : {e}")
            ok = False
            continue
        if f"PARTUUID={old_root}" in content or f"PARTUUID={old_boot}" in content:
            log(f"[WARN] {path} contient encore un PARTUUID du NVMe : la copie ne démarrera pas seule.")
            ok = False
        elif f"PARTUUID={new_root}" not in content and f"PARTUUID={new_boot}" not in content:
            log(f"[WARN] {path} ne contient aucun PARTUUID de la copie ({new_root}/{new_boot}).")
            ok = False
    return ok

# ---------------------------------------------------------------------- #
# Sauvegarde d'un seul support (USB ou SD)                               #
# Renvoie True si succès, False si échec (n'interrompt jamais le reste)  #
# ---------------------------------------------------------------------- #
def backup_one(target):
    global _current_target
    _current_target = target
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

        # ---- Journaux persistants (log2ram) ----
        log("Synchronisation JOURNAUX...")
        sync_logs(dest)

        # ---- Correction des PARTUUID (fstab + cmdline.txt de la copie) ----
        log("Correction des identifiants de démarrage (fstab, cmdline.txt)...")
        if not fix_boot_identifiers(dest, dest_boot_path, boot_src):
            log(f"[WARN] {label} : identifiants non corrigés, la copie risque de ne pas démarrer seule.")

        # ---- Contrôle de la partition BOOT de la copie ----
        missing = verify_boot_files(dest_boot_path)
        if missing:
            log(f"[WARN] {label} : fichiers de démarrage manquants dans la copie : {', '.join(missing)}")

        # Occupation du support après la sauvegarde
        dst_used_go, dst_total_go, dst_pct = usage_pct(dest)
        log(f"Occupation {label} (après backup)  : {dst_pct:.1f}% ({dst_used_go:.1f} Go / {dst_total_go:.1f} Go)")
        if dst_pct >= WARN_THRESHOLD_PCT:
            log(f"[WARNING] {label} occupée à {dst_pct:.1f}% (seuil d'alerte : {WARN_THRESHOLD_PCT}%). "
                f"Pensez à surveiller l'espace disponible ou à prévoir un support plus grand.")

        elapsed = datetime.now() - start_time
        minutes, seconds = divmod(int(elapsed.total_seconds()), 60)
        STATS[target] = {"duree": f"{minutes}m {seconds:02d}s", "pct": dst_pct,
                         "used": dst_used_go, "total": dst_total_go}
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
    parser.add_argument("--test-telegram", action="store_true",
                        help="Envoie un message de test Telegram puis quitte")
    args = parser.parse_args()

    if args.test_telegram:
        ok, info = send_telegram("🧪 Test Backup_cron.py : les notifications Telegram fonctionnent.")
        print("Telegram :", "message envoyé" if ok else f"ÉCHEC ({info})")
        sys.exit(0 if ok else 1)

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

    # ---- Compte rendu Telegram ----
    failed = [t for t, ok in results.items() if not ok]
    if NOTIFY_TELEGRAM and (failed or ANOMALIES or NOTIFY_ON_SUCCESS):
        icon = "❌" if failed else ("⚠️" if ANOMALIES else "✅")
        title = ("ÉCHEC" if failed else ("terminée avec anomalies" if ANOMALIES else "terminée"))
        lines = [f"{icon} Sauvegarde NVMe {title}",
                 f"🕒 {global_start.strftime('%d/%m/%Y %H:%M')} — durée {minutes}m {seconds:02d}s"]
        for t, ok in results.items():
            if ok:
                st = STATS.get(t, {})
                extra = f" — {st['duree']}, support occupé à {st['pct']:.0f}%" if st else ""
                lines.append(f"• {t.upper()} : OK{extra}")
            else:
                lines.append(f"• {t.upper()} : ❌ ÉCHEC")
        if ANOMALIES:
            lines.append("")
            lines.append(f"Anomalies ({len(ANOMALIES)}) :")
            lines += [f"- {a[:300]}" for a in ANOMALIES[:10]]
            if len(ANOMALIES) > 10:
                lines.append(f"… et {len(ANOMALIES) - 10} autre(s), voir backup_cron.log")
        ok, info = send_telegram("\n".join(lines))
        log(f"Telegram : {'notification envoyée' if ok else f'ÉCHEC envoi ({info})'}")

    # Code retour non nul si au moins un support a échoué (utile pour cron / alerting)
    if failed:
        sys.exit(1)

if __name__ == "__main__":
    main()
