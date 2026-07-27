from django.test import SimpleTestCase

from comments.markdown import render_comment_markdown, render_reply_markdown


class CommentMarkdownTests(SimpleTestCase):
    def test_renders_gfm_style_table(self):
        rendered = render_comment_markdown(
            "| 名称 | 数量 |\n| --- | ---: |\n| 苹果 | 2 |"
        )

        self.assertIn("<table>", rendered)
        self.assertIn("<th>名称</th>", rendered)
        self.assertIn("<td>苹果</td>", rendered)

    def test_strips_unsafe_html_while_preserving_emoji(self):
        rendered = render_comment_markdown("开心 😄 <script>alert(1)</script>")

        self.assertIn("😄", rendered)
        self.assertNotIn("<script", rendered)

    def test_reply_renderer_removes_only_matching_leading_mention(self):
        rendered = render_reply_markdown("@root 怎么回事", "root")
        different_user = render_reply_markdown("@other 怎么回事", "root")
        longer_name = render_reply_markdown("@rooted 怎么回事", "root")

        self.assertEqual(rendered, "<p>怎么回事</p>\n")
        self.assertIn("@other 怎么回事", different_user)
        self.assertIn("@rooted 怎么回事", longer_name)
