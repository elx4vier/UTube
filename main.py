import logging
import requests
import json
import os
import io
import re
import locale
from concurrent.futures import ThreadPoolExecutor, as_completed
from PIL import Image, ImageOps, ImageDraw
from ulauncher.api.client.Extension import Extension
from ulauncher.api.client.EventListener import EventListener
from ulauncher.api.shared.event import KeywordQueryEvent
from ulauncher.api.shared.item.ExtensionResultItem import ExtensionResultItem
from ulauncher.api.shared.item.ExtensionSmallResultItem import ExtensionSmallResultItem
from ulauncher.api.shared.action.RenderResultListAction import RenderResultListAction
from ulauncher.api.shared.action.OpenUrlAction import OpenUrlAction

logger = logging.getLogger(__name__)

class UTube(Extension):
    def __init__(self):
        super().__init__()
        self.subscribe(KeywordQueryEvent, KeywordQueryEventListener())
        self.cache_dir = os.path.join(os.path.expanduser("~"), '.cache', 'ulauncher-yt-speed')
        os.makedirs(self.cache_dir, exist_ok=True)
        
        self.cleanup_cache()
        self.translations = self.load_translations()
        # Executor otimizado
        self.executor = ThreadPoolExecutor(max_workers=min(4, os.cpu_count() or 1))
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.5"
        })

    def cleanup_cache(self, max_files=100):
        try:
            files = [os.path.join(self.cache_dir, f) for f in os.listdir(self.cache_dir) if f.endswith('.png')]
            if len(files) > max_files:
                files.sort(key=os.path.getmtime)
                for f in files[:-max_files]:
                    os.remove(f)
        except Exception as e:
            logger.error(f"Erro ao limpar cache: {e}")

    def load_translations(self):
        try:
            sys_lang = (locale.getdefaultlocale() or ['en'])[0]
            lang_code = sys_lang.split('_')[0].lower() if sys_lang else 'en'
        except Exception:
            lang_code = 'en'

        base_path = os.path.join(os.path.dirname(__file__), 'translations')
        lang_file = os.path.join(base_path, f"{lang_code}.json")
        fallback_file = os.path.join(base_path, "en.json")

        try:
            target = lang_file if os.path.exists(lang_file) else fallback_file
            with open(target, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}

    def i18n(self, key, default=""):
        return self.translations.get(key, default)

    def translate_date(self, date_str):
        if not date_str:
            return ""
        words = ["ago","hour","hours","day","days","week","weeks","month","months","year","years","minute","minutes"]
        text = date_str.lower()
        for w in words:
            tr = self.i18n(w, w)
            text = re.sub(r'\b' + re.escape(w) + r'\b', tr, text)
        return text

    def format_views(self, v):
        try:
            if not v: 
                return ""
            v = v.lower().replace(',', '.')
            num_match = re.search(r'(\d+[\d.]*)', v)
            if not num_match: 
                return ""
            num_float = float(num_match.group(1))

            suffix_billion = self.i18n('suffix_billion', 'bi')
            suffix_million = self.i18n('suffix_million', 'mi')
            suffix_thousand = self.i18n('suffix_thousand', ' mil')

            if 'bi' in v or 'b' in v:
                return f"{int(num_float)} {suffix_billion}"
            elif 'mi' in v or ('m' in v and 'mil' not in v and 'k' not in v):
                return f"{int(num_float)} {suffix_million}"
            elif 'mil' in v or 'k' in v:
                return f"{int(num_float)}{suffix_thousand}"
            return str(int(num_float))
        except:
            return ""

    def download_and_cache(self, path, url, is_channel):
        if os.path.exists(path): 
            return path
        try:
            r = self.session.get(url, timeout=3.0)
            r.raise_for_status()
            img = Image.open(io.BytesIO(r.content)).convert("RGBA")
            img = ImageOps.fit(img, (100, 100), Image.LANCZOS)
            mask = Image.new('L', (100, 100), 0)
            draw = ImageDraw.Draw(mask)
            if is_channel: 
                draw.ellipse((0, 0, 100, 100), fill=255)
            else: 
                draw.rounded_rectangle((0, 0, 100, 100), radius=12, fill=255)
            img.putalpha(mask)
            img.save(path, "PNG")
            return path
        except Exception:
            return None


class KeywordQueryEventListener(EventListener):
    def on_event(self, event, extension):
        query = (event.get_argument() or "").strip()
        icon_default = 'images/icon.png'

        if len(query) < 3:
            return RenderResultListAction([
                ExtensionResultItem(
                    icon=icon_default, 
                    name=extension.i18n("search_prompt", "Digite para pesquisar no YouTube"), 
                    on_enter=None
                )
            ])

        try:
            search_url = f"https://www.youtube.com/results?search_query={query.replace(' ', '+')}"
            try:
                r = extension.session.get(search_url, timeout=3.0)
                r.raise_for_status()
            except requests.exceptions.RequestException:
                return RenderResultListAction([
                    ExtensionResultItem(
                        icon=icon_default, 
                        name=extension.i18n("network_error", "Erro de conexão"), 
                        description=extension.i18n("search_error_desc", "Verifique sua internet e tente novamente"), 
                        on_enter=None
                    )
                ])
            
            try:
                split_data = r.text.split("var ytInitialData = ")[1].split(";</script>")[0]
                data = json.loads(split_data)
            except Exception:
                return RenderResultListAction([
                    ExtensionResultItem(
                        icon=icon_default, 
                        name=f"'{query}'", 
                        description=extension.i18n("search_browser_line2", "Pesquisar no YouTube via navegador"), 
                        on_enter=OpenUrlAction(search_url)
                    )
                ])

            contents = data.get('contents', {}).get('twoColumnSearchResultsRenderer', {})\
                           .get('primaryContents', {}).get('sectionListRenderer', {}).get('contents', [])

            pref_max = int(extension.preferences.get('max_results', 7))
            pref_thumb = extension.preferences.get('thumb_type', 'channel')
            pref_layout = extension.preferences.get('search_layout', 'layout_inverted')

            items = [
                ExtensionResultItem(
                    icon=icon_default, 
                    name=f"'{query}'", 
                    description=extension.i18n("search_browser_line2", "Pesquisar no navegador"), 
                    on_enter=OpenUrlAction(search_url)
                )
            ]
            
            videos = []
            for section in contents:
                item_list = section.get('itemSectionRenderer', {}).get('contents', [])
                for item in item_list:
                    v = item.get('videoRenderer')
                    if v:
                        videos.append(v)
                        if len(videos) >= pref_max: 
                            break
                if len(videos) >= pref_max: 
                    break

            thumb_futures = {}
            thumb_paths = {}

            for v in videos:
                v_id = v.get('videoId')
                is_ch = (pref_thumb == 'channel')
                chan_data = v.get('longBylineText', {}).get('runs', [{}])[0]
                chan_id = chan_data.get('navigationEndpoint', {}).get('browseEndpoint', {}).get('browseId', v_id)
                
                img_path = os.path.join(extension.cache_dir, f"{'c' if is_ch else 'v'}_{chan_id if is_ch else v_id}.png")

                if pref_thumb == "none":
                    thumb_paths[v_id] = icon_default
                elif os.path.exists(img_path):
                    thumb_paths[v_id] = img_path
                else:
                    t_url = v.get('channelThumbnailSupportedRenderers', {}).get('channelThumbnailWithLinkRenderer', {}).get('thumbnail', {}).get('thumbnails', [{}])[0].get('url') if is_ch else v.get('thumbnail', {}).get('thumbnails', [{}])[0].get('url')
                    if t_url:
                        if t_url.startswith('//'): 
                            t_url = 'https:' + t_url
                        thumb_futures[extension.executor.submit(extension.download_and_cache, img_path, t_url, is_ch)] = v_id

            for future in as_completed(thumb_futures):
                v_id = thumb_futures[future]
                res = future.result()
                thumb_paths[v_id] = res if res else icon_default

            for v in videos:
                v_id = v.get('videoId')
                title = v.get('title', {}).get('runs', [{}])[0].get('text', 'No title')
                chan = v.get('longBylineText', {}).get('runs', [{}])[0].get('text', 'Channel')
                dur = v.get('lengthText', {}).get('simpleText', 'LIVE')
                
                views = extension.format_views(v.get('shortViewCountText', {}).get('simpleText', ''))
                pub = extension.translate_date(v.get('publishedTimeText', {}).get('simpleText', ''))
                
                current_icon = thumb_paths.get(v_id, icon_default)
                link = OpenUrlAction(f"https://www.youtube.com/watch?v={v_id}")

                if pref_layout == 'layout_classic':
                    desc = f"{dur} • {chan}" + (f" • {pub}" if pub else "")
                    items.append(ExtensionResultItem(icon=current_icon, name=title, description=desc, on_enter=link))
                elif pref_layout == 'layout_inverted':
                    desc = f"{title} • {dur}\n{views}" + (f" • {pub}" if pub else "")
                    items.append(ExtensionResultItem(icon=current_icon, name=chan, description=desc, on_enter=link))
                elif pref_layout == 'layout_minimal':
                    items.append(ExtensionSmallResultItem(icon=current_icon, name=f"{chan} • {title}", on_enter=link))

            return RenderResultListAction(items)
            
        except Exception as e:
            logger.error(f"YouTube Speed Extension Error: {e}")
            return RenderResultListAction([
                ExtensionResultItem(
                    icon=icon_default, 
                    name=extension.i18n("search_error", "Ops! Algo deu errado na busca"), 
                    description=extension.i18n("search_error_desc", "Tente novamente ou verifique sua conexão"), 
                    on_enter=None
                )
            ])


if __name__ == "__main__":
    UTube().run()
