# Запуск и сборка

## Подготовка окружения Windows

Нужен Python 3.12+ x64 с Tkinter. Для готовых записей используется Windows Media Player Legacy.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt pyinstaller
```

`start.cmd` использует `.venv\Scripts\pythonw.exe`, если окружение существует. Иначе использует системный Python.

## Сборка

Запустите `build.cmd`. PyInstaller создаёт отдельный `dist\TellMe_v7.exe`; Python на целевом компьютере не требуется. Параметр `--hidden-import _cffi_backend` нужен для miniaudio. PyInstaller собирает Tcl/Tk и библиотеки PyAV автоматически.

Перед запуском новой версии закройте старую. Пользовательские данные находятся в APPDATA и читаются новой версией без миграции. Предыдущие EXE не удаляются и не перезаписываются.

Проверка новой сборки должна использовать отдельный временный APPDATA и закрывать только тестовые процессы.
