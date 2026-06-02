"""
Vision Master — .solw vs Master Comparator

Compares parameters of a working .solw file against a Master reference file
and shows the differences in a clean dark-themed UI.

Re-uses the one-pass parser from solw_viewer.py.
"""

from __future__ import annotations

import os 
import re
import subprocess
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk

from solw_viewer import load_solw, Solution, categorize


IMG_EXTS = (".jpg")


def _looks_like_image_path(text: str) -> bool:
    return isinstance(text, str) and text.lower().endswith(IMG_EXTS)


def _open_with_os(path: str) -> None:
    """Open file with the OS's default viewer."""
    if not os.path.isfile(path):
        messagebox.showwarning("ไม่พบไฟล์", f"ไฟล์ไม่อยู่:\n{path}")
        return
    try:
        os.startfile(path)            # Windows
    except AttributeError:
        subprocess.Popen(["xdg-open", path])     # Linux fallback


# ── Colour palette (matches read_solw.py vibe, just a bit richer) ────────────
BG       = "#2b2b2b"
BG_HEAD  = "#1e1e1e"
BG_CARD  = "#3c3c3c"
BG_ROW   = "#1e1e1e"
FG       = "#e0e0e0"
FG_DIM   = "#aaaaaa"
ACCENT   = "#0078d4"
GOOD     = "#4ec94e"
WARN     = "#e0af68"
BAD      = "#f44747"
INFO     = "#4ec9b0"


# ── Diff engine ──────────────────────────────────────────────────────────────
def diff_solutions(test: Solution, master: Solution) -> list[dict]:
    """
    Walk every module that exists in either solution and pair its parameters
    by name. Each output row is ready to drop into the table.

    status:
        "same"        — identical value
        "diff"        — both have the key but values differ
        "only_test"   — exists in test but not in master
        "only_master" — exists in master but not in test
        "missing_mod" — whole module missing on one side
    """
    rows: list[dict] = []

    # Index modules on each side by display name so we can pair them.
    t_mods = {m.display_name: m for m in test.modules}
    m_mods = {m.display_name: m for m in master.modules}

    all_module_names = sorted(set(t_mods) | set(m_mods))

    for mod_name in all_module_names:
        t_mod = t_mods.get(mod_name)
        m_mod = m_mods.get(mod_name)

        if t_mod is None or m_mod is None:
            rows.append({
                "module":   mod_name,
                "category": "—",
                "param":    "(module missing)",
                "test":     "✓ present" if t_mod else "✗ absent",
                "master":   "✓ present" if m_mod else "✗ absent",
                "status":   "missing_mod",
            })
            continue

        all_keys = sorted(set(t_mod.params) | set(m_mod.params))
        for key in all_keys:
            t_val = t_mod.params.get(key)
            m_val = m_mod.params.get(key)

            if t_val is None:
                status = "only_master"
            elif m_val is None:
                status = "only_test"
            elif t_val == m_val:
                status = "same"
            else:
                status = "diff"

            rows.append({
                "module":   mod_name,
                "category": categorize(key),
                "param":    key,
                "test":     t_val if t_val is not None else "—",
                "master":   m_val if m_val is not None else "—",
                "status":   status,
            })

    return rows


def get_image_source_path(sol: Solution | None) -> str:
    """Pick the most useful image path from the solution's Image Source module."""
    if sol is None:
        return ""
    img_mod = next((m for m in sol.modules if "Image" in m.display_name), None)
    if not img_mod:
        return ""
    return (img_mod.params.get("CurrentImagePath", "")
            or img_mod.params.get("SubscribeFolderPath", ""))


# ── UI ───────────────────────────────────────────────────────────────────────
class CompareApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Vision Master — Solution vs Master Comparator")
        self.geometry("1100x650")
        self.minsize(900, 500)
        self.configure(bg=BG)

        self.test_sol: Solution | None = None
        self.master_sol: Solution | None = None
        self._rows: list[dict] = []

        self._build_ui()

        # Auto-fill defaults if files exist
        default_test = r"D:\Project1\Test.solw"
        default_master = r"D:\Project1\Master.solw"
        if os.path.isfile(default_test):
            self.test_path.set(default_test)
        if os.path.isfile(default_master):
            self.master_path.set(default_master)
        if os.path.isfile(default_test) and os.path.isfile(default_master):
            self.after(150, self._compare)

    # ── Layout ──────────────────────────────────────────────────────────────
    def _build_ui(self):
        # Header
        hdr = tk.Frame(self, bg=BG_HEAD, pady=12)
        hdr.pack(fill="x")
        tk.Label(hdr, text="Vision Master  |  Solution vs Master Comparator",
                 font=("Segoe UI", 14, "bold"), bg=BG_HEAD, fg=FG
                 ).pack(side="left", padx=18)
        self.meta_lbl = tk.Label(hdr, text="", bg=BG_HEAD, fg=FG_DIM,
                                  font=("Consolas", 9))
        self.meta_lbl.pack(side="right", padx=18)

        # File rows
        files = tk.Frame(self, bg=BG, pady=10)
        files.pack(fill="x", padx=15)

        self.test_path   = tk.StringVar()
        self.master_path = tk.StringVar()

        self._mk_file_row(files, "Test file:",   self.test_path,   row=0, side="Test")
        self._mk_file_row(files, "Master file:", self.master_path, row=1, side="Master")
        files.columnconfigure(1, weight=1)

        # Action bar
        act = tk.Frame(self, bg=BG)
        act.pack(fill="x", padx=15, pady=(0, 8))

        tk.Button(act, text="เปรียบเทียบ", command=self._compare,
                  bg=ACCENT, fg="white", font=("Segoe UI", 10, "bold"),
                  relief="flat", padx=20, pady=6, cursor="hand2"
                  ).pack(side="left")

        # Filter chips
        self.filter_var = tk.StringVar(value="diff_only")
        tk.Label(act, text="   แสดง:", bg=BG, fg=FG_DIM,
                 font=("Segoe UI", 9)).pack(side="left", padx=(15, 6))
        for label, value in (("ต่างเท่านั้น", "diff_only"),
                              ("ทั้งหมด", "all"),
                              ("Long Axis", "long_axis")):
            tk.Radiobutton(act, text=label, value=value,
                            variable=self.filter_var,
                            command=self._refresh,
                            bg=BG, fg=FG, selectcolor=BG_CARD,
                            activebackground=BG, activeforeground=FG,
                            font=("Segoe UI", 9), bd=0
                            ).pack(side="left", padx=2)

        tk.Label(act, text="   ค้นหา:", bg=BG, fg=FG_DIM,
                 font=("Segoe UI", 9)).pack(side="left", padx=(15, 6))
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self._refresh())
        tk.Entry(act, textvariable=self.search_var, width=20,
                 font=("Consolas", 10), bg=BG_CARD, fg=FG,
                 insertbackground="white", relief="flat"
                 ).pack(side="left", ipady=4)

        # Summary line (legend + counts)
        self.summary = tk.Label(self, text="", bg=BG, fg=FG_DIM, anchor="w",
                                 font=("Segoe UI", 9), pady=4)
        self.summary.pack(fill="x", padx=15)

        # Main split: table on top, image preview on bottom (PanedWindow so user can drag)
        paned = tk.PanedWindow(self, orient="vertical", bg=BG,
                                sashwidth=6, sashrelief="flat",
                                sashpad=2, bd=0)
        paned.pack(fill="both", expand=True, padx=15, pady=(0, 8))

        # Table
        wrap = tk.Frame(paned, bg=BG)
        paned.add(wrap, minsize=180, stretch="always")

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Cmp.Treeview",
                        background=BG_ROW, fieldbackground=BG_ROW,
                        foreground=FG, rowheight=26,
                        font=("Segoe UI", 10), borderwidth=0)
        style.configure("Cmp.Treeview.Heading",
                        background=ACCENT, foreground="white",
                        font=("Segoe UI", 10, "bold"), relief="flat")
        style.map("Cmp.Treeview", background=[("selected", "#264f78")])

        cols = ("module", "param", "test", "master", "status")
        self.tree = ttk.Treeview(wrap, columns=cols, show="headings",
                                  style="Cmp.Treeview")
        headers = {
            "module": ("Module",        180, "w"),
            "param":  ("Parameter",     220, "w"),
            "test":   ("Test Value",    180, "w"),
            "master": ("Master Value",  180, "w"),
            "status": ("Status",        140, "center"),
        }
        for col, (label, width, anchor) in headers.items():
            self.tree.heading(col, text=label)
            self.tree.column(col, width=width, anchor=anchor, stretch=True)

        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", self._on_row_double_click)

        # Image preview panel (Test on left, Master on right)
        preview = tk.Frame(paned, bg=BG_HEAD)
        paned.add(preview, minsize=160, stretch="never", height=240)

        bar2 = tk.Frame(preview, bg=BG_HEAD)
        bar2.pack(fill="x", pady=(6, 4), padx=8)
        tk.Label(bar2, text="🖼  Image Preview", bg=BG_HEAD, fg=FG,
                 font=("Segoe UI", 10, "bold")).pack(side="left")
        tk.Label(bar2,
                 text="  (ดับเบิลคลิกที่แถวเพื่อเปิดด้วย OS  |  คลิกภาพเพื่อขยาย)",
                 bg=BG_HEAD, fg=FG_DIM, font=("Segoe UI", 9)).pack(side="left")
        tk.Button(bar2, text="🔄 Refresh", command=self._refresh_preview,
                  bg=BG_CARD, fg=FG, relief="flat", padx=10, cursor="hand2"
                  ).pack(side="right")

        imgs = tk.Frame(preview, bg=BG_HEAD)
        imgs.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        self.test_img_panel   = self._mk_image_panel(imgs, "Test")
        self.master_img_panel = self._mk_image_panel(imgs, "Master")
        self.test_img_panel["frame"].pack(side="left", fill="both", expand=True,
                                            padx=(0, 4))
        self.master_img_panel["frame"].pack(side="left", fill="both", expand=True,
                                              padx=(4, 0))

        # Row colour tags
        self.tree.tag_configure("same",        background=BG_ROW, foreground=FG_DIM)
        self.tree.tag_configure("diff",        background="#3a2a2a", foreground=BAD)
        self.tree.tag_configure("only_test",   background="#2a3a2a", foreground=GOOD)
        self.tree.tag_configure("only_master", background="#3a3a2a", foreground=WARN)
        self.tree.tag_configure("missing_mod", background="#3a2a3a", foreground=INFO)
        self.tree.tag_configure("group",       background=BG_HEAD,  foreground=ACCENT,
                                 font=("Segoe UI", 9, "bold"))

        # Status bar
        self.status = tk.Label(self, text="กด 'เปรียบเทียบ' เพื่อเริ่มต้น",
                                bg=BG_HEAD, fg=FG_DIM, anchor="w",
                                font=("Consolas", 9), pady=4)
        self.status.pack(fill="x")

    def _mk_image_panel(self, parent, side_label: str) -> dict:
        """Build one half of the image preview area; return widget refs."""
        f = tk.Frame(parent, bg=BG_ROW, bd=1, relief="solid",
                      highlightbackground="#444", highlightthickness=1)
        hdr = tk.Frame(f, bg=BG_HEAD)
        hdr.pack(fill="x")
        title = tk.Label(hdr, text=side_label, bg=BG_HEAD, fg=ACCENT,
                          font=("Segoe UI", 9, "bold"))
        title.pack(side="left", padx=6, pady=2)
        path_lbl = tk.Label(hdr, text="(ยังไม่ได้โหลด)", bg=BG_HEAD, fg=FG_DIM,
                             font=("Consolas", 8), anchor="w")
        path_lbl.pack(side="left", fill="x", expand=True, padx=4)

        # Manual file/folder picker — no longer tied to .solw filter
        browse_btn = tk.Button(hdr, text="📂 เลือกภาพ",
                                 bg=BG_CARD, fg=FG, relief="flat", padx=8,
                                 cursor="hand2", font=("Segoe UI", 8),
                                 command=lambda lbl=side_label: self._browse_image(lbl))
        browse_btn.pack(side="right", padx=2, pady=2)

        open_btn = tk.Button(hdr, text="เปิด", state="disabled",
                              bg=BG_CARD, fg=FG, relief="flat", padx=8,
                              cursor="hand2", font=("Segoe UI", 8))
        open_btn.pack(side="right", padx=2, pady=2)

        # Footer: prev/next + index for browsing all images in a folder
        foot = tk.Frame(f, bg=BG_HEAD)
        foot.pack(fill="x", side="bottom")
        prev_btn = tk.Button(foot, text="◀", state="disabled",
                              bg=BG_CARD, fg=FG, relief="flat", padx=10,
                              cursor="hand2",
                              command=lambda lbl=side_label: self._step_image(lbl, -1))
        prev_btn.pack(side="left", padx=2, pady=2)
        next_btn = tk.Button(foot, text="▶", state="disabled",
                              bg=BG_CARD, fg=FG, relief="flat", padx=10,
                              cursor="hand2",
                              command=lambda lbl=side_label: self._step_image(lbl, +1))
        next_btn.pack(side="left", padx=2, pady=2)
        idx_lbl = tk.Label(foot, text="", bg=BG_HEAD, fg=FG_DIM,
                            font=("Consolas", 8))
        idx_lbl.pack(side="left", padx=6)

        canvas = tk.Label(f, bg=BG_ROW, fg=FG_DIM, text="(ไม่มีภาพ)",
                           font=("Segoe UI", 10))
        canvas.pack(fill="both", expand=True)
        canvas.bind("<Configure>", lambda _e, p=side_label: self._redraw_image(p))

        return {"frame": f, "title": title, "path_lbl": path_lbl,
                "canvas": canvas, "open_btn": open_btn,
                "prev_btn": prev_btn, "next_btn": next_btn, "idx_lbl": idx_lbl,
                "path": "", "pil_image": None, "tk_image": None,
                "folder_files": [], "folder_idx": -1}

    def _set_image(self, panel: dict, path: str, *, scan_folder: bool = True):
        panel["path"] = path
        panel["path_lbl"].config(text=path or "(ยังไม่ได้โหลด)")

        if not path or not os.path.isfile(path):
            panel["canvas"].config(image="",
                                     text=("(ไม่พบไฟล์)\n" + path) if path else "(ไม่มีภาพ)")
            panel["pil_image"] = None
            panel["tk_image"]  = None
            panel["open_btn"].config(state="disabled", command=lambda: None)
            panel["prev_btn"].config(state="disabled")
            panel["next_btn"].config(state="disabled")
            panel["idx_lbl"].config(text="")
            return

        try:
            panel["pil_image"] = Image.open(path)
        except Exception as exc:
            panel["canvas"].config(image="", text=f"(เปิดไม่ได้)\n{exc}")
            panel["pil_image"] = None
            return

        panel["open_btn"].config(state="normal",
                                   command=lambda p=path: _open_with_os(p))
        panel["canvas"].bind("<Button-1>", lambda _e, p=path: _open_with_os(p))

        # Build the folder's image list so the user can step through it.
        if scan_folder:
            folder = os.path.dirname(path)
            try:
                files = sorted(
                    os.path.join(folder, n) for n in os.listdir(folder)
                    if n.lower().endswith(IMG_EXTS)
                )
            except OSError:
                files = [path]
            panel["folder_files"] = files
            panel["folder_idx"] = files.index(path) if path in files else 0

        n_total = len(panel["folder_files"])
        if n_total > 1:
            panel["prev_btn"].config(state="normal")
            panel["next_btn"].config(state="normal")
            panel["idx_lbl"].config(
                text=f"{panel['folder_idx'] + 1} / {n_total}   "
                      f"{os.path.basename(path)}")
        else:
            panel["prev_btn"].config(state="disabled")
            panel["next_btn"].config(state="disabled")
            panel["idx_lbl"].config(text=os.path.basename(path))

        self._fit_image(panel)

    def _browse_image(self, side_label: str):
        """Manually pick any image file (not restricted to what's in the .solw)."""
        panel = self.test_img_panel if side_label == "Test" else self.master_img_panel
        # Suggest starting folder: current image's dir, or D:\Project1\image1, or cwd
        start_dir = ""
        if panel.get("path") and os.path.isfile(panel["path"]):
            start_dir = os.path.dirname(panel["path"])
        elif os.path.isdir(r"D:\Project1\image1"):
            start_dir = r"D:\Project1\image1"

        p = filedialog.askopenfilename(
            title=f"เลือกภาพ ({side_label})",
            initialdir=start_dir,
            filetypes=[
                ("Image files", "*.jpg *.jpeg *.png *.bmp *.tif *.tiff *.gif"),
                ("JPEG", "*.jpg *.jpeg"),
                ("PNG", "*.png"),
                ("All files", "*.*"),
            ])
        if p:
            self._set_image(panel, p)

    def _step_image(self, side_label: str, direction: int):
        """◀/▶ buttons: walk through other images in the same folder."""
        panel = self.test_img_panel if side_label == "Test" else self.master_img_panel
        files = panel.get("folder_files", [])
        if not files:
            return
        new_idx = (panel["folder_idx"] + direction) % len(files)
        panel["folder_idx"] = new_idx
        self._set_image(panel, files[new_idx], scan_folder=False)

    def _fit_image(self, panel: dict):
        """Resize the PIL image to fit the canvas, preserving aspect ratio."""
        img = panel["pil_image"]
        if img is None:
            return
        w = max(panel["canvas"].winfo_width(),   60)
        h = max(panel["canvas"].winfo_height(),  60)
        iw, ih = img.size
        scale = min(w / iw, h / ih) if iw and ih else 1
        nw, nh = max(1, int(iw * scale)), max(1, int(ih * scale))
        resized = img.resize((nw, nh), Image.LANCZOS)
        panel["tk_image"] = ImageTk.PhotoImage(resized)
        panel["canvas"].config(image=panel["tk_image"], text="")

    def _redraw_image(self, side_label: str):
        panel = self.test_img_panel if side_label == "Test" else self.master_img_panel
        if panel.get("pil_image") is not None:
            self._fit_image(panel)

    def _refresh_preview(self):
        self._set_image(self.test_img_panel,   get_image_source_path(self.test_sol))
        self._set_image(self.master_img_panel, get_image_source_path(self.master_sol))

    def _on_row_double_click(self, _event):
        sel = self.tree.focus()
        if not sel:
            return
        vals = self.tree.item(sel, "values")
        # vals = (category, param, test_value, master_value, status)
        if len(vals) < 4:
            return
        for v in (vals[2], vals[3]):
            if _looks_like_image_path(v):
                _open_with_os(v)
                return

    def _mk_file_row(self, parent, label, var, *, row, side: str):
        tk.Label(parent, text=label, bg=BG, fg=FG_DIM,
                 font=("Segoe UI", 10), width=12, anchor="w"
                 ).grid(row=row, column=0, sticky="w", pady=3)
        tk.Entry(parent, textvariable=var, font=("Consolas", 10),
                 bg=BG_CARD, fg=FG, insertbackground="white", relief="flat"
                 ).grid(row=row, column=1, sticky="ew", padx=6, ipady=4)
        # Single button: accepts EITHER .solw or an image; routes by extension
        tk.Button(parent, text="Browse…",
                  command=lambda v=var, s=side: self._browse(v, s),
                  bg="#555", fg="white", relief="flat", padx=10,
                  cursor="hand2"
                  ).grid(row=row, column=2)

    # ── Handlers ────────────────────────────────────────────────────────────
    def _browse(self, var: tk.StringVar, side: str):
        """
        One Browse button, two file types:
          • *.solw  → put path into the entry, user then hits 'เปรียบเทียบ'
          • image   → push directly into that side's preview panel
        """
        p = filedialog.askopenfilename(
            title=f"เลือกไฟล์ ({side}) — .solw หรือรูปภาพ",
            filetypes=[
                ("ทุกไฟล์ที่รองรับ",
                    "*.solw *.jpg *.jpeg *.png *.bmp *.tif *.tiff *.gif"),
                ("Vision Master Solution", "*.solw"),
                ("Image files",
                    "*.jpg *.jpeg *.png *.bmp *.tif *.tiff *.gif"),
                ("All files", "*.*"),
            ])
        if not p:
            return

        if p.lower().endswith(".solw"):
            var.set(p)
        elif _looks_like_image_path(p):
            panel = self.test_img_panel if side == "Test" else self.master_img_panel
            self._set_image(panel, p)
        else:
            # Unknown file type — assume the user knows what they're doing
            var.set(p)

    def _compare(self):
        t_path = self.test_path.get().strip()
        m_path = self.master_path.get().strip()

        if not os.path.isfile(t_path):
            messagebox.showerror("ไม่พบไฟล์", f"Test file:\n{t_path}")
            return
        if not os.path.isfile(m_path):
            messagebox.showerror("ไม่พบไฟล์", f"Master file:\n{m_path}")
            return

        try:
            self.test_sol   = load_solw(t_path)
            self.master_sol = load_solw(m_path)
        except Exception as exc:
            messagebox.showerror("ข้อผิดพลาดในการอ่านไฟล์", repr(exc))
            return

        self._rows = diff_solutions(self.test_sol, self.master_sol)

        self.meta_lbl.config(
            text=(f"Test: {self.test_sol.name} v{self.test_sol.version}   "
                  f"|   Master: {self.master_sol.name} v{self.master_sol.version}  ")
        )
        self._refresh()
        # Defer until canvas has a real size, otherwise resize=0×0.
        self.after(120, self._refresh_preview)

    def _refresh(self):
        self.tree.delete(*self.tree.get_children())
        if not self._rows:
            return

        mode = self.filter_var.get()
        q = self.search_var.get().strip().lower()

        rows = self._rows
        if mode == "diff_only":
            rows = [r for r in rows if r["status"] != "same"]
        elif mode == "long_axis":
            rows = [r for r in rows if "LongAxis" in r["param"]
                    or r["param"] in ("MinLongAxis", "MaxLongAxis")]
        if q:
            rows = [r for r in rows
                    if q in r["param"].lower()
                    or q in r["test"].lower()
                    or q in r["master"].lower()]

        # Group by module
        current_mod = None
        counts = {"same": 0, "diff": 0, "only_test": 0,
                   "only_master": 0, "missing_mod": 0}

        for r in rows:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
            if r["module"] != current_mod:
                self.tree.insert("", "end",
                                  values=(f"▾ {r['module']}", "", "", "", ""),
                                  tags=("group",))
                current_mod = r["module"]

            self.tree.insert("", "end",
                              values=(r["category"], r["param"],
                                       r["test"], r["master"],
                                       self._status_text(r["status"])),
                              tags=(r["status"],))

        total = len(self._rows)
        # Summary legend
        legend = (
            "Legend:  "
            "🟢 only-in-test    "
            "🟡 only-in-master    "
            "🔴 ค่าต่างกัน    "
            "⚫ เหมือนกัน"
        )
        self.summary.config(text=legend)

        self.status.config(
            text=(f"  Total {total}   |   "
                  f"ต่างกัน {counts.get('diff', 0)}   "
                  f"เฉพาะ Test {counts.get('only_test', 0)}   "
                  f"เฉพาะ Master {counts.get('only_master', 0)}   "
                  f"เหมือนกัน {counts.get('same', 0)}   "
                  f"|   แสดง {len(rows)} แถว"),
            fg=GOOD if counts.get("diff", 0) == 0 else WARN,
        )

    @staticmethod
    def _status_text(status: str) -> str:
        return {
            "same":        "✓ เหมือนกัน",
            "diff":        "≠ ค่าต่างกัน",
            "only_test":   "+ เฉพาะ Test",
            "only_master": "+ เฉพาะ Master",
            "missing_mod": "⚠ Module หาย",
        }.get(status, status)


if __name__ == "__main__":
    CompareApp().mainloop()
