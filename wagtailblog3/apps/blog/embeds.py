# wagtailblog3/apps/blog/embeds.py
import re
from wagtail.embeds.finders.base import EmbedFinder

# ==========================================
# 1. Bilibili (B站)
# ==========================================
class BilibiliFinder(EmbedFinder):
    pattern = re.compile(r'bilibili\.com/video/(BV[a-zA-Z0-9]+)')
    def accept(self, url): return bool(self.pattern.search(url))
    def find_embed(self, url, max_width=None):
        bvid = self.pattern.search(url).group(1)
        html = f"""
        <div class="video-embed-wrapper responsive-16by9">
            <iframe src="//player.bilibili.com/player.html?bvid={bvid}&high_quality=1&danmaku=0"
                    scrolling="no" border="0" frameborder="no" framespacing="0" allowfullscreen="true">
            </iframe>
        </div>
        """
        # 修复点：增加 width, height, thumbnail_url
        return {'title': 'Bilibili Video', 'provider_name': 'Bilibili', 'type': 'video', 'width': 1000, 'height': 562, 'thumbnail_url': '', 'html': html}

# ==========================================
# 2. 腾讯视频 (Tencent Video)
# ==========================================
class TencentVideoFinder(EmbedFinder):
    pattern = re.compile(r'qq\.com/x/(?:cover/[^/]+/|page/)([a-zA-Z0-9]+)\.html')
    def accept(self, url): return bool(self.pattern.search(url))
    def find_embed(self, url, max_width=None):
        vid = self.pattern.search(url).group(1)
        html = f"""
        <div class="video-embed-wrapper responsive-16by9">
            <iframe src="https://v.qq.com/txp/iframe/player.html?vid={vid}" allowFullScreen="true" frameborder="0"></iframe>
        </div>
        """
        # 修复点：增加 width, height, thumbnail_url
        return {'title': 'Tencent Video', 'provider_name': 'Tencent', 'type': 'video', 'width': 1000, 'height': 562, 'thumbnail_url': '', 'html': html}

# ==========================================
# 3. 优酷视频 (Youku)
# ==========================================
class YoukuFinder(EmbedFinder):
    pattern = re.compile(r'youku\.com/v_show/id_([a-zA-Z0-9=]+)')
    def accept(self, url): return bool(self.pattern.search(url))
    def find_embed(self, url, max_width=None):
        vid = self.pattern.search(url).group(1)
        html = f"""
        <div class="video-embed-wrapper responsive-16by9">
            <iframe src="https://player.youku.com/embed/{vid}" allowfullscreen="true" frameborder="0"></iframe>
        </div>
        """
        # 修复点：增加 width, height, thumbnail_url
        return {'title': 'Youku Video', 'provider_name': 'Youku', 'type': 'video', 'width': 1000, 'height': 562, 'thumbnail_url': '', 'html': html}

# ==========================================
# 4. 网易云音乐 (NetEase Cloud Music)
# ==========================================
class NetEaseMusicFinder(EmbedFinder):
    pattern = re.compile(r'music\.163\.com/(?:#/)?song\?id=(\d+)')
    def accept(self, url): return bool(self.pattern.search(url))
    def find_embed(self, url, max_width=None):
        song_id = self.pattern.search(url).group(1)
        html = f"""
        <div class="audio-embed-wrapper shadow-sm mt-3 mb-3">
            <iframe frameborder="no" border="0" marginwidth="0" marginheight="0" width="100%" height="66"
                    src="//music.163.com/outchain/player?type=2&id={song_id}&auto=0&height=66">
            </iframe>
        </div>
        """
        # 修复点：增加 width, height
        return {'title': 'NetEase Music', 'provider_name': 'NetEase', 'type': 'rich', 'width': 1000, 'height': 66, 'html': html}

# ==========================================
# 5. QQ音乐 (QQ Music)
# ==========================================
class QQMusicFinder(EmbedFinder):
    pattern = re.compile(r'y\.qq\.com/n/ryqq/songDetail/([a-zA-Z0-9]+)')
    def accept(self, url): return bool(self.pattern.search(url))
    def find_embed(self, url, max_width=None):
        songmid = self.pattern.search(url).group(1)
        html = f"""
        <div class="audio-embed-wrapper shadow-sm mt-3 mb-3">
            <iframe frameborder="no" border="0" marginwidth="0" marginheight="0" width="100%" height="86"
                    src="//i.y.qq.com/v8/playsong.html?songmid={songmid}&ADTAG=myqq&from=myqq&channel=10007100">
            </iframe>
        </div>
        """
        # 修复点：增加 width, height
        return {'title': 'QQ Music', 'provider_name': 'QQ Music', 'type': 'rich', 'width': 1000, 'height': 86, 'html': html}

# ==========================================
# 6. 酷狗音乐 (Kugou)
# ==========================================
class KugouMusicFinder(EmbedFinder):
    pattern = re.compile(r'kugou\.com/mixsong/([a-zA-Z0-9]+)\.html')
    def accept(self, url): return bool(self.pattern.search(url))
    def find_embed(self, url, max_width=None):
        song_hash = self.pattern.search(url).group(1)
        html = f"""
        <div class="audio-embed-wrapper shadow-sm mt-3 mb-3" style="height: 100px; overflow: hidden; position: relative;">
            <iframe frameborder="no" border="0" marginwidth="0" marginheight="0" width="100%" height="150"
                    src="https://www.kugou.com/mixsong/{song_hash}.html" style="position: absolute; top: -50px; left: 0;">
            </iframe>
        </div>
        """
        # 修复点：增加 width, height
        return {'title': 'Kugou Music', 'provider_name': 'Kugou', 'type': 'rich', 'width': 1000, 'height': 100, 'html': html}

# ==========================================
# 7. 咪咕音乐 (Migu)
# ==========================================
class MiguMusicFinder(EmbedFinder):
    pattern = re.compile(r'music\.migu\.cn/v3/music/song/([0-9]+)')
    def accept(self, url): return bool(self.pattern.search(url))
    def find_embed(self, url, max_width=None):
        song_id = self.pattern.search(url).group(1)
        html = f"""
        <div class="audio-embed-wrapper shadow-sm mt-3 mb-3" style="height: 120px; overflow: hidden; position: relative;">
            <iframe frameborder="no" border="0" marginwidth="0" marginheight="0" width="100%" height="300"
                    src="https://music.migu.cn/v3/music/song/{song_id}" style="position: absolute; top: -80px; left: 0;">
            </iframe>
        </div>
        """
        # 修复点：增加 width, height
        return {'title': 'Migu Music', 'provider_name': 'Migu', 'type': 'rich', 'width': 1000, 'height': 120, 'html': html}