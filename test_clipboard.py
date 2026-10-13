import gc
import json
from pathlib import Path
import tempfile
import tkinter as tk
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import app as app_module


class ClipboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.patches = [
            patch.object(app_module, 'DATA_DIR', root),
            patch.object(app_module, 'DATA_FILE', root / 'notes.json'),
            patch.object(app_module, 'NarrationEngine', return_value=None),
        ]
        for item in self.patches:
            item.start()
        self.app = app_module.TellMeApp()
        self.app.withdraw()
        self.app.update()
        self.editor = self.app.editors[self.app._active_id()]

    def tearDown(self):
        for timer in self.app.tk.call('after', 'info'):
            self.app.after_cancel(timer)
        self.app._close()
        self.editor = None
        self.app = None
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()
        gc.collect()

    def contents(self, editor=None):
        return (editor or self.editor).get('1.0', 'end-1c')

    def test_paste_replaces_selection_once_and_undo_restores_it(self):
        self.editor.insert('1.0', 'До старый После')
        self.editor.tag_add('sel', '1.3', '1.9')
        self.editor.mark_set('insert', 'end-1c')
        with patch.object(self.app, 'clipboard_get', return_value='Новый\r\nтекст 🙂'):
            self.editor.event_generate('<<Paste>>')
        self.assertEqual(self.contents(), 'До Новый\nтекст 🙂 После')
        self.editor.edit_undo()
        self.assertEqual(self.contents(), 'До старый После')
        self.editor.edit_redo()
        self.assertEqual(self.contents(), 'До Новый\nтекст 🙂 После')

    def test_buttons_use_active_note_and_copy_full_original_text(self):
        self.editor.insert('1.0', 'Другая вкладка')
        self.app._add_note()
        active = self.app.editors[self.app._active_id()]
        active.insert('1.0', 'Начало https://example.com Конец')
        active.tag_add('sel', '1.0', '1.6')
        with patch.object(self.app, 'clipboard_clear') as clear, \
                patch.object(self.app, 'clipboard_append') as append:
            self.app._copy_note()
        clear.assert_called_once_with()
        append.assert_called_once_with('Начало https://example.com Конец')
        active.tag_remove('sel', '1.0', 'end')
        active.mark_set('insert', '1.6')
        with patch.object(self.app, 'clipboard_get', return_value=' + '):
            self.app._paste_note()
        self.assertEqual(self.contents(active), 'Начало +  https://example.com Конец')
        self.assertEqual(self.contents(), 'Другая вкладка')

    def test_paste_and_clear_leave_karaoke_and_are_saved(self):
        self.app.karaoke_var.set(True)
        self.app._toggle_karaoke()
        with patch.object(self.app, 'clipboard_get', return_value='Сохраняемый текст'):
            self.app._paste_note()
        self.assertFalse(self.app.karaoke_var.get())
        self.assertEqual(self.app.karaoke_frame.winfo_manager(), '')
        self.app._save()
        saved = json.loads(app_module.DATA_FILE.read_text(encoding='utf-8'))
        self.assertEqual(saved['notes'][0]['text'], 'Сохраняемый текст')
        self.app.auto_prepare_var.set(False)
        self.app._clear_note()
        self.assertEqual(self.contents(), '')
        self.app._save()
        saved = json.loads(app_module.DATA_FILE.read_text(encoding='utf-8'))
        self.assertEqual(saved['notes'][0]['text'], '')
        self.editor.edit_undo()
        self.assertEqual(self.contents(), 'Сохраняемый текст')

    def test_empty_or_unavailable_clipboard_keeps_selected_text(self):
        self.editor.insert('1.0', 'Сохранить')
        self.editor.tag_add('sel', '1.0', 'end-1c')
        for result in ('', tk.TclError('unavailable')):
            with self.subTest(result=result):
                kwargs = {'side_effect': result} if isinstance(result, Exception) else {'return_value': result}
                with patch.object(self.app, 'clipboard_get', **kwargs):
                    self.app._paste_note()
                self.assertEqual(self.contents(), 'Сохранить')
                self.assertTrue(self.editor.tag_ranges('sel'))

    def test_russian_shortcuts_paste_and_undo_without_duplicate_insertion(self):
        self.app.deiconify()
        self.editor.focus_force()
        self.app.update()
        event = SimpleNamespace(keycode=86, keysym='Cyrillic_em', state=4, widget=self.editor)
        with patch.object(self.app, 'clipboard_get', return_value='Вставлено'):
            self.assertEqual(self.app._editor_shortcut(event), 'break')
        self.assertEqual(self.contents(), 'Вставлено')
        event.keycode = 90
        event.keysym = 'Cyrillic_ya'
        self.app._editor_shortcut(event)
        self.assertEqual(self.contents(), '')
        event.state |= 1
        self.app._editor_shortcut(event)
        self.assertEqual(self.contents(), 'Вставлено')
        event.keycode = 86
        event.state = 4 | 0x20000
        self.assertIsNone(self.app._editor_shortcut(event))
        self.assertEqual(self.contents(), 'Вставлено')

    def test_control_v_and_shift_insert_use_the_same_paste_handler(self):
        self.app.deiconify()
        self.editor.focus_force()
        self.app.update()
        with patch.object(self.app, 'clipboard_get', return_value='Текст'):
            self.editor.event_generate('<Control-KeyPress>', keycode=86)
            self.assertEqual(self.contents(), 'Текст')
            self.editor.event_generate('<Shift-KeyPress-Insert>')
        self.assertEqual(self.contents(), 'ТекстТекст')


if __name__ == '__main__':
    unittest.main()
