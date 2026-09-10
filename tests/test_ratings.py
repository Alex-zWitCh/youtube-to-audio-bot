import os
import tempfile
import unittest


class RatingStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        os.environ["YT_AUDIO_DB"] = os.path.join(self.temp_dir.name, "ratings.db")
        os.environ["YT_AUDIO_BOT_TOKEN"] = "test-token"
        import bot

        self.bot = bot
        self.bot.DB_PATH = os.environ["YT_AUDIO_DB"]
        self.bot.init_db()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_rating_is_updated_and_personal(self):
        self.bot.save_rating(1, "abcdefghijk", 3, "First", "Author", "https://example.test/1")
        self.bot.save_rating(1, "abcdefghijk", 5, "First updated", "Author", "https://example.test/1")
        self.bot.save_rating(2, "abcdefghijk", 1, "First", "Author", "https://example.test/1")

        first_user = self.bot.get_rated_audio(1)
        second_user = self.bot.get_rated_audio(2)

        self.assertEqual(len(first_user), 1)
        self.assertEqual(first_user[0]["rating"], 5)
        self.assertEqual(first_user[0]["title"], "First updated")
        self.assertEqual(second_user[0]["rating"], 1)

    def test_ratings_are_sorted_descending(self):
        self.bot.save_rating(1, "aaaaaaaaaaa", 2, "Low", "", "")
        self.bot.save_rating(1, "bbbbbbbbbbb", 5, "High", "", "")
        self.bot.save_rating(1, "ccccccccccc", 4, "Middle", "", "")

        ratings = self.bot.get_rated_audio(1)
        self.assertEqual([item["rating"] for item in ratings], [5, 4, 2])

    def test_rating_can_be_removed(self):
        self.bot.save_rating(1, "abcdefghijk", 4, "Rated", "Author", "")
        self.assertEqual(self.bot.get_rating(1, "abcdefghijk"), 4)

        self.bot.delete_rating(1, "abcdefghijk")

        self.assertIsNone(self.bot.get_rating(1, "abcdefghijk"))
        self.assertEqual(self.bot.get_rated_audio(1), [])

    def test_rating_keyboard_contains_all_choices(self):
        keyboard = self.bot.rating_keyboard("abcdefghijk", selected=4)
        buttons = keyboard.inline_keyboard[0]
        self.assertEqual([button.callback_data for button in buttons], [
            "rate:abcdefghijk:1", "rate:abcdefghijk:2", "rate:abcdefghijk:3",
            "rate:abcdefghijk:4", "rate:abcdefghijk:5",
        ])
        self.assertEqual(buttons[3].text, "✓ 4")


if __name__ == "__main__":
    unittest.main()
