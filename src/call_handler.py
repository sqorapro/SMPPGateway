import threading
import logging
import time
from collections import deque

import audio

logger = logging.getLogger(__name__)


class CallHandler:
    def __init__(self, modem_manager, database, callback=None):
        self.modem_manager = modem_manager
        self.database = database
        self.callback = callback
        self.active = False
        self.lock = threading.Lock()
        self._call_thread = None
        self._stop_flag = False

    def start_campaign(self, numbers, audio_file_path, timeout_sec=30):
        if self.active:
            return False, "Campaign already running"
        self.active = True
        self._stop_flag = False
        self._call_thread = threading.Thread(
            target=self._run_campaign,
            args=(numbers, audio_file_path, timeout_sec),
            daemon=True
        )
        self._call_thread.start()
        return True, "Campaign started"

    def stop_campaign(self):
        self._stop_flag = True
        self.active = False

    def _run_campaign(self, numbers, audio_file_path, timeout_sec):
        voice_modems = self.modem_manager.get_active_voice_modems()
        if not voice_modems:
            self._notify("error", "Нет активных модемов с поддержкой голоса (E173)")
            self.active = False
            return

        # Конвертируем аудио ОДИН РАЗ в PCM 8кГц/моно/16бит, потом шлём всем.
        try:
            pcm_data, audio_total_sec = audio.load_pcm_8k_mono16(audio_file_path)
        except Exception as e:
            self._notify("error", f"Не удалось загрузить аудио: {e}")
            self.active = False
            return
        if not pcm_data:
            self._notify("error", "Пустой аудиофайл")
            self.active = False
            return

        total = len(numbers)
        done = 0
        self._notify("progress", f"Старт кампании: {total} номеров, {len(voice_modems)} модемов, аудио {audio_total_sec:.1f}с")

        queue = deque(numbers)
        modem_threads = []

        # Если модем подряд проваливает MAX_CONSECUTIVE_FAILS попыток С НАСТОЯЩИМ
        # признаком поломки, выводим его из кампании. ВАЖНО: считаем только «жёсткие»
        # ошибки — когда модем сам отверг набор (AT^ATD вернул ERROR мгновенно,
        # не дойдя даже до ^ORIG). Это реальный симптом сломанного/зависшего модема
        # (так и было с COM86 — он не мог набрать НИ ОДНОГО номера подряд).
        # «Нет ответа», «Занято», «Нет линии», «Сброшен» — это НОРМАЛЬНЫЙ исход
        # реального звонка (абонент не взял трубку, сеть не смогла соединить) и НЕ
        # означает поломку модема. На кампании в десятки тысяч номеров пять «Нет
        # ответа» подряд — обычная статистика, а не признак сломанного железа.
        # Раньше предохранитель считал ЛЮБую неудачу и на реальной кампании из 99528
        # номеров вывел из строя все 8 модемов за первые 44 звонка — это и было
        # исправлено здесь.
        HARD_FAIL_STATUSES = {"Ошибка набора", "Modem not connected", "Модем без голоса"}
        MAX_CONSECUTIVE_FAILS = 5

        def worker(modem):
            nonlocal done
            consecutive_fails = 0
            while queue and not self._stop_flag:
                try:
                    number = queue.popleft()
                except IndexError:
                    break
                self._notify("calling", {"number": number, "modem": modem.port})
                ok, info, audio_played, audio_total = modem.make_call_with_audio(number, pcm_data, timeout_sec)
                status = "answered" if ok else info
                duration_str = f"{audio_played}s / {audio_total:.0f}s" if ok else ""
                self.database.log_call(number, modem.port, status, audio_file_path, duration_str)
                self._notify("result", {"number": number, "status": status, "modem": modem.port, "audio_played": audio_played, "audio_total": audio_total, "success": ok})
                done += 1
                self._notify("progress", f"Progress: {done}/{total}")

                if ok or status not in HARD_FAIL_STATUSES:
                    # успех ИЛИ обычный сетевой исход звонка — не признак поломки
                    consecutive_fails = 0
                else:
                    consecutive_fails += 1
                    if consecutive_fails >= MAX_CONSECUTIVE_FAILS:
                        # Отдельное событие, не "error" — иначе GUI показал бы модальное окно
                        # и сбросил кнопки Начать/Стоп, как будто ВСЯ кампания завершилась,
                        # хотя другие модемы продолжают звонить.
                        self._notify("modem_disabled",
                                    f"Модем {modem.port}: {consecutive_fails} ошибок подряд "
                                    f"({status}) — похоже, модем неисправен. Выведен из этой "
                                    f"кампании, проверьте физически. Оставшиеся номера доберут "
                                    f"другие модемы.")
                        logger.warning(f"Modem {modem.port} disabled after {consecutive_fails} consecutive failures")
                        break
                time.sleep(1)

        for modem in voice_modems:
            t = threading.Thread(target=worker, args=(modem,), daemon=True)
            modem_threads.append(t)
            t.start()

        for t in modem_threads:
            t.join()

        self._notify("done", f"Campaign finished. Total: {done}/{total}")
        self.active = False

    def _notify(self, event, data):
        if self.callback:
            self.callback(event, data)
        logger.info(f"CallHandler: {event} -> {data}")
