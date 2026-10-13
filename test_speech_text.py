import unittest

from speech_text import find_links, prepare_for_speech


class SpeechTextTests(unittest.TestCase):
    def test_bare_url_is_not_spoken(self):
        text = "Откройте https://example.com/path?q=1, затем слушайте."
        spoken = prepare_for_speech(text)
        self.assertIn("Это ссылка", spoken)
        self.assertNotIn("example.com", spoken)
        self.assertIn("слушайте", spoken)
        self.assertNotIn(". ,", spoken)

    def test_markdown_link_keeps_label(self):
        spoken = prepare_for_speech("Читайте [инструкцию](https://example.com/help).")
        self.assertIn("инструкцию", spoken)
        self.assertIn("Это ссылка", spoken)
        self.assertNotIn("example.com", spoken)
        self.assertNotIn(". .", spoken)

    def test_editor_link_ranges_exclude_punctuation(self):
        text = "Ссылка: https://example.com/a. И [сайт](www.example.org)."
        links = find_links(text)
        self.assertEqual(len(links), 2)
        self.assertEqual(text[links[0][0]:links[0][1]], "https://example.com/a")
        self.assertEqual(text[links[1][0]:links[1][1]], "[сайт](www.example.org)")


if __name__ == "__main__":
    unittest.main()
