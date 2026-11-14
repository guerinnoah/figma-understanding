import unittest

from figma_understanding.source import FigmaSourceError, parse_figma_url


class SourceTests(unittest.TestCase):
    def test_parses_supported_url_and_records_selection_without_narrowing_scope(self):
        source = parse_figma_url("https://www.figma.com/design/Abc_def-123/File-name?node-id=12-34&t=ignored")
        self.assertEqual("Abc_def-123", source.file_key)
        self.assertEqual("12-34", source.selected_node_id)

    def test_rejects_non_figma_credentials_versions_and_branches(self):
        values = [
            "https://example.com/design/Abcdef/File",
            "https://user@www.figma.com/design/Abcdef/File",
            "https://www.figma.com/design/Abcdef/File?version-id=99",
            "https://www.figma.com/design/Abcdef/branch/BranchKey/File",
            "https://www.figma.com:invalid/design/Abcdef/File",
        ]
        for value in values:
            with self.subTest(value=value), self.assertRaises(FigmaSourceError):
                parse_figma_url(value)

    def test_rejects_non_file_routes(self):
        with self.assertRaises(FigmaSourceError):
            parse_figma_url("https://www.figma.com/community/file/123456")


if __name__ == "__main__":
    unittest.main()

