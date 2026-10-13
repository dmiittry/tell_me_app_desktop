# Проект Tell Me

Настольное приложение для Windows: заметки во вкладках, автоматическое сохранение, подготовка речи в фоне, воспроизведение с караоке, перемоткой, скоростью и громкостью.

Стек: Python 3.12+ x64, Tkinter, edge-tts, PyAV, miniaudio и pywin32. Онлайн-голоса используют Microsoft; голоса Windows работают через SAPI. Готовые записи проигрываются через Windows Media Player Legacy.

Основные файлы: `app.py`, `narration.py`, `streaming_audio.py`, `audio_chunks.py`, `speech_text.py`, `karaoke.py`. Зависимости закреплены в `requirements.txt`. Запуск и сборка: `start.cmd`, `build.cmd`.

Версия v7 добавляет кнопки работы с текстом активной заметки и сочетания клавиш, устойчивые к смене раскладки Windows.
