"""Обмен с GRBL 1.1h по последовательному порту.

Реализован потоковый протокол с подсчётом символов (character counting),
пауза/возобновление, аварийный останов и опрос статуса.
"""

from __future__ import annotations

import queue
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

try:  # pyserial может отсутствовать — предпросмотр всё равно должен работать
    import serial
    from serial.tools import list_ports
except Exception:  # pragma: no cover - зависит от окружения
    serial = None  # type: ignore[assignment]
    list_ports = None  # type: ignore[assignment]


LogCallback = Callable[[str], None]
StatusCallback = Callable[["GrblStatus"], None]
ProgressCallback = Callable[[float], None]


class GrblError(RuntimeError):
    pass


class GrblAlarm(RuntimeError):
    pass


class JobCancelled(RuntimeError):
    pass


class GrblTimeout(RuntimeError):
    pass


@dataclass
class GrblStatus:
    state: str = "Unknown"
    mpos: tuple[float, float, float] | None = None
    wpos: tuple[float, float, float] | None = None
    feed: float = 0.0
    spindle: float = 0.0
    raw: str = ""

    @property
    def position(self) -> tuple[float, float, float] | None:
        return self.wpos or self.mpos

    @property
    def is_idle(self) -> bool:
        return self.state.lower().startswith("idle")


def parse_status(line: str) -> GrblStatus | None:
    """Разбирает строку `<Idle|MPos:...|FS:...>`."""
    line = line.strip()
    if not (line.startswith("<") and line.endswith(">")):
        return None
    body = line[1:-1]
    parts = body.split("|")
    st = GrblStatus(raw=line, state=parts[0].split(":")[0] if parts else "Unknown")
    for part in parts[1:]:
        if ":" not in part:
            continue
        key, value = part.split(":", 1)
        try:
            if key in {"MPos", "WPos", "WCO"}:
                nums = [float(v) for v in value.split(",")]
                while len(nums) < 3:
                    nums.append(0.0)
                if key == "MPos":
                    st.mpos = (nums[0], nums[1], nums[2])
                elif key == "WPos":
                    st.wpos = (nums[0], nums[1], nums[2])
            elif key == "FS":
                nums = [float(v) for v in value.split(",")]
                st.feed = nums[0] if nums else 0.0
                st.spindle = nums[1] if len(nums) > 1 else 0.0
            elif key == "F":
                st.feed = float(value)
        except ValueError:
            continue
    return st


def available_ports() -> list[tuple[str, str]]:
    if list_ports is None:
        return []
    out: list[tuple[str, str]] = []
    try:
        for p in list_ports.comports():
            out.append((p.device, p.description or p.device))
    except Exception:
        return []
    out.sort(key=lambda x: x[0])
    return out


class GrblClient:
    """Потокобезопасный клиент GRBL.

    Публичные методы, кроме real-time (pause/resume/stop), сериализуются
    внутренним lock. Поток задания должен быть один.
    """

    RX_BUFFER = 120
    MAX_WINDOW_LINES = 6
    STATUS_INTERVAL = 0.5

    def __init__(
        self,
        on_log: LogCallback | None = None,
        on_status: StatusCallback | None = None,
    ) -> None:
        self.on_log = on_log or (lambda _s: None)
        self.on_status = on_status or (lambda _s: None)
        self._serial = None
        self._reader: threading.Thread | None = None
        self._poller: threading.Thread | None = None
        self._running = threading.Event()
        self._connected = False
        self._responses: queue.Queue[str] = queue.Queue()
        self._io_lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._job_active = False
        self.status = GrblStatus()
        self.port = ""
        self.baud = 115200

    # ------------------------------------------------------------------
    # Подключение
    # ------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._connected and self._serial is not None

    def connect(self, port: str, baud: int = 115200, timeout: float = 4.0) -> None:
        if serial is None:
            raise GrblError("Не установлен pyserial. Выполните: pip install pyserial")
        if not port:
            raise GrblError("Не выбран последовательный порт")
        self.close()
        try:
            self._serial = serial.Serial(
                port=port,
                baudrate=int(baud),
                timeout=0.08,
                write_timeout=2.0,
            )
        except Exception as exc:
            self._serial = None
            raise GrblError(f"Не удалось открыть {port}: {exc}") from exc

        self.port = port
        self.baud = int(baud)
        self._connected = True
        self._running.set()
        self._responses = queue.Queue()
        self._reader = threading.Thread(target=self._read_loop, name="grbl-reader", daemon=True)
        self._reader.start()
        self._poller = threading.Thread(target=self._poll_loop, name="grbl-status", daemon=True)
        self._poller.start()

        # Arduino при открытии порта перезагружается, поэтому даём паузу
        time.sleep(1.2)
        self._write_raw(b"\r\n")
        deadline = time.monotonic() + timeout
        got_any = False
        while time.monotonic() < deadline:
            if self._wait_status(0.15):
                got_any = True
                break
            self._write_raw(b"?")
            time.sleep(0.2)
        if not got_any:
            # возможно, плата уже работала и ответила только на ?; проверим ещё раз
            if not self._wait_status(0.6):
                self.close()
                raise GrblError(
                    "Плата не ответила. Проверьте порт, питание и скорость 115200. "
                    "Закройте другие программы, использующие COM-порт."
                )
        # убираем накопившиеся ok/приветствие
        time.sleep(0.2)
        self._drain_responses()
        self.on_log(f"Подключено: {port} @ {baud}")

    def close(self) -> None:
        self._running.clear()
        self._connected = False
        ser = self._serial
        self._serial = None
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass
        for t in (self._reader, self._poller):
            if t and t.is_alive() and t is not threading.current_thread():
                t.join(timeout=0.5)
        self._reader = None
        self._poller = None
        self.status = GrblStatus()

    def _read_loop(self) -> None:
        buf = ""
        while self._running.is_set() and self._serial is not None:
            try:
                ser = self._serial
                if ser is None:
                    break
                n = ser.in_waiting or 1
                data = ser.read(n)
            except Exception as exc:
                if self._running.is_set():
                    self.on_log(f"Ошибка чтения: {exc}")
                break
            if not data:
                continue
            buf += data.decode("utf-8", errors="replace")
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                line = line.strip("\r\x00")
                if not line:
                    continue
                if line.startswith("<") and line.endswith(">"):
                    st = parse_status(line)
                    if st is not None:
                        self.status = st
                        self.on_status(st)
                    continue
                self._responses.put(line)
                self.on_log(f"← {line}")

    def _poll_loop(self) -> None:
        while self._running.is_set():
            if self.connected:
                try:
                    self._write_raw(b"?")
                except Exception:
                    pass
            time.sleep(self.STATUS_INTERVAL)

    def _wait_status(self, timeout: float) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            st = self.status
            if st.state != "Unknown":
                return True
            time.sleep(0.02)
        return self.status.state != "Unknown"

    # ------------------------------------------------------------------
    # Низкоуровневый ввод-вывод
    # ------------------------------------------------------------------

    def _write_raw(self, data: bytes) -> None:
        ser = self._serial
        if ser is None:
            raise GrblError("Порт не открыт")
        with self._write_lock:
            ser.write(data)
            ser.flush()

    def _write_line(self, line: str) -> None:
        self.on_log(f"→ {line}")
        self._write_raw((line + "\n").encode("utf-8"))

    def _drain_responses(self) -> None:
        while True:
            try:
                self._responses.get_nowait()
            except queue.Empty:
                break

    def _next_response(self, timeout: float) -> str | None:
        try:
            return self._responses.get(timeout=timeout)
        except queue.Empty:
            return None

    # ------------------------------------------------------------------
    # Команды
    # ------------------------------------------------------------------

    def send_command(self, command: str, timeout: float = 8.0) -> str:
        """Отправляет команду и ждёт ok/error."""
        if not self.connected:
            raise GrblError("Нет соединения")
        with self._io_lock:
            if self._job_active:
                raise GrblError("Идёт выполнение программы")
            self._write_line(command)
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                resp = self._next_response(0.25)
                if resp is None:
                    continue
                if resp == "ok":
                    return resp
                if resp.startswith("error"):
                    raise GrblError(f"{command}: {resp}")
                if resp.startswith("ALARM"):
                    raise GrblAlarm(resp)
                if resp.startswith("Grbl"):
                    continue
            raise GrblTimeout(f"Нет ответа на {command!r}")

    def send_realtime(self, char: bytes) -> None:
        if not self.connected:
            raise GrblError("Нет соединения")
        self._write_raw(char)

    def pause(self) -> None:
        self.send_realtime(b"!")
        self.on_log("Пауза (feed hold)")

    def resume(self) -> None:
        self.send_realtime(b"~")
        self.on_log("Продолжение")

    def soft_reset(self) -> None:
        try:
            self.send_realtime(b"\x18")
        except Exception:
            pass
        self._drain_responses()
        self.status = GrblStatus()
        self.on_log("Сброс контроллера (Ctrl-X)")

    def unlock(self) -> None:
        self.send_command("$X")

    def home(self) -> None:
        self.send_command("$H", timeout=60.0)

    def jog(self, axis: str, delta: float, feed: float = 1500.0) -> None:
        axis = axis.upper()
        if axis not in {"X", "Y", "Z"}:
            raise ValueError("axis должен быть X, Y или Z")
        self.send_command(f"$J=G91 {axis}{delta:.3f} F{feed:.0f}")

    def set_work_zero(self, x: bool = True, y: bool = True, z: bool = False) -> None:
        parts = ["G10", "L20", "P1"]
        if x:
            parts.append("X0")
        if y:
            parts.append("Y0")
        if z:
            parts.append("Z0")
        self.send_command(" ".join(parts))

    def goto_pen_up(self, z: float, feed: float = 500.0) -> None:
        self.send_command(f"G0 Z{z:.3f} F{feed:.0f}")

    # ------------------------------------------------------------------
    # Потоковая отправка задания
    # ------------------------------------------------------------------

    def stream_job(
        self,
        lines: Sequence[str],
        *,
        progress: ProgressCallback | None = None,
        stop_event: threading.Event | None = None,
        pause_event: threading.Event | None = None,
        timeout: float = 30.0,
    ) -> None:
        """Отправляет программу с подсчётом символов.

        progress получает значения 0..1 по количеству подтверждённых строк.
        stop_event — аварийный останов (soft reset).
        pause_event — удержание; пока установлен, станок стоит.
        """
        if not self.connected:
            raise GrblError("Нет соединения")
        if progress is None:
            progress = lambda _p: None
        if stop_event is None:
            stop_event = threading.Event()
        if pause_event is None:
            pause_event = threading.Event()

        clean: list[str] = []
        for line in lines:
            s = line.split(";", 1)[0].strip()
            if s:
                clean.append(s)
        if not clean:
            return

        with self._io_lock:
            if self._job_active:
                raise GrblError("Уже выполняется другая программа")
            self._job_active = True
            self._drain_responses()
            total = len(clean)
            pending: deque[int] = deque()
            sent_chars = 0
            acked_chars = 0
            acked_lines = 0
            idx = 0
            paused = False
            last_response = time.monotonic()
            self.on_log(f"Старт программы: {total} строк")

            try:
                while idx < total or pending:
                    if stop_event.is_set():
                        self.on_log("Аварийный останов")
                        self.send_realtime(b"!")
                        time.sleep(0.15)
                        self.soft_reset()
                        raise JobCancelled("Остановлено пользователем")

                    if pause_event.is_set() and not paused:
                        self.send_realtime(b"!")
                        paused = True
                        self.on_log("Удержание")
                    elif not pause_event.is_set() and paused:
                        self.send_realtime(b"~")
                        paused = False
                        self.on_log("Возобновление")

                    # заполняем буфер
                    if not paused:
                        while idx < total and len(pending) < self.MAX_WINDOW_LINES:
                            line = clean[idx]
                            n = len(line.encode("utf-8")) + 1
                            if n > self.RX_BUFFER:
                                raise GrblError(f"Слишком длинная строка G-кода: {line}")
                            if sent_chars - acked_chars + n > self.RX_BUFFER:
                                break
                            self._write_line(line)
                            pending.append(n)
                            sent_chars += n
                            idx += 1
                            last_response = time.monotonic()

                    resp = self._next_response(0.15)
                    if resp is None:
                        if paused:
                            continue
                        if pending and time.monotonic() - last_response > timeout:
                            raise GrblTimeout("Станок не отвечает (нет ok)")
                        continue

                    last_response = time.monotonic()
                    if resp == "ok":
                        if pending:
                            acked_chars += pending.popleft()
                        acked_lines += 1
                        progress(min(1.0, acked_lines / total))
                    elif resp.startswith("error"):
                        if pending:
                            acked_chars += pending.popleft()
                        raise GrblError(f"GRBL error: {resp}")
                    elif resp.startswith("ALARM"):
                        raise GrblAlarm(resp)
                    elif resp.startswith("Grbl"):
                        continue
                    else:
                        self.on_log(f"GRBL: {resp}")

                progress(1.0)
                self.on_log("Программа завершена")
            finally:
                self._job_active = False
