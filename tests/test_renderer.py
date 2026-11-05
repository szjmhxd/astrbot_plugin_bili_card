"""渲染层测试。

重点验证那些"看起来能用但实际会翻车"的取舍：
头像型内容不能用 16:9 裁切、时长要挪到封面角标、统计列数不能留空位。
"""

import unittest

from core.models import CardData, Stat
from core.renderer import build_context, plain_text


def make_video() -> CardData:
    return CardData(
        kind="video",
        title="测试视频标题",
        subtitle="P1/3 · 第一集",
        cover="data:image/jpeg;base64,AAA",
        avatar="data:image/jpeg;base64,BBB",
        author="某个UP主",
        desc="这是一段简介",
        stats=[Stat("播放", "12.3万"), Stat("点赞", "4567")],
        tags=["动画", "12:34", "原创"],
        link="https://www.bilibili.com/video/BV1xx411c7mD",
        link_label="BV1xx411c7mD",
    )


class TestHero(unittest.TestCase):
    def test_video_uses_cover(self):
        ctx = build_context(make_video())
        self.assertEqual(ctx["hero"], "cover")

    def test_space_uses_avatar_hero(self):
        card = CardData(kind="space", title="某UP", cover="data:image/jpeg;base64,AAA")
        ctx = build_context(card)
        self.assertEqual(ctx["hero"], "avatar")

    def test_comment_has_no_hero(self):
        card = CardData(kind="comment", title="视频", cover="data:image/jpeg;base64,AAA")
        ctx = build_context(card)
        self.assertEqual(ctx["hero"], "none")

    def test_comment_avatar_reuses_cover(self):
        # 评论区只拿得到评论者头像，正文作者行必须复用它，否则只剩色块
        card = CardData(kind="comment", title="视频", cover="data:image/jpeg;base64,AAA")
        ctx = build_context(card)
        self.assertEqual(ctx["avatar"], "data:image/jpeg;base64,AAA")

    def test_no_cover_falls_back_to_none(self):
        card = CardData(kind="video", title="无封面")
        self.assertEqual(build_context(card)["hero"], "none")

    def test_show_cover_off(self):
        ctx = build_context(make_video(), show_cover=False)
        self.assertEqual(ctx["hero"], "none")


class TestChips(unittest.TestCase):
    def test_duration_moved_to_chip(self):
        ctx = build_context(make_video())
        self.assertEqual(ctx["cover_chip"], "12:34")
        self.assertNotIn("12:34", ctx["tags"])

    def test_long_duration_format(self):
        card = make_video()
        card.tags = ["1:02:33"]
        self.assertEqual(build_context(card)["cover_chip"], "1:02:33")

    def test_non_duration_tag_stays(self):
        card = make_video()
        card.tags = ["动画"]
        ctx = build_context(card)
        self.assertEqual(ctx["cover_chip"], "")
        self.assertEqual(ctx["tags"], ["动画"])

    def test_live_status_becomes_chip_when_cover_exists(self):
        card = CardData(
            kind="live",
            title="直播中",
            cover="data:image/jpeg;base64,AAA",
            highlight="● 直播中",
        )
        ctx = build_context(card)
        self.assertEqual(ctx["cover_chip"], "直播中")
        self.assertTrue(ctx["chip_live"])
        # 有角标就不再在作者行重复显示
        self.assertEqual(ctx["highlight"], "")

    def test_live_status_stays_inline_without_cover(self):
        card = CardData(kind="live", title="未开播", highlight="○ 未开播")
        ctx = build_context(card)
        self.assertEqual(ctx["highlight"], "未开播")
        self.assertEqual(ctx["cover_chip"], "")


class TestStats(unittest.TestCase):
    def test_two_stats_two_cols(self):
        self.assertEqual(build_context(make_video())["stat_cols"], 2)

    def test_six_stats_three_cols(self):
        card = make_video()
        card.stats = [Stat(f"k{i}", str(i)) for i in range(6)]
        self.assertEqual(build_context(card)["stat_cols"], 3)

    def test_four_stats_use_two_cols(self):
        # 四格用三列会剩一行两个空格子，必须退成两列两行
        card = make_video()
        card.stats = [Stat(f"k{i}", str(i)) for i in range(4)]
        self.assertEqual(build_context(card)["stat_cols"], 2)

    def test_five_stats_use_three_cols(self):
        card = make_video()
        card.stats = [Stat(f"k{i}", str(i)) for i in range(5)]
        self.assertEqual(build_context(card)["stat_cols"], 3)

    def test_three_stats_three_cols(self):
        card = make_video()
        card.stats = [Stat(f"k{i}", str(i)) for i in range(3)]
        self.assertEqual(build_context(card)["stat_cols"], 3)

    def test_no_stats_cols_is_one(self):
        card = make_video()
        card.stats = []
        ctx = build_context(card)
        self.assertEqual(ctx["stats"], [])
        self.assertEqual(ctx["stat_cols"], 1)

    def test_show_stats_off(self):
        self.assertEqual(build_context(make_video(), show_stats=False)["stats"], [])


class TestText(unittest.TestCase):
    def test_desc_truncated(self):
        card = make_video()
        card.desc = "字" * 300
        ctx = build_context(card, desc_max_chars=20)
        self.assertEqual(len(ctx["desc"]), 20)
        self.assertTrue(ctx["desc"].endswith("…"))

    def test_desc_kept_when_short(self):
        ctx = build_context(make_video(), desc_max_chars=96)
        self.assertEqual(ctx["desc"], "这是一段简介")

    def test_show_desc_off(self):
        self.assertEqual(build_context(make_video(), show_desc=False)["desc"], "")

    def test_long_title_truncated(self):
        card = make_video()
        card.title = "标" * 300
        ctx = build_context(card)
        self.assertLessEqual(len(ctx["title"]), 120)

    def test_empty_title_placeholder(self):
        card = CardData(kind="video", title="")
        self.assertEqual(build_context(card)["title"], "（无标题）")

    def test_theme_dark(self):
        self.assertEqual(build_context(make_video(), theme="dark")["theme"], "dark")

    def test_theme_fallback(self):
        self.assertEqual(build_context(make_video(), theme="weird")["theme"], "light")


class TestPlainText(unittest.TestCase):
    def test_contains_key_fields(self):
        text = plain_text(make_video())
        self.assertIn("测试视频标题", text)
        self.assertIn("某个UP主", text)
        self.assertIn("BV1xx411c7mD", text)
        self.assertIn("【视频】", text)

    def test_contains_link(self):
        self.assertIn("https://www.bilibili.com/video/BV1xx411c7mD", plain_text(make_video()))

    def test_minimal_card(self):
        text = plain_text(CardData(kind="video", title="只有标题"))
        self.assertIn("只有标题", text)


if __name__ == "__main__":
    unittest.main()
