import zipfile, os, math, re, sys, threading, time, socket
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from PIL import Image, ImageTk
import cv2
import numpy as np
from ctypes import cast, POINTER, memset, byref, sizeof, memmove, c_ubyte
from typing import Any, Literal

# Declare HIKROBOT SDK types/variables as Any to satisfy Pylance
MvCamera: Any = None
MV_CC_DEVICE_INFO_LIST: Any = None
MV_CC_DEVICE_INFO: Any = None
MV_FRAME_OUT: Any = None
MV_GIGE_DEVICE: Any = None
MV_USB_DEVICE: Any = None
MV_ACCESS_Exclusive: Any = None
MV_TRIGGER_MODE_OFF: Any = None
PixelType_Gvsp_Mono8: Any = None
PixelType_Gvsp_RGB8_Packed: Any = None
MV_FRAME_OUT_INFO_EX: Any = None

# ──────────────────────────────────────────────
# HIKROBOT Camera SDK
# ──────────────────────────────────────────────
_SDK_AVAILABLE = False
try:
    _MV_PATH = os.path.join(
        os.getenv("MVCAM_COMMON_RUNENV", r"C:\Program Files (x86)\MVS\Development"),
        "Samples", "Python", "MvImport"
    )
    if _MV_PATH not in sys.path:
        sys.path.insert(0, _MV_PATH)
    from MvCameraControl_class import (  # type: ignore
        MvCamera, MV_CC_DEVICE_INFO_LIST, MV_CC_DEVICE_INFO,
        MV_FRAME_OUT, MV_GIGE_DEVICE, MV_USB_DEVICE,
        MV_ACCESS_Exclusive, MV_TRIGGER_MODE_OFF,
        PixelType_Gvsp_Mono8, PixelType_Gvsp_RGB8_Packed,
    )
    from CameraParams_header import MV_FRAME_OUT_INFO_EX  # type: ignore
    _SDK_AVAILABLE = True
except Exception as _e:
    print(f"[Camera SDK] Not available: {_e}")


# ──────────────────────────────────────────────
# .solw binary constants
# ──────────────────────────────────────────────
RECORD_SIZE   = 1284
KEY_SIZE      = 260
FILE_HDR_SIZE = 90


# ──────────────────────────────────────────────
# .solw parsing
# ──────────────────────────────────────────────
def _first_record_after(data: bytes, marker: bytes) -> int:
    pos = data.find(marker)
    if pos == -1:
        raise ValueError(f"Marker not found: {marker!r}")
    return pos + (RECORD_SIZE - (pos - FILE_HDR_SIZE) % RECORD_SIZE)


def _parse_records(data: bytes, start: int, end: int) -> dict:
    params, offset = {}, start
    while offset + RECORD_SIZE <= end:
        kb = data[offset : offset + KEY_SIZE]
        vb = data[offset + KEY_SIZE : offset + RECORD_SIZE]
        n = kb.find(b"\x00"); k = kb[:n].decode("utf-8", "ignore").strip() if n >= 0 else ""
        n = vb.find(b"\x00"); v = vb[:n].decode("utf-8", "ignore").strip() if n >= 0 else ""
        if k:
            params[k] = v
        offset += RECORD_SIZE
    return params


def load_solw(filepath: str) -> tuple[dict, str]:
    with zipfile.ZipFile(filepath, "r") as z:
        with z.open("SolutionFile/MoudleFrame") as f:
            data = f.read()

    blob_start = _first_record_after(data, b"1-IMVSBlobFindModu.mdata")
    try:
        blob_end = _first_record_after(data, b"11000-CommManagerModule.mdata") - RECORD_SIZE
    except ValueError:
        blob_end = len(data)

    blob_params = _parse_records(data, blob_start, blob_end)

    m = re.search(b"CurrentImagePath\x00{244}(.*?)\x00", data, re.DOTALL)
    image_path = m.group(1).decode("utf-8", "ignore").strip() if m else ""
    return blob_params, image_path


# ──────────────────────────────────────────────
# Pass / Fail check (from .solw limits)
# ──────────────────────────────────────────────
_CHECKS = [
    ("LongAxisLimitEnable",    "long_axis",      "LongAxisLimitLow",      "LongAxisLimitHigh",      "Long Axis"),
    ("ShortAxisLimitEnable",   "short_axis",     "ShortAxisLimitLow",     "ShortAxisLimitHigh",     "Short Axis"),
    ("CircularityLimitEnable", "circularity",    "CircularityLimitLow",   "CircularityLimitHigh",   "Circularity"),
    ("RectangularityLimitEnable","rectangularity","RectangularityLimitLow","RectangularityLimitHigh","Rectangularity"),
    ("BlobAreaLimitEnable",    "area",           "BlobAreaLimitLow",      "BlobAreaLimitHigh",      "Area"),
    ("PerimeterLimitEnable",   "perimeter",      "PerimeterLimitLow",     "PerimeterLimitHigh",     "Perimeter"),
    ("AngleLimitEnable",       "angle",          "AngleLimitLow",         "AngleLimitHigh",         "Angle"),
]

def check_pass_fail(blobs: list, params: dict, master: dict | None = None) -> tuple[bool, list[str]]:
    """Return (is_ok, list_of_fail_reasons).
    Uses .solw LimitEnable/Low/High if enabled.
    Falls back to master comparison if master is provided.
    """
    fails = []

    # ── limit checks from .solw ───────────────────────────────────────────
    if not blobs:
        fails.append("ไม่พบ Blob ในภาพ")
        return False, fails

    # BlobNumLimitEnable
    if params.get("BlobNumLimitEnable", "False").lower() == "true":
        lo = float(params.get("BlobNumLimitLow",  "1"))
        hi = float(params.get("BlobNumLimitHigh", "100"))
        n  = len(blobs)
        if not (lo <= n <= hi):
            fails.append(f"จำนวน Blob = {n}  (ต้องการ {lo:.0f}–{hi:.0f})")

    b = blobs[0]  # check the largest (first after sort)
    for enable_key, blob_field, low_key, high_key, label in _CHECKS:
        if params.get(enable_key, "False").lower() != "true":
            continue
        val = b.get(blob_field, 0)
        lo  = float(params.get(low_key,  "0"))
        hi  = float(params.get(high_key, "1e9"))
        in_range = lo <= val <= hi
        # Angle has 180° periodicity for rectangular objects — also accept val±180°
        if not in_range and blob_field == "angle":
            in_range = (lo <= val + 180.0 <= hi) or (lo <= val - 180.0 <= hi)
        if not in_range:
            fails.append(f"{label} = {val:.4f}  (ต้องการ {lo:.4f}–{hi:.4f})")

    # ── master comparison (if no .solw limits active) ─────────────────────
    if not fails and master:
        tol = master.get("_tolerance", 0.10)   # 10 % default tolerance
        for field in ("area", "long_axis", "short_axis"):
            ref = master.get(field, 0)
            val = b.get(field, 0)
            if ref > 0:
                diff = abs(val - ref) / ref
                if diff > tol:
                    fails.append(
                        f"{field} = {val:.2f}  (Master {ref:.2f}  Δ {diff*100:.1f}% > {tol*100:.0f}%)"
                    )

    return (len(fails) == 0), fails


# ──────────────────────────────────────────────
# Blob analysis (OpenCV)
# ──────────────────────────────────────────────
def _sf(s, d=0.0):
    try: return float(s)
    except: return d

def _si(s, d=0):
    try: return int(s)
    except: return d


def run_blob_analysis(image_path_or_array, params: dict) -> tuple[np.ndarray, list[dict]]:
    if isinstance(image_path_or_array, np.ndarray):
        img_bgr = image_path_or_array
    else:
        img_bgr = cv2.imread(image_path_or_array)
        if img_bgr is None:
            raise FileNotFoundError(f"Cannot read image: {image_path_or_array}")

    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)

    low_thr  = _si(params.get("LowThreshold",   "100"))
    polarity = _si(params.get("Polarity",        "1"))
    find_num = _si(params.get("FindNum",         "100"))
    min_area = _sf(params.get("MinArea",         "50000"))
    max_area = _sf(params.get("MaxArea",         "100000"))
    min_long = _sf(params.get("MinLongAxis",     "10"))
    max_long = _sf(params.get("MaxLongAxis",     "1e9"))
    min_short= _sf(params.get("MinShortAxis",    "1"))
    max_short= _sf(params.get("MaxShortAxis",    "1e9"))
    min_circ = _sf(params.get("MinCircularity",  "0.1"))
    max_circ = _sf(params.get("MaxCircularity",  "1"))
    min_rect = _sf(params.get("MinRectangularity","0.1"))
    max_rect = _sf(params.get("MaxRectangularity","1"))

    # Polarity 1 = dark blob → BINARY_INV; 0 = bright blob → BINARY
    thr_type = cv2.THRESH_BINARY if polarity == 0 else cv2.THRESH_BINARY_INV
    _, binary = cv2.threshold(gray, low_thr, 255, thr_type)

    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)

    # Otsu auto-threshold fallback when fixed threshold finds nothing
    if not contours:
        _, binary = cv2.threshold(gray, 0, 255, thr_type | cv2.THRESH_OTSU)
        contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)

    blobs = []
    for cnt in contours:
        M = cv2.moments(cnt)
        if M["m00"] == 0: continue
        area      = M["m00"]
        perimeter = cv2.arcLength(cnt, True)
        cx        = M["m10"] / M["m00"]
        cy        = M["m01"] / M["m00"]

        (rx, ry), (rw, rh), raw_ang = cv2.minAreaRect(cnt)
        # +1 converts center-to-center (OpenCV) → edge-to-edge (Vision Master convention)
        long_axis  = max(rw, rh) + 1.0
        short_axis = min(rw, rh) + 1.0
        vm_angle   = -(90.0 - raw_ang) if rh >= rw else raw_ang

        # Circularity = area / area-of-min-enclosing-circle  (Vision Master formula)
        _, R = cv2.minEnclosingCircle(cnt)
        circularity    = area / (math.pi * R * R) if R > 0 else 0
        rectangularity = area / (long_axis * short_axis) if long_axis * short_axis > 0 else 0
        bx, by, bw, bh = cv2.boundingRect(cnt)

        if not (min_area  <= area       <= max_area):  continue
        if not (min_long  <= long_axis  <= max_long):  continue
        if not (min_short <= short_axis <= max_short): continue
        if not (min_circ  <= circularity<= max_circ):  continue
        if not (min_rect  <= rectangularity<= max_rect):continue

        blobs.append({
            "contour": cnt, "area": area, "perimeter": perimeter,
            "centroid_x": cx, "centroid_y": cy, "angle": vm_angle,
            "long_axis": long_axis, "short_axis": short_axis,
            "circularity": circularity, "rectangularity": rectangularity,
            "min_area_rect": ((rx, ry), (rw, rh), raw_ang),
            "bbox": (bx, by, bw, bh),
        })

    if params.get("SelectByArea", "False").lower() == "true":
        blobs.sort(key=lambda b: b["area"], reverse=True)
    blobs = blobs[:find_num]

    annotated = img_bgr.copy()
    for i, b in enumerate(blobs):
        box = np.array(cv2.boxPoints(b["min_area_rect"]), dtype=np.int32)
        cv2.drawContours(annotated, [box], 0, (0, 255, 0), 2)
        cxi, cyi = int(b["centroid_x"]), int(b["centroid_y"])
        cv2.line(annotated, (cxi-8, cyi), (cxi+8, cyi), (0, 255, 0), 1)
        cv2.line(annotated, (cxi, cyi-8), (cxi, cyi+8), (0, 255, 0), 1)
        bx, by = b["bbox"][:2]
        cv2.putText(annotated, f"#{i}  A={int(b['area'])}", (bx, max(by-6, 12)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 255, 0), 1, cv2.LINE_AA)
    return annotated, blobs


# ──────────────────────────────────────────────
# Camera Manager
# ──────────────────────────────────────────────
class CameraManager:
    def __init__(self):
        self._cam: Any = None
        self._opened  = False
        self._grabbing= False
        self._lock    = threading.Lock()

    @property
    def is_connected(self):
        return self._opened

    def enum_devices(self) -> list[str]:
        """Return list of device description strings."""
        if not _SDK_AVAILABLE:
            return []
        devList = MV_CC_DEVICE_INFO_LIST()
        MvCamera.MV_CC_Initialize()
        ret = MvCamera.MV_CC_EnumDevices(MV_GIGE_DEVICE | MV_USB_DEVICE, devList)
        if ret != 0 or devList.nDeviceNum == 0:
            return []
        result = []
        for i in range(devList.nDeviceNum):
            info = cast(devList.pDeviceInfo[i], POINTER(MV_CC_DEVICE_INFO)).contents  # type: ignore
            if info.nTLayerType == MV_USB_DEVICE:
                u = info.SpecialInfo.stUsb3VInfo
                model = bytes(u.chModelName).split(b"\x00")[0].decode("utf-8", "ignore")
                sn    = bytes(u.chSerialNumber).split(b"\x00")[0].decode("utf-8", "ignore")
                result.append(f"[USB]  {model}  SN:{sn}")
            elif info.nTLayerType == MV_GIGE_DEVICE:
                g = info.SpecialInfo.stGigEInfo
                model = bytes(g.chModelName).split(b"\x00")[0].decode("utf-8", "ignore")
                ip = g.nCurrentIp
                result.append(f"[GigE] {model}  {ip>>24}.{(ip>>16)&0xff}.{(ip>>8)&0xff}.{ip&0xff}")
        self._devList = devList
        return result

    def connect(self, index: int = 0) -> str:
        """Connect to camera at *index*. Returns '' on success or error message."""
        if not _SDK_AVAILABLE:
            return "Camera SDK ไม่พร้อม"
        if self._opened:
            return ""
        try:
            stDev = cast(self._devList.pDeviceInfo[index], POINTER(MV_CC_DEVICE_INFO)).contents  # type: ignore
            self._cam = MvCamera()
            ret = self._cam.MV_CC_CreateHandle(stDev)
            if ret != 0: raise RuntimeError(f"CreateHandle failed: 0x{ret:x}")
            ret = self._cam.MV_CC_OpenDevice(MV_ACCESS_Exclusive, 0)
            if ret != 0: raise RuntimeError(f"OpenDevice failed: 0x{ret:x}")

            if stDev.nTLayerType == MV_GIGE_DEVICE:
                pkt = self._cam.MV_CC_GetOptimalPacketSize()
                if pkt > 0:
                    self._cam.MV_CC_SetIntValue("GevSCPSPacketSize", pkt)

            self._cam.MV_CC_SetEnumValue("TriggerMode", MV_TRIGGER_MODE_OFF)
            self._opened = True
            return ""
        except Exception as e:
            if self._cam:
                self._cam.MV_CC_DestroyHandle()
                self._cam = None
            return str(e)

    def disconnect(self):
        if not self._opened: return
        try:
            if self._grabbing:
                self._cam.MV_CC_StopGrabbing()
                self._grabbing = False
            self._cam.MV_CC_CloseDevice()
            self._cam.MV_CC_DestroyHandle()
        except: pass
        finally:
            self._cam    = None
            self._opened = False

    def capture_frame(self, timeout_ms: int = 3000) -> np.ndarray:
        """Grab one frame and return as BGR numpy array."""
        if not self._opened:
            raise RuntimeError("กล้องยังไม่ได้เชื่อมต่อ")

        with self._lock:
            # ── reset grabbing state ก่อนเสมอ (ป้องกัน MV_E_CALLORDER 0x80000007)
            if self._grabbing:
                try:
                    self._cam.MV_CC_StopGrabbing()
                except Exception:
                    pass
                self._grabbing = False

            # ── เริ่ม grabbing พร้อม retry 1 ครั้ง ──────────────────────────
            ret = self._cam.MV_CC_StartGrabbing()
            if ret != 0:
                # ลอง stop แล้ว start ใหม่อีกครั้ง
                try:
                    self._cam.MV_CC_StopGrabbing()
                except Exception:
                    pass
                time.sleep(0.2)
                ret = self._cam.MV_CC_StartGrabbing()
                if ret != 0:
                    raise RuntimeError(
                        f"StartGrabbing failed: 0x{ret:x}  "
                        f"(ลองตัดการเชื่อมต่อแล้วเชื่อมใหม่)")
            self._grabbing = True

            stFrame = MV_FRAME_OUT()
            memset(byref(stFrame), 0, sizeof(stFrame))
            try:
                ret = self._cam.MV_CC_GetImageBuffer(stFrame, timeout_ms)
                if ret != 0:
                    raise RuntimeError(
                        f"GetImageBuffer failed: 0x{ret:x}  "
                        f"(กล้องอาจค้าง — ตัดการเชื่อมต่อแล้วเชื่อมใหม่)")

                info = stFrame.stFrameInfo
                w, h = info.nWidth, info.nHeight
                buf  = (c_ubyte * info.nFrameLen)()
                memmove(buf, stFrame.pBufAddr, info.nFrameLen)
                self._cam.MV_CC_FreeImageBuffer(stFrame)
            finally:
                self._cam.MV_CC_StopGrabbing()
                self._grabbing = False

        raw = np.frombuffer(buf, dtype=np.uint8)

        if info.enPixelType == PixelType_Gvsp_Mono8:
            gray  = raw.reshape((h, w))
            frame = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
        elif info.enPixelType == PixelType_Gvsp_RGB8_Packed:
            frame = raw.reshape((h, w, 3))
            frame = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
        else:
            # Bayer → BGR
            bayer = raw.reshape((h, w))
            frame = cv2.cvtColor(bayer, cv2.COLOR_BayerRG2BGR)

        return frame


# ──────────────────────────────────────────────
# VM TCP Server
# ──────────────────────────────────────────────
class VMTCPServer:
    """TCP server that accepts one connection from Vision Master (TCP Client)
    and fires on_data(line) for each newline-terminated message received."""

    def __init__(self, host: str = "127.0.0.1", port: int = 5001, on_data=None):
        self._host    = host
        self._port    = port
        self._on_data = on_data
        self._running = False
        self._srv_sock: socket.socket | None = None

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self, port: int | None = None) -> str:
        """Start listening. Returns '' on success or error string."""
        if self._running:
            return ""
        if port is not None:
            self._port = port
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((self._host, self._port))
            s.listen(5)
            s.settimeout(1.0)
            self._srv_sock = s
            self._running  = True
            threading.Thread(target=self._accept_loop, daemon=True).start()
            return ""
        except Exception as e:
            self._running = False
            if self._srv_sock:
                try: self._srv_sock.close()
                except: pass
                self._srv_sock = None
            return str(e)

    def stop(self):
        self._running = False
        if self._srv_sock:
            try: self._srv_sock.close()
            except: pass
            self._srv_sock = None

    def _accept_loop(self):
        while self._running:
            try:
                conn, _ = self._srv_sock.accept()
                threading.Thread(target=self._client_loop,
                                 args=(conn,), daemon=True).start()
            except socket.timeout:
                continue
            except Exception:
                break

    def _client_loop(self, conn: socket.socket):
        buf = ""
        with conn:
            conn.settimeout(2.0)
            while self._running:
                try:
                    chunk = conn.recv(1024).decode("utf-8", "ignore")
                    if not chunk:
                        break
                    buf += chunk
                    while "\n" in buf:
                        line, buf = buf.split("\n", 1)
                        line = line.strip()
                        if line and self._on_data:
                            self._on_data(line)
                except socket.timeout:
                    continue
                except Exception:
                    break


# ──────────────────────────────────────────────
# UI helpers
# ──────────────────────────────────────────────
def _lbl_bar(parent, text: str, bg: str, var: tk.StringVar | None = None):
    """Coloured label bar at the top of an image panel."""
    fr = tk.Frame(parent, bg=bg, pady=3)
    fr.pack(fill="x")
    lbl = tk.Label(fr, text=text, bg=bg, fg="white",
                   font=("Segoe UI", 9, "bold"))
    lbl.pack(side="left", padx=8)
    if var is not None:
        lbl.config(textvariable=var)
    return lbl


# ──────────────────────────────────────────────
# UI constants
# ──────────────────────────────────────────────
RESULT_COLS = [
    ("source",         "Source",          72, "center"),
    ("Number",         "#",               42, "center"),
    ("area",           "Area",            85, "center"),
    ("perimeter",      "Perimeter",       90, "center"),
    ("centroid_x",     "Centroid X",      90, "center"),
    ("centroid_y",     "Centroid Y",      90, "center"),
    ("angle",          "Angle",           85, "center"),
    ("long_axis",      "Long Axis",       88, "center"),
    ("short_axis",     "Short Axis",      88, "center"),
    ("circularity",    "Circularity",     88, "center"),
    ("rectangularity", "Rectangularity", 100, "center"),
]

DARK_BG = "#1e1e1e"
MID_BG  = "#2b2b2b"
FG      = "#e0e0e0"
ACCENT  = "#0078d4"


# ──────────────────────────────────────────────
# Main Application
# ──────────────────────────────────────────────
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Vision Master – Blob Analysis Viewer")
        self.geometry("1500x860")
        self.minsize(1100, 660)
        self.configure(bg=DARK_BG)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # image buffers (BGR numpy arrays)
        self._master_bgr  : np.ndarray | None = None
        self._capture_bgr : np.ndarray | None = None
        # Tk image references (must stay alive)
        self._tk_master  = None
        self._tk_capture = None

        self._current_params   : dict       = {}
        self._master_is_custom : bool        = False
        self._master_lbl_var   = tk.StringVar(value="MASTER")
        self._master_blobs     : list | None = None
        # result-table state (persists across master re-loads)
        self._tbl_master_blobs : list | None = None
        self._tbl_cap_blobs    : list | None = None
        self._tbl_cap_ok       : bool        = True
        # overlay state for capture canvas redraws
        self._capture_blobs    : list | None = None
        self._capture_fails    : list        = []
        # master image resolution for area-param scaling
        self._master_img_hw    : tuple | None = None   # (height, width)
        self._cam_mgr          = CameraManager()
        self._cam_devices      : list[str]   = []

        # VM TCP Server state
        self._tcp_srv  = VMTCPServer(on_data=self._vm_data_received)
        self._vm_h_lo  = tk.StringVar(value="0")
        self._vm_h_hi  = tk.StringVar(value="9999")
        self._vm_w_lo  = tk.StringVar(value="0")
        self._vm_w_hi  = tk.StringVar(value="9999")

        self._build_ui()

    # ────────────────────────── UI builder ──────────────────────────────────
    def _build_ui(self):
        # ── header ──────────────────────────────────────────────────────────
        hdr = tk.Frame(self, bg="#111", pady=7)
        hdr.pack(fill="x")
        tk.Label(hdr, text="Vision Master  |  Blob Analysis Viewer",
                 font=("Segoe UI", 13, "bold"), bg="#111", fg=FG).pack(side="left", padx=14)

        # ── .solw row ───────────────────────────────────────────────────────
        row1 = tk.Frame(self, bg=MID_BG, pady=6)
        row1.pack(fill="x", padx=10)
        tk.Label(row1, text=".solw :", bg=MID_BG, fg="#aaa",
                 font=("Segoe UI", 9)).pack(side="left", padx=(4, 2))
        self.path_var = tk.StringVar(value=r"D:\Project1\Test.solw")
        tk.Entry(row1, textvariable=self.path_var, width=54,
                 font=("Consolas", 10), bg="#3c3c3c", fg=FG,
                 insertbackground="white", relief="flat").pack(side="left", ipady=3)
        tk.Button(row1, text="Browse…", command=self._browse,
                  bg="#555", fg=FG, relief="flat", padx=7).pack(side="left", padx=4)
        tk.Button(row1, text=" วิเคราะห์ Master ", command=self._analyze,
                  bg=ACCENT, fg="white", font=("Segoe UI", 9, "bold"),
                  relief="flat", padx=10).pack(side="left")
        tk.Button(row1, text=" 📂 Master ", command=self._select_master_image,
                  bg="#7a5200", fg="white", font=("Segoe UI", 9, "bold"),
                  relief="flat", padx=10).pack(side="left", padx=(6, 0))
        tk.Button(row1, text=" Open Before ", command=self._open_picture,
                  bg="#2d7a2d", fg="white", font=("Segoe UI", 9, "bold"),
                  relief="flat", padx=10).pack(side="left", padx=(6, 0))
        tk.Button(row1, text=" ⚙ ", command=self._open_settings,
                  bg="#444", fg=FG, font=("Segoe UI", 12),
                  relief="flat", padx=6).pack(side="left", padx=(6, 0))

        # ── camera row ──────────────────────────────────────────────────────
        row2 = tk.Frame(self, bg="#252525", pady=6)
        row2.pack(fill="x", padx=10)

        self._dot_var = tk.StringVar(value="●")
        self._dot_lbl = tk.Label(row2, textvariable=self._dot_var,
                                 font=("Segoe UI", 14), bg="#252525", fg="#555")
        self._dot_lbl.pack(side="left", padx=(6, 2))

        self._cam_var = tk.StringVar(value="— กด Scan เพื่อค้นหากล้อง —")
        self._cam_cb  = ttk.Combobox(row2, textvariable=self._cam_var, width=42,
                                     state="readonly", font=("Segoe UI", 9))
        self._cam_cb.pack(side="left", padx=4)

        tk.Button(row2, text=" 🔍 Scan ", command=self._scan_cameras,
                  bg="#444", fg=FG, relief="flat", padx=8).pack(side="left", padx=(0, 4))
        self._btn_connect = tk.Button(row2, text=" เชื่อมต่อ ",
                                      command=self._toggle_connect,
                                      bg="#444", fg=FG, relief="flat", padx=10)
        self._btn_connect.pack(side="left", padx=(0, 8))

        tk.Label(row2, text="Tolerance ±", bg="#252525", fg="#aaa",
                 font=("Segoe UI", 9)).pack(side="left")
        self._tol_var = tk.StringVar(value="10")
        tk.Entry(row2, textvariable=self._tol_var, width=4,
                 font=("Segoe UI", 9), bg="#3c3c3c", fg=FG,
                 insertbackground="white", relief="flat").pack(side="left")
        tk.Label(row2, text="%", bg="#252525", fg="#aaa",
                 font=("Segoe UI", 9)).pack(side="left", padx=(0, 12))

        self._btn_cap = tk.Button(row2, text="  📷  ถ่ายภาพ  ",
                                  command=self._capture,
                                  bg="#555", fg="#888",
                                  font=("Segoe UI", 11, "bold"),
                                  relief="flat", padx=18, pady=4,
                                  state="disabled")
        self._btn_cap.pack(side="left")

        # ── VM TCP Server row ────────────────────────────────────────────────
        row3 = tk.Frame(self, bg="#1a1a2e", pady=5)
        row3.pack(fill="x", padx=10)

        self._tcp_dot_var = tk.StringVar(value="●")
        self._tcp_dot_lbl = tk.Label(row3, textvariable=self._tcp_dot_var,
                                     font=("Segoe UI", 14), bg="#1a1a2e", fg="#555")
        self._tcp_dot_lbl.pack(side="left", padx=(6, 2))

        tk.Label(row3, text="VM TCP Server", bg="#1a1a2e", fg="#aaa",
                 font=("Segoe UI", 9)).pack(side="left", padx=(2, 6))
        tk.Label(row3, text="Port:", bg="#1a1a2e", fg="#777",
                 font=("Segoe UI", 9)).pack(side="left")
        self._tcp_port_var = tk.StringVar(value="5001")
        tk.Entry(row3, textvariable=self._tcp_port_var, width=5,
                 font=("Segoe UI", 9), bg="#3c3c3c", fg=FG,
                 insertbackground="white", relief="flat").pack(side="left", padx=(2, 6))

        self._btn_tcp = tk.Button(row3, text=" ▶ Start ",
                                  command=self._toggle_tcp_server,
                                  bg="#444", fg=FG, relief="flat", padx=8)
        self._btn_tcp.pack(side="left", padx=(0, 10))

        tk.Label(row3, text="│", bg="#1a1a2e", fg="#333").pack(side="left", padx=4)

        tk.Label(row3, text="H:", bg="#1a1a2e", fg="#7dd3fc",
                 font=("Segoe UI", 9, "bold")).pack(side="left", padx=(4, 2))
        self._vm_h_var = tk.StringVar(value="—")
        tk.Label(row3, textvariable=self._vm_h_var, bg="#1a1a2e", fg="#7dd3fc",
                 font=("Consolas", 10, "bold"), width=7).pack(side="left")

        tk.Label(row3, text="W:", bg="#1a1a2e", fg="#86efac",
                 font=("Segoe UI", 9, "bold")).pack(side="left", padx=(10, 2))
        self._vm_w_var = tk.StringVar(value="—")
        tk.Label(row3, textvariable=self._vm_w_var, bg="#1a1a2e", fg="#86efac",
                 font=("Consolas", 10, "bold"), width=7).pack(side="left")

        tk.Label(row3, text="px", bg="#1a1a2e", fg="#555",
                 font=("Segoe UI", 8)).pack(side="left", padx=(2, 10))

        self._vm_ok_var = tk.StringVar(value="")
        self._vm_ok_lbl = tk.Label(row3, textvariable=self._vm_ok_var,
                                    bg="#1a1a2e", font=("Segoe UI", 10, "bold"))
        self._vm_ok_lbl.pack(side="left")

        # ── main pane: [Master | Captured] || [Tables] ─────────────────────
        outer = tk.PanedWindow(self, orient="horizontal",
                               bg=DARK_BG, sashwidth=5, sashrelief="flat")
        outer.pack(fill="both", expand=True, padx=10, pady=(4, 0))

        # ── left sub-pane: Master + Captured side by side ───────────────────
        img_pane = tk.PanedWindow(outer, orient="horizontal",
                                  bg=DARK_BG, sashwidth=4, sashrelief="flat")
        outer.add(img_pane, minsize=700)

        # Master panel
        master_fr = tk.Frame(img_pane, bg=DARK_BG)
        img_pane.add(master_fr, minsize=280)
        _lbl_bar(master_fr, "MASTER", "#1a3a5c", var=self._master_lbl_var)
        self.master_canvas = tk.Canvas(master_fr, bg="#0d0d0d", highlightthickness=0)
        self.master_canvas.pack(fill="both", expand=True)
        self.master_canvas.bind("<Configure>",
                                lambda e: self._redraw_canvas(self.master_canvas,
                                                               "_master_bgr", "_tk_master"))

        # Captured panel
        cap_fr = tk.Frame(img_pane, bg=DARK_BG)
        img_pane.add(cap_fr, minsize=280)
        self._cap_lbl_var = tk.StringVar(value="ภาพที่ถ่าย")
        self._cap_lbl_bar = _lbl_bar(cap_fr, "ภาพที่ถ่าย", "#1a3a1a",
                                      var=self._cap_lbl_var)
        self.capture_canvas = tk.Canvas(cap_fr, bg="#0a0a0a", highlightthickness=0)
        self.capture_canvas.pack(fill="both", expand=True)
        self.capture_canvas.bind("<Configure>",
                                 lambda e: self._redraw_capture_full())

        img_pane.sash_place(0, 440, 0)

        # ── right panel: tables ─────────────────────────────────────────────
        right = tk.Frame(outer, bg=DARK_BG)
        outer.add(right, minsize=370)

        tk.Label(right, text="Result  (🔵 Master  /  🟢 Captured)",
                 bg=DARK_BG, fg="#aaa", font=("Segoe UI", 9, "bold")).pack(anchor="nw",
                                                                             padx=4, pady=(4, 1))
        tbl_fr = tk.Frame(right, bg=DARK_BG)
        tbl_fr.pack(fill="x", padx=4)
        self._build_result_table(tbl_fr)

        tk.Label(right, text="Blob Configuration (.solw)",
                 bg=DARK_BG, fg="#888", font=("Segoe UI", 9)).pack(anchor="nw",
                                                                     padx=4, pady=(8, 0))
        cfg_fr = tk.Frame(right, bg=DARK_BG)
        cfg_fr.pack(fill="both", expand=True, padx=4)
        self._build_config_table(cfg_fr)

        # ── Log ─────────────────────────────────────────────────────────────
        log_hdr = tk.Frame(right, bg="#1a1a1a")
        log_hdr.pack(fill="x", padx=4, pady=(6, 0))
        tk.Label(log_hdr, text="📋 Log", bg="#1a1a1a", fg="#888",
                 font=("Segoe UI", 8, "bold")).pack(side="left", padx=6, pady=2)
        tk.Button(log_hdr, text="Clear", command=self._log_clear,
                  bg="#333", fg="#888", font=("Segoe UI", 7),
                  relief="flat", padx=4, pady=1).pack(side="right", padx=4)

        log_fr = tk.Frame(right, bg="#111")
        log_fr.pack(fill="x", padx=4, pady=(0, 4))
        self._log_text = tk.Text(log_fr, height=5, bg="#111", fg="#9ca3af",
                                  font=("Consolas", 8), relief="flat",
                                  state="disabled", wrap="none", cursor="arrow")
        log_sb = ttk.Scrollbar(log_fr, orient="vertical",
                                command=self._log_text.yview)
        self._log_text.configure(yscrollcommand=log_sb.set)
        self._log_text.pack(side="left", fill="x", expand=True)
        log_sb.pack(side="right", fill="y")
        # colour tags for log
        self._log_text.tag_configure("ok",  foreground="#4ade80")
        self._log_text.tag_configure("ng",  foreground="#f87171")
        self._log_text.tag_configure("info",foreground="#60a5fa")
        self._log_text.tag_configure("ts",  foreground="#6b7280")

        outer.sash_place(0, 900, 0)

        # ── status bar ──────────────────────────────────────────────────────
        self.status = tk.Label(self, text="เลือกไฟล์ .solw แล้วกด 'วิเคราะห์ Master'",
                               anchor="w", bg="#111", fg="#888",
                               font=("Segoe UI", 9), pady=3)
        self.status.pack(fill="x", padx=10, pady=(2, 5))

        if os.path.isfile(self.path_var.get()):
            self.after(600, self._analyze)   # wait for full layout render
        self.after(400, self._scan_cameras)

    # ────────────────────── table builders ─────────────────────────────────
    def _build_result_table(self, parent):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Result.Treeview", background=DARK_BG, foreground=FG,
                        fieldbackground=DARK_BG, rowheight=26, font=("Segoe UI", 9))
        style.configure("Result.Treeview.Heading", background=ACCENT, foreground="white",
                        font=("Segoe UI", 9, "bold"))
        style.map("Result.Treeview", background=[("selected", "#005a9e")])

        cols = [c[0] for c in RESULT_COLS]
        self.result_tree = ttk.Treeview(parent, columns=cols,
                                         show="headings", height=7,
                                         style="Result.Treeview")
        for key, head, w, anc in RESULT_COLS:
            self.result_tree.heading(key, text=head)
            self.result_tree.column(key, width=w, anchor=anc, stretch=False)  # type: ignore

        sb_x = ttk.Scrollbar(parent, orient="horizontal", command=self.result_tree.xview)
        self.result_tree.configure(xscrollcommand=sb_x.set)
        self.result_tree.pack(fill="x")
        sb_x.pack(fill="x")

        # row colour tags
        self.result_tree.tag_configure("master",   background="#0d2a45", foreground="#7dd3fc")
        self.result_tree.tag_configure("cap_ok",   background="#0d2e0d", foreground="#86efac")
        self.result_tree.tag_configure("cap_ng",   background="#3a0d0d", foreground="#fca5a5")
        self.result_tree.tag_configure("diff_ok",  background="#1a1a1a", foreground="#6b7280")
        self.result_tree.tag_configure("diff_ng",  background="#2a1a0d", foreground="#fb923c")

    def _build_config_table(self, parent):
        style = ttk.Style()
        style.configure("Cfg.Treeview", background=DARK_BG, foreground=FG,
                        fieldbackground=DARK_BG, rowheight=22, font=("Segoe UI", 9))
        style.configure("Cfg.Treeview.Heading", background="#444", foreground="white",
                        font=("Segoe UI", 9, "bold"))
        style.map("Cfg.Treeview", background=[("selected", "#005a9e")])

        self.cfg_tree = ttk.Treeview(parent, columns=("param", "value"),
                                      show="headings", style="Cfg.Treeview")
        self.cfg_tree.heading("param", text="Parameter")
        self.cfg_tree.heading("value", text="Value")
        self.cfg_tree.column("param", width=200, anchor="w")
        self.cfg_tree.column("value", width=160, anchor="center")
        sb = ttk.Scrollbar(parent, orient="vertical", command=self.cfg_tree.yview)
        self.cfg_tree.configure(yscrollcommand=sb.set)
        self.cfg_tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

    # ────────────────────── generic canvas draw ────────────────────────────
    def _redraw_canvas(self, canvas: tk.Canvas, bgr_attr: str, tk_attr: str,
                       tag: str = "image"):
        bgr = getattr(self, bgr_attr, None)
        if bgr is None:
            return
        self.update_idletasks()           # flush geometry so winfo_* is accurate
        cw = canvas.winfo_width()
        ch = canvas.winfo_height()
        if cw < 2 or ch < 2:
            # Canvas not yet rendered — retry after a layout pass
            self.after(120, lambda: self._redraw_canvas(canvas, bgr_attr, tk_attr, tag))
            return
        h, w = bgr.shape[:2]
        scale = min(cw / w, ch / h)
        nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        resampling = getattr(Image, "Resampling", Image).LANCZOS
        pil = Image.fromarray(rgb).resize((nw, nh), resampling)
        tk_img = ImageTk.PhotoImage(pil)
        setattr(self, tk_attr, tk_img)
        canvas.delete(tag)
        canvas.create_image(cw // 2, ch // 2, anchor="center",
                            image=tk_img, tags=tag)

    def _draw_verdict(self, canvas: tk.Canvas, ok: bool, fails: list[str]):
        canvas.delete("verdict")
        cw = canvas.winfo_width()
        ch = canvas.winfo_height()
        text  = "OK" if ok else "NG"
        color = "#00dd44" if ok else "#ff2222"
        # shadow
        canvas.create_text(cw - 18, ch - 18, anchor="se",
                           text=text, font=("Segoe UI", 72, "bold"),
                           fill="#000000", tags="verdict")
        # main
        canvas.create_text(cw - 20, ch - 20, anchor="se",
                           text=text, font=("Segoe UI", 72, "bold"),
                           fill=color, tags="verdict")
        if not ok:
            for i, f in enumerate(fails[:5]):
                canvas.create_text(cw - 14, ch - 115 - i * 22, anchor="se",
                                   text=f"▸ {f}", font=("Segoe UI", 9),
                                   fill="#ffbb55", tags="verdict")

    # ────────────────────── camera helpers ─────────────────────────────────
    def _scan_cameras(self):
        self._cam_devices = self._cam_mgr.enum_devices()
        self._cam_cb["values"] = self._cam_devices
        if self._cam_devices:
            self._cam_cb.current(0)
            self.status.config(text=f"พบกล้อง {len(self._cam_devices)} ตัว", fg="#4ec94e")
        else:
            self._cam_var.set("— ไม่พบกล้อง —")
            self.status.config(text="ไม่พบกล้อง", fg="#e04040")

    def _toggle_connect(self):
        if self._cam_mgr.is_connected:
            self._cam_mgr.disconnect()
            self._dot_lbl.config(fg="#555")
            self._dot_var.set("●")
            self._btn_connect.config(text=" เชื่อมต่อ ", bg="#444")
            self._btn_cap.config(state="disabled", bg="#555", fg="#888")
            self.status.config(text="ตัดการเชื่อมต่อกล้องแล้ว", fg="#888")
        else:
            idx = self._cam_cb.current()
            if idx < 0 or not self._cam_devices:
                messagebox.showwarning("ไม่พบกล้อง", "กรุณากด Scan ก่อน")
                return
            err = self._cam_mgr.connect(idx)
            if err:
                messagebox.showerror("เชื่อมต่อไม่สำเร็จ", err)
                return
            self._dot_lbl.config(fg="#4ec94e")
            self._dot_var.set("●")
            self._btn_connect.config(text=" ตัดการเชื่อมต่อ ", bg="#7a2d2d")
            self._btn_cap.config(state="normal", bg="#c84b00", fg="white")
            cam_name = self._cam_var.get()
            self._log_add(f"เชื่อมต่อกล้อง: {cam_name}", "info")
            self.status.config(text=f"เชื่อมต่อกล้องสำเร็จ  ▸  {cam_name}", fg="#4ec94e")

    # ────────────────────── VM TCP Server ──────────────────────────────────
    def _toggle_tcp_server(self):
        if self._tcp_srv.is_running:
            self._tcp_srv.stop()
            self._tcp_dot_lbl.config(fg="#555")
            self._btn_tcp.config(text=" ▶ Start ", bg="#444")
            self._vm_ok_var.set("")
            self._log_add("VM TCP Server หยุดแล้ว", "info")
            self.status.config(text="TCP Server หยุด", fg="#888")
        else:
            try:
                port = int(self._tcp_port_var.get())
            except ValueError:
                messagebox.showerror("Port ไม่ถูกต้อง", "กรุณาใส่ตัวเลข Port")
                return
            err = self._tcp_srv.start(port)
            if err:
                messagebox.showerror("เริ่ม TCP Server ไม่สำเร็จ", err)
                return
            self._tcp_dot_lbl.config(fg="#f59e0b")   # amber = listening
            self._btn_tcp.config(text=" ■ Stop  ", bg="#7a2d2d")
            self._log_add(f"VM TCP Server เริ่มแล้ว  127.0.0.1:{port}", "info")
            self.status.config(
                text=f"TCP Server รอรับข้อมูลจาก VM  port:{port}", fg="#f59e0b")

    def _vm_data_received(self, line: str):
        """Called from TCP thread — forward to Tk main thread."""
        self.after(0, lambda l=line: self._process_vm_data(l))

    def _process_vm_data(self, line: str):
        """Parse 'H,W' line from Vision Master, update UI and OK/NG."""
        parts = line.split(",")
        if len(parts) < 2:
            self._log_add(f"VM data รูปแบบไม่ถูก: {line!r}", "ng")
            return
        try:
            h = float(parts[0].strip())
            w = float(parts[1].strip())
        except ValueError:
            self._log_add(f"VM data แปลงค่าไม่ได้: {line!r}", "ng")
            return

        self._vm_h_var.set(f"{h:.1f}")
        self._vm_w_var.set(f"{w:.1f}")
        self._tcp_dot_lbl.config(fg="#4ec94e")   # green = data flowing

        ok, fails = self._check_vm_limits(h, w)
        if ok:
            self._vm_ok_var.set("✅ OK")
            self._vm_ok_lbl.config(fg="#4ade80")
        else:
            self._vm_ok_var.set("❌ NG")
            self._vm_ok_lbl.config(fg="#f87171")

        fail_str = "  |  " + " | ".join(fails) if fails else ""
        self._log_add(
            f"VM → H:{h:.1f}  W:{w:.1f}{'  ✅ OK' if ok else '  ❌ NG' + fail_str}",
            "ok" if ok else "ng")
        self.status.config(
            text=f"VM Data  H:{h:.1f} px  W:{w:.1f} px  {'✅ OK' if ok else '❌ NG'}",
            fg="#4ec94e" if ok else "#e04040")

    def _check_vm_limits(self, h: float, w: float) -> tuple[bool, list[str]]:
        """Check received H/W against user-defined limits from Settings."""
        fails = []
        try:
            h_lo, h_hi = float(self._vm_h_lo.get()), float(self._vm_h_hi.get())
            if not (h_lo <= h <= h_hi):
                fails.append(f"H={h:.1f} (ต้องการ {h_lo:.1f}–{h_hi:.1f})")
        except ValueError:
            pass
        try:
            w_lo, w_hi = float(self._vm_w_lo.get()), float(self._vm_w_hi.get())
            if not (w_lo <= w <= w_hi):
                fails.append(f"W={w:.1f} (ต้องการ {w_lo:.1f}–{w_hi:.1f})")
        except ValueError:
            pass
        return len(fails) == 0, fails

    def _browse(self):
        """Browse and select a .solw file."""
        filename = filedialog.askopenfilename(
            title="เลือกไฟล์ .solw",
            filetypes=[("SOLW files", "*.solw"), ("All files", "*.*")]
        )
        if filename:
            self.path_var.set(filename)

    def _open_picture(self):
        """Open a picture, run blob analysis as a capture, and display it."""
        filename = filedialog.askopenfilename(
            title="เลือกรูปภาพ",
            filetypes=[("Image files", "*.jpg *.jpeg *.png *.bmp"), ("All files", "*.*")]
        )
        if not filename:
            return

        params = self._current_params if self._current_params else {}
        try:
            annotated, blobs = run_blob_analysis(filename, params)
        except Exception as e:
            messagebox.showerror("Analysis Error", f"ไม่สามารถวิเคราะห์รูปภาพได้:\n{e}")
            return

        # Build master for comparison (from last .solw analysis)
        master = None
        if self._master_blobs:
            mb = self._master_blobs[0]
            try:
                tol = float(self._tol_var.get()) / 100.0
            except:
                tol = 0.10
            master = {**mb, "_tolerance": tol}

        ok, fails = check_pass_fail(blobs, params, master)
        self._capture_done(annotated, blobs, ok, fails, filename)

    # ────────────────────── settings popup ─────────────────────────────────
    def _open_settings(self):
        """เปิดหน้าต่างการตั้งค่า (Angle Range และอื่น ๆ)"""
        win = tk.Toplevel(self)
        win.title("⚙  การตั้งค่า")
        win.configure(bg=MID_BG)
        win.resizable(False, False)
        win.grab_set()
        # จัดกึ่งกลางบนหน้าต่างหลัก
        self.update_idletasks()
        px = self.winfo_x() + self.winfo_width()  // 2 - 220
        py = self.winfo_y() + self.winfo_height() // 2 - 215
        win.geometry(f"440x430+{px}+{py}")

        # ── ดึงค่าปัจจุบันจาก params ───────────────────────────────────────
        p = self._current_params
        ang_enable = tk.BooleanVar(
            value=p.get("AngleLimitEnable", "False").lower() == "true")
        ang_lo_var = tk.StringVar(value=p.get("AngleLimitLow",  "-180"))
        ang_hi_var = tk.StringVar(value=p.get("AngleLimitHigh", "180"))

        # ── หัวข้อ ──────────────────────────────────────────────────────────
        tk.Label(win, text="⚙  การตั้งค่า", bg=MID_BG, fg=FG,
                 font=("Segoe UI", 11, "bold")).pack(anchor="w", padx=16, pady=(12, 4))
        ttk.Separator(win, orient="horizontal").pack(fill="x", padx=12, pady=(0, 8))

        # ── Angle Range frame ────────────────────────────────────────────────
        frm = tk.LabelFrame(win,
                            text="  Angle Range  ",
                            bg=MID_BG, fg="#7dd3fc",
                            font=("Segoe UI", 9, "bold"),
                            labelanchor="nw", bd=1, relief="groove")
        frm.pack(fill="x", padx=16, pady=(0, 10), ipadx=8, ipady=8)

        # Enable checkbox
        chk_fr = tk.Frame(frm, bg=MID_BG)
        chk_fr.pack(anchor="w", padx=6, pady=(4, 8))
        tk.Checkbutton(chk_fr,
                       text="เปิดใช้งาน Angle Limit",
                       variable=ang_enable,
                       bg=MID_BG, fg=FG,
                       selectcolor="#3c3c3c",
                       activebackground=MID_BG, activeforeground=FG,
                       font=("Segoe UI", 9)).pack(side="left")

        # Low / High spinboxes
        rng_fr = tk.Frame(frm, bg=MID_BG)
        rng_fr.pack(anchor="w", padx=6, pady=(0, 4))

        _lbl_kw = dict(bg=MID_BG, fg="#aaa", font=("Segoe UI", 9))
        _spn_kw = dict(from_=-180, to=180, increment=1, width=7,
                       font=("Segoe UI", 9), bg="#3c3c3c", fg=FG,
                       buttonbackground="#555", relief="flat",
                       insertbackground="white")

        tk.Label(rng_fr, text="Low", **_lbl_kw).pack(side="left")
        tk.Spinbox(rng_fr, textvariable=ang_lo_var, **_spn_kw).pack(
            side="left", padx=(4, 20))

        tk.Label(rng_fr, text="High", **_lbl_kw).pack(side="left")
        tk.Spinbox(rng_fr, textvariable=ang_hi_var, **_spn_kw).pack(
            side="left", padx=(4, 0))

        # ── VM Measurement Limits frame ─────────────────────────────────────
        vm_frm = tk.LabelFrame(win,
                               text="  VM Measurement Limits  ",
                               bg=MID_BG, fg="#86efac",
                               font=("Segoe UI", 9, "bold"),
                               labelanchor="nw", bd=1, relief="groove")
        vm_frm.pack(fill="x", padx=16, pady=(0, 10), ipadx=8, ipady=6)

        vm_r1 = tk.Frame(vm_frm, bg=MID_BG)
        vm_r1.pack(anchor="w", padx=6, pady=(4, 2))
        tk.Label(vm_r1, text="H  Low", **_lbl_kw).pack(side="left")
        tk.Entry(vm_r1, textvariable=self._vm_h_lo, width=7,
                 font=("Segoe UI", 9), bg="#3c3c3c", fg=FG,
                 insertbackground="white", relief="flat").pack(side="left", padx=(4, 16))
        tk.Label(vm_r1, text="H  High", **_lbl_kw).pack(side="left")
        tk.Entry(vm_r1, textvariable=self._vm_h_hi, width=7,
                 font=("Segoe UI", 9), bg="#3c3c3c", fg=FG,
                 insertbackground="white", relief="flat").pack(side="left", padx=(4, 0))

        vm_r2 = tk.Frame(vm_frm, bg=MID_BG)
        vm_r2.pack(anchor="w", padx=6, pady=(2, 4))
        tk.Label(vm_r2, text="W  Low", **_lbl_kw).pack(side="left")
        tk.Entry(vm_r2, textvariable=self._vm_w_lo, width=7,
                 font=("Segoe UI", 9), bg="#3c3c3c", fg=FG,
                 insertbackground="white", relief="flat").pack(side="left", padx=(4, 16))
        tk.Label(vm_r2, text="W  High", **_lbl_kw).pack(side="left")
        tk.Entry(vm_r2, textvariable=self._vm_w_hi, width=7,
                 font=("Segoe UI", 9), bg="#3c3c3c", fg=FG,
                 insertbackground="white", relief="flat").pack(side="left", padx=(4, 0))

        # ── ปุ่ม Apply / Cancel ─────────────────────────────────────────────
        ttk.Separator(win, orient="horizontal").pack(fill="x", padx=12, pady=(4, 8))

        btn_fr = tk.Frame(win, bg=MID_BG)
        btn_fr.pack(fill="x", padx=16, pady=(0, 12))

        def _apply():
            try:
                lo = float(ang_lo_var.get())
                hi = float(ang_hi_var.get())
            except ValueError:
                messagebox.showerror("ค่าไม่ถูกต้อง",
                                     "กรุณาใส่ตัวเลขที่ถูกต้องสำหรับ Angle Range",
                                     parent=win)
                return
            if lo > hi:
                messagebox.showerror("ค่าไม่ถูกต้อง",
                                     "Low ต้องน้อยกว่าหรือเท่ากับ High",
                                     parent=win)
                return
            # บันทึกค่าลง params
            self._current_params["AngleLimitEnable"] = str(ang_enable.get())
            self._current_params["AngleLimitLow"]    = str(lo)
            self._current_params["AngleLimitHigh"]   = str(hi)
            self._update_config_table(self._current_params)
            self._log_add(
                f"ตั้งค่า Angle Range: {'เปิด' if ang_enable.get() else 'ปิด'}"
                f"  Low={lo:.0f}°  High={hi:.0f}°",
                "info")
            win.destroy()

        tk.Button(btn_fr, text=" ยกเลิก ", command=win.destroy,
                  bg="#555", fg=FG, relief="flat", padx=10).pack(side="right", padx=(6, 0))
        tk.Button(btn_fr, text=" ✓ ใช้งาน ", command=_apply,
                  bg=ACCENT, fg="white",
                  font=("Segoe UI", 9, "bold"),
                  relief="flat", padx=10).pack(side="right")

    def _redraw_capture_full(self):
        """Redraw capture canvas, verdict text, and measurements."""
        if self._capture_bgr is not None:
            self._redraw_canvas(self.capture_canvas, "_capture_bgr", "_tk_capture")
            self._draw_verdict(self.capture_canvas, self._tbl_cap_ok, self._capture_fails)
            self._draw_measurements(self.capture_canvas, self._capture_blobs or [], self._master_blobs)

    def _draw_measurements(self, canvas: tk.Canvas, blobs: list,
                            master_blobs: list | None):
        """Overlay Long / Short / Area values + Δ% vs Master on the canvas."""
        canvas.delete("measurements")
        if not blobs:
            return
        b = blobs[0]
        try:
            tol = float(self._tol_var.get()) / 100.0
        except Exception:
            tol = 0.10

        mb = master_blobs[0] if master_blobs else None
        rows = []
        for field, label in [("long_axis", "Long "),
                              ("short_axis", "Short"),
                              ("area",       "Area ")]:
            val = b.get(field, 0)
            if mb:
                ref = mb.get(field, 0)
                if ref > 0:
                    d   = (val - ref) / ref * 100
                    out = abs(d) > tol * 100
                    mark = " ⚠" if out else " ✓"
                    if field == "area":
                        text = f"{label}: {val:>10.0f}  M:{ref:>10.0f}  Δ{d:+.1f}%{mark}"
                    else:
                        text = f"{label}: {val:>10.2f}  M:{ref:>10.2f}  Δ{d:+.1f}%{mark}"
                else:
                    text = f"{label}: {val:>10.0f}" if field == "area" else f"{label}: {val:>10.2f}"
                    out  = False
            else:
                text = f"{label}: {val:>10.0f}" if field == "area" else f"{label}: {val:>10.2f}"
                out  = False
            rows.append((text, out))

        x, y = 8, 8
        for text, out in rows:
            color = "#ff7744" if out else "#44ff99"
            canvas.create_text(x + 1, y + 1, anchor="nw", text=text,
                               font=("Consolas", 11, "bold"), fill="#000000",
                               tags="measurements")
            canvas.create_text(x, y, anchor="nw", text=text,
                               font=("Consolas", 11, "bold"), fill=color,
                               tags="measurements")
            y += 22

    def _capture_error(self, err_msg: str):
        """Handle errors during image capture or analysis."""
        self._btn_cap.config(state="normal", text="  📷  ถ่ายภาพ  ")
        messagebox.showerror("Capture Error", err_msg)
        self._log_add(f"Capture error: {err_msg}", "ng")

    def _capture(self):
        self._btn_cap.config(state="disabled", text="  ⏳  กำลังถ่าย…  ")
        self.update_idletasks()

        def _do():
            try:
                frame = self._cam_mgr.capture_frame(timeout_ms=4000)
                params = dict(self._current_params) if self._current_params else {}

                # Scale MinArea/MaxArea when camera resolution differs from master image
                if self._master_img_hw and frame is not None:
                    mh, mw = self._master_img_hw
                    fh, fw = frame.shape[:2]
                    ratio = (fw * fh) / (mw * mh)
                    if abs(ratio - 1.0) > 0.02:
                        for k in ('MinArea', 'MaxArea'):
                            if k in params:
                                try:
                                    params[k] = str(round(float(params[k]) * ratio))
                                except Exception:
                                    pass

                try:
                    annotated, blobs = run_blob_analysis(frame, params)
                except Exception as e:
                    self.after(0, lambda: self._capture_error(str(e)))
                    return

                # Fallback: area filter might be wrong scale – find largest blob freely
                if not blobs:
                    try:
                        fallback = dict(params)
                        fallback['MinArea']          = '500'
                        fallback['MaxArea']          = '999999999'
                        fallback['MinLongAxis']      = '5'
                        fallback['MaxLongAxis']      = '999999999'
                        fallback['MinShortAxis']     = '5'
                        fallback['MaxShortAxis']     = '999999999'
                        fallback['MinCircularity']   = '0'
                        fallback['MaxCircularity']   = '2'
                        fallback['MinRectangularity']= '0'
                        fallback['MaxRectangularity']= '2'
                        fallback['SelectByArea']     = 'True'
                        fallback['FindNum']          = '1'
                        annotated, blobs = run_blob_analysis(frame, fallback)
                    except Exception:
                        pass

                # Build master for comparison (from last .solw analysis)
                master = None
                if self._master_blobs:
                    mb = self._master_blobs[0]
                    try:
                        tol = float(self._tol_var.get()) / 100.0
                    except:
                        tol = 0.10
                    master = {**mb, "_tolerance": tol}

                ok, fails = check_pass_fail(blobs, params, master)
                self.after(0, lambda: self._capture_done(annotated, blobs, ok, fails))
            except Exception as e:
                self.after(0, lambda: self._capture_error(str(e)))

        threading.Thread(target=_do, daemon=True).start()

    def _capture_done(self, annotated: np.ndarray, blobs: list,
                      ok: bool, fails: list[str], filename: str | None = None):
        # store captured image and refresh its canvas
        self._capture_bgr   = annotated
        self._capture_blobs = blobs
        self._capture_fails = fails
        self._tbl_cap_ok    = ok
        self._redraw_canvas(self.capture_canvas, "_capture_bgr", "_tk_capture")
        self._draw_verdict(self.capture_canvas, ok, fails)
        self._draw_measurements(self.capture_canvas, blobs, self._master_blobs)

        # update label bar colour
        bar_bg = "#1a3a1a" if ok else "#5a1a1a"
        if filename:
            self._cap_lbl_var.set("✅  OK  —  ภาพที่เปิด" if ok else "❌  NG  —  ภาพที่เปิด")
        else:
            self._cap_lbl_var.set("✅  OK  —  ภาพที่ถ่าย" if ok else "❌  NG  —  ภาพที่ถ่าย")
        self._cap_lbl_bar.config(bg=bar_bg)
        self._cap_lbl_bar.master.config(bg=bar_bg)  # type: ignore

        # re-enable capture button if camera is connected
        if self._cam_mgr.is_connected:
            self._btn_cap.config(state="normal", text="  📷  ถ่ายภาพ  ",
                                 bg="#c84b00", fg="white")
        else:
            self._btn_cap.config(state="disabled", text="  📷  ถ่ายภาพ  ",
                                 bg="#555", fg="#888")

        self._update_result_table(capture_blobs=blobs, capture_ok=ok)

        b0 = blobs[0] if blobs else {}
        if ok:
            self._log_add(
                f"CAPTURE ✅ OK  Long:{b0.get('long_axis',0):.2f}"
                f"  Short:{b0.get('short_axis',0):.2f}"
                f"  Area:{b0.get('area',0):.0f}",
                "ok")
        else:
            self._log_add(
                f"CAPTURE ❌ NG  " + " | ".join(fails[:3]),
                "ng")

        if filename:
            status_text = f"Open Picture  |  {os.path.basename(filename)}  |  {'✅ OK' if ok else '❌ NG'}  |  Blob: {len(blobs)}"
        else:
            status_text = f"ถ่ายภาพสำเร็จ  |  {'✅ OK' if ok else '❌ NG'}  |  Blob: {len(blobs)}"
            
        self.status.config(
            text=status_text,
            fg="#4ec94e" if ok else "#e04040")

    def _select_master_image(self):
        """เลือกภาพ Master โดยตรงจากไฟล์ โดยใช้ params จาก .solw เดิม"""
        if not self._current_params:
            messagebox.showwarning(
                "ยังไม่โหลด .solw",
                "กรุณากด 'วิเคราะห์ Master' เพื่อโหลด config ก่อน\n"
                "แล้วจึงเลือกภาพ Master ใหม่")
            return

        p = filedialog.askopenfilename(
            title="เลือกภาพ Master",
            filetypes=[("Image files", "*.jpg *.jpeg *.png *.bmp *.tif *.tiff"),
                       ("All files", "*.*")])
        if not p:
            return

        try:
            annotated, blobs = run_blob_analysis(p, self._current_params)
        except Exception as e:
            messagebox.showerror("Analysis Error", str(e))
            return

        if not blobs:
            relaxed = dict(self._current_params)
            relaxed['MinArea']           = '100'
            relaxed['MaxArea']           = '999999999'
            relaxed['MinLongAxis']       = '5'
            relaxed['MaxLongAxis']       = '999999999'
            relaxed['MinShortAxis']      = '5'
            relaxed['MaxShortAxis']      = '999999999'
            relaxed['MinCircularity']    = '0'
            relaxed['MaxCircularity']    = '2'
            relaxed['MinRectangularity'] = '0'
            relaxed['MaxRectangularity'] = '2'
            relaxed['SelectByArea']      = 'True'
            relaxed['FindNum']           = '1'
            try:
                annotated, blobs = run_blob_analysis(p, relaxed)
            except Exception:
                pass

        # อัปเดต Master
        self._master_blobs    = blobs
        self._master_bgr      = annotated
        self._master_img_hw   = annotated.shape[:2]
        self._master_is_custom = True
        self._master_lbl_var.set(f"MASTER  [{os.path.basename(p)}]")
        self._redraw_canvas(self.master_canvas, "_master_bgr", "_tk_master")
        self._update_result_table(master_blobs=blobs)

        b0 = blobs[0] if blobs else {}
        self._log_add(
            f"MASTER (เลือกเอง)  {os.path.basename(p)}"
            f"  Blob:{len(blobs)}"
            + (f"  Long:{b0.get('long_axis',0):.2f}"
               f"  Short:{b0.get('short_axis',0):.2f}"
               f"  Area:{b0.get('area',0):.0f}" if b0 else ""),
            "info")
        self.status.config(
            text=f"✅  Master (ภาพเลือกเอง)  ▸  {os.path.basename(p)}"
                 f"  |  Blob: {len(blobs)} รายการ",
            fg="#4ec94e")

    def _analyze(self):
        """Load .solw, run blob analysis on master image, save as reference."""
        solw = self.path_var.get().strip()
        if not os.path.isfile(solw):
            messagebox.showerror("ไม่พบไฟล์", f"ไม่พบ:\n{solw}")
            return
        try:
            params, img_path = load_solw(solw)
        except Exception as e:
            messagebox.showerror("Parse Error", str(e))
            return

        # If user has set a custom master, ask whether to reset it
        skip_master_update = False
        if self._master_is_custom:
            reset = messagebox.askyesno(
                "รีเซ็ต Master?",
                "ตอนนี้ใช้ภาพ Master ที่เลือกเอง\n"
                "ต้องการรีเซ็ตเป็น Master จากไฟล์ .solw หรือไม่?")
            if reset:
                self._master_is_custom = False
                self._master_lbl_var.set("MASTER")
            else:
                skip_master_update = True

        self._current_params = params

        if not img_path or not os.path.isfile(img_path):
            self._update_config_table(params)
            self.status.config(text="โหลด config สำเร็จ  |  ไม่พบภาพ Master", fg="#e0a030")
            return

        if skip_master_update:
            self._update_config_table(params)
            self.status.config(
                text=f"✅  อัปเดต config แล้ว  |  ยังคงใช้ Master เดิม",
                fg="#4ec94e")
            return

        try:
            annotated, blobs = run_blob_analysis(img_path, params)
        except Exception as e:
            messagebox.showerror("Analysis Error", str(e))
            return

        self._master_blobs  = blobs
        self._master_bgr    = annotated
        self._master_img_hw = annotated.shape[:2]   # (h, w) — used to scale area limits
        self._redraw_canvas(self.master_canvas, "_master_bgr", "_tk_master")
        self._update_config_table(params)
        # update master rows — captured rows are kept as-is
        self._update_result_table(master_blobs=blobs)

        b0 = blobs[0] if blobs else {}
        self._log_add(
            f"MASTER  {os.path.basename(img_path)}"
            f"  Blob:{len(blobs)}"
            + (f"  Long:{b0.get('long_axis',0):.2f}"
               f"  Short:{b0.get('short_axis',0):.2f}"
               f"  Area:{b0.get('area',0):.0f}" if b0 else ""),
            "info"
        )
        self.status.config(
            text=f"✅  Master โหลดแล้ว  |  {os.path.basename(solw)}"
                 f"  ▸  {os.path.basename(img_path)}"
                 f"  |  Blob: {len(blobs)} รายการ",
            fg="#4ec94e")

    # ────────────────────── table updates ──────────────────────────────────
    @staticmethod
    def _blob_row(source: str, idx: int, b: dict) -> tuple:
        return (
            source, str(idx),
            f"{b['area']:.0f}",
            f"{b['perimeter']:.3f}",
            f"{b['centroid_x']:.4f}",
            f"{b['centroid_y']:.4f}",
            f"{b['angle']:.5f}",
            f"{b['long_axis']:.4f}",
            f"{b['short_axis']:.4f}",
            f"{b['circularity']:.7f}",
            f"{b['rectangularity']:.7f}",
        )

    def _update_result_table(self,
                              master_blobs: list | None = None,
                              capture_blobs: list | None = None,
                              capture_ok: bool = True):
        """Rebuild the result table from stored state.
        Only the supplied argument(s) are updated; the other keeps its last value.
        """
        # ── update stored state ────────────────────────────────────────────
        if master_blobs is not None:
            self._tbl_master_blobs = master_blobs
        if capture_blobs is not None:
            self._tbl_cap_blobs = capture_blobs
            self._tbl_cap_ok    = capture_ok

        # ── full rebuild from stored state ─────────────────────────────────
        self.result_tree.delete(*self.result_tree.get_children())

        # Master rows (blue)
        for i, b in enumerate(self._tbl_master_blobs or []):
            self.result_tree.insert("", "end",
                                     values=self._blob_row("MASTER", i, b),
                                     tags=("master",), iid=f"m_{i}")

        # Captured rows (green/red)
        tag = "cap_ok" if self._tbl_cap_ok else "cap_ng"
        for i, b in enumerate(self._tbl_cap_blobs or []):
            self.result_tree.insert("", "end",
                                     values=self._blob_row("CAPTURED", i, b),
                                     tags=(tag,), iid=f"c_{i}")

        # Δ% diff row — each delta in its own column
        mb_list = self._tbl_master_blobs or []
        cb_list = self._tbl_cap_blobs    or []
        if mb_list and cb_list:
            mb, cb = mb_list[0], cb_list[0]
            try:
                tol = float(self._tol_var.get()) / 100.0
            except Exception:
                tol = 0.10
            diff_map = {"source": "Δ%"}
            any_out  = False
            for key in ("area", "perimeter", "long_axis", "short_axis"):
                ref = mb.get(key, 0)
                val = cb.get(key, 0)
                if ref > 0:
                    d   = (val - ref) / ref * 100
                    out = abs(d) > tol * 100
                    diff_map[key] = f"Δ{d:+.1f}%{'  ⚠' if out else '  ✓'}"
                    if out:
                        any_out = True
            diff_row = tuple(diff_map.get(c[0], "") for c in RESULT_COLS)
            self.result_tree.insert("", "end", values=diff_row,
                                     tags=("diff_ng" if any_out else "diff_ok",),
                                     iid="d_0")

    def _log_add(self, msg: str, level: str = "info"):
        """Append a timestamped line to the log (level: info / ok / ng)."""
        import datetime
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        self._log_text.config(state="normal")
        self._log_text.insert("end", f"[{ts}] ", "ts")
        self._log_text.insert("end", msg + "\n", level)
        # keep only last 200 lines
        lines = int(self._log_text.index("end-1c").split(".")[0])
        if lines > 200:
            self._log_text.delete("1.0", f"{lines - 200}.0")
        self._log_text.see("end")
        self._log_text.config(state="disabled")

    def _log_clear(self):
        self._log_text.config(state="normal")
        self._log_text.delete("1.0", "end")
        self._log_text.config(state="disabled")

    def _update_config_table(self, params: dict):
        self.cfg_tree.delete(*self.cfg_tree.get_children())
        hl = {"MinLongAxis","MaxLongAxis","LongAxisLimitEnable","LongAxisLimitLow","LongAxisLimitHigh",
              "MinArea","MaxArea","ThresholdType","LowThreshold","HightThreshold","Polarity","FindNum"}
        for k, v in params.items():
            self.cfg_tree.insert("", "end", values=(k, v),
                                  tags=("hl" if k in hl else "normal",))
        self.cfg_tree.tag_configure("hl",     background="#1a2a3a", foreground="#7dd3fc")
        self.cfg_tree.tag_configure("normal", background=DARK_BG,   foreground=FG)

    # ────────────────────── cleanup ────────────────────────────────────────
    def _on_close(self):
        self._tcp_srv.stop()
        self._cam_mgr.disconnect()
        if _SDK_AVAILABLE:
            try: MvCamera.MV_CC_Finalize()
            except: pass
        self.destroy()


if __name__ == "__main__":
    app = App()
    app.mainloop()
