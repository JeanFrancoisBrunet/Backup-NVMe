#!/usr/bin/env python3
# =============================================================================
#  Programme de Sauvegarde du disk SSD - NVMe Samsung de 256 Go vers
#                       une Clé USB Bootable ou une Carte SD Bootable
#  Raspberry PI5 (16 Go RAM, SSD NVMe 256 Go, OS Bookworm)
#
#  Auteur  : Jean‑François BRUNET - JFBConseils - Avril 2026
# =============================================================================

import tkinter as tk
from tkinter import ttk, scrolledtext
from PIL import Image, ImageTk, ImageOps
import subprocess
import os
import shutil
import threading
from datetime import datetime

# ---------------------------------------------------------------------- #
# Configuration                                                          #
# ---------------------------------------------------------------------- #
DEST_FALLBACK    = "/media/usb"  # point de montage fallback clé USB
DEST_FALLBACK_SD = "/media/sd"   # point de montage fallback carte SD
ICON_PATH        = "/home/jfbrunet/Projects/Backup_NVMe/icons"

# Système de fichiers considérés comme ROOT Linux
EXT_FS = ("ext4", "ext3", "ext2")

# ---------------------------------------------------------------------- #
# APP                                                                    #
# ---------------------------------------------------------------------- #
class BackupApp(tk.Toplevel):
    def __init__(self, master=None):
        super().__init__(master)
        self.title("Backup NVMe / USB-SD")
        self.geometry("800x580")    # largeur x hauteur de la fenêtre
        self.resizable(False, False)

        self.mounted_boot = False
        self.mounted_dest = False
        self.dest         = None    # point de montage réel
        self._proc        = None    # processus rsync en cours (pour Annuler)
        self._cancelled   = False   # drapeau d'annulation

        # Cache lsblk : rempli une seule fois par opération de sauvegarde
        self._lsblk_cache = None

        # Variable de sélection du support cible
        self.target_var = tk.StringVar(value="usb")

        # ---------------- FENÊTRE ----------------
        header = tk.Frame(self)
        header.pack(pady=10, fill="x")

        # Image à gauche — resize proportionnel sans déformation
        img = Image.open(f"{ICON_PATH}/PI5_Hat_NVMe.png")
        img = ImageOps.contain(img, (250, 250))
        self.logo = ImageTk.PhotoImage(img)
        tk.Label(header, image=self.logo).pack(side="left", padx=10)

        # Titre + sélection support à droite
        right_frame = tk.Frame(header)
        right_frame.pack(side="left", padx=55, pady=10)

        tk.Label(right_frame,
                 text="Backup SSD-NVMe 256 GB",
                 font=("Helvetica", 16, "bold"),
                 anchor="w").pack(anchor="w")

        # ---------------- SÉLECTION SUPPORT ----------------
        select_frame = tk.LabelFrame(right_frame,
                                     text="Support de destination",
                                     font=("Helvetica", 10),
                                     padx=8, pady=6)
        select_frame.pack(anchor="w", pady=(8, 0), fill="x")

        tk.Radiobutton(select_frame,
                       text="  Clé USB",
                       variable=self.target_var,
                       value="usb",
                       font=("Helvetica", 11),
                       command=self._on_target_change).pack(side="left", padx=12)

        tk.Radiobutton(select_frame,
                       text="  Carte SD",
                       variable=self.target_var,
                       value="sd",
                       font=("Helvetica", 11),
                       command=self._on_target_change).pack(side="left", padx=12)

        # ---------------- BOUTONS ----------------
        btn_frame = tk.Frame(self)
        btn_frame.pack(pady=5)

        self.btn = tk.Button(btn_frame,
                             text="▶  Lancer Synchronisation vers Clé USB",
                             font=("Helvetica", 12), bg="#2e86de", fg="white",
                             padx=10, pady=8, command=self.start_backup)
        self.btn.pack(side="left", padx=6)

        self.btn_cancel = tk.Button(btn_frame,
                                    text="⏹  Annuler",
                                    font=("Helvetica", 12), bg="#c0392b", fg="white",
                                    padx=10, pady=8, command=self.cancel_backup,
                                    state="disabled")
        self.btn_cancel.pack(side="left", padx=6)

        # ---------------- PROGRESS BAR ----------------
        self.progress = ttk.Progressbar(self, mode="indeterminate", length=640)
        self.progress.pack(pady=10)

        # ---------------- LOG ----------------
        self.log = scrolledtext.ScrolledText(self, height=18,
                                             font=("Courier", 9),
                                             state="disabled")
        self.log.pack(padx=10, pady=5, fill="both", expand=True)

        self.status = tk.Label(self, text="Prêt", fg="gray")
        self.status.pack(pady=5)

    # ------------------------------------------------------------------ #
    #  Interface                                                         #
    # ------------------------------------------------------------------ #

    def _on_target_change(self):
        """Met à jour le libellé du bouton selon le support sélectionné."""
        if self.target_var.get() == "usb":
            self.btn.config(text="▶  Lancer Synchronisation vers Clé USB")
        else:
            self.btn.config(text="▶  Lancer Synchronisation vers Carte SD")

    def cancel_backup(self):
        """Interrompt proprement le rsync en cours."""
        self._cancelled = True
        if self._proc and self._proc.poll() is None:
            self.log_write("\n[INFO] Annulation demandée…\n")
            self._proc.terminate()

    # ------------------------------------------------------------------ #
    #  Utilitaires log                                                   #
    # ------------------------------------------------------------------ #

    def log_write(self, text):
        self.log.config(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.config(state="disabled")

    def explain_rsync_code(self, code, phase):
        if code == 0:
            return f"{phase} : OK (aucune anomalie détectée)\n"
        if code in (23, 24):
            return (f"{phase} : Terminé avec quelques fichiers ignorés "
                    f"(code {code}). Normal : fichiers temporaires, caches, logs.\n")
        return f"{phase} : Échec (code {code}) – voir détails ci‑dessus.\n"

    # ------------------------------------------------------------------ #
    #  Détection boot source                                             #
    # ------------------------------------------------------------------ #

    def detect_boot_source(self):
        if os.path.ismount("/boot/firmware"):
            return "/boot/firmware/"
        if os.path.ismount("/boot"):
            return "/boot/"
        if os.path.isdir("/boot/firmware"):
            return "/boot/firmware/"
        return "/boot/"

    # ------------------------------------------------------------------ #
    #  Cache lsblk — une seule exécution par opération                   #
    # ------------------------------------------------------------------ #

    def _refresh_lsblk_cache(self):
        try:
            out = subprocess.check_output(
                ["lsblk", "--output", "NAME,MOUNTPOINT,FSTYPE,TYPE,PKNAME,TRAN",
                 "--pairs", "--noheadings"],
                text=True
            )
            rows = []
            for line in out.strip().splitlines():
                # Chaque ligne ressemble à : NAME="sda" MOUNTPOINT="" FSTYPE="" TYPE="disk" PKNAME="" TRAN="usb"
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
            self._lsblk_cache = rows
        except Exception as e:
            self.log_write(f"[WARN] lsblk : {e}\n")
            self._lsblk_cache = []

    def get_lsblk_rows(self):
        if self._lsblk_cache is None:
            self._refresh_lsblk_cache()
        return self._lsblk_cache

    # ------------------------------------------------------------------ #
    #  Montage / démontage partition BOOT                                #
    # ------------------------------------------------------------------ #

    def find_usb_boot_partition(self):
        rows = self.get_lsblk_rows()
        root_part = None
        for (name, mnt, fstype, typ, pk, tran) in rows:
            if mnt == self.dest and typ == "part":
                root_part = (name, mnt, fstype, typ, pk, tran)
                break
        if not root_part:
            return ""
        parent = root_part[4]

        candidates = [n for (n, m, fs, t, pk, tr) in rows
                      if t == "part" and pk == parent
                      and fs.lower() in ("vfat", "fat32")]

        if not candidates:
            return ""

        # Priorité à la première partition (p1 ou 1), ordre alphanumérique ensuite
        candidates.sort(key=lambda n: (not (n.endswith("1") or n.endswith("p1")), n))
        return f"/dev/{candidates[0]}"

    def ensure_usb_boot_mounted(self, dest_boot_path):
        os.makedirs(dest_boot_path, exist_ok=True)
        if os.path.ismount(dest_boot_path):
            return True

        boot_dev = self.find_usb_boot_partition()
        if not boot_dev:
            self.log_write("[WARN] Partition BOOT introuvable.\n")
            return False

        self.log_write(f"Montage de {boot_dev} → {dest_boot_path}\n")
        proc = subprocess.run(["sudo", "-n", "mount", boot_dev, dest_boot_path],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if proc.returncode == 0:
            self.mounted_boot = True
            self.log_write("→ Partition BOOT montée.\n")
            return True

        self.log_write("[WARN] Impossible de monter automatiquement BOOT.\n")
        return False

    def safe_umount_boot(self, dest_boot_path):
        if self.mounted_boot and os.path.ismount(dest_boot_path):
            self.log_write("Démontage BOOT…\n")
            subprocess.run(["sudo", "-n", "umount", dest_boot_path])
            self.mounted_boot = False

    # ------------------------------------------------------------------ #
    #  Détection USB                                                     #
    # ------------------------------------------------------------------ #

    def find_usb_disks(self):
        """Disques USB = disques de type 'disk' dont le nom commence par 'sd'."""
        disks = set()
        for (name, mnt, fstype, typ, pk, tran) in self.get_lsblk_rows():
            if typ == "disk" and name.startswith("sd"):
                disks.add(name)
        return disks

    def find_already_mounted_usb(self):
        usb_disks = self.find_usb_disks()
        if not usb_disks:
            return "", ""
        for (name, mnt, fstype, typ, pk, tran) in self.get_lsblk_rows():
            if typ != "part" or pk not in usb_disks:
                continue
            if fstype.lower() not in EXT_FS:
                continue
            if mnt:
                return f"/dev/{name}", mnt
        return "", ""

    def find_usb_root_partition(self):
        usb_disks = self.find_usb_disks()
        if not usb_disks:
            return ""
        SYSTEM_MOUNTS = ("/", "/boot", "/boot/firmware")
        candidates = []
        for (name, mnt, fstype, typ, pk, tran) in self.get_lsblk_rows():
            if typ != "part" or pk not in usb_disks:
                continue
            if fstype.lower() not in EXT_FS:
                continue
            if mnt not in SYSTEM_MOUNTS:
                candidates.append(name)
        return f"/dev/{candidates[0]}" if candidates else ""

    # ------------------------------------------------------------------ #
    #  Détection Carte SD                                                #
    # ------------------------------------------------------------------ #

    def find_sd_disks(self):
        """Cartes SD = disques de type 'disk' dont le nom commence par 'mmcblk'."""
        disks = set()
        for (name, mnt, fstype, typ, pk, tran) in self.get_lsblk_rows():
            if typ == "disk" and name.startswith("mmcblk"):
                disks.add(name)
        return disks

    def find_already_mounted_sd(self):
        sd_disks = self.find_sd_disks()
        if not sd_disks:
            return "", ""
        for (name, mnt, fstype, typ, pk, tran) in self.get_lsblk_rows():
            if typ != "part" or pk not in sd_disks:
                continue
            if fstype.lower() not in EXT_FS:
                continue
            if mnt:
                return f"/dev/{name}", mnt
        return "", ""

    def find_sd_root_partition(self):
        sd_disks = self.find_sd_disks()
        if not sd_disks:
            return ""
        SYSTEM_MOUNTS = ("/", "/boot", "/boot/firmware")
        candidates = []
        for (name, mnt, fstype, typ, pk, tran) in self.get_lsblk_rows():
            if typ != "part" or pk not in sd_disks:
                continue
            if fstype.lower() not in EXT_FS:
                continue
            if mnt not in SYSTEM_MOUNTS:
                candidates.append(name)
        return f"/dev/{candidates[0]}" if candidates else ""

    # ------------------------------------------------------------------ #
    #  Résolution destination                                            #
    # ------------------------------------------------------------------ #

    def resolve_dest(self):
        target = self.target_var.get()

        if target == "usb":
            label           = "Clé USB"
            fallback        = DEST_FALLBACK
            find_mounted    = self.find_already_mounted_usb
            find_root_part  = self.find_usb_root_partition
        else:
            label           = "Carte SD"
            fallback        = DEST_FALLBACK_SD
            find_mounted    = self.find_already_mounted_sd
            find_root_part  = self.find_sd_root_partition

        dev, mnt = find_mounted()
        if mnt:
            self.log_write(f"{label} trouvée : {dev} montée sur {mnt}\n")
            return mnt

        root_dev = find_root_part()
        if not root_dev:
            return ""

        os.makedirs(fallback, exist_ok=True)
        self.log_write(f"{label} détectée : {root_dev} → montage sur {fallback}\n")
        proc = subprocess.run(
            ["sudo", "-n", "mount", root_dev, fallback],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        if proc.returncode == 0:
            self.mounted_dest = True
            self.log_write(f"→ {label} montée automatiquement.\n")
            return fallback
        self.log_write(f"[WARN] Impossible de monter {root_dev} : {proc.stderr.strip()}\n")
        return ""

    def safe_umount_dest(self):
        """Démonte le support seulement si c'est le programme qui l'a monté."""
        if self.mounted_dest and self.dest and os.path.ismount(self.dest):
            label = "Clé USB" if self.target_var.get() == "usb" else "Carte SD"
            self.log_write(f"Démontage {label} (ROOT)…\n")
            subprocess.run(["sudo", "-n", "umount", self.dest])
            self.mounted_dest = False

    # ------------------------------------------------------------------ #
    #  Flux principal                                                    #
    # ------------------------------------------------------------------ #

    def start_backup(self):
        target = self.target_var.get()
        label  = "Clé USB" if target == "usb" else "Carte SD"

        self._refresh_lsblk_cache()
        self._cancelled = False

        dest = self.resolve_dest()
        if not dest:
            self.on_error(f"{label} introuvable. Insérez-la et réessayez.")
            return
        self.dest = dest

        # Vérification capacité vs occupation réelle du NVMe (marge +5 %)
        _, src_used, _   = shutil.disk_usage("/")
        _, _, free       = shutil.disk_usage(self.dest)
        safety_ratio     = 1.05
        needed           = src_used * safety_ratio

        if free < needed:
            needed_gb = needed / (1024 ** 3)
            free_gb   = free   / (1024 ** 3)
            self.on_error(
                f"La {label} est trop petite "
                f"({free_gb:.2f} Go libres < {needed_gb:.2f} Go nécessaires avec marge +5 %)"
            )
            return

        self.log_write(f"Support    : {label}\n")
        self.log_write(f"Montage    : {self.dest}\n")
        self.log_write(f"Espace lib : {free / (1024 ** 3):.2f} Go\n")

        self.btn.config(state="disabled")
        self.btn_cancel.config(state="normal")
        self.progress.start(10)
        self.status.config(text=f"Synchronisation vers {label}…", fg="orange")

        threading.Thread(target=self.run_backup, daemon=True).start()

    def run_backup(self):
        dest_boot_path = os.path.join(self.dest, "boot")
        start_time     = datetime.now()

        try:
            boot_src = self.detect_boot_source()
            self.after(0, self.log_write,
                       f"--- Début : {start_time.strftime('%d/%m/%Y %H:%M:%S')} ---\n")
            self.after(0, self.log_write, f"BOOT source : {boot_src}\n")

            # ---- Rsync ROOT ----
            rsync_root = [
                "sudo", "rsync", "-aAXv", "--delete-after",
                f"--exclude={self.dest}/*",
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
                "--exclude=/home/jfbrunet/.cache/*",
                "--exclude=/var/log/*",
                "/", self.dest + "/"
            ]

            self.after(0, self.log_write, "\nSynchronisation ROOT…\n")
            self._proc = subprocess.Popen(rsync_root, stdout=subprocess.PIPE,
                                          stderr=subprocess.STDOUT, text=True)
            for line in self._proc.stdout:
                if self._cancelled:
                    break
                self.after(0, self.log_write, line)

            ret1 = self._proc.wait()

            if self._cancelled:
                self.after(0, self.on_cancelled)
                return

            self.after(0, self.log_write, self.explain_rsync_code(ret1, "Rsync ROOT"))
            if ret1 not in (0, 23, 24):
                self.after(0, self.on_error,
                           f"Rsync ROOT a rencontré une erreur (code {ret1}).")
                return

            # ---- Montage partition BOOT ----
            self.ensure_usb_boot_mounted(dest_boot_path)

            # ---- Rsync BOOT ----
            rsync_boot = [
                "sudo", "rsync", "-rtv", "--delete-after",
                "--no-perms", "--no-owner", "--no-group",
                boot_src, dest_boot_path + "/"
            ]

            self.after(0, self.log_write, "\nSynchronisation BOOT…\n")
            self._proc = subprocess.Popen(rsync_boot, stdout=subprocess.PIPE,
                                          stderr=subprocess.STDOUT, text=True)
            for line in self._proc.stdout:
                if self._cancelled:
                    break
                self.after(0, self.log_write, line)

            ret2 = self._proc.wait()

            if self._cancelled:
                self.after(0, self.on_cancelled)
                return

            self.after(0, self.log_write, self.explain_rsync_code(ret2, "Rsync BOOT"))
            if ret2 not in (0, 23, 24):
                self.after(0, self.on_error,
                           f"Rsync BOOT a rencontré une erreur (code {ret2}).")
                return

            elapsed = datetime.now() - start_time
            self.after(0, self.on_success, elapsed)

        except Exception as e:
            self.after(0, self.on_error, str(e))
        finally:
            self._proc = None
            self.safe_umount_boot(dest_boot_path)
            self.safe_umount_dest()
            self.after(0, self.btn_cancel.config, {"state": "disabled"})

    # ------------------------------------------------------------------ #
    #  Callbacks résultat                                                #
    # ------------------------------------------------------------------ #

    def on_success(self, elapsed):
        self.progress.stop()
        end_time = datetime.now()
        minutes, seconds = divmod(int(elapsed.total_seconds()), 60)
        self.status.config(text="✅ Sauvegarde terminée avec succès", fg="green")
        self.btn.config(state="normal")
        self.log_write(
            f"--- Terminé : {end_time.strftime('%d/%m/%Y %H:%M:%S')} "
            f"(durée : {minutes}m {seconds:02d}s) ---\n"
        )

    def on_cancelled(self):
        self.progress.stop()
        self.status.config(text="⚠️ Sauvegarde annulée", fg="darkorange")
        self.btn.config(state="normal")
        self.log_write("\n--- Sauvegarde annulée par l'utilisateur ---\n")

    def on_error(self, err):
        self.progress.stop()
        self.status.config(text=f"❌ Erreur : {err}", fg="red")
        self.btn.config(state="normal")
        self.log_write(f"\n--- ERREUR : {err} ---\n")

# ============================================================
#  Entrée du programme
# ============================================================
if __name__ == "__main__":

    root = tk.Tk()
    root.withdraw()

    # --- Splash screen ---
    splash = tk.Toplevel(root)
    splash.title("Démarrage…")
    splash.resizable(False, False)
    splash.attributes("-topmost", True)

    splash_img = ImageTk.PhotoImage(
        ImageOps.contain(Image.open(f"{ICON_PATH}/Disk.png"), (350, 250))
    )

    tk.Label(splash, image=splash_img).pack(pady=10)
    tk.Label(splash, text="Initialisation de l'application…",
             font=("Helvetica", 12)).pack()

    splash.update_idletasks()
    w = splash.winfo_width()
    h = splash.winfo_height()
    x = (splash.winfo_screenwidth()  - w) // 2
    y = (splash.winfo_screenheight() - h) // 2
    splash.geometry(f"{w}x{h}+{x}+{y}")

    def show_main():
        splash.destroy()
        BackupApp(master=root)

    root.after(2000, show_main)
    root.mainloop()
