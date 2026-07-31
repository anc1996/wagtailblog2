#!/user/bin/env python3
# -*- coding: utf-8 -*-
# wagtailblog3/apps/blog/embeds.py
import re
from wagtail.embeds.finders.base import EmbedFinder

# ==========================================
# 1. Bilibili (B站) 视频解析器
# ==========================================
class BilibiliFinder(EmbedFinder):
    pattern = re.compile(r'bilibili\.com/video/(BV[a-zA-Z0-9]+)')

    def accept(self, url):
        return bool(self.pattern.search(url))

    def find_embed(self, url, max_width=None):
        bvid = self.pattern.search(url).group(1)
        html = f"""
        <div class="video-embed-wrapper responsive-16by9">
            <iframe title="Bilibili 视频播放器"
                    src="//player.bilibili.com/player.html?bvid={bvid}&high_quality=1&danmaku=0"
                    scrolling="no" border="0" frameborder="no" framespacing="0" allowfullscreen="true">
            </iframe>
        </div>
        """
        return {'title': 'Bilibili Video', 'provider_name': 'Bilibili', 'type': 'video', 'width': 1000, 'height': 562, 'thumbnail_url': '', 'html': html}

# ==========================================
# 2. 腾讯视频 (Tencent Video) 解析器
# ==========================================
class TencentVideoFinder(EmbedFinder):
    pattern = re.compile(r'qq\.com/x/(?:cover/[^/]+/|page/)([a-zA-Z0-9]+)\.html')

    def accept(self, url):
        return bool(self.pattern.search(url))

    def find_embed(self, url, max_width=None):
        vid = self.pattern.search(url).group(1)
        html = f"""
        <div class="video-embed-wrapper responsive-16by9">
            <iframe title="腾讯视频播放器"
                    src="https://v.qq.com/txp/iframe/player.html?vid={vid}"
                    allowFullScreen="true" frameborder="0">
            </iframe>
        </div>
        """
        return {'title': 'Tencent Video', 'provider_name': 'Tencent', 'type': 'video', 'width': 1000, 'height': 562, 'thumbnail_url': '', 'html': html}

# ==========================================
# 3. 优酷视频 (Youku) 解析器
# ==========================================
class YoukuFinder(EmbedFinder):
    pattern = re.compile(r'youku\.com/v_show/id_([a-zA-Z0-9=]+)')

    def accept(self, url):
        return bool(self.pattern.search(url))

    def find_embed(self, url, max_width=None):
        vid = self.pattern.search(url).group(1)
        html = f"""
        <div class="video-embed-wrapper responsive-16by9">
            <iframe title="优酷视频播放器"
                    src="https://player.youku.com/embed/{vid}"
                    allowfullscreen="true" frameborder="0">
            </iframe>
        </div>
        """
        return {'title': 'Youku Video', 'provider_name': 'Youku', 'type': 'video', 'width': 1000, 'height': 562, 'thumbnail_url': '', 'html': html}

# ==========================================
# 4. 网易云音乐 (NetEase Cloud Music)
# ==========================================
class NetEaseMusicFinder(EmbedFinder):
    song_pattern = re.compile(r'music\.163\.com/(?:#/)?song\?id=(\d+)')
    outchain_pattern = re.compile(
        r'music\.163\.com/#/outchain/\d+/(\d+)/m/([^/?#]+)',
        re.IGNORECASE,
    )
    player_pattern = re.compile(
        r'music\.163\.com/outchain/player\?[^#]*?\bid=(\d+)',
        re.IGNORECASE,
    )

    def accept(self, url):
        return bool(
            self.song_pattern.search(url)
            or self.outchain_pattern.search(url)
            or self.player_pattern.search(url)
        )

    def _parse_url(self, url):
        match = self.outchain_pattern.search(url)
        if match:
            return match.group(1), match.group(2).lower() == 'use'

        for pattern in (self.song_pattern, self.player_pattern):
            match = pattern.search(url)
            if match:
                return match.group(1), False

        return None, False

    def find_embed(self, url, max_width=None):
        song_id, autoplay = self._parse_url(url)
        if not song_id:
            raise ValueError('网易云音乐链接缺少歌曲 ID')

        auto = '1' if autoplay else '0'
        html = f"""
        <div class="audio-embed-wrapper netease-music-wrapper">
            <iframe class="netease-music-player" title="网易云音乐播放器"
                    frameborder="no" border="0" marginwidth="0" marginheight="0"
                    width="330" height="86"
                    src="https://music.163.com/outchain/player?type=2&id={song_id}&auto={auto}&height=66">
            </iframe>
        </div>
        """
        return {
            'title': 'NetEase Cloud Music',
            'provider_name': '网易云音乐',
            'type': 'rich',
            'width': 330,
            'height': 86,
            'html': html,
        }

# ==========================================
# 5. QQ音乐 (QQ Music) 解析器
# ==========================================
class QQMusicFinder(EmbedFinder):
    pattern = re.compile(r'y\.qq\.com/n/ryqq/songDetail/([a-zA-Z0-9]+)')

    def accept(self, url):
        return bool(self.pattern.search(url))

    def find_embed(self, url, max_width=None):
        songmid = self.pattern.search(url).group(1)
        html = f"""
        <div class="audio-embed-wrapper shadow-sm mt-3 mb-3">
            <iframe title="QQ音乐播放器"
                    frameborder="no" border="0" marginwidth="0" marginheight="0" width="100%" height="86"
                    src="//i.y.qq.com/v8/playsong.html?songmid={songmid}&ADTAG=myqq&from=myqq&channel=10007100">
            </iframe>
        </div>
        """
        return {'title': 'QQ Music', 'provider_name': 'QQ Music', 'type': 'rich', 'width': 1000, 'height': 86, 'html': html}

# ==========================================
# 6. 酷狗音乐 (Kugou Music) 解析器
# ==========================================
class KugouMusicFinder(EmbedFinder):
    pattern = re.compile(r'kugou\.com/mixsong/([a-zA-Z0-9]+)\.html')

    def accept(self, url):
        return bool(self.pattern.search(url))

    def find_embed(self, url, max_width=None):
        song_hash = self.pattern.search(url).group(1)
        html = f"""
        <div class="audio-embed-wrapper shadow-sm mt-3 mb-3" style="height: 100px; overflow: hidden; position: relative;">
            <iframe title="酷狗音乐播放器"
                    frameborder="no" border="0" marginwidth="0" marginheight="0" width="100%" height="150"
                    src="https://www.kugou.com/mixsong/{song_hash}.html"
                    style="position: absolute; top: -50px; left: 0;">
            </iframe>
        </div>
        """
        return {'title': 'Kugou Music', 'provider_name': 'Kugou', 'type': 'rich', 'width': 1000, 'height': 100, 'html': html}

# ==========================================
# 7. 咪咕音乐 (Migu Music) 解析器
# ==========================================
class MiguMusicFinder(EmbedFinder):
    pattern = re.compile(r'music\.migu\.cn/v3/music/song/([0-9]+)')

    def accept(self, url):
        return bool(self.pattern.search(url))

    def find_embed(self, url, max_width=None):
        song_id = self.pattern.search(url).group(1)
        html = f"""
        <div class="audio-embed-wrapper shadow-sm mt-3 mb-3" style="height: 120px; overflow: hidden; position: relative;">
            <iframe title="咪咕音乐播放器"
                    frameborder="no" border="0" marginwidth="0" marginheight="0" width="100%" height="300"
                    src="https://music.migu.cn/v3/music/song/{song_id}"
                    style="position: absolute; top: -80px; left: 0;">
            </iframe>
        </div>
        """
        return {'title': 'Migu Music', 'provider_name': 'Migu', 'type': 'rich', 'width': 1000, 'height': 120, 'html': html}
