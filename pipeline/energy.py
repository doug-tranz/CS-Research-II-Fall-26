"""
energy.py
---------
What a local model call costs in electricity. A local run has no bill, but it is not free:
the GPU draws power while it reads the prompt and writes the answer.

PowerMeter samples the GPU's power draw with nvidia-smi (NVIDIA GPUs; it ships with the
driver) every --power-interval-ms while a call runs, sums the GPUs, and integrates the samples
over the call's duration:

    energy (Wh) = mean watts during the call x seconds / 3600
    cost (USD)  = energy / 1000 x --kwh-price

Only the GPU is measured. --extra-watts adds a constant for the rest of the machine (CPU,
memory, fans; a desktop under load is typically 50-150 W more than the GPU alone). Without an
NVIDIA GPU, --watts sets a fixed draw instead (read it from a wall meter, or from the GPU's
rated power as an upper bound).
"""
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass

NVIDIA_SMI = "nvidia-smi"
QUERY = ["--query-gpu=power.draw", "--format=csv,noheader,nounits"]


@dataclass(frozen=True)
class EnergyReading:
    """Energy drawn during one call."""
    seconds: float
    avg_watts: float
    wh: float                   # watt-hours
    samples: int                # power readings taken (0 when a fixed --watts was used)
    source: str                 # "nvidia-smi" or "fixed"

    def usd(self, kwh_price: float) -> float:
        """The reading's cost at a price per kilowatt-hour."""
        return self.wh / 1000 * kwh_price


def read_gpu_watts() -> float | None:
    """One reading: the current power draw of every NVIDIA GPU, summed. None when nvidia-smi is
    missing or reports no number (some GPUs show [N/A])."""
    if not shutil.which(NVIDIA_SMI):
        return None
    try:
        out = subprocess.run([NVIDIA_SMI, *QUERY], capture_output=True, text=True, timeout=10).stdout
        return sum(float(line) for line in out.split()) if out.strip() else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


class PowerMeter:
    """Measures the energy one call draws. Use as:

        meter.start()  ... the call ...  reading = meter.stop()

    With `fixed_watts` it does not sample: the reading is that draw times the call's duration.
    Otherwise nvidia-smi runs in the background for the length of the call, printing one line
    per GPU every `interval_ms`; a reader thread collects them."""

    def __init__(self, fixed_watts: float | None = None, extra_watts: float = 0.0, interval_ms: int = 200):
        self.fixed, self.extra, self.interval = fixed_watts, extra_watts, interval_ms
        self.gpus = 0
        if fixed_watts is None:
            idle = read_gpu_watts()
            if idle is None:
                raise RuntimeError("cannot read GPU power: nvidia-smi is missing or reports no power draw; "
                                   "pass --watts <the machine's draw under load> instead")
            self.idle_watts = idle
            out = subprocess.run([NVIDIA_SMI, *QUERY], capture_output=True, text=True, timeout=10).stdout
            self.gpus = len(out.split())
        else:
            self.idle_watts = None
        self._proc, self._thread, self._samples, self._t0 = None, None, [], 0.0

    @property
    def source(self) -> str:
        return "fixed" if self.fixed is not None else "nvidia-smi"

    def start(self) -> None:
        """Starts timing (and sampling) one call."""
        self._samples, self._t0 = [], time.perf_counter()
        if self.fixed is not None:
            return
        self._proc = subprocess.Popen([NVIDIA_SMI, *QUERY, "-lms", str(self.interval)],
                                      stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()

    def _read(self) -> None:
        """Reader thread: groups nvidia-smi's lines into one summed reading per interval."""
        group = []
        for line in self._proc.stdout:
            try:
                group.append(float(line))
            except ValueError:
                continue                       # [N/A] or a header line
            if len(group) == self.gpus:
                self._samples.append(sum(group))
                group = []

    def stop(self) -> EnergyReading:
        """Stops timing; the energy the call drew (plus extra_watts for its duration)."""
        seconds = time.perf_counter() - self._t0
        if self.fixed is not None:
            watts, samples = self.fixed, 0
        else:
            self._proc.terminate()
            self._proc.wait(timeout=10)
            self._thread.join(timeout=10)
            samples = len(self._samples)
            # A call shorter than one interval gets no sample: read once now instead.
            watts = sum(self._samples) / samples if samples else (read_gpu_watts() or 0.0)
        watts += self.extra
        return EnergyReading(seconds=seconds, avg_watts=round(watts, 2), wh=watts * seconds / 3600,
                             samples=samples, source=self.source)
