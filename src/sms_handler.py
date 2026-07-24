import threading
import logging
import time
from collections import deque

logger = logging.getLogger(__name__)


class SmsHandler:
    def __init__(self, modem_manager, database, callback=None):
        self.modem_manager = modem_manager
        self.database = database
        self.callback = callback
        self.active = False
        self._sms_thread = None
        self._stop_flag = False

    def start_campaign(self, numbers, message, source="manual"):
        if self.active:
            return False, "Campaign already running"
        self.active = True
        self._stop_flag = False
        self._sms_thread = threading.Thread(
            target=self._run_campaign,
            args=(numbers, message, source),
            daemon=True
        )
        self._sms_thread.start()
        return True, "Campaign started"

    def stop_campaign(self):
        self._stop_flag = True
        self.active = False

    def _run_campaign(self, numbers, message, source):
        modems = self.modem_manager.get_active_modems()
        if not modems:
            self._notify("error", "No modems available")
            self.active = False
            return

        total = len(numbers)
        done = 0
        self._notify("progress", f"Starting SMS campaign: {total} numbers, {len(modems)} modems")

        queue = deque(numbers)
        modem_threads = []

        def worker(modem):
            nonlocal done
            while queue and not self._stop_flag:
                try:
                    number = queue.popleft()
                except IndexError:
                    break
                self._notify("sending", f"Sending SMS to {number} via {modem.port}")
                ok, info = modem.send_sms_multipart(number, message)
                status = "sent" if ok else f"error: {info}"
                self.database.log_sms(number, message, modem.port, status, source, info)
                self._notify("result", {"number": number, "status": status, "modem": modem.port, "success": ok})
                done += 1
                self._notify("progress", f"Progress: {done}/{total}")
                time.sleep(2)

        for modem in modems:
            t = threading.Thread(target=worker, args=(modem,), daemon=True)
            modem_threads.append(t)
            t.start()

        for t in modem_threads:
            t.join()

        self._notify("done", f"SMS campaign finished. Total: {done}/{total}")
        self.active = False

    def send_single(self, number, message, source="manual"):
        """
        Отправка одного SMS (используется SMPP-сервером). Перебирает модемы по кругу:
        если на текущем ошибка (нет SMS-баланса, занят звонком и т.п.) — пробует
        следующий модем, а не просто падает.
        """
        candidates = self.modem_manager.get_candidate_modems()
        if not candidates:
            self.database.log_sms(number, message, "none", "error", source, "No modem available")
            return False, "No modem available"
        last_info = "No modem available"
        for modem in candidates:
            ok, info = modem.send_sms_multipart(number, message)
            if ok:
                self.database.log_sms(number, message, modem.port, "sent", source, info)
                return True, info
            last_info = info
            logger.warning(f"SMS to {number} via {modem.port} failed: {info} — пробую следующий модем")
        self.database.log_sms(number, message, "none", "error", source, last_info)
        return False, last_info

    def _notify(self, event, data):
        if self.callback:
            self.callback(event, data)
        logger.info(f"SmsHandler: {event} -> {data}")
