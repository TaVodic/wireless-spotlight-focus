from __future__ import annotations

import ctypes
import math
import queue
import re
import statistics
import threading
import time
import tkinter as tk
from collections import deque
from dataclasses import dataclass
from tkinter import messagebox, ttk


DEFAULT_PORT = "COM5"
DEFAULT_BAUD = 115200
REFRESH_MS = 500
MAX_SAMPLES = 5000
OVERFLOW_SECONDS = 0.06
RESTART_BANNER = "STM32WL SUBGHZ DEVEL"


@dataclass(frozen=True)
class PacketSample:
    rssi_dbm: int
    crc_errors: int
    packet_period_s: float
    elapsed_overflows: int
    cfo_khz: int
    received_at: float


@dataclass(frozen=True)
class RestartEvent:
    message: str


class PacketParser:
    def __init__(self) -> None:
        self.current: dict[str, int] = {}

    def feed_line(self, line: str) -> PacketSample | None:
        line = line.strip()
        if not line:
            return None
        if line == "OnRxDone!":
            self.current = {}
            return None

        rx = re.search(r"RssiValue=(-?\d+)\s*dBm,\s*Cfo=(-?\d+)kHz", line)
        if rx:
            self.current["rssi_dbm"] = int(rx.group(1))
            self.current["cfo_khz"] = int(rx.group(2))
            return None

        crc = re.search(r"CRC error sum:(-?\d+)", line)
        if crc:
            self.current["crc_errors"] = int(crc.group(1))
            return None

        elapsed = re.search(r"elapsed=(-?\d+)\s*ns", line)
        if elapsed:
            self.current["elapsed_ns"] = int(elapsed.group(1))
            return None

        overflows = re.search(r"elapsed_overflows=(-?\d+)", line)
        if overflows:
            self.current["elapsed_overflows"] = int(overflows.group(1))
            return self._build_sample()

        return None

    def _build_sample(self) -> PacketSample | None:
        required = {"rssi_dbm", "cfo_khz", "crc_errors", "elapsed_ns", "elapsed_overflows"}
        if not required.issubset(self.current):
            return None
        packet_period_s = self.current["elapsed_ns"] / 1_000_000_000 + (
            OVERFLOW_SECONDS * self.current["elapsed_overflows"]
        )
        return PacketSample(
            rssi_dbm=self.current["rssi_dbm"],
            crc_errors=self.current["crc_errors"],
            packet_period_s=packet_period_s,
            elapsed_overflows=self.current["elapsed_overflows"],
            cfo_khz=self.current["cfo_khz"],
            received_at=time.time(),
        )


class SerialError(RuntimeError):
    pass


class PySerialPort:
    def __init__(self, port: str, baud: int) -> None:
        import serial

        self._serial = serial.Serial(port, baud, timeout=0.2)

    def readline(self) -> bytes:
        return self._serial.readline()

    def close(self) -> None:
        self._serial.close()


class WindowsSerialPort:
    GENERIC_READ = 0x80000000
    GENERIC_WRITE = 0x40000000
    OPEN_EXISTING = 3
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    class DCB(ctypes.Structure):
        _fields_ = [
            ("DCBlength", ctypes.c_uint32),
            ("BaudRate", ctypes.c_uint32),
            ("fBinary", ctypes.c_uint32, 1),
            ("fParity", ctypes.c_uint32, 1),
            ("fOutxCtsFlow", ctypes.c_uint32, 1),
            ("fOutxDsrFlow", ctypes.c_uint32, 1),
            ("fDtrControl", ctypes.c_uint32, 2),
            ("fDsrSensitivity", ctypes.c_uint32, 1),
            ("fTXContinueOnXoff", ctypes.c_uint32, 1),
            ("fOutX", ctypes.c_uint32, 1),
            ("fInX", ctypes.c_uint32, 1),
            ("fErrorChar", ctypes.c_uint32, 1),
            ("fNull", ctypes.c_uint32, 1),
            ("fRtsControl", ctypes.c_uint32, 2),
            ("fAbortOnError", ctypes.c_uint32, 1),
            ("fDummy2", ctypes.c_uint32, 17),
            ("wReserved", ctypes.c_uint16),
            ("XonLim", ctypes.c_uint16),
            ("XoffLim", ctypes.c_uint16),
            ("ByteSize", ctypes.c_uint8),
            ("Parity", ctypes.c_uint8),
            ("StopBits", ctypes.c_uint8),
            ("XonChar", ctypes.c_char),
            ("XoffChar", ctypes.c_char),
            ("ErrorChar", ctypes.c_char),
            ("EofChar", ctypes.c_char),
            ("EvtChar", ctypes.c_char),
            ("wReserved1", ctypes.c_uint16),
        ]

    class COMMTIMEOUTS(ctypes.Structure):
        _fields_ = [
            ("ReadIntervalTimeout", ctypes.c_uint32),
            ("ReadTotalTimeoutMultiplier", ctypes.c_uint32),
            ("ReadTotalTimeoutConstant", ctypes.c_uint32),
            ("WriteTotalTimeoutMultiplier", ctypes.c_uint32),
            ("WriteTotalTimeoutConstant", ctypes.c_uint32),
        ]

    def __init__(self, port: str, baud: int) -> None:
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        path = port if port.startswith("\\\\.\\") else f"\\\\.\\{port}"
        handle = self._kernel32.CreateFileW(
            ctypes.c_wchar_p(path),
            self.GENERIC_READ | self.GENERIC_WRITE,
            0,
            None,
            self.OPEN_EXISTING,
            0,
            None,
        )
        if handle == self.INVALID_HANDLE_VALUE:
            raise SerialError(f"Could not open {port}: Windows error {ctypes.get_last_error()}")
        self._handle = handle
        try:
            self._configure(baud)
        except Exception:
            self.close()
            raise

    def _configure(self, baud: int) -> None:
        dcb = self.DCB()
        dcb.DCBlength = ctypes.sizeof(self.DCB)
        if not self._kernel32.GetCommState(self._handle, ctypes.byref(dcb)):
            raise SerialError(f"GetCommState failed: Windows error {ctypes.get_last_error()}")
        dcb.BaudRate = baud
        dcb.ByteSize = 8
        dcb.Parity = 0
        dcb.StopBits = 0
        dcb.fBinary = 1
        dcb.fDtrControl = 1
        dcb.fRtsControl = 1
        if not self._kernel32.SetCommState(self._handle, ctypes.byref(dcb)):
            raise SerialError(f"SetCommState failed: Windows error {ctypes.get_last_error()}")
        timeouts = self.COMMTIMEOUTS(50, 0, 200, 0, 200)
        if not self._kernel32.SetCommTimeouts(self._handle, ctypes.byref(timeouts)):
            raise SerialError(f"SetCommTimeouts failed: Windows error {ctypes.get_last_error()}")

    def readline(self) -> bytes:
        result = bytearray()
        while True:
            buf = ctypes.create_string_buffer(1)
            count = ctypes.c_uint32(0)
            ok = self._kernel32.ReadFile(self._handle, buf, 1, ctypes.byref(count), None)
            if not ok:
                raise SerialError(f"ReadFile failed: Windows error {ctypes.get_last_error()}")
            if count.value == 0:
                return bytes(result)
            result.extend(buf.raw[: count.value])
            if result.endswith(b"\n") or result.endswith(b"\r"):
                return bytes(result)

    def close(self) -> None:
        if getattr(self, "_handle", None):
            self._kernel32.CloseHandle(self._handle)
            self._handle = None


def open_serial_port(port: str, baud: int):
    try:
        return PySerialPort(port, baud)
    except ModuleNotFoundError:
        if not hasattr(ctypes, "WinDLL"):
            raise SerialError("pyserial is not installed, and the built-in fallback only supports Windows.")
        return WindowsSerialPort(port, baud)


class SerialReader(threading.Thread):
    def __init__(self, sample_queue: queue.Queue, status_queue: queue.Queue) -> None:
        super().__init__(daemon=True)
        self.sample_queue = sample_queue
        self.status_queue = status_queue
        self.stop_event = threading.Event()
        self.serial_port = None

    def connect(self, port: str, baud: int) -> None:
        self.port_name = port
        self.baud = baud
        self.start()

    def run(self) -> None:
        parser = PacketParser()
        try:
            self.serial_port = open_serial_port(self.port_name, self.baud)
            self.status_queue.put(("connected", f"Connected to {self.port_name} at {self.baud} baud"))
            while not self.stop_event.is_set():
                raw = self.serial_port.readline()
                if not raw:
                    continue
                line = raw.decode("ascii", errors="ignore")
                if line.strip() == RESTART_BANNER:
                    parser = PacketParser()
                    self.sample_queue.put(RestartEvent("Device restart detected; capture stats cleared"))
                    continue
                sample = parser.feed_line(line)
                if sample:
                    self.sample_queue.put(sample)
        except Exception as exc:
            self.status_queue.put(("error", str(exc)))
        finally:
            if self.serial_port:
                self.serial_port.close()

    def stop(self) -> None:
        self.stop_event.set()


class HistogramCanvas(ttk.Frame):
    def __init__(self, parent, title: str, unit: str, color: str) -> None:
        super().__init__(parent)
        self.title = title
        self.unit = unit
        self.color = color
        ttk.Label(self, text=title, style="ChartTitle.TLabel").pack(anchor="w")
        self.canvas = tk.Canvas(self, height=190, highlightthickness=0, bg="#101820")
        self.canvas.pack(fill="both", expand=True, pady=(8, 0))

    def draw(self, values: list[float], show_midpoint: bool = False) -> None:
        canvas = self.canvas
        canvas.delete("all")
        width = max(canvas.winfo_width(), 320)
        height = max(canvas.winfo_height(), 160)
        pad_l, pad_r, pad_t, pad_b = 44, 14, 18, 34
        plot_w = width - pad_l - pad_r
        plot_h = height - pad_t - pad_b
        canvas.create_rectangle(0, 0, width, height, fill="#101820", outline="")

        if not values:
            canvas.create_text(
                width / 2,
                height / 2,
                text="Waiting for samples",
                fill="#8fa3b8",
                font=("Segoe UI", 11),
            )
            return

        bins = make_bins(values)
        max_count = max(count for _, _, count in bins) or 1
        canvas.create_line(pad_l, pad_t, pad_l, pad_t + plot_h, fill="#2b3a46")
        canvas.create_line(pad_l, pad_t + plot_h, pad_l + plot_w, pad_t + plot_h, fill="#2b3a46")

        bar_gap = 3
        bar_w = max(1, (plot_w - bar_gap * (len(bins) - 1)) / len(bins))
        for index, (_, _, count) in enumerate(bins):
            x0 = pad_l + index * (bar_w + bar_gap)
            x1 = x0 + bar_w
            y1 = pad_t + plot_h
            y0 = y1 - (count / max_count) * plot_h
            canvas.create_rectangle(x0, y0, x1, y1, fill=self.color, outline="")

        low = min(values)
        high = max(values)
        axis_y = height - 15
        canvas.create_text(pad_l, axis_y, text=f"{axis_fmt(low)} {self.unit}", fill="#8fa3b8", anchor="w")
        if show_midpoint:
            midpoint = (low + high) / 2
            canvas.create_text(
                pad_l + plot_w / 2,
                axis_y,
                text=f"{axis_fmt(midpoint)} {self.unit}",
                fill="#8fa3b8",
                anchor="center",
            )
        canvas.create_text(
            width - pad_r,
            axis_y,
            text=f"{axis_fmt(high)} {self.unit}",
            fill="#8fa3b8",
            anchor="e",
        )
        canvas.create_text(
            pad_l - 8,
            pad_t,
            text=str(max_count),
            fill="#8fa3b8",
            anchor="e",
        )


def make_bins(values: list[float], target_bins: int = 22) -> list[tuple[float, float, int]]:
    low = min(values)
    high = max(values)
    if math.isclose(low, high):
        return [(low - 0.5, high + 0.5, len(values))]
    if all(float(v).is_integer() for v in values) and high - low <= target_bins:
        bins = [(value, value, 0) for value in range(int(low), int(high) + 1)]
        counts = {value: 0 for value, _, _ in bins}
        for value in values:
            counts[int(value)] += 1
        return [(float(value), float(value), counts[value]) for value, _, _ in bins]

    step = (high - low) / target_bins
    counts = [0] * target_bins
    for value in values:
        index = min(target_bins - 1, int((value - low) / step))
        counts[index] += 1
    return [(low + i * step, low + (i + 1) * step, counts[i]) for i in range(target_bins)]


def fmt(value: float) -> str:
    if abs(value) >= 100:
        return f"{value:.0f}"
    if abs(value) >= 10:
        return f"{value:.1f}"
    return f"{value:.3g}"


def axis_fmt(value: float) -> str:
    return f"{value:.4f}"


def fmt4(value: float) -> str:
    return f"{value:.4f}"


def fmt3(value: float) -> str:
    return f"{value:.3f}"


class MonitorApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Wireless Link Monitor")
        self.geometry("1180x780")
        self.minsize(980, 680)
        self.configure(bg="#0b1117")

        self.samples: deque[PacketSample] = deque(maxlen=MAX_SAMPLES)
        self.sample_queue: queue.Queue[PacketSample] = queue.Queue()
        self.status_queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self.reader: SerialReader | None = None
        self.capture_extrema: dict[str, tuple[float, float]] = {}

        self._build_styles()
        self._build_layout()
        self.after(200, self._poll_queues)
        self.after(REFRESH_MS, self._refresh)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_styles(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(".", background="#0b1117", foreground="#dce7f2", font=("Segoe UI", 10))
        style.configure("TFrame", background="#0b1117")
        style.configure("Panel.TFrame", background="#111a22", relief="flat")
        style.configure("TLabel", background="#0b1117", foreground="#dce7f2")
        style.configure("Muted.TLabel", foreground="#8fa3b8")
        style.configure("Title.TLabel", font=("Segoe UI Semibold", 20), foreground="#f3f8ff")
        style.configure("Metric.TLabel", background="#111a22", font=("Segoe UI Semibold", 22), foreground="#f3f8ff")
        style.configure("MetricAlert.TLabel", background="#111a22", font=("Segoe UI Semibold", 22), foreground="#ef4444")
        style.configure("MetricName.TLabel", background="#111a22", foreground="#8fa3b8")
        style.configure("ChartTitle.TLabel", font=("Segoe UI Semibold", 12), foreground="#f3f8ff")
        style.configure("TButton", background="#1d6f8f", foreground="#f3f8ff", borderwidth=0, padding=(14, 8))
        style.map("TButton", background=[("active", "#2589ad"), ("disabled", "#25313b")])
        style.configure("TEntry", fieldbackground="#101820", foreground="#f3f8ff", bordercolor="#263747")
        style.configure(
            "Treeview",
            background="#f8fafc",
            fieldbackground="#f8fafc",
            foreground="#05080c",
            rowheight=28,
        )
        style.configure(
            "Treeview.Heading",
            background="#e5ebf1",
            foreground="#05080c",
            font=("Segoe UI Semibold", 10),
        )

    def _build_layout(self) -> None:
        top = ttk.Frame(self, padding=(22, 18, 22, 12))
        top.pack(fill="x")
        ttk.Label(top, text="Wireless Link Monitor", style="Title.TLabel").pack(side="left")

        controls = ttk.Frame(top)
        controls.pack(side="right")
        ttk.Label(controls, text="Port", style="Muted.TLabel").pack(side="left", padx=(0, 6))
        self.port_var = tk.StringVar(value=DEFAULT_PORT)
        ttk.Entry(controls, textvariable=self.port_var, width=10).pack(side="left", padx=(0, 12))
        ttk.Label(controls, text="Baud", style="Muted.TLabel").pack(side="left", padx=(0, 6))
        self.baud_var = tk.IntVar(value=DEFAULT_BAUD)
        ttk.Entry(controls, textvariable=self.baud_var, width=10).pack(side="left", padx=(0, 12))
        self.connect_button = ttk.Button(controls, text="Connect", command=self._toggle_connection)
        self.connect_button.pack(side="left")
        ttk.Button(controls, text="Clear", command=self._clear).pack(side="left", padx=(8, 0))

        self.status_var = tk.StringVar(value="Disconnected")
        ttk.Label(self, textvariable=self.status_var, style="Muted.TLabel", padding=(22, 0, 22, 12)).pack(fill="x")

        metrics = ttk.Frame(self, padding=(22, 0, 22, 14))
        metrics.pack(fill="x")
        self.metric_vars = {}
        self.metric_value_labels = {}
        for name in ("Packets", "RSSI", "CRC Errors", "Packet Period", "CFO", "Packet Loss Total"):
            panel = ttk.Frame(metrics, style="Panel.TFrame", padding=14)
            panel.pack(side="left", fill="x", expand=True, padx=(0, 10))
            ttk.Label(panel, text=name, style="MetricName.TLabel").pack(anchor="w")
            value = tk.StringVar(value="--")
            self.metric_vars[name] = value
            value_label = ttk.Label(panel, textvariable=value, style="Metric.TLabel")
            value_label.pack(anchor="w", pady=(4, 0))
            self.metric_value_labels[name] = value_label

        body = ttk.Frame(self, padding=(22, 0, 22, 22))
        body.pack(fill="both", expand=True)
        body.columnconfigure((0, 1), weight=1, uniform="charts")
        body.rowconfigure(0, weight=1)

        self.rssi_chart = HistogramCanvas(body, "RSSI Histogram", "dBm", "#2dd4bf")
        self.rssi_chart.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        self.elapsed_chart = HistogramCanvas(body, "Packet Period Histogram", "ms", "#60a5fa")
        self.elapsed_chart.grid(row=0, column=1, sticky="nsew")

        table_panel = ttk.Frame(self, style="Panel.TFrame", padding=14)
        table_panel.pack(fill="x", padx=22, pady=(0, 22))
        columns = ("metric", "best", "worst", "mean")
        self.table = ttk.Treeview(table_panel, columns=columns, show="headings", height=8)
        headings = {
            "metric": "Metric",
            "best": "Best",
            "worst": "Worst",
            "mean": "Mean",
        }
        widths = {"metric": 180, "best": 180, "worst": 180, "mean": 160}
        for column in columns:
            self.table.heading(column, text=headings[column])
            self.table.column(column, width=widths[column], anchor="w")
        self.table.pack(fill="x")

    def _toggle_connection(self) -> None:
        if self.reader:
            self.reader.stop()
            self.reader = None
            self.connect_button.configure(text="Connect")
            self.status_var.set("Disconnected")
            return
        try:
            baud = int(self.baud_var.get())
        except tk.TclError:
            messagebox.showerror("Invalid baud", "Baud rate must be a number.")
            return
        self.reader = SerialReader(self.sample_queue, self.status_queue)
        self.reader.connect(self.port_var.get().strip() or DEFAULT_PORT, baud)
        self.connect_button.configure(text="Disconnect")
        self.status_var.set("Connecting...")

    def _clear(self) -> None:
        self._reset_capture()
        self._refresh()

    def _reset_capture(self) -> None:
        self.samples.clear()
        self.capture_extrema.clear()

    def _poll_queues(self) -> None:
        while True:
            try:
                event = self.sample_queue.get_nowait()
            except queue.Empty:
                break
            if isinstance(event, RestartEvent):
                self._reset_capture()
                self.status_var.set(event.message)
                continue
            self.samples.append(event)
            self._record_capture_extrema(event)
        while True:
            try:
                state, message = self.status_queue.get_nowait()
            except queue.Empty:
                break
            self.status_var.set(message)
            if state == "error":
                self.reader = None
                self.connect_button.configure(text="Connect")
        self.after(100, self._poll_queues)

    def _refresh(self) -> None:
        samples = list(self.samples)

        self.metric_vars["Packets"].set(str(len(samples)))
        packet_loss_total = int(self.capture_extrema.get("Packet Loss Total", (0, 0))[1])
        self.metric_vars["Packet Loss Total"].set(str(packet_loss_total))
        self.metric_value_labels["Packet Loss Total"].configure(
            style="MetricAlert.TLabel" if packet_loss_total else "Metric.TLabel"
        )
        if samples:
            self.metric_vars["RSSI"].set(f"{samples[-1].rssi_dbm} dBm")
            self.metric_vars["CRC Errors"].set(str(samples[-1].crc_errors))
            self.metric_vars["Packet Period"].set(f"{samples[-1].packet_period_s * 1000:.4f} ms")
            self.metric_vars["CFO"].set(f"{samples[-1].cfo_khz} kHz")
        else:
            for key in ("RSSI", "CRC Errors", "Packet Period", "CFO"):
                self.metric_vars[key].set("--")

        rssi = [sample.rssi_dbm for sample in samples]
        crc = [sample.crc_errors for sample in samples]
        packet_period_ms = [sample.packet_period_s * 1000 for sample in samples]
        packet_jitter_us = [abs(50 - period_ms) * 1000 for period_ms in packet_period_ms]
        overflows = [sample.elapsed_overflows for sample in samples]
        cfo = [sample.cfo_khz for sample in samples]
        self.rssi_chart.draw(rssi)
        self.elapsed_chart.draw(packet_period_ms, show_midpoint=True)
        self._update_table(rssi, crc, packet_period_ms, packet_jitter_us, overflows, cfo)
        self.after(REFRESH_MS, self._refresh)

    def _update_table(
        self,
        rssi: list[int],
        crc: list[int],
        packet_period_ms: list[float],
        packet_jitter_us: list[float],
        overflows: list[int],
        cfo: list[int],
    ) -> None:
        burst_loss_count = self.capture_extrema.get("Burst Loss", (0, 0))[1]
        packet_loss_total = self.capture_extrema.get("Packet Loss Total", (0, 0))[1]
        self.table.delete(*self.table.get_children())
        rows = [
            metric_row("RSSI", rssi, "dBm", self.capture_extrema.get("RSSI")),
            metric_row("CRC Errors", crc, "", self.capture_extrema.get("CRC Errors")),
            metric_row("Overflows", overflows, "", self.capture_extrema.get("Overflows")),
            count_row("Burst Loss", burst_loss_count),
            count_row("Packet Loss Total", packet_loss_total),
            metric_row(
                "Packet Period",
                packet_period_ms,
                "ms",
                self.capture_extrema.get("Packet Period"),
                value_fmt=fmt4,
            ),
            metric_row(
                "Packet Jitter",
                packet_jitter_us,
                "us",
                self.capture_extrema.get("Packet Jitter"),
                value_fmt=fmt3,
            ),
            metric_row("CFO", cfo, "kHz", self.capture_extrema.get("CFO")),
        ]
        for row in rows:
            self.table.insert("", "end", values=row)

    def _record_capture_extrema(self, sample: PacketSample) -> None:
        packet_period_ms = sample.packet_period_s * 1000
        packet_jitter_us = abs(50 - packet_period_ms) * 1000
        values = {
            "RSSI": sample.rssi_dbm,
            "CRC Errors": sample.crc_errors,
            "Overflows": sample.elapsed_overflows,
            "Packet Period": packet_period_ms,
            "Packet Jitter": packet_jitter_us,
            "CFO": sample.cfo_khz,
        }
        burst_loss_count = self.capture_extrema.get("Burst Loss", (0, 0))[1]
        if sample.elapsed_overflows != 0:
            burst_loss_count += 1
        self.capture_extrema["Burst Loss"] = (burst_loss_count, burst_loss_count)

        packet_loss_total = self.capture_extrema.get("Packet Loss Total", (0, 0))[1]
        packet_loss_total += sample.elapsed_overflows
        self.capture_extrema["Packet Loss Total"] = (packet_loss_total, packet_loss_total)
        for name, value in values.items():
            if name not in self.capture_extrema:
                self.capture_extrema[name] = (value, value)
                continue
            best_value, worst_value = self.capture_extrema[name]
            if name == "RSSI":
                best_value = max(best_value, value)
                worst_value = min(worst_value, value)
            elif name == "CFO":
                best_value = min(best_value, value, key=abs)
                worst_value = max(worst_value, value, key=abs)
            else:
                best_value = min(best_value, value)
                worst_value = max(worst_value, value)
            self.capture_extrema[name] = (best_value, worst_value)

    def _on_close(self) -> None:
        if self.reader:
            self.reader.stop()
        self.destroy()


def metric_row(
    name: str,
    values: list[float],
    unit: str,
    extrema: tuple[float, float] | None,
    value_fmt=fmt,
) -> tuple[str, str, str, str]:
    if not values or extrema is None:
        return (name, "--", "--", "--")
    suffix = f" {unit}" if unit else ""
    best_value, worst_value = extrema
    return (
        name,
        f"{value_fmt(best_value)}{suffix}",
        f"{value_fmt(worst_value)}{suffix}",
        f"{value_fmt(statistics.fmean(values))}{suffix}",
    )


def count_row(name: str, count: float) -> tuple[str, str, str, str]:
    value = str(int(count))
    return (name, value, value, value)


if __name__ == "__main__":
    app = MonitorApp()
    app.mainloop()
