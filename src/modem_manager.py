# -*- coding: utf-8 -*-
import os
import sys
import serial
import serial.tools.list_ports
import threading
import time
import re
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

# Скорость AT-порта E173 (рабочая программа-эталон открывает модемы на 115200)
DEFAULT_BAUD = 115200


def _debug_path():
    base = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.getcwd()
    return os.path.join(base, "call_debug.log")


_debug_lock = threading.Lock()


def call_debug(msg):
    """Пишет сырые ответы модема в call_debug.log для диагностики звонков."""
    try:
        with _debug_lock:
            with open(_debug_path(), "a", encoding="utf-8") as f:
                f.write(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}\n")
    except Exception:
        pass


# ===========================================================================
# Сопоставление портов модема
# ---------------------------------------------------------------------------
# У каждого Huawei E173 несколько COM-портов с ОДНИМ USB-serial:
#   PC UI Interface      -> AT-команды, набор, DDSETEX  (командный порт)
#   Application Interface -> голосовой PCM               (voice-порт)
#   Modem #N             -> запасной dial-порт
# Проверено на железе заказчика: AT=COM28, VOICE=COM29 (Application).
# ===========================================================================

def _extract_serial(hwid):
    m = re.search(r'SER=([^\s]+)', hwid or "")
    if m:
        return m.group(1)
    return None


def discover_modem_groups():
    """
    Группирует COM-порты по USB-serial и раскладывает роли.
    Возвращает список словарей: {serial, at_port, voice_port, modem_port, desc}.
    Только группы, где есть командный порт (PC UI или Modem).
    """
    ports = list(serial.tools.list_ports.comports())
    groups = {}
    for p in ports:
        desc = p.description or ""
        if "HUAWEI" not in desc.upper() and (p.vid != 0x12D1):
            continue
        ser = _extract_serial(p.hwid) or (p.serial_number or p.device)
        # у модемного порта serial бывает без хвоста — нормализуем по первым сегментам
        g = groups.setdefault(ser, {"serial": ser, "at_port": None,
                                     "voice_port": None, "modem_port": None,
                                     "desc": desc})
        d = desc.upper()
        if "PC UI" in d:
            g["at_port"] = p.device
        elif "APPLICATION" in d:
            g["voice_port"] = p.device
        elif "MODEM" in d:
            g["modem_port"] = p.device

    result = []
    for ser, g in groups.items():
        # командный порт: сперва PC UI, иначе Modem
        if not g["at_port"]:
            g["at_port"] = g["modem_port"]
        if g["at_port"]:
            result.append(g)
    return result


# ===========================================================================
# Модем
# ===========================================================================

class Modem:
    def __init__(self, at_port, voice_port=None, baudrate=DEFAULT_BAUD, timeout=5):
        self.port = at_port          # командный порт (совместимость со старым кодом)
        self.at_port = at_port
        self.voice_port = voice_port
        self.baudrate = baudrate
        self.timeout = timeout
        self.serial_conn = None
        self.lock = threading.Lock()
        self.model = None
        self.supports_voice = False
        self.imei = None
        self.signal_quality = 0
        self._connected = False
        self.active = True
        self._last_sms_time = 0   # для паузы между SMS (E173 не любит частую отправку)

    def _throttle_sms(self, gap=2.0):
        """Гарантирует паузу между SMS на одном модеме — иначе E173 даёт CMS ERROR 500/timeout."""
        dt = time.time() - self._last_sms_time
        if dt < gap:
            time.sleep(gap - dt)

    @property
    def connected(self):
        return self._connected

    def connect(self):
        try:
            self.serial_conn = serial.Serial(
                self.at_port, self.baudrate, timeout=self.timeout,
                write_timeout=self.timeout
            )
            self._connected = True
            self._init_modem()
            return True
        except Exception as e:
            logger.error(f"Error connecting to {self.at_port}: {e}")
            self._connected = False
            return False

    def disconnect(self):
        if self.serial_conn and self.serial_conn.is_open:
            try:
                self.serial_conn.close()
            except Exception:
                pass
        self._connected = False

    def _init_modem(self):
        self.send_at("AT", timeout=3)
        resp = self.send_at("ATI", timeout=3)
        if resp:
            for line in resp:
                if "Model" in line or "E173" in line or "E3372" in line or "E171" in line or "E1550" in line:
                    self.model = line.strip()
        cgmm = self.send_at("AT+CGMM", timeout=3)
        if cgmm:
            for line in cgmm:
                if re.match(r'^E\d', line.strip()):
                    self.model = line.strip()
        imei_resp = self.send_at("AT+CGSN", timeout=3)
        if imei_resp:
            for line in imei_resp:
                digits = re.sub(r'\D', '', line)
                if len(digits) >= 14:
                    self.imei = digits
                    break
        self.check_voice_support()
        self.update_signal_quality()

    def check_voice_support(self):
        """Голос доступен, если найден отдельный голосовой порт И модем отвечает на ^CVOICE."""
        has_voice_port = bool(self.voice_port)
        resp = self.send_at("AT^CVOICE=?", timeout=3)
        cvoice_ok = bool(resp and any("^CVOICE" in l or "OK" in l for l in resp)
                         and not any("ERROR" in l for l in resp))
        self.supports_voice = has_voice_port and cvoice_ok

    def update_signal_quality(self):
        resp = self.send_at("AT+CSQ", timeout=3)
        if resp:
            for line in resp:
                match = re.search(r'\+CSQ:\s*(\d+)', line)
                if match:
                    self.signal_quality = int(match.group(1))
                    break

    def send_at(self, command, timeout=5, wait_data=None):
        if not self.serial_conn or not self.serial_conn.is_open:
            return None
        with self.lock:
            try:
                self.serial_conn.reset_input_buffer()
                self.serial_conn.reset_output_buffer()
                self.serial_conn.write((command + "\r").encode('ascii'))
                self.serial_conn.flush()
                time.sleep(0.3)
                end_time = time.time() + timeout
                response = []
                while time.time() < end_time:
                    if self.serial_conn.in_waiting > 0:
                        data = self.serial_conn.read(self.serial_conn.in_waiting).decode('ascii', errors='ignore')
                        lines = data.split('\n')
                        for line in lines:
                            line = line.strip()
                            if line:
                                response.append(line)
                        if any("OK" in l for l in response) or any("ERROR" in l for l in response):
                            break
                    time.sleep(0.1)
                return response
            except Exception as e:
                logger.error(f"AT command error on {self.at_port}: {command} -> {e}")
                return None

    # ==================== SMS ====================

    def send_sms(self, number, message, unicode=False, lock_timeout=3.0):
        if not self._connected:
            return False, "Modem not connected"
        # Таймаут на захват блокировки — если модем сейчас занят долгим звонком,
        # SMS не должна зависать на всю его длительность, а быстро уступить другому модему.
        if not self.lock.acquire(timeout=lock_timeout):
            return False, "Модем занят (идёт звонок)"
        try:
            self._throttle_sms()
            self._last_sms_time = time.time()
            if unicode:
                self.serial_conn.write(b'AT+CSCS="UCS2"\r')
                time.sleep(0.3)
                self.serial_conn.reset_input_buffer()
                self.serial_conn.write(b'AT+CMGF=0\r')
                time.sleep(0.3)
                self.serial_conn.reset_input_buffer()
                encoded = self._encode_pdu(number, message)
                # длина TPDU в октетах БЕЗ ведущего SMSC-байта (00) — иначе +CMS ERROR 304
                cmd = f'AT+CMGS={len(encoded) // 2 - 1}\r'
                self.serial_conn.write(cmd.encode('ascii'))
                time.sleep(0.5)
                self.serial_conn.reset_input_buffer()
                self.serial_conn.write((encoded + chr(26)).encode('ascii'))
            else:
                self.serial_conn.write(b'AT+CSCS="GSM"\r')
                time.sleep(0.3)
                self.serial_conn.reset_input_buffer()
                self.serial_conn.write(b'AT+CMGF=1\r')
                time.sleep(0.3)
                self.serial_conn.reset_input_buffer()
                cmd = f'AT+CMGS="{number}"\r'
                self.serial_conn.write(cmd.encode('ascii'))
                time.sleep(0.5)
                self.serial_conn.reset_input_buffer()
                self.serial_conn.write((message + chr(26)).encode('ascii'))

            time.sleep(0.3)
            end_time = time.time() + 15
            response = ""
            while time.time() < end_time:
                if self.serial_conn.in_waiting > 0:
                    data = self.serial_conn.read(self.serial_conn.in_waiting).decode('ascii', errors='ignore')
                    response += data
                    if "+CMGS:" in response:
                        return True, "OK"
                    if "ERROR" in response:
                        return False, response.strip()
                time.sleep(0.2)
            if "+CMGS:" in response:
                return True, "OK"
            return False, "Timeout"
        except Exception as e:
            return False, str(e)
        finally:
            self.lock.release()

    def _encode_pdu(self, number, message):
        number = number.lstrip('+')
        if len(number) % 2 != 0:
            number = number + 'F'
        encoded_number = ''
        for i in range(0, len(number), 2):
            encoded_number += number[i+1] + number[i]
        addr_len = len(number.lstrip('F'))
        msg_hex = message.encode('utf-16-be').hex().upper()
        msg_len = len(msg_hex) // 2
        # SMS-SUBMIT без TP-VP: первый октет 0x01 (VPF=00), поэтому байта срока (AA) быть НЕ должно
        pdu = f"00" \
              f"0100" \
              f"{addr_len:02X}91{encoded_number}" \
              f"0008" \
              f"{msg_len:02X}{msg_hex}"
        return pdu

    def send_sms_multipart(self, number, message, lock_timeout=3.0):
        if not self._connected:
            return False, "Modem not connected"
        is_unicode = any(ord(c) > 127 for c in message)
        max_chars = 70 if is_unicode else 160
        if len(message) <= max_chars:
            return self.send_sms(number, message, unicode=is_unicode, lock_timeout=lock_timeout)
        # ДЛИННОЕ сообщение шлём всегда как UCS2 — для латиницы GSM-7 требует 7-битной
        # упаковки, которую модем в PDU-режиме ждёт строго; UCS2 надёжен для любого языка.
        is_unicode = True
        part_len = 67
        parts = [message[i:i+part_len] for i in range(0, len(message), part_len)]
        ref = int(time.time()) & 0xFF
        results = []
        for idx, part in enumerate(parts):
            ok, info = self._send_pdu_multipart(number, part, ref, idx + 1, len(parts), is_unicode,
                                                lock_timeout=lock_timeout)
            results.append((ok, info))
            if not ok:
                break  # первая же часть не ушла — дальше нет смысла, отдаём ошибку вызывающему
            time.sleep(1)
        all_ok = all(r[0] for r in results)
        return all_ok, "; ".join(r[1] for r in results)

    def _send_pdu_multipart(self, number, message, ref, part_num, total_parts, is_unicode, lock_timeout=3.0):
        if not self.lock.acquire(timeout=lock_timeout):
            return False, "Модем занят (идёт звонок)"
        try:
            self._throttle_sms()
            self._last_sms_time = time.time()
            self.serial_conn.write(b'AT+CMGF=0\r')
            time.sleep(0.3)
            self.serial_conn.reset_input_buffer()
            self.serial_conn.write(b'AT+CSCS="UCS2"\r')
            time.sleep(0.3)
            self.serial_conn.reset_input_buffer()
            number_clean = number.lstrip('+')
            if len(number_clean) % 2 != 0:
                number_clean = number_clean + 'F'
            encoded_number = ''
            for i in range(0, len(number_clean), 2):
                encoded_number += number_clean[i+1] + number_clean[i]
            addr_len = len(number.lstrip('+'))
            if is_unicode:
                msg_hex = message.encode('utf-16-be').hex().upper()
                dcs = "08"
            else:
                msg_hex = message.encode('gsm0338', errors='replace').hex().upper() \
                    if _has_gsm0338() else message.encode('ascii', 'replace').hex().upper()
                dcs = "00"
            udh = f"050003{ref:02X}{total_parts:02X}{part_num:02X}"
            ud = udh + msg_hex
            ud_len = len(ud) // 2
            # 0x41 = SMS-SUBMIT + UDHI (без VP), 0x00 = TP-MR. Без байта срока (AA).
            pdu = f"00" \
                  f"4100" \
                  f"{addr_len:02X}91{encoded_number}" \
                  f"00{dcs}" \
                  f"{ud_len:02X}{ud}"
            cmd = f'AT+CMGS={len(pdu) // 2 - 1}\r'
            self.serial_conn.write(cmd.encode('ascii'))
            time.sleep(0.5)
            self.serial_conn.reset_input_buffer()
            self.serial_conn.write((pdu + chr(26)).encode('ascii'))
            time.sleep(0.3)
            end_time = time.time() + 15
            response = ""
            while time.time() < end_time:
                if self.serial_conn.in_waiting > 0:
                    data = self.serial_conn.read(self.serial_conn.in_waiting).decode('ascii', errors='ignore')
                    response += data
                    if "+CMGS:" in response:
                        return True, "OK"
                    if "ERROR" in response:
                        return False, response.strip()
                time.sleep(0.2)
            return False, "Timeout"
        except Exception as e:
            return False, str(e)
        finally:
            self.lock.release()

    # ==================== ГОЛОСОВЫЕ ЗВОНКИ ====================

    def make_call_with_audio(self, number, pcm_data, timeout_sec=30):
        """
        Звонок с проигрыванием заранее подготовленного PCM (8000/моно/16бит).
        Возвращает (success, status, audio_played_sec, audio_total_sec).

        Рабочая схема (проверена на E173 заказчика):
          1) ATD<number>;         на AT-порту
          2) ждём ^CONN           (абонент ответил)
          3) AT^DDSETEX=2         открыть голосовой мост
          4) льём PCM в voice_port (отдельный Application-порт!)
          5) ATH
        Успех = получен ^CONN (звонок реально отвечен). Это чинит Баг 2:
        отвеченные звонки больше не падают в «неуспешные».
        """
        if not self._connected:
            return False, "Modem not connected", 0, 0.0
        if not self.supports_voice:
            return False, "Модем без голоса", 0, 0.0

        total_sec = len(pcm_data) / 2 / 8000 if pcm_data else 0.0

        with self.lock:
            try:
                self.serial_conn.write(b'AT+CVHU=0\r')
                time.sleep(0.3)
                self.serial_conn.reset_input_buffer()

                # набор
                call_debug(f"{self.at_port} -> ATD{number}; (timeout={timeout_sec}s)")
                self.serial_conn.write(f'ATD{number};\r'.encode('ascii'))

                answered = False
                response = ""
                end_time = time.time() + timeout_sec
                while time.time() < end_time:
                    if self.serial_conn.in_waiting > 0:
                        data = self.serial_conn.read(self.serial_conn.in_waiting).decode('ascii', errors='ignore')
                        response += data
                        if data.strip():
                            logger.info(f"[{self.at_port}] URC: {data.strip()}")
                            call_debug(f"{self.at_port} URC: {data.strip()}")
                        if "^CONN" in response:
                            answered = True
                            break
                        if "BUSY" in response:
                            self._hangup_locked()
                            return False, "Занято", 0, total_sec
                        if "NO ANSWER" in response:
                            self._hangup_locked()
                            return False, "Нет ответа", 0, total_sec
                        if "NO CARRIER" in response:
                            self._hangup_locked()
                            return False, "Нет соединения", 0, total_sec
                        if "NO DIALTONE" in response:
                            self._hangup_locked()
                            return False, "Нет линии", 0, total_sec
                        if "ERROR" in response:
                            self._hangup_locked()
                            return False, "Ошибка набора", 0, total_sec
                        if "^CEND" in response:
                            # звонок завершился до ответа
                            status = self._parse_call_status(response)
                            self._hangup_locked()
                            return False, status, 0, total_sec
                    time.sleep(0.2)

                if not answered:
                    self._hangup_locked()
                    return False, "Нет ответа", 0, total_sec

                # ^CONN получен — абонент ответил. Проигрываем аудио.
                played = self._play_pcm_via_voice(pcm_data)

                # Небольшая пауза и отбой
                time.sleep(0.5)
                self._hangup_locked()
                return True, "Отвечен", played, total_sec
            except Exception as e:
                try:
                    self._hangup_locked()
                except Exception:
                    pass
                return False, str(e), 0, total_sec

    def _hangup_locked(self):
        try:
            self.serial_conn.write(b'ATH\r')
            time.sleep(0.3)
        except Exception:
            pass

    def _play_pcm_via_voice(self, pcm_data):
        """Открыть мост DDSETEX=2 и лить PCM в отдельный голосовой порт. Возвращает секунды."""
        if not pcm_data:
            return 0
        if not self.voice_port:
            logger.warning(f"[{self.at_port}] нет голосового порта — звук пропущен")
            return 0

        # открыть голосовой мост на командном порту
        self.serial_conn.reset_input_buffer()
        self.serial_conn.write(b'AT^DDSETEX=2\r')
        time.sleep(0.3)
        if self.serial_conn.in_waiting > 0:
            r = self.serial_conn.read(self.serial_conn.in_waiting).decode('ascii', errors='ignore')
            logger.info(f"[{self.at_port}] DDSETEX: {r.strip()}")

        vconn = None
        bytes_sent = 0
        try:
            vconn = serial.Serial(self.voice_port, self.baudrate, timeout=1, write_timeout=3)
            chunk_size = 320  # 160 сэмплов = 20 мс при 8кГц 16бит
            frame_sec = chunk_size / 2 / 8000.0   # ровно 0.02 с на кадр
            start = time.perf_counter()
            frame = 0
            for i in range(0, len(pcm_data), chunk_size):
                chunk = pcm_data[i:i + chunk_size]
                vconn.write(chunk)
                bytes_sent += len(chunk)
                frame += 1
                # ровная подача по «часам»: спим до целевого времени кадра,
                # чтобы модем получал ровно 8000 сэмплов/с (без дрожания и шума)
                target = start + frame * frame_sec
                dt = target - time.perf_counter()
                if dt > 0:
                    time.sleep(dt)
                # проверка отбоя абонента на командном порту
                if self.serial_conn.in_waiting > 0:
                    urc = self.serial_conn.read(self.serial_conn.in_waiting).decode('ascii', errors='ignore')
                    if "^CEND" in urc or "NO CARRIER" in urc:
                        logger.info(f"[{self.at_port}] абонент завершил звонок во время аудио")
                        break
        except Exception as e:
            logger.error(f"[{self.at_port}] ошибка проигрывания в {self.voice_port}: {e}")
        finally:
            if vconn:
                try:
                    vconn.close()
                except Exception:
                    pass
        # точное число секунд (float) — иначе floor даёт 14 из 14.4 => ложные 97%
        return bytes_sent / 2 / 8000.0

    def make_call(self, number, timeout_sec=30):
        """Звонок без аудио (для теста дозвона)."""
        if not self._connected:
            return False, "Modem not connected"
        if not self.supports_voice:
            return False, "Модем без голоса"
        with self.lock:
            try:
                self.serial_conn.write(b'AT+CVHU=0\r')
                time.sleep(0.3)
                self.serial_conn.reset_input_buffer()
                self.serial_conn.write(f'ATD{number};\r'.encode('ascii'))
                end_time = time.time() + timeout_sec
                response = ""
                answered = False
                while time.time() < end_time:
                    if self.serial_conn.in_waiting > 0:
                        data = self.serial_conn.read(self.serial_conn.in_waiting).decode('ascii', errors='ignore')
                        response += data
                        if "^CONN" in response:
                            answered = True
                        if "^CEND" in response or "NO CARRIER" in response or "BUSY" in response or "NO ANSWER" in response:
                            break
                    time.sleep(0.2)
                self._hangup_locked()
                if answered:
                    return True, "Отвечен"
                return False, self._parse_call_status(response)
            except Exception as e:
                return False, str(e)

    def _parse_call_status(self, response):
        # Сюда попадаем только если ^CONN НЕ был получен, т.е. абонент НЕ ответил.
        # Поэтому «Отвечен» тут не возвращаем — только причину завершения.
        if "^CEND" in response:
            match = re.search(r'\^CEND:\s*(\d+),\s*(\d+),\s*(\d+),\s*(\d+)', response)
            if match:
                end_status = int(match.group(3))
                cc_cause = int(match.group(4))
                return self._cause_to_status(end_status, cc_cause)
            return "Завершён"
        if "BUSY" in response:
            return "Занято"
        if "NO ANSWER" in response:
            return "Нет ответа"
        if "NO CARRIER" in response:
            return "Нет соединения"
        if "NO DIALTONE" in response:
            return "Нет линии"
        if "ERROR" in response:
            return "Ошибка"
        return "Нет ответа"

    @staticmethod
    def _cause_to_status(end_status, cc_cause):
        # Без ^CONN звонок не был отвечен — «Отвечен» здесь не возвращаем.
        mapping = {
            16: "Сброшен",       # normal clearing без ответа (сеть/абонент отбили)
            17: "Занято",
            18: "Нет ответа",
            21: "Отклонён",
            28: "Нет линии",
            31: "Нет линии",
            34: "Нет соединения",
        }
        return mapping.get(cc_cause, "Нет ответа")

    def hangup(self):
        self.send_at("ATH", timeout=3)

    def get_status(self):
        return {
            'port': self.at_port,
            'at_port': self.at_port,
            'voice_port': self.voice_port,
            'connected': self._connected,
            'model': self.model,
            'imei': self.imei,
            'supports_voice': self.supports_voice,
            'signal_quality': self.signal_quality,
            'active': self.active,
        }


def _has_gsm0338():
    try:
        "test".encode('gsm0338')
        return True
    except Exception:
        return False


# ===========================================================================
# Менеджер модемов
# ===========================================================================

class ModemManager:
    def __init__(self):
        self.modems = {}   # ключ = at_port
        self.lock = threading.Lock()
        self._rr_index = 0   # round-robin для распределения SMS по модемам

    @staticmethod
    def _connect_with_timeout(modem, timeout=8.0):
        """
        modem.connect() -> serial.Serial(...) на Windows не имеет тайм-аута
        на само открытие порта. Если порт занят другим процессом/потоком
        (например, тем же физическим USB-модемом под активным звонком),
        открытие может зависнуть НАВСЕГДА, а вместе с ним и весь поток
        сканирования — пользователь видит «зависшее» приложение.
        Оборачиваем в отдельный поток с join(timeout): если не успели —
        просто бросаем эту попытку и идём дальше, зависший поток остаётся
        демоном и не мешает работе остального приложения.

        ВАЖНО: если поток всё же завершится ПОЗЖЕ тайм-аута и порт УСПЕШНО
        откроется — тот же порт нужно сразу закрыть. Иначе он останется
        занятым нашим же процессом навсегда, и все следующие попытки
        сканирования будут видеть его как «занят», хотя на деле это наш
        собственный «зомби»-хэндл (ровно так и было в прошлый раз — после
        нескольких зависших попыток сканирования порты переставали находиться
        именно из-за этой утечки).
        """
        result = {}
        done = threading.Event()

        def _do():
            try:
                result['ok'] = modem.connect()
            except Exception as e:
                result['ok'] = False
                result['err'] = str(e)
            finally:
                done.set()

        t = threading.Thread(target=_do, daemon=True)
        t.start()
        finished = done.wait(timeout)
        if not finished:
            logger.warning(f"connect timeout on {modem.at_port} — порт не отвечает, пропускаем")

            def _cleanup_if_late():
                done.wait()
                if result.get('ok'):
                    logger.warning(f"{modem.at_port}: подключение завершилось ПОСЛЕ тайм-аута — закрываю порт")
                    try:
                        modem.disconnect()
                    except Exception:
                        pass

            threading.Thread(target=_cleanup_if_late, daemon=True).start()
            return False
        return result.get('ok', False)

    def scan_ports(self):
        """Список командных (AT) портов обнаруженных модемов."""
        return [g["at_port"] for g in discover_modem_groups() if g["at_port"]]

    def scan_all_detailed(self):
        return discover_modem_groups()

    def auto_connect_all(self, baudrate=DEFAULT_BAUD):
        groups = discover_modem_groups()
        results = []
        # IMEI уже подключённых модемов — чтобы не задваивать один физический модем,
        # который через хаб виден под несколькими COM-портами (частая беда E3372)
        with self.lock:
            connected_imeis = {m.imei for m in self.modems.values() if m.imei}
        for g in groups:
            voice_port = g.get("voice_port")
            candidates = [p for p in (g.get("at_port"), g.get("modem_port")) if p]
            already = any(c in self.modems and self.modems[c].connected for c in candidates)
            if already:
                continue

            connected = False
            last_port = candidates[0] if candidates else "?"
            for cand in candidates:
                modem = Modem(cand, voice_port=voice_port, baudrate=baudrate)
                if self._connect_with_timeout(modem):
                    # мусорный/вспомогательный порт (не опознан) — не показываем
                    if not modem.imei and not modem.model:
                        modem.disconnect()
                        last_port = cand
                        continue
                    # тот же физический модем уже подключён под другим COM — пропускаем дубль
                    if modem.imei and modem.imei in connected_imeis:
                        modem.disconnect()
                        connected = True
                        break
                    if modem.imei:
                        connected_imeis.add(modem.imei)
                    with self.lock:
                        self.modems[cand] = modem
                    results.append({'port': cand, 'voice_port': voice_port,
                                    'success': True, 'model': modem.model,
                                    'voice': modem.supports_voice})
                    connected = True
                    break
                else:
                    last_port = cand
            if not connected:
                results.append({'port': last_port, 'success': False,
                                'model': None, 'voice': False,
                                'reason': 'порт занят или не отвечает'})
        return results

    def connect_modem(self, at_port, baudrate=DEFAULT_BAUD):
        with self.lock:
            if at_port in self.modems and self.modems[at_port].connected:
                return True
        # найти голосовой порт для этого AT-порта
        voice_port = None
        for g in discover_modem_groups():
            if g["at_port"] == at_port:
                voice_port = g.get("voice_port")
                break
        modem = Modem(at_port, voice_port=voice_port, baudrate=baudrate)
        if self._connect_with_timeout(modem):
            with self.lock:
                self.modems[at_port] = modem
            logger.info(f"Modem connected on {at_port} (voice={voice_port}): {modem.model}")
            return True
        return False

    def disconnect_modem(self, at_port):
        with self.lock:
            if at_port in self.modems:
                self.modems[at_port].disconnect()
                del self.modems[at_port]

    def get_all_modems(self):
        with self.lock:
            return list(self.modems.values())

    def get_available_modem(self, prefer_voice=False):
        """Выбор модема для отправки SMS: сначала свободный (не занят), иначе по кругу —
        чтобы одновременные SMS уходили через РАЗНЫЕ модемы, а не ждали один."""
        with self.lock:
            mods = [m for m in self.modems.values()
                    if m.connected and m.active and (not prefer_voice or m.supports_voice)]
            if not mods:
                # запасной вариант: любой подключённый (даже неактивный)
                mods = [m for m in self.modems.values()
                        if m.connected and (not prefer_voice or m.supports_voice)]
            if not mods:
                return None
            now = time.time()
            # предпочесть свободный и не зарезервированный только что другим потоком
            for m in mods:
                if not m.lock.locked() and getattr(m, "_reserved_until", 0) < now:
                    m._reserved_until = now + 3   # краткий резерв, чтобы параллельный
                    return m                       # поток не выбрал этот же модем
            # все заняты — отдаём по кругу
            idx = self._rr_index % len(mods)
            self._rr_index += 1
            return mods[idx]

    def get_candidate_modems(self, prefer_voice=False):
        """
        Список модемов для отправки SMS в порядке round-robin, свободные — первыми.
        Используется для перебора: если на одном модеме ошибка (нет баланса, занят
        звонком и т.п.), отправитель пробует следующий из списка.
        """
        with self.lock:
            mods = [m for m in self.modems.values()
                    if m.connected and m.active and (not prefer_voice or m.supports_voice)]
            if not mods:
                mods = [m for m in self.modems.values()
                        if m.connected and (not prefer_voice or m.supports_voice)]
            if not mods:
                return []
            idx = self._rr_index % len(mods)
            self._rr_index += 1
            ordered = mods[idx:] + mods[:idx]
            free = [m for m in ordered if not m.lock.locked()]
            busy = [m for m in ordered if m.lock.locked()]
            return free + busy

    def get_voice_modems(self):
        with self.lock:
            return [m for m in self.modems.values() if m.connected and m.supports_voice and m.active]

    def get_all_status(self):
        with self.lock:
            return [m.get_status() for m in self.modems.values()]

    def set_active(self, at_port, active):
        with self.lock:
            if at_port in self.modems:
                self.modems[at_port].active = active

    def get_active_modems(self):
        with self.lock:
            return [m for m in self.modems.values() if m.connected and m.active]

    def get_active_voice_modems(self):
        with self.lock:
            return [m for m in self.modems.values() if m.connected and m.supports_voice and m.active]

    def sync_modems(self):
        """
        Удаляем модем ТОЛЬКО если его последовательный порт реально закрыт
        (модем физически вынут). НЕ удаляем по исчезновению COM-номера из списка:
        через USB-хаб Windows временно переназначает номера, а соединение живо —
        иначе рабочие модемы ложно отключались. Проверка is_open не берёт блокировку,
        поэтому не мешает активным звонкам.
        """
        with self.lock:
            to_remove = []
            for at_port, modem in self.modems.items():
                conn = modem.serial_conn
                if conn is None or not getattr(conn, "is_open", False):
                    to_remove.append(at_port)
            for at_port in to_remove:
                try:
                    self.modems[at_port].disconnect()
                except Exception:
                    pass
                del self.modems[at_port]
                logger.info(f"Removed modem on {at_port} (порт закрыт)")
            return to_remove

    def disconnect_all(self):
        with self.lock:
            for modem in self.modems.values():
                modem.disconnect()
            self.modems.clear()
