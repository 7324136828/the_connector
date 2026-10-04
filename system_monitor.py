#!/usr/bin/env python3
"""Live Windows system monitor for CPU, RAM, and NVIDIA GPU usage.

Run normally with:
    python system_monitor.py

For a non-GUI diagnostics check:
    python system_monitor.py --self-test

Dependencies:
    pip install psutil nvidia-ml-py
"""

from __future__ import annotations

import argparse
import sys
import time
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

import psutil

try:
    import pynvml
except ImportError:
    pynvml = None


class NvidiaMonitor:
    """Small wrapper that keeps NVML optional and always cleans it up."""

    def __init__(self) -> None:
        self.initialized = False
        self.error = "NVIDIA GPU monitoring is unavailable."
        self.handles: list[tuple[int, int]] = []

        if pynvml is None:
            return

        try:
            pynvml.nvmlInit()
            self.initialized = True
            self.error = ""
            self.handles = [
                (index, pynvml.nvmlDeviceGetHandleByIndex(index))
                for index in range(pynvml.nvmlDeviceGetCount())
            ]
        except Exception as exc:
            self.error = f"NVIDIA GPU monitoring unavailable: {exc}"

    def samples(self) -> list[dict[str, object]]:
        if not self.initialized:
            return []

        result: list[dict[str, object]] = []
        for index, handle in self.handles:
            try:
                name = pynvml.nvmlDeviceGetName(handle)
                if isinstance(name, bytes):
                    name = name.decode("utf-8", errors="replace")
                rates = pynvml.nvmlDeviceGetUtilizationRates(handle)
                memory = pynvml.nvmlDeviceGetMemoryInfo(handle)
                result.append(
                    {
                        "index": index,
                        "name": name,
                        "utilization": int(rates.gpu),
                        "memory_used": int(memory.used),
                        "memory_total": int(memory.total),
                    }
                )
            except Exception:
                # A device can disappear or be in a transient error state.
                continue
        return result

    def close(self) -> None:
        if self.initialized:
            try:
                pynvml.nvmlShutdown()
            except Exception:
                pass
            self.initialized = False


def format_bytes(value: float) -> str:
    value = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < 1024.0 or unit == "TiB":
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} TiB"


def read_system_metrics() -> tuple[float, object]:
    # The first CPU reading establishes the baseline; later calls show live usage.
    cpu_percent = float(psutil.cpu_percent(interval=None))
    memory = psutil.virtual_memory()
    return cpu_percent, memory


def read_metrics(nvidia: NvidiaMonitor) -> dict[str, object]:
    cpu_percent, memory = read_system_metrics()
    return {
        "cpu_percent": cpu_percent,
        "cpu_count": psutil.cpu_count(logical=True) or 1,
        "memory": memory,
        "gpus": nvidia.samples(),
    }


class SystemMonitorWindow:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Live System Monitor")
        self.root.geometry("560x430")
        self.root.minsize(500, 380)
        self.root.configure(background="#101827")

        self.nvidia = NvidiaMonitor()
        self._job: str | None = None
        self._gpu_labels: list[tk.Label] = []
        self._gpu_bars: list[ttk.Progressbar] = []
        self._build_style()
        self._build_widgets()

        # Establish the CPU baseline before the first scheduled update.
        psutil.cpu_percent(interval=None)
        self.refresh()
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    def _build_style(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Dark.Horizontal.TProgressbar", troughcolor="#263449", background="#38bdf8", lightcolor="#38bdf8", darkcolor="#38bdf8")
        style.configure("Green.Horizontal.TProgressbar", troughcolor="#263449", background="#4ade80", lightcolor="#4ade80", darkcolor="#4ade80")
        style.configure("Purple.Horizontal.TProgressbar", troughcolor="#263449", background="#c084fc", lightcolor="#c084fc", darkcolor="#c084fc")

    def _build_widgets(self) -> None:
        self.root.grid_columnconfigure(0, weight=1)
        self.root.grid_rowconfigure(4, weight=1)

        heading = tk.Label(
            self.root,
            text="Live System Monitor",
            font=("Segoe UI", 18, "bold"),
            fg="#f8fafc",
            bg="#101827",
        )
        heading.grid(row=0, column=0, padx=24, pady=(22, 4), sticky="w")

        subtitle = tk.Label(
            self.root,
            text="Current resource usage • updates every second",
            font=("Segoe UI", 10),
            fg="#94a3b8",
            bg="#101827",
        )
        subtitle.grid(row=1, column=0, padx=24, pady=(0, 18), sticky="w")

        self.cpu_value = tk.Label(self.root, text="CPU  --%", font=("Segoe UI", 13, "bold"), fg="#38bdf8", bg="#172235", anchor="w", padx=14, pady=10)
        self.cpu_value.grid(row=2, column=0, padx=24, pady=5, sticky="ew")
        self.cpu_bar = ttk.Progressbar(self.root, maximum=100, style="Dark.Horizontal.TProgressbar")
        self.cpu_bar.grid(row=3, column=0, padx=24, pady=(0, 12), sticky="ew")

        self.memory_value = tk.Label(self.root, text="RAM  --", font=("Segoe UI", 13, "bold"), fg="#4ade80", bg="#172235", anchor="w", padx=14, pady=10)
        self.memory_value.grid(row=4, column=0, padx=24, pady=5, sticky="ew")
        self.memory_bar = ttk.Progressbar(self.root, maximum=100, style="Green.Horizontal.TProgressbar")
        self.memory_bar.grid(row=5, column=0, padx=24, pady=(0, 12), sticky="ew")

        gpu_heading = tk.Label(self.root, text="NVIDIA GPU", font=("Segoe UI", 12, "bold"), fg="#f8fafc", bg="#101827", anchor="w")
        gpu_heading.grid(row=6, column=0, padx=24, pady=(2, 6), sticky="w")

        self.gpu_frame = tk.Frame(self.root, bg="#101827")
        self.gpu_frame.grid(row=7, column=0, padx=24, pady=(0, 8), sticky="nsew")
        self.gpu_frame.grid_columnconfigure(0, weight=1)

        if self.nvidia.initialized and self.nvidia.handles:
            for index, _ in self.nvidia.handles:
                self._add_gpu_row(index)
        else:
            tk.Label(
                self.gpu_frame,
                text=self.nvidia.error or "No NVIDIA GPU detected.",
                font=("Segoe UI", 10),
                fg="#94a3b8",
                bg="#101827",
                anchor="w",
                justify="left",
                wraplength=480,
            ).grid(row=0, column=0, sticky="w")

        self.status = tk.Label(self.root, text="", font=("Segoe UI", 9), fg="#64748b", bg="#101827", anchor="w")
        self.status.grid(row=8, column=0, padx=24, pady=(8, 18), sticky="w")

    def _add_gpu_row(self, index: int) -> None:
        row = len(self._gpu_labels)
        label = tk.Label(self.gpu_frame, text=f"GPU {index}", font=("Segoe UI", 10), fg="#c4b5fd", bg="#101827", anchor="w")
        label.grid(row=row * 2, column=0, pady=(2, 3), sticky="w")
        bar = ttk.Progressbar(self.gpu_frame, maximum=100, style="Purple.Horizontal.TProgressbar")
        bar.grid(row=row * 2 + 1, column=0, pady=(0, 7), sticky="ew")
        self._gpu_labels.append(label)
        self._gpu_bars.append(bar)

    def refresh(self) -> None:
        metrics = read_metrics(self.nvidia)
        cpu = float(metrics["cpu_percent"])
        memory = metrics["memory"]
        self.cpu_value.configure(text=f"CPU   {cpu:5.1f}%   •   {metrics['cpu_count']} logical processors")
        self.cpu_bar["value"] = cpu

        memory_percent = float(memory.percent)
        self.memory_value.configure(text=f"RAM   {memory_percent:5.1f}%   •   {format_bytes(memory.used)} / {format_bytes(memory.total)}")
        self.memory_bar["value"] = memory_percent

        gpu_samples = metrics["gpus"]
        assert isinstance(gpu_samples, list)
        for index, sample in enumerate(gpu_samples):
            if index >= len(self._gpu_labels):
                break
            utilization = float(sample["utilization"])
            used = int(sample["memory_used"])
            total = int(sample["memory_total"])
            name = str(sample["name"])
            self._gpu_labels[index].configure(text=f"GPU {sample['index']}  {utilization:5.1f}%  •  {format_bytes(used)} / {format_bytes(total)}  •  {name}")
            self._gpu_bars[index]["value"] = utilization

        self.status.configure(text=f"Last update: {time.strftime('%H:%M:%S')}    |    CPU, RAM, and GPU values are system-wide")
        self._job = self.root.after(1000, self.refresh)

    def close(self) -> None:
        if self._job is not None:
            try:
                self.root.after_cancel(self._job)
            except tk.TclError:
                pass
        self.nvidia.close()
        self.root.destroy()


def self_test() -> int:
    monitor = NvidiaMonitor()
    try:
        # Take a short CPU sample so the diagnostic output is meaningful.
        psutil.cpu_percent(interval=0.25)
        metrics = read_metrics(monitor)
        memory = metrics["memory"]
        print(f"CPU: {metrics['cpu_percent']:.1f}% ({metrics['cpu_count']} logical processors)")
        print(f"RAM: {memory.percent:.1f}% ({format_bytes(memory.used)} / {format_bytes(memory.total)})")
        gpus = metrics["gpus"]
        assert isinstance(gpus, list)
        if not gpus:
            print(f"GPU: {monitor.error or 'No NVIDIA GPU data available'}")
        for gpu in gpus:
            print(f"GPU {gpu['index']}: {gpu['utilization']}% ({gpu['name']}); VRAM {format_bytes(gpu['memory_used'])} / {format_bytes(gpu['memory_total'])}")
        return 0
    finally:
        monitor.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Show live CPU, RAM, and NVIDIA GPU usage in a window.")
    parser.add_argument("--self-test", action="store_true", help="Print one metrics sample without opening a window.")
    args = parser.parse_args()
    if args.self_test:
        return self_test()

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(f"Could not open a window: {exc}", file=sys.stderr)
        return 1
    SystemMonitorWindow(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
