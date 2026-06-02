"""
Vision Master .solw Viewer — single-pass parser, no rescans.

Strategy
--------
1. Open the .solw ZIP once. Pull VmServer.xml (metadata + module list) and
   MoudleFrame (the binary parameter blob) into memory.
2. Walk MoudleFrame ONE TIME left→right:
     • Detect each module by its ".mdata" sentinel.
     • For each module: skip header padding, then read fixed 1284-byte
       records (260-byte key + 1024-byte value) until the next sentinel.
   Result is a complete {module: {key: value}} dict cached in memory.
3. The UI never reparses — it only filters the cached dict.
"""

from __future__ import annotations

import json
import os
import re
import zipfile
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET

import tkinter as tk
from tkinter import filedialog, messagebox, ttk


# ── Binary layout constants ──────────────────────────────────────────────────
KEY_SIZE = 260
VALUE_SIZE = 1024
RECORD_SIZE = KEY_SIZE + VALUE_SIZE        # 1284
MDATA_MARKER = b".mdata"


# ── Domain model ─────────────────────────────────────────────────────────────
@dataclass
class Module:
    index: int
    name: str                  # internal name e.g. "IMVSBlobFindModu"
    display_name: str          # user-facing e.g. "Blob Analysis1"
    type_id: int = 0
    guid: str = ""
    enabled: bool = True
    params: dict[str, str] = field(default_factory=dict)


@dataclass
class Solution:
    name: str = ""
    version: str = ""
    saved_at: str = ""
    modules: list[Module] = field(default_factory=list)

    def by_display_name(self, label: str) -> Module | None:
        return next((m for m in self.modules if m.display_name == label), None)


# ── Parser ───────────────────────────────────────────────────────────────────
def _decode_field(buf: bytes) -> str:
    """Read up to the first NUL, decode UTF-8, strip whitespace."""
    end = buf.find(b"\x00")
    raw = buf[:end] if end >= 0 else buf
    return raw.decode("utf-8", errors="ignore").strip()


# Real keys look like C identifiers terminated by NUL — e.g. "PixelFormat\x00",
# "lThresholdType\x00". The first record's key area is 1 byte longer than
# subsequent ones (observed: record 0→1 gap = 1285, all others = 1284), so we
# don't rely on fixed record stride at all. Instead we locate EVERY key by
# pattern and read its trailing 1024-byte value buffer in one pass.
_KEY_PATTERN = re.compile(rb"[A-Za-z_][A-Za-z0-9_]{3,}\x00")


def _find_record_starts(frame: bytes, after: int, limit: int) -> list[int]:
    """
    Return every offset in [after, limit) that begins a real record key.

    A real key sits at the start of a NUL-padded 260-byte region, so the byte
    immediately before it is always NUL (or the section boundary). This filter
    rejects alphanumeric runs that happen to occur inside value buffers
    (paths, names, etc.) because those are preceded by non-NUL data.
    """
    starts: list[int] = []
    for m in _KEY_PATTERN.finditer(frame, after, limit):
        s = m.start()
        if s == after or frame[s - 1] == 0:
            starts.append(s)
    return starts


def _parse_moudle_frame(frame: bytes, modules_meta: list[Module]) -> dict[int, dict[str, str]]:
    """
    Single left-to-right walk.

    Locate every `<idx>-<Name>.mdata` sentinel in order. For each section,
    skip padding, then read 1284-byte records until the next sentinel (or EOF).
    """
    # Find every sentinel's offset. re.finditer = one pass.
    sentinels: list[tuple[int, str]] = []          # (offset, raw_header_text)
    for m in re.finditer(rb"(\d+)-([A-Za-z0-9_]+)\.mdata", frame):
        sentinels.append((m.start(), m.group(0).decode("ascii")))

    # Map "<index>-<Name>" prefix → module index for resolution.
    by_prefix = {f"{mod.index}-{mod.name}": mod.index for mod in modules_meta}

    out: dict[int, dict[str, str]] = {mod.index: {} for mod in modules_meta}

    for n, (offset, header) in enumerate(sentinels):
        prefix = header[:-len(".mdata")]
        mod_idx = by_prefix.get(prefix)
        if mod_idx is None:
            # Header truncated (max ~20 chars) — match by prefix on internal name.
            for full_prefix, idx in by_prefix.items():
                if full_prefix.startswith(prefix) or prefix.startswith(full_prefix.split("-",1)[0]+"-"):
                    if full_prefix.split("-",1)[1].startswith(prefix.split("-",1)[1]):
                        mod_idx = idx
                        break
        if mod_idx is None:
            continue

        section_end = sentinels[n + 1][0] if n + 1 < len(sentinels) else len(frame)
        body_start = offset + len(header)
        key_offsets = _find_record_starts(frame, body_start, section_end)

        # For each detected key, value occupies the 1024 bytes following the
        # 260-byte key buffer — bounded by the next key offset to be safe.
        params: dict[str, str] = {}
        for i, k_off in enumerate(key_offsets):
            key = _decode_field(frame[k_off : k_off + KEY_SIZE])
            if not key:
                continue
            val_start = k_off + KEY_SIZE
            val_end = min(val_start + VALUE_SIZE,
                          key_offsets[i + 1] if i + 1 < len(key_offsets) else section_end)
            params[key] = _decode_field(frame[val_start:val_end])
        out[mod_idx] = params

    return out


def _parse_vmserver_xml(xml_text: str) -> Solution:
    root = ET.fromstring(xml_text)
    sol = Solution()

    if (v := root.find(".//Version")) is not None:
        sol.version = v.get("CurrentVersionCustom", "")
    if (s := root.find(".//SaveInfo/SaveInfo")) is not None:
        sol.saved_at = s.get("Time", "")
    if (n := root.find(".//SolutionInfo/Solution")) is not None:
        sol.name = n.get("SolutionName", "")

    for node in root.findall(".//ModulesInfo/ModuleBase/Module"):
        sol.modules.append(Module(
            index=int(node.get("Index", "0")),
            name=node.get("Name", ""),
            display_name=node.get("DisplayName", ""),
            type_id=int(node.get("Type", "0")),
            guid=node.get("Guid", ""),
            enabled=node.get("EnableModule", "1") == "1",
        ))
    return sol


def load_solw(path: str) -> Solution:
    """One zip open → one XML parse → one binary walk. Done."""
    with zipfile.ZipFile(path, "r") as z:
        xml_text = z.read("SolutionFile/VmServer.xml").decode("utf-8")
        frame = z.read("SolutionFile/MoudleFrame")

    sol = _parse_vmserver_xml(xml_text)
    params_by_idx = _parse_moudle_frame(frame, sol.modules)
    for mod in sol.modules:
        mod.params = params_by_idx.get(mod.index, {})
    return sol


# ── Parameter categorization for the Blob Analysis module ────────────────────
# Used purely for display grouping; the dict carries the full payload regardless.
CATEGORIES: dict[str, tuple[str, ...]] = {
    "Threshold":  ("lThresholdType", "Polarity", "LowThreshold", "HightThreshold",
                   "LowSoftThreshold", "HightSoftThreshold", "Softness",
                   "SoftLeftRatio", "SoftRightRatio", "SoftLowRatio", "SoftHighRatio"),
    "Area / Size": ("MinArea", "MaxArea", "MinPerimeter", "MaxPerimeter",
                    "MinShortAxis", "MaxShortAxis", "MinLongAxis", "MaxLongAxis",
                    "HoleMinArea"),
    "Shape":     ("MinCircularity", "MaxCircularity",
                  "MinRectangularity", "MaxRectangularity",
                  "MinCenterBias", "MaxCenterBias",
                  "MinAxisRatio", "MaxAxisRatio", "AxisRatioEnable"),
    "Long Axis": ("SelectByLongAxis", "LongAxisLimitEnable",
                  "LongAxisLimitLow", "LongAxisLimitHigh"),
    "Short Axis": ("SelectByShortAxis", "ShortAxisLimitEnable",
                   "ShortAxisLimitLow", "ShortAxisLimitHigh"),
    "Selection": ("SelectByArea", "SelectByPerimeter", "SelectByCircularuty",
                  "SelectByRectangularity", "SelectByCentraBias",
                  "FindNum", "SortFeature", "SortMode", "connectivity",
                  "BlobContourType"),
}


def categorize(key: str) -> str:
    for cat, keys in CATEGORIES.items():
        if key in keys:
            return cat
    return "Other"


# ── UI ───────────────────────────────────────────────────────────────────────
BG       = "#1e1e2e"
BG_ALT   = "#2a2a3e"
BG_CARD  = "#252535"
FG       = "#e0e0e8"
FG_DIM   = "#8a8aa0"
ACCENT   = "#7aa2f7"
GOOD     = "#9ece6a"
WARN     = "#e0af68"
HILITE   = "#f7768e"


class SolwViewer(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Vision Master .solw Viewer")
        self.geometry("1000x620")
        self.configure(bg=BG)
        self.minsize(820, 480)

        self.solution: Solution | None = None
        self._all_rows: list[tuple[str, str, str, str]] = []   # (module, category, key, value)

        self._build_ui()

        default = "D:\\Project1\\Test.solw"
        if os.path.isfile(default):
            self.path_var.set(default)
            self.after(150, self._load)

    # ── layout ──
    def _build_ui(self):
        # Header
        head = tk.Frame(self, bg=BG_ALT, height=58)
        head.pack(fill="x")
        head.pack_propagate(False)
        tk.Label(head, text="◆  Vision Master  .solw  Viewer", bg=BG_ALT, fg=FG,
                 font=("Segoe UI", 14, "bold")).pack(side="left", padx=18)
        self.meta_lbl = tk.Label(head, text="", bg=BG_ALT, fg=FG_DIM,
                                  font=("Consolas", 9))
        self.meta_lbl.pack(side="right", padx=18)

        # File / search bar
        bar = tk.Frame(self, bg=BG, pady=10)
        bar.pack(fill="x", padx=14)

        self.path_var = tk.StringVar()
        tk.Entry(bar, textvariable=self.path_var, font=("Consolas", 10),
                 bg=BG_CARD, fg=FG, insertbackground=FG, relief="flat",
                 ).pack(side="left", fill="x", expand=True, ipady=5)
        self._mk_btn(bar, "📂 Browse", self._browse).pack(side="left", padx=(8, 0))
        self._mk_btn(bar, "↻ Reload", self._load, accent=True).pack(side="left", padx=(6, 0))

        ctrl = tk.Frame(self, bg=BG)
        ctrl.pack(fill="x", padx=14, pady=(0, 6))

        tk.Label(ctrl, text="Module:", bg=BG, fg=FG_DIM,
                 font=("Segoe UI", 9)).pack(side="left", padx=(0, 6))
        self.module_var = tk.StringVar(value="(all)")
        self.module_cb = ttk.Combobox(ctrl, textvariable=self.module_var,
                                       state="readonly", width=28,
                                       font=("Segoe UI", 9))
        self.module_cb.pack(side="left")
        self.module_cb.bind("<<ComboboxSelected>>", lambda _e: self._refresh_table())

        tk.Label(ctrl, text="   Search:", bg=BG, fg=FG_DIM,
                 font=("Segoe UI", 9)).pack(side="left", padx=(14, 6))
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self._refresh_table())
        tk.Entry(ctrl, textvariable=self.search_var, font=("Consolas", 10),
                 bg=BG_CARD, fg=FG, insertbackground=FG, relief="flat", width=24,
                 ).pack(side="left", ipady=4)

        self._mk_btn(ctrl, "⭐ Long Axis only", self._filter_long_axis).pack(side="left", padx=10)
        self._mk_btn(ctrl, "💾 Export JSON", self._export_json).pack(side="right")

        # Table
        wrap = tk.Frame(self, bg=BG)
        wrap.pack(fill="both", expand=True, padx=14, pady=(0, 6))

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("V.Treeview", background=BG_CARD, fieldbackground=BG_CARD,
                        foreground=FG, rowheight=26, font=("Segoe UI", 10),
                        borderwidth=0)
        style.configure("V.Treeview.Heading", background=ACCENT, foreground="#1a1a2a",
                        font=("Segoe UI", 9, "bold"), relief="flat")
        style.map("V.Treeview", background=[("selected", "#3b4a6b")])

        cols = ("module", "category", "param", "value")
        self.tree = ttk.Treeview(wrap, columns=cols, show="headings", style="V.Treeview")
        for c, w, anchor in (("module", 180, "w"), ("category", 110, "w"),
                              ("param", 240, "w"),  ("value", 350, "w")):
            self.tree.heading(c, text=c.title())
            self.tree.column(c, width=w, anchor=anchor, stretch=True)

        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        self.tree.tag_configure("long_axis", background="#3a2a4a", foreground=HILITE)
        self.tree.tag_configure("enabled", foreground=GOOD)
        self.tree.tag_configure("category", background=BG_ALT, foreground=ACCENT,
                                font=("Segoe UI", 9, "bold"))

        # Status
        self.status = tk.Label(self, text="Ready.", bg=BG_ALT, fg=FG_DIM,
                               anchor="w", font=("Consolas", 9), pady=4)
        self.status.pack(fill="x")

    def _mk_btn(self, parent, label, cmd, *, accent=False):
        bg = ACCENT if accent else BG_CARD
        fg = "#1a1a2a" if accent else FG
        return tk.Button(parent, text=label, command=cmd, bg=bg, fg=fg,
                          relief="flat", padx=12, pady=4,
                          font=("Segoe UI", 9, "bold" if accent else "normal"),
                          activebackground="#5d7fc7", activeforeground="white",
                          cursor="hand2", bd=0)

    # ── handlers ──
    def _browse(self):
        p = filedialog.askopenfilename(
            title="Open .solw",
            filetypes=[("Vision Master Solution", "*.solw"), ("All files", "*.*")])
        if p:
            self.path_var.set(p)
            self._load()

    def _load(self):
        path = self.path_var.get().strip()
        if not os.path.isfile(path):
            messagebox.showerror("Not found", f"File not found:\n{path}")
            return
        try:
            self.solution = load_solw(path)
        except Exception as exc:
            messagebox.showerror("Parse error", repr(exc))
            return

        # Flatten into rows ONCE — UI only filters this list afterward.
        self._all_rows = []
        for mod in self.solution.modules:
            for k, v in mod.params.items():
                self._all_rows.append((mod.display_name, categorize(k), k, v))

        # Module selector
        labels = ["(all)"] + [m.display_name for m in self.solution.modules]
        self.module_cb["values"] = labels
        self.module_var.set("(all)")

        sol = self.solution
        self.meta_lbl.config(
            text=f"  {sol.name}   |   VM {sol.version}   |   saved {sol.saved_at}  ")
        self._refresh_table()

    def _refresh_table(self):
        """Render the cached rows under the current filter — no reparse."""
        self.tree.delete(*self.tree.get_children())
        if not self._all_rows:
            return

        mod_filter = self.module_var.get()
        q = self.search_var.get().strip().lower()

        rows = self._all_rows
        if mod_filter != "(all)":
            rows = [r for r in rows if r[0] == mod_filter]
        if q:
            rows = [r for r in rows if q in r[2].lower() or q in r[3].lower()]

        # Group by (module, category) for readable display.
        rows.sort(key=lambda r: (r[0], r[1], r[2]))

        current_group = None
        shown = 0
        long_axis_hits = 0
        for module, cat, key, val in rows:
            group = (module, cat)
            if group != current_group:
                self.tree.insert("", "end",
                                  values=(f"▾ {module}", cat, "", ""),
                                  tags=("category",))
                current_group = group

            tags: list[str] = []
            if "LongAxis" in key or key in ("MinLongAxis", "MaxLongAxis"):
                tags.append("long_axis")
                long_axis_hits += 1
            if val.lower() == "true":
                tags.append("enabled")

            display_val = val if val else "—"
            self.tree.insert("", "end",
                              values=("", "", key, display_val),
                              tags=tuple(tags))
            shown += 1

        self.status.config(
            text=f"  rows: {shown} of {len(self._all_rows)}   |   "
                 f"long-axis params: {long_axis_hits}   |   "
                 f"modules: {len(self.solution.modules) if self.solution else 0}")

    def _filter_long_axis(self):
        self.search_var.set("LongAxis")

    def _export_json(self):
        if not self.solution:
            return
        out = filedialog.asksaveasfilename(
            defaultextension=".json",
            initialfile=f"{self.solution.name or 'solw'}_params.json",
            filetypes=[("JSON", "*.json")])
        if not out:
            return
        payload = {
            "solution": self.solution.name,
            "version": self.solution.version,
            "saved_at": self.solution.saved_at,
            "modules": [
                {"index": m.index, "name": m.name,
                 "display_name": m.display_name,
                 "type_id": m.type_id, "enabled": m.enabled,
                 "params": m.params}
                for m in self.solution.modules
            ],
        }
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
        messagebox.showinfo("Exported", f"Saved:\n{out}")


# ── CLI shortcut: `python solw_viewer.py path.solw --print` ─────────────────
def _cli(argv: list[str]) -> int:
    if len(argv) < 2:
        SolwViewer().mainloop()
        return 0
    sol = load_solw(argv[1])
    blob = next((m for m in sol.modules if "Blob" in m.display_name), None)
    print(f"Solution: {sol.name}  VM {sol.version}  ({sol.saved_at})")
    print(f"Modules : {len(sol.modules)}")
    if blob:
        print(f"\n[{blob.display_name}] Long Axis parameters:")
        for k in ("MinLongAxis", "MaxLongAxis",
                  "SelectByLongAxis", "LongAxisLimitEnable",
                  "LongAxisLimitLow", "LongAxisLimitHigh"):
            print(f"  {k:24s} = {blob.params.get(k, '(unset)')}")
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(_cli(sys.argv))
