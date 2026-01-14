import sys
import os
import sqlite3
import time
import re
import json
import urllib.parse

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QVBoxLayout, QHBoxLayout,
    QWidget, QListWidget, QPushButton, QSlider, QLabel,
    QLineEdit, QTextEdit, QListWidgetItem
)
from PyQt6.QtCore import QThread, pyqtSignal, Qt

import vlc
import speech_recognition as sr
import requests
import yt_dlp


MUSIC_FOLDERS = [r"C:\Users"]
DB_PATH = "music_library.db"
WAKE_WORD = "ассистент"


class MusicScanner(QThread):
    finished = pyqtSignal(list)

    def __init__(self):
        super().__init__()

    def run(self):
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            """CREATE TABLE IF NOT EXISTS tracks
               (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, artist TEXT, path TEXT UNIQUE)"""
        )
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_title ON tracks(title)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_artist ON tracks(artist)")

        for folder in MUSIC_FOLDERS:
            if os.path.exists(folder):
                for root, _, files in os.walk(folder):
                    for file in files:
                        if file.lower().endswith((".mp3", ".wav", ".flac", ".m4a")):
                            path = os.path.join(root, file)
                            title = os.path.splitext(file)[0]
                            artist = "Unknown"
                            cursor.execute(
                                "INSERT OR IGNORE INTO tracks (title, artist, path) VALUES (?, ?, ?)",
                                (title, artist, path),
                            )

        conn.commit()
        cursor.execute("SELECT title, artist, path FROM tracks ORDER BY artist, title")
        tracks = cursor.fetchall()
        conn.close()
        self.finished.emit(tracks)


class VoiceListener(QThread):
    command_received = pyqtSignal(str)
    listening_status = pyqtSignal(bool)

    def __init__(self, wake_word: str = WAKE_WORD):
        super().__init__()
        self.running = True
        self.wake_word = wake_word.strip().lower()

    def stop(self):
        self.running = False

    def run(self):
        recognizer = sr.Recognizer()

        try:
            microphone = sr.Microphone()
        except Exception:
            return

        try:
            with microphone as source:
                recognizer.dynamic_energy_threshold = True
                recognizer.adjust_for_ambient_noise(source, duration=0.6)
        except Exception:
            return

        while self.running:
            self.listening_status.emit(True)
            try:
                with microphone as source:
                    audio = recognizer.listen(source, timeout=0.6, phrase_time_limit=4)

                if not self.running:
                    break

                text = recognizer.recognize_google(audio, language="ru-RU").lower().strip()
                if self.wake_word and self.wake_word in text:
                    cmd = text.replace(self.wake_word, "", 1).strip()
                    if cmd:
                        self.command_received.emit(cmd)

            except (sr.WaitTimeoutError, sr.UnknownValueError):
                pass
            except sr.RequestError:
                time.sleep(0.3)
            except Exception:
                time.sleep(0.1)
            finally:
                self.listening_status.emit(False)

            time.sleep(0.05)


def find_first_video_renderer(data):
    if isinstance(data, dict) and "videoRenderer" in data:
        return data["videoRenderer"]
    if isinstance(data, dict):
        for value in data.values():
            result = find_first_video_renderer(value)
            if result:
                return result
    elif isinstance(data, list):
        for item in data:
            result = find_first_video_renderer(item)
            if result:
                return result
    return None


class YouTubeSearchThread(QThread):
    search_finished = pyqtSignal(str, str, str)

    def __init__(self, query: str):
        super().__init__()
        self.query = query

    def run(self):
        search_url = f"https://www.youtube.com/results?search_query={urllib.parse.quote(self.query)}"
        try:
            r = requests.get(search_url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()

            match = re.search(r"var ytInitialData\s*=\s*(\{.+?\});", r.text, re.DOTALL)
            if not match:
                self.search_finished.emit("", "", "❌ Не удалось разобрать страницу YouTube")
                return

            data = json.loads(match.group(1))
            video = find_first_video_renderer(data)
            if not video:
                self.search_finished.emit("", "", "❌ Видео не найдено")
                return

            video_id = video.get("videoId", "")
            if not video_id:
                self.search_finished.emit("", "", "❌ Не удалось получить videoId")
                return

            title_runs = video.get("title", {}).get("runs", [])
            title = title_runs[0].get("text", "Unknown") if title_runs else "Unknown"

            artist = "Unknown"
            if "shortBylineText" in video and "simpleText" in video["shortBylineText"]:
                artist = video["shortBylineText"]["simpleText"]
            elif "longBylineText" in video and "runs" in video["longBylineText"]:
                artist = "".join(run.get("text", "") for run in video["longBylineText"]["runs"]).strip() or "Unknown"

            watch_url = f"https://www.youtube.com/watch?v={video_id}"
            self.search_finished.emit(watch_url, title, artist)

        except Exception as e:
            self.search_finished.emit("", "", f"❌ Ошибка: {e}")


class YouTubeResolveThread(QThread):
    resolved = pyqtSignal(str, str)

    def __init__(self, watch_url: str):
        super().__init__()
        self.watch_url = watch_url

    def run(self):
        try:
            ydl_opts = {
                "quiet": True,
                "no_warnings": True,
                "noplaylist": True,
                "format": "bestaudio/best",
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(self.watch_url, download=False)

            direct = info.get("url")
            if not direct:
                self.resolved.emit("", "❌ yt-dlp не вернул прямой аудио URL")
                return

            self.resolved.emit(direct, "")
        except Exception as e:
            self.resolved.emit("", f"❌ yt-dlp ошибка: {e}")


class MusicAssistant(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("голосовой музыкальный ассистент")
        self.setGeometry(100, 100, 900, 600)

        self.setStyleSheet("""
            QMainWindow { background-color: #1e1e1e; color: #ffffff; }
            QListWidget { background-color: #2d2d2d; border: 1px solid #555; color: #ffffff; }
            QPushButton { background-color: #4a4a4a; border: 1px solid #666; padding: 10px; font-size: 14px; color: #ffffff; }
            QPushButton:hover { background-color: #5a5a5a; }
            QPushButton:pressed { background-color: #3a3a3a; }
            QSlider { background-color: #2d2d2d; }
            QLabel { color: #ffffff; }
            QLineEdit { background-color: #2d2d2d; border: 1px solid #555; padding: 5px; color: #ffffff; }
            QTextEdit { background-color: #111; color: #ddd; }
        """)

        self.instance = vlc.Instance("--intf=dummy", "--no-video-title-show")
        self.player = self.instance.media_player_new()
        self.player.audio_set_volume(50)

        self.voice_thread = None
        self.yt_thread = None
        self.yt_resolve_thread = None

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        layout = QHBoxLayout(central_widget)

        self.music_list = QListWidget()
        self.music_list.itemClicked.connect(self.play_selected)
        layout.addWidget(self.music_list, 1)

        center_layout = QVBoxLayout()
        layout.addLayout(center_layout, 2)

        self.current_track_label = QLabel("Нет трека")
        self.current_track_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.current_track_label.setStyleSheet("font-size: 18px; font-weight: bold;")
        center_layout.addWidget(self.current_track_label)

        btn_layout = QHBoxLayout()
        self.play_btn = QPushButton("Play/Pause")
        self.play_btn.clicked.connect(self.toggle_play)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.clicked.connect(self.stop)
        self.prev_btn = QPushButton("Prev")
        self.prev_btn.clicked.connect(self.prev_track)
        self.next_btn = QPushButton("Next")
        self.next_btn.clicked.connect(self.next_track)

        btn_layout.addWidget(self.play_btn)
        btn_layout.addWidget(self.stop_btn)
        btn_layout.addWidget(self.prev_btn)
        btn_layout.addWidget(self.next_btn)
        center_layout.addLayout(btn_layout)

        vol_layout = QHBoxLayout()
        vol_label = QLabel("Громкость:")
        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.valueChanged.connect(self.set_volume)
        self.volume_slider.setValue(50)
        vol_layout.addWidget(vol_label)
        vol_layout.addWidget(self.volume_slider)
        center_layout.addLayout(vol_layout)

        search_layout = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Поиск музыки...")
        self.search_input.returnPressed.connect(self.search_music)
        search_btn = QPushButton("Искать")
        search_btn.clicked.connect(self.search_music)
        search_layout.addWidget(self.search_input)
        search_layout.addWidget(search_btn)
        center_layout.addLayout(search_layout)

        self.voice_btn = QPushButton("Голосовой помощник")
        self.voice_btn.clicked.connect(self.toggle_voice)
        center_layout.addWidget(self.voice_btn)

        self.voice_indicator = QLabel("Голос: выкл")
        center_layout.addWidget(self.voice_indicator)

        self.status_label = QLabel("Готов к работе")
        center_layout.addWidget(self.status_label)

        self.log_text = QTextEdit()
        self.log_text.setMaximumHeight(130)
        center_layout.addWidget(self.log_text)

        self.scanner = MusicScanner()
        self.scanner.finished.connect(self.load_music_list)
        self.scanner.start()
        self.log("Сканирую локальную музыку...")

    def closeEvent(self, event):
        try:
            if self.voice_thread is not None and self.voice_thread.isRunning():
                self.voice_thread.stop()
                self.voice_thread.wait(1500)
        finally:
            super().closeEvent(event)

    def log(self, message: str):
        self.log_text.append(f"[LOG] {message}")
        self.status_label.setText(message)

    def load_music_list(self, tracks):
        self.music_list.clear()
        for title, artist, path in tracks:
            item = QListWidgetItem(f"{artist} - {title}")
            item.setData(Qt.ItemDataRole.UserRole, path)
            self.music_list.addItem(item)
        self.log(f"Загружено {len(tracks)} треков")

    def play_selected(self, item):
        path = item.data(Qt.ItemDataRole.UserRole)
        if not path:
            return

        text = item.text().replace(" (YouTube)", "")
        try:
            artist, title = text.rsplit(" - ", 1)
        except ValueError:
            artist, title = "Unknown", text

        if isinstance(path, str) and "youtube.com/watch" in path:
            self.play_youtube(path, title, artist)
        else:
            self.play_track(path, title, artist)

    def play_track(self, path, title, artist):
        media = self.instance.media_new(path)
        self.player.set_media(media)
        self.player.play()
        self.current_track_label.setText(f"{artist} - {title}")
        self.log(f"Играю: {artist} - {title}")

    def play_youtube(self, watch_url: str, title: str, artist: str):
        self.log("YouTube: получаю прямую ссылку на аудио...")
        self.current_track_label.setText(f"{artist} - {title} (YouTube)")

        if self.yt_resolve_thread is not None and self.yt_resolve_thread.isRunning():
            return

        self.yt_resolve_thread = YouTubeResolveThread(watch_url)
        self.yt_resolve_thread.resolved.connect(
            lambda direct, err: self._on_youtube_resolved(direct, err, title, artist)
        )
        self.yt_resolve_thread.start()

    def _on_youtube_resolved(self, direct_audio_url: str, err: str, title: str, artist: str):
        if err:
            self.log(err)
            return

        media = self.instance.media_new(direct_audio_url)
        media.add_option(":no-video")
        media.add_option(":network-caching=2000")
        self.player.set_media(media)
        self.player.play()
        self.log(f"✅ YouTube играет: {artist} - {title}")

    def toggle_play(self):
        if self.player.is_playing():
            self.player.pause()
        else:
            self.player.play()

    def stop(self):
        self.player.stop()
        self.current_track_label.setText("Нет трека")

    def next_track(self):
        row = self.music_list.currentRow()
        if row < self.music_list.count() - 1:
            self.music_list.setCurrentRow(row + 1)
            item = self.music_list.currentItem()
            if item:
                self.play_selected(item)

    def prev_track(self):
        row = self.music_list.currentRow()
        if row > 0:
            self.music_list.setCurrentRow(row - 1)
            item = self.music_list.currentItem()
            if item:
                self.play_selected(item)

    def set_volume(self, value: int):
        self.player.audio_set_volume(value)

    def search_music(self):
        query = self.search_input.text().lower().strip()
        if not query:
            return

        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT title, artist, path FROM tracks WHERE LOWER(title) LIKE ? OR LOWER(artist) LIKE ?",
            (f"%{query}%", f"%{query}%"),
        )
        results = cursor.fetchall()
        conn.close()

        self.music_list.clear()
        for title, artist, path in results:
            item = QListWidgetItem(f"{artist} - {title}")
            item.setData(Qt.ItemDataRole.UserRole, path)
            self.music_list.addItem(item)

        if results:
            self.music_list.setCurrentRow(0)
            self.play_selected(self.music_list.item(0))
            self.log(f"Найдено локально: {len(results)} треков")
        else:
            self.log("Не найдено локально, ищу в YouTube...")
            self.yt_thread = YouTubeSearchThread(query)
            self.yt_thread.search_finished.connect(self.on_yt_search_finished)
            self.yt_thread.start()

    def on_yt_search_finished(self, watch_url, title, artist_or_error):
        if not watch_url:
            self.log(artist_or_error)
            return

        item = QListWidgetItem(f"{artist_or_error} - {title} (YouTube)")
        item.setData(Qt.ItemDataRole.UserRole, watch_url)
        self.music_list.addItem(item)
        self.music_list.setCurrentItem(item)
        self.play_selected(item)

    def toggle_voice(self):
        if self.voice_thread is None or not self.voice_thread.isRunning():
            self.voice_thread = VoiceListener(WAKE_WORD)
            self.voice_thread.command_received.connect(self.process_voice_command)
            self.voice_thread.listening_status.connect(self.update_voice_indicator)
            self.voice_thread.start()
            self.voice_btn.setText("Стоп голос")
            self.log("Голосовой помощник включен (скажите 'ассистент ...')")
        else:
            self.voice_thread.stop()
            self.voice_thread.wait(1500)
            self.voice_thread = None
            self.voice_btn.setText("Голосовой помощник")
            self.voice_indicator.setText("Голос: выкл")
            self.voice_indicator.setStyleSheet("")
            self.log("Голосовой помощник выключен")

    def update_voice_indicator(self, listening: bool):
        if listening:
            self.voice_indicator.setText("Голос: слушает...")
            self.voice_indicator.setStyleSheet("color: #00ff00;")
        else:
            self.voice_indicator.setText("Голос: ждет...")
            self.voice_indicator.setStyleSheet("color: #ffff00;")

    def process_voice_command(self, command: str):
        self.log(f"Команда: {command}")
        command = command.lower().strip()

        if any(word in command for word in ["включи", "играй", "поставь"]):
            match = re.search(r"(музыку|трек|песню|песня)\s+(.+)", command)
            if match:
                query = match.group(2).strip()
                if query:
                    self.search_input.setText(query)
                    self.search_music()
        elif "пауза" in command:
            self.player.pause()
        elif any(word in command for word in ["стоп", "выключи", "останови"]):
            self.stop()
        elif "следующий" in command or "next" in command:
            self.next_track()
        elif "предыдущий" in command or "prev" in command:
            self.prev_track()
        elif "громче" in command:
            self.volume_slider.setValue(min(100, self.player.audio_get_volume() + 10))
        elif "тише" in command:
            self.volume_slider.setValue(max(0, self.player.audio_get_volume() - 10))
        elif "громкость" in command:
            m = re.search(r"громкость\s+(\d+)", command)
            if m:
                self.volume_slider.setValue(min(100, max(0, int(m.group(1)))))


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MusicAssistant()
    window.show()
    sys.exit(app.exec())