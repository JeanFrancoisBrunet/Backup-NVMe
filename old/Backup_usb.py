#!/usr/bin/env python3
# =========================================================================================
#  Programme de Sauvegarde du disk SSD - NVMe Samsung de 256 Go vers une Clé USB Bootable
#  Raspberry PI5 (16 Go RAM, SSD NVMe 256 Go, OS Bookworm)
#     
#  Auteur  : Jean‑François BRUNET - JFBConseils - Mars 2026
# =========================================================================================

import tkinter as tk
from tkinter import ttk, scrolledtext
from PIL import Image, ImageTk
import subprocess
import os
import shutil
import threading
from datetime import datetime

# ----------------------------------------
# Configuration
# ----------------------------------------
DEST = "/media/usb"   # point de montage de la partition root de l’USB
ICON_PATH = "/home/jfbrunet/Projects/Backup_NVMe/icons"

# ----------------------------------------
# APP
# ----------------------------------------
class BackupApp(tk.Toplevel):
    def __init__(self, master=None):
        super().__init__(master)
        self.title("Backup NVMe / USB")
        self.geometry("720x520")
        self.resizable(False, False)

        self.mounted_boot = False
        self.mounted_dest = False
        self.dest = None   # point de montage réel

        # ---------------- FENÊTRE ----------------
        header = tk.Frame(self)
        header.pack(pady=10, fill="x")

        # Image à gauche
        img = Image.open(f"{ICON_PATH}/PI5_Hat_NVMe.png")
        img = img.resize((120, 120))
        self.logo = ImageTk.PhotoImage(img)
        tk.Label(header, image=self.logo).pack(side="left", padx=10)

        # Titre à droite
        tk.Label(header,
                text="Backup SSD-NVMe 256 GB sur Clé USB",
                font=("Helvetica", 16, "bold"),
                anchor="w").pack(side="left", padx=10, pady=40)

        # ---------------- BOUTON ----------------
        self.btn = tk.Button(self, text="▶  Lancer la synchronisation",
                             font=("Helvetica", 12), bg="#2e86de", fg="white",
                             padx=10, pady=8, command=self.start_backup)
        self.btn.pack(pady=5)

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

    # ---------------- UTILITAIRES ----------------
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

    def detect_boot_source(self):
        if os.path.ismount("/boot/firmware"):
            return "/boot/firmware/"
        if os.path.ismount("/boot"):
            return "/boot/"
        if os.path.isdir("/boot/firmware"):
            return "/boot/firmware/"
        return "/boot/"

    def get_lsblk_rows(self):
        try:
            out = subprocess.check_output(
                ["lsblk", "-rno", "NAME,MOUNTPOINT,FSTYPE,TYPE,PKNAME"], text=True
            )
            rows = []
            for line in out.strip().splitlines():
                parts = line.split()
                while len(parts) < 5:
                    parts.append('')
                rows.append(tuple(parts[:5]))
            return rows
        except:
            return []

    def find_usb_boot_partition(self):
        rows = self.get_lsblk_rows()
        root_part = None
        for (name, mnt, fstype, typ, pk) in rows:
            if mnt == self.dest and typ == "part":
                root_part = (name, mnt, fstype, typ, pk)
                break
        if not root_part:
            return ""
        parent = root_part[4]

        candidates = [n for (n, m, fs, t, pk) in rows
                      if t == "part" and pk == parent and fs.lower() in ("vfat", "fat32")]

        if not candidates:
            return ""

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
            self.log_write("Démontage BOOT USB…\n")
            subprocess.run(["sudo", "-n", "umount", dest_boot_path])

    def find_usb_disks(self):
        """Retourne les noms de disques USB (lsblk TRAN=usb)."""
        try:
            out = subprocess.check_output(["lsblk", "-rno", "NAME,TRAN,TYPE"], text=True)
            disks = set()
            for line in out.strip().splitlines():
                p = line.split()
                if len(p) >= 3 and p[1] == "usb" and p[2] == "disk":
                    disks.add(p[0])
            return disks
        except Exception as e:
            self.log_write(f"[WARN] find_usb_disks : {e}\n")
            return set()

    def find_already_mounted_usb(self):
        """Cherche une partition ext4 USB déjà montée (udisks2, systemd, manuel).
        Retourne (device, mountpoint) ou ('', '')."""
        usb_disks = self.find_usb_disks()
        if not usb_disks:
            return "", ""
        for (name, mnt, fstype, typ, pk) in self.get_lsblk_rows():
            if typ != "part" or pk not in usb_disks:
                continue
            if fstype.lower() not in ("ext4", "ext3", "ext2"):
                continue
            if mnt:
                return f"/dev/{name}", mnt
        return "", ""

    def find_usb_root_partition(self):
        """Cherche une partition ext4 USB non encore montée."""
        usb_disks = self.find_usb_disks()
        if not usb_disks:
            return ""
        candidates = []
        for (name, mnt, fstype, typ, pk) in self.get_lsblk_rows():
            if typ != "part" or pk not in usb_disks:
                continue
            if fstype.lower() not in ("ext4", "ext3", "ext2"):
                continue
            if mnt == "":
                candidates.append(name)
        return f"/dev/{candidates[0]}" if candidates else ""

    def resolve_dest(self):
        """Détermine le point de montage réel de la clé USB.
        Priorité :
          1. Clé déjà montée (udisks2 /media/jfbrunet/XX, systemd /media/usb…)
          2. Clé présente mais non montée → montage sur DEST_FALLBACK.
        Retourne le chemin de montage ou '' si aucune clé trouvée."""
        dev, mnt = self.find_already_mounted_usb()
        if mnt:
            self.log_write(f"Clé USB trouvée : {dev} montée sur {mnt}\n")
            return mnt
        root_dev = self.find_usb_root_partition()
        if not root_dev:
            return ""
        os.makedirs(DEST_FALLBACK, exist_ok=True)
        self.log_write(f"Clé USB détectée : {root_dev} → montage sur {DEST_FALLBACK}\n")
        proc = subprocess.run(
            ["sudo", "-n", "mount", root_dev, DEST_FALLBACK],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        if proc.returncode == 0:
            self.mounted_dest = True
            self.log_write("→ Clé USB montée automatiquement.\n")
            return DEST_FALLBACK
        self.log_write(f"[WARN] Impossible de monter {root_dev} : {proc.stderr.strip()}\n")
        return ""

    def safe_umount_dest(self):
        """Démonte la clé seulement si c'est le programme qui l'a montée."""
        if self.mounted_dest and self.dest and os.path.ismount(self.dest):
            self.log_write("Démontage USB (ROOT)…\n")
            subprocess.run(["sudo", "-n", "umount", self.dest])
            self.mounted_dest = False

    # ---------------- FLUX PRINCIPAL ----------------
    def start_backup(self):
        # Résolution dynamique du point de montage USB
        dest = self.resolve_dest()
        if not dest:
            self.on_error("Clé USB introuvable. Branchez-la et réessayez.")
            return
        self.dest = dest

        # Espace disponible sur la clé
        total, used, free = shutil.disk_usage(self.dest)

        # Vérification capacité USB vs occupation réelle du NVMe
        src_total, src_used, src_free = shutil.disk_usage("/")
        safety_ratio = 1.05  # marge +5%
        if free < src_used * safety_ratio:
            needed_gb = (src_used * safety_ratio) / (1024 ** 3)
            free_gb = free / (1024 ** 3)
            self.on_error(
                f"La clé USB est trop petite "
                f"({free_gb:.2f} Go libres < {needed_gb:.2f} Go nécessaires avec marge)"
            )
            return

        # Vérification minimale de 1 Go
        if free < 1 * 1024 * 1024 * 1024:
            self.on_error("Moins de 1 Go libre sur l'USB")
            return

        self.log_write(f"Point de montage USB : {self.dest}\n")
        self.log_write(f"Espace libre : {free / (1024 ** 3):.2f} Go\n")

        self.btn.config(state="disabled")
        self.progress.start(10)
        self.status.config(text="Synchronisation…", fg="orange")

        threading.Thread(target=self.run_backup, daemon=True).start()

    def run_backup(self):
        dest_boot_path = os.path.join(self.dest, "boot")
        try:
            boot_src = self.detect_boot_source()
            self.after(0, self.log_write, f"BOOT source : {boot_src}\n")

            # RSync ROOT
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

            self.after(0, self.log_write, "Synchronisation ROOT…\n")
            proc1 = subprocess.Popen(rsync_root, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True)
            for line in proc1.stdout:
                self.after(0, self.log_write, line)

            ret1 = proc1.wait()
            self.after(0, self.log_write, self.explain_rsync_code(ret1, "Rsync ROOT"))

            if ret1 not in (0, 23, 24):
                self.after(0, self.on_error,
                           f"Rsync ROOT a rencontré une erreur (code {ret1}).")
                return

            # Monter Partition Boot
            mounted = self.ensure_usb_boot_mounted(dest_boot_path)

            # RSync BOOT
            rsync_boot = [
                "sudo", "rsync", "-rtv", "--delete-after",
                "--no-perms", "--no-owner", "--no-group",
                boot_src, dest_boot_path + "/"
            ]

            self.after(0, self.log_write, "\nSynchronisation BOOT…\n")
            proc2 = subprocess.Popen(rsync_boot, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True)
            for line in proc2.stdout:
                self.after(0, self.log_write, line)

            ret2 = proc2.wait()
            self.after(0, self.log_write, self.explain_rsync_code(ret2, "Rsync BOOT"))

            if ret2 not in (0, 23, 24):
                self.after(0, self.on_error,
                           f"Rsync BOOT a rencontré une erreur (code {ret2}).")
                return

            self.after(0, self.on_success)

        except Exception as e:
            self.after(0, self.on_error, str(e))
        finally:
            self.safe_umount_boot(dest_boot_path)
            self.safe_umount_dest()

    def on_success(self):
        self.progress.stop()
        self.status.config(text="✅ Sauvegarde terminée avec succès", fg="green")
        self.btn.config(state="normal")
        self.log_write(f"--- Terminé : {datetime.now().strftime('%d/%m/%Y %H:%M:%S')} ---\n")

    def on_error(self, err):
        self.progress.stop()
        self.status.config(text=f"❌ Erreur : {err}", fg="red")
        self.btn.config(state="normal")
        self.log_write(f"\n--- ERREUR : {err} ---\n")

if __name__ == "__main__":

    root = tk.Tk()
    root.withdraw()  # ...

    # --- Splash screen ---
    splash = tk.Toplevel(root)
    splash.title("Démarrage…")
    splash.resizable(False, False)
    splash.attributes("-topmost", True)

    splash_img = ImageTk.PhotoImage(
        Image.open(f"{ICON_PATH}/Disk.png").resize((300, 200))
    )

    tk.Label(splash, image=splash_img).pack(pady=10)
    tk.Label(splash, text="Initialisation de l'application…",
             font=("Helvetica", 12)).pack()

    splash.update_idletasks()
    w = splash.winfo_width()
    h = splash.winfo_height()
    x = (splash.winfo_screenwidth() - w) // 2
    y = (splash.winfo_screenheight() - h) // 2
    splash.geometry(f"{w}x{h}+{x}+{y}")

    def show_main():
        splash.destroy()
        BackupApp(master=root)

    root.after(2000, show_main)
    root.mainloop()
