"""Growth OS Phase 7 (2026-08-04): AI Video Engine -- turns today's real
free signals (api/market_pulse.py's _compute_free_signals(), the exact
same data free-signals.html/the Telegram push/the email digest already
use) into a short narrated video for distribution channels where a text
post gets no traction but a short video does.

Honesty note on scope (matches this codebase's standing anti-
fabrication rule -- see e.g. stress-lab.html's Monte Carlo fix,
services/widget_service.py's "real signal-strength heatmap, not a
fabricated price-change one"): this renders REAL K-line data (last ~20
daily bars fetched via services/technical_analysis_service.py, the same
OHLC source every chart page already uses) as static candlestick slide
images, one per featured signal, each narrated by real TTS
(services/tts_service.py) and timed to match its own narration clip's
actual duration. It is a data-slide-plus-narration video, not a fully
animated chart-drawing sequence -- frame-by-frame chart animation would
require a much larger rendering pipeline than this scope justifies, and
this docstring says so plainly rather than overselling it.

Pipeline (all via ffmpeg subprocess calls + Pillow-rendered PNG slides):
  1. Build a short script per slide (intro + up to 3 signals + outro).
     Tries an AI rewrite first (_ai_rewrite_narration(), via the same
     ai/ai_router.py used site-wide for chat/analysis) so the narration
     reads like a professional summary instead of a filled-in template;
     falls back to the honest, always-available _SCRIPT templates below
     if the AI call fails, times out, or returns the wrong shape -- this
     is a best-effort quality upgrade, never a hard dependency.
  2. Wrap each line in simple SSML (_build_ssml(): short pauses after
     clause punctuation) -- Google Cloud TTS parses SSML natively, no
     extra dependency. Deliberately limited to <break> only (see
     _build_ssml()'s own docstring): Neural2 voices reject <emphasis>.
  3. TTS each slide's line separately (services/tts_service.py) so each
     slide can be timed to its own narration clip's real duration.
  4. Render each slide as a PNG (size depends on the chosen aspect
     ratio): XFINLAB branding, ticker, direction, confidence, a real
     candlestick strip, and (on signal slides) a burned-in caption of
     the actual narration line, for silent/muted autoplay feeds.
  5. ffmpeg concat-demuxer the images (each held for its matching
     narration duration) into a silent video.
  6. ffmpeg filter_complex-concat the narration clips into one audio
     track, then mux it onto the silent video.

Storage: Railway's filesystem is NOT persisted across deploys/restarts
(litestream.yml only replicates xfinlab.db, never arbitrary files) --
this writes only ONE rolling file (generated_videos/daily_video_latest
.mp4), regenerated on demand, never treated as a permanent archive.
That's an accepted tradeoff for a daily marketing asset, not a defect;
if a durable archive is ever wanted, that's a separate follow-up
(uploading to real object storage), not something to fake here.

is_available() gates on BOTH services.tts_service.is_available() (needs
GOOGLE_TTS_API_KEY) AND the `ffmpeg`/`ffprobe` binaries actually being on
PATH (added via nixpacks.toml's aptPkgs for the Railway build) -- if
either is missing, every entrypoint here returns {"available": False,
"message": ...} instead of raising, so a misconfigured deploy degrades
this ONE optional feature instead of crashing anything else.
"""
import os
import re
import shutil
import subprocess
import tempfile
from datetime import date, datetime, timezone
from typing import List, Optional

from PIL import Image, ImageDraw, ImageFont

from services import tts_service

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "xfinlab.db")
_OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "generated_videos")
_OUTPUT_FILENAME = "daily_video_latest.mp4"

# 2026-08-04: aspect ratio options. "9:16" (vertical) is the original
# Shorts/Reels/TikTok shape; "1:1" (square) suits an Instagram feed
# post; "16:9" (landscape) suits YouTube/X. All layout math in
# _render_slide() below is proportional to whichever (width, height)
# is passed in, not hardcoded to one shape.
_ASPECT_RATIOS = {
    "9:16": (1080, 1920),
    "1:1": (1080, 1080),
    "16:9": (1920, 1080),
}
_DEFAULT_ASPECT = "9:16"

# 2026-08-04: visual themes. "dark" is the original look; "light" is a
# minimal/bright alternative for contexts where a dark video looks out
# of place (e.g. embedded on a light-themed page). "fg" = foreground
# text color (kept a distinct name from the old "white" constant since
# it's dark text in the light theme).
_THEMES = {
    "dark": {
        "bg": (8, 12, 20), "accent": (0, 212, 255), "green": (34, 197, 94),
        "red": (239, 68, 68), "muted": (148, 163, 184), "fg": (226, 232, 240),
    },
    "light": {
        "bg": (248, 250, 252), "accent": (37, 99, 235), "green": (22, 163, 74),
        "red": (220, 38, 38), "muted": (100, 116, 139), "fg": (15, 23, 42),
    },
}
_DEFAULT_THEME = "dark"

# 2026-08-04 expansion (user request: more narration languages): grew
# from zh-HK/en to 7 languages. Kept deliberately narrower than the
# 47-language site-wide i18n convention (same precedent as
# content_repurpose_service.py's EN/ES-only social fan-out) -- these are
# the languages services/tts_service.py has a real Google voice for.
# Every entry here is used as (a) the honest fallback if the AI rewrite
# below fails, and (b) the prompt-language hint for the AI rewrite.
_SCRIPT = {
    "zh-HK": {
        "intro": "XFINLAB 今日AI市場速覽。",
        "signal": "{ticker}，{direction}，信心度 {confidence} 巴仙。",
        "outro": "以上為技術面參考，並非投資建議。想睇更多，去 xfinlab.com。",
        "direction_label": {"bullish": "偏多", "bearish": "偏空", "neutral": "中性"},
        "footer": "技術面參考，並非投資建議",
    },
    "zh-CN": {
        "intro": "XFINLAB 今日AI市场速览。",
        "signal": "{ticker}，{direction}，信心度 {confidence} 百分比。",
        "outro": "以上为技术面参考，并非投资建议。想了解更多，请访问 xfinlab.com。",
        "direction_label": {"bullish": "偏多", "bearish": "偏空", "neutral": "中性"},
        "footer": "技术面参考，并非投资建议",
    },
    "zh-TW": {
        "intro": "XFINLAB 今日AI市場速覽。",
        "signal": "{ticker}，{direction}，信心度 {confidence} 百分比。",
        "outro": "以上為技術面參考，並非投資建議。想了解更多，請造訪 xfinlab.com。",
        "direction_label": {"bullish": "偏多", "bearish": "偏空", "neutral": "中性"},
        "footer": "技術面參考，並非投資建議",
    },
    "en": {
        "intro": "XFINLAB's daily AI market snapshot.",
        "signal": "{ticker}: {direction}, {confidence} percent confidence.",
        "outro": "This is technical reference only, not investment advice. More at xfinlab.com.",
        "direction_label": {"bullish": "bullish", "bearish": "bearish", "neutral": "neutral"},
        "footer": "Technical reference only, not investment advice",
    },
    "es": {
        "intro": "El resumen diario del mercado con IA de XFINLAB.",
        "signal": "{ticker}: {direction}, {confidence} por ciento de confianza.",
        "outro": "Esto es solo referencia técnica, no es asesoría de inversión. Más en xfinlab.com.",
        "direction_label": {"bullish": "alcista", "bearish": "bajista", "neutral": "neutral"},
        "footer": "Solo referencia técnica, no es asesoría de inversión",
    },
    "ja": {
        "intro": "XFINLABの本日のAI市場スナップショットです。",
        "signal": "{ticker}、{direction}、信頼度 {confidence} パーセント。",
        "outro": "これは技術的参考情報であり、投資助言ではありません。詳細は xfinlab.com へ。",
        "direction_label": {"bullish": "強気", "bearish": "弱気", "neutral": "中立"},
        "footer": "技術的参考情報であり、投資助言ではありません",
    },
    "ko": {
        "intro": "XFINLAB의 오늘의 AI 시장 스냅샷입니다.",
        "signal": "{ticker}, {direction}, 신뢰도 {confidence} 퍼센트.",
        "outro": "이것은 기술적 참고 자료일 뿐이며 투자 조언이 아닙니다. 자세한 내용은 xfinlab.com에서 확인하세요.",
        "direction_label": {"bullish": "강세", "bearish": "약세", "neutral": "중립"},
        "footer": "기술적 참고 자료이며 투자 조언이 아닙니다",
    },
    # 2026-08-04 second expansion (user request: pt/fr/de/hi/id/ar/ru/bn/ur).
    # Real Google TTS voices for all 9 confirmed in services/tts_service.py's
    # _VOICE_MAP (see that file's comment for the per-language verification
    # depth) -- these narration scripts only exist for languages that
    # actually have a working voice behind them.
    "pt": {
        "intro": "Resumo diário do mercado com IA da XFINLAB.",
        "signal": "{ticker}: {direction}, {confidence} por cento de confiança.",
        "outro": "Isto é apenas referência técnica, não é aconselhamento de investimento. Mais em xfinlab.com.",
        "direction_label": {"bullish": "otimista", "bearish": "pessimista", "neutral": "neutro"},
        "footer": "Apenas referência técnica, não é aconselhamento de investimento",
    },
    "fr": {
        "intro": "Le résumé quotidien du marché par l'IA de XFINLAB.",
        "signal": "{ticker} : {direction}, {confidence} pour cent de confiance.",
        "outro": "Ceci est uniquement une référence technique, pas un conseil en investissement. Plus sur xfinlab.com.",
        "direction_label": {"bullish": "haussier", "bearish": "baissier", "neutral": "neutre"},
        "footer": "Référence technique uniquement, pas un conseil en investissement",
    },
    "de": {
        "intro": "Der tägliche KI-Marktüberblick von XFINLAB.",
        "signal": "{ticker}: {direction}, {confidence} Prozent Konfidenz.",
        "outro": "Dies ist nur eine technische Referenz, keine Anlageberatung. Mehr auf xfinlab.com.",
        "direction_label": {"bullish": "bullisch", "bearish": "bärisch", "neutral": "neutral"},
        "footer": "Nur technische Referenz, keine Anlageberatung",
    },
    "hi": {
        "intro": "XFINLAB का आज का एआई मार्केट स्नैपशॉट।",
        "signal": "{ticker}: {direction}, {confidence} प्रतिशत विश्वास।",
        "outro": "यह केवल तकनीकी संदर्भ है, निवेश सलाह नहीं। अधिक जानकारी के लिए xfinlab.com पर जाएं।",
        "direction_label": {"bullish": "तेजी", "bearish": "मंदी", "neutral": "तटस्थ"},
        "footer": "केवल तकनीकी संदर्भ, निवेश सलाह नहीं",
    },
    "id": {
        "intro": "Ringkasan pasar harian berbasis AI dari XFINLAB.",
        "signal": "{ticker}: {direction}, tingkat keyakinan {confidence} persen.",
        "outro": "Ini hanya referensi teknis, bukan saran investasi. Info lebih lanjut di xfinlab.com.",
        "direction_label": {"bullish": "cenderung naik", "bearish": "cenderung turun", "neutral": "netral"},
        "footer": "Hanya referensi teknis, bukan saran investasi",
    },
    "ar": {
        "intro": "الملخص اليومي لسوق XFINLAB المدعوم بالذكاء الاصطناعي.",
        "signal": "{ticker}: {direction}، بثقة {confidence} بالمئة.",
        "outro": "هذا مرجع فني فقط، وليس نصيحة استثمارية. لمزيد من المعلومات، تفضل بزيارة xfinlab.com.",
        "direction_label": {"bullish": "صاعد", "bearish": "هابط", "neutral": "محايد"},
        "footer": "مرجع فني فقط، وليس نصيحة استثمارية",
    },
    "ru": {
        "intro": "Ежедневный обзор рынка от XFINLAB на основе ИИ.",
        "signal": "{ticker}: {direction}, уверенность {confidence} процентов.",
        "outro": "Это только техническая справка, а не инвестиционный совет. Подробнее на xfinlab.com.",
        "direction_label": {"bullish": "растущий", "bearish": "падающий", "neutral": "нейтральный"},
        "footer": "Только техническая справка, не инвестиционный совет",
    },
    "bn": {
        "intro": "XFINLAB-এর আজকের এআই মার্কেট স্ন্যাপশট।",
        "signal": "{ticker}: {direction}, আত্মবিশ্বাস {confidence} শতাংশ।",
        "outro": "এটি শুধুমাত্র প্রযুক্তিগত তথ্য, বিনিয়োগের পরামর্শ নয়। আরও জানতে xfinlab.com দেখুন।",
        "direction_label": {"bullish": "ঊর্ধ্বমুখী", "bearish": "নিম্নমুখী", "neutral": "নিরপেক্ষ"},
        "footer": "শুধুমাত্র প্রযুক্তিগত তথ্য, বিনিয়োগের পরামর্শ নয়",
    },
    "ur": {
        "intro": "XFINLAB کا آج کا اے آئی مارکیٹ خلاصہ۔",
        "signal": "{ticker}: {direction}, {confidence} فیصد اعتماد۔",
        "outro": "یہ صرف تکنیکی حوالہ ہے، سرمایہ کاری کا مشورہ نہیں۔ مزید معلومات کے لیے xfinlab.com ملاحظہ کریں۔",
        "direction_label": {"bullish": "تیزی", "bearish": "مندی", "neutral": "غیر جانبدار"},
        "footer": "صرف تکنیکی حوالہ، سرمایہ کاری کا مشورہ نہیں",
    },
}

_AI_LANG_NAMES = {
    "zh-HK": "Cantonese (Hong Kong, Traditional Chinese written form, spoken-Cantonese phrasing)",
    "zh-CN": "Mandarin Chinese (Simplified characters, Mainland China)",
    "zh-TW": "Mandarin Chinese (Traditional characters, Taiwan)",
    "en": "English",
    "es": "Spanish",
    "ja": "Japanese",
    "ko": "Korean",
    "pt": "Portuguese (Brazilian)",
    "fr": "French",
    "de": "German",
    "hi": "Hindi",
    "id": "Indonesian",
    "ar": "Modern Standard Arabic",
    "ru": "Russian",
    "bn": "Bengali",
    "ur": "Urdu",
}

# 2026-08-04 fix (user-reported: video's on-screen direction word and
# sector/ticker label stayed Chinese regardless of the chosen narration
# language, e.g. a Japanese-language video still showing "偏多" and
# "能源板塊"). Root cause of the direction bug: api/market_pulse.py's
# _compute_free_signals() (the exact data this module renders) sets
# confluence_direction to the RAW Chinese literal from technical_
# analysis_service.py ("偏多"/"偏空"), never "bullish"/"bearish" -- but
# _render_slide()/generate_daily_video() below used to do
# `direction = signal["confluence_direction"].lower()` and then look
# that up in _SCRIPT[lang]["direction_label"], whose keys are the
# English words "bullish"/"bearish"/"neutral". "偏多".lower() is still
# "偏多" (no-op on CJK), so the lookup always missed and silently fell
# back to the untranslated raw Chinese via dict.get(direction, direction)
# -- on every language, not just Chinese. This table normalizes the raw
# Chinese (or already-English) value to the canonical bullish/bearish/
# neutral key _SCRIPT actually indexes by, so the lookup can succeed.
_DIRECTION_NORMALIZE = {
    "偏多": "bullish", "偏空": "bearish",
    "bullish": "bullish", "bearish": "bearish",
    "訊號分歧，中性": "neutral", "數據不足": "neutral", "neutral": "neutral",
}


def _normalize_direction(raw: Optional[str]) -> str:
    if not raw:
        return "neutral"
    return _DIRECTION_NORMALIZE.get(raw, raw.lower() if raw.isascii() else "neutral")


# Root cause of the sector/ticker label bug: signal["label"] comes from
# api/market_pulse.py's _PULSE_LABELS/_TICKER_LABELS dicts, which are
# Chinese-only (e.g. "XLE" -> "能源板塊") -- there's no English/other-
# language version at the source. Rather than inventing a second label
# dictionary to maintain, this reuses the EXACT SAME i18n keys services/
# telegram_push_service.py already established for this identical
# problem (its 2026-07-26 fix: "en/es Telegram channels were posting
# Chinese ticker names" -- see that file's _TICKER_LABEL_KEYS comment)
# -- those keys are real, human-translated across all 47 site languages
# in services/i18n.py, so every language this module supports gets a
# real translation, not a guess.
_LABEL_I18N_KEYS = {
    "標普500": "tl_spy500", "納指100": "tl_qqq100", "道瓊工業": "pulse3",
    "羅素2000小型股": "tl_iwm_smallcap", "科技板塊": "pulse5", "金融板塊": "pulse6",
    "能源板塊": "pulse7", "標普500期貨": "tl_es_futures", "原油期貨": "tl_cl_futures",
    "黃金期貨": "tl_gc_futures", "比特幣": "tl_btc", "以太幣": "tl_eth",
}
# services/i18n.py's zh-HK/zh-TW/zh-CN entries for these keys don't
# always exactly match market_pulse.py's raw literal (traditional vs
# simplified, wording drift over time) -- for the Chinese narration
# languages the raw label is already correct as-is, so skip the lookup
# entirely rather than risk swapping in a slightly different phrasing.
_ZH_LANGS = {"zh-HK", "zh-CN", "zh-TW", "zh"}


def _translate_label(raw_label: str, lang: str) -> str:
    if not raw_label or lang in _ZH_LANGS:
        return raw_label
    key = _LABEL_I18N_KEYS.get(raw_label)
    if not key:
        return raw_label
    try:
        from services.i18n import get_translations

        return get_translations(lang).get(key, raw_label)
    except Exception:
        return raw_label


def _get_db():
    import sqlite3

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _init_log_table():
    conn = _get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS video_generation_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            status TEXT NOT NULL,
            message TEXT,
            duration_sec REAL,
            slides_count INTEGER,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    conn.close()


_init_log_table()


def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def is_available() -> bool:
    return tts_service.is_available() and _ffmpeg_available()


def _log_generation(status: str, message: str = "", duration_sec: Optional[float] = None, slides_count: int = 0):
    conn = _get_db()
    conn.execute(
        "INSERT INTO video_generation_log (date, status, message, duration_sec, slides_count) VALUES (?, ?, ?, ?, ?)",
        (date.today().isoformat(), status, message, duration_sec, slides_count),
    )
    conn.commit()
    conn.close()


def get_status() -> dict:
    """Admin panel status: availability + the most recent generation
    attempt's outcome (success or failure, with the real error message
    -- never hidden) + the option lists the admin UI needs to populate
    its language/aspect-ratio/theme dropdowns from a single call."""
    conn = _get_db()
    row = conn.execute(
        "SELECT * FROM video_generation_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    return {
        "available": is_available(),
        "tts_configured": tts_service.is_available(),
        "ffmpeg_present": _ffmpeg_available(),
        "output_exists": os.path.exists(os.path.join(_OUTPUT_DIR, _OUTPUT_FILENAME)),
        "last_run": dict(row) if row else None,
        "languages": list(_SCRIPT.keys()),
        "aspect_ratios": list(_ASPECT_RATIOS.keys()),
        "themes": list(_THEMES.keys()),
    }


# 2026-08-04 fix: DejaVuSans (the only font this module used to try) has
# NO Chinese/Japanese/Korean glyphs at all -- every CJK character (5 of
# this module's 7 narration languages, plus the bilingual disclaimer
# footer on every slide regardless of language) was rendering as tofu
# boxes. nixpacks.toml now installs fonts-noto-cjk (Noto Sans CJK, one
# font file covering Simplified/Traditional Chinese + Japanese + Korean
# + Latin), tried first; DejaVu stays as a fallback for the Latin-only
# case if that package is ever missing, then Pillow's own bitmap font as
# the last resort so a font problem can never crash the render.
_FONT_CANDIDATES = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)

# 2026-08-04 second expansion: ar/ur (Arabic script) and hi/bn (Devanagari/
# Bengali script) have no glyphs at all in NotoSansCJK or DejaVuSans, so
# without this those 4 languages would render as tofu boxes -- the exact
# same bug class the 2026-08-04 CJK fix above already fixed once for
# Chinese/Japanese/Korean. nixpacks.toml's aptPkgs now also installs
# fonts-noto-core + fonts-noto-unhinted (broad Noto script coverage,
# including Arabic/Devanagari/Bengali) alongside the existing fonts-noto-
# cjk. Rather than hardcoding exact file paths for these packages (their
# internal file naming isn't something this codebase controls or can
# verify in advance), _scan_fonts() below does a one-time recursive scan
# of the actual font directories at runtime and matches by keyword in the
# filename -- resilient to whatever the real installed file names turn
# out to be, same "never invent, always verify against the real thing"
# posture as every other part of this module.
_SCRIPT_FONT_KEYWORDS = {
    "ar": ("Arabic",), "ur": ("Arabic",),
    "hi": ("Devanagari",), "bn": ("Bengali",),
}
_font_dir_index: Optional[List[str]] = None


def _scan_fonts() -> List[str]:
    global _font_dir_index
    if _font_dir_index is not None:
        return _font_dir_index
    index = []
    for base in ("/usr/share/fonts", "/usr/local/share/fonts"):
        if not os.path.isdir(base):
            continue
        for root, _dirs, files in os.walk(base):
            for fn in files:
                if fn.lower().endswith((".ttf", ".ttc", ".otf")):
                    index.append(os.path.join(root, fn))
    _font_dir_index = index
    return index


def _find_script_font(lang: str) -> Optional[str]:
    keywords = _SCRIPT_FONT_KEYWORDS.get(lang)
    if not keywords:
        return None
    matches = [p for p in _scan_fonts() if all(k.lower() in os.path.basename(p).lower() for k in keywords)]
    if not matches:
        return None
    bold = [p for p in matches if "bold" in os.path.basename(p).lower()]
    return sorted(bold or matches)[0]


def _resolve_font_path(lang: Optional[str] = None) -> Optional[str]:
    """2026-08-13: font-path resolution factored out of _get_font() below
    so the new ffmpeg drawtext-based marquee captions (_marquee_filters(),
    which need a real fontfile= path on disk, not a PIL ImageFont object)
    can reuse the exact same script-aware selection logic instead of
    duplicating it -- same candidates list, same CJK/Arabic/Devanagari/
    Bengali script-font lookup via _find_script_font()."""
    candidates = list(_FONT_CANDIDATES)
    script_font = _find_script_font(lang) if lang else None
    if script_font:
        candidates = [script_font] + candidates
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


def _get_font(size: int, lang: Optional[str] = None) -> ImageFont.FreeTypeFont:
    size = max(int(size), 8)
    path = _resolve_font_path(lang)
    if path:
        try:
            return ImageFont.truetype(path, size, index=0)
        except Exception:
            pass
    return ImageFont.load_default()


# 2026-08-04: real XFINLAB logo mark (the same asset img/logo-mark-512.png
# already used site-wide for favicons/nav branding, see task #335/#337 --
# not a new/invented asset), composited onto every slide next to the
# "XFINLAB" wordmark and, larger, on the outro end-screen. RGBA with a
# transparent background so it sits cleanly on both the dark and light
# slide themes. Cached per requested pixel size since the same handful of
# sizes repeat across every slide in a render.
_LOGO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "img", "logo-mark-512.png")
_logo_cache: dict = {}


def _get_logo(size: int) -> Optional[Image.Image]:
    size = max(int(size), 1)
    if size in _logo_cache:
        return _logo_cache[size]
    if not os.path.exists(_LOGO_PATH):
        _logo_cache[size] = None
        return None
    try:
        logo = Image.open(_LOGO_PATH).convert("RGBA").resize((size, size), Image.LANCZOS)
    except Exception:
        logo = None
    _logo_cache[size] = logo
    return logo


def _ai_rewrite_narration(signals: List[dict], lang: str) -> Optional[List[str]]:
    """Best-effort quality upgrade: ask the site's existing AI router
    (ai/ai_router.py, the same one chat.html/ai-analysis.html use) to
    rewrite the slide narration into natural, professional spoken copy
    instead of the fixed fill-in-the-blank _SCRIPT templates below.
    Returns None -- never a partial/malformed list -- on ANY failure
    (import error, API error, timeout, wrong line count), so the caller
    always has a clean signal to fall back to the honest template
    script. This is deliberately optional: is_available() and every
    other entrypoint in this module work identically whether or not
    this succeeds."""
    try:
        from ai.ai_router import get_ai_response
    except Exception:
        return None

    lang_name = _AI_LANG_NAMES.get(lang, "English")
    expected_lines = len(signals) + 2  # intro + one per signal + outro

    facts = []
    for s in signals:
        # 2026-08-04 fix: confluence_direction from _compute_free_signals()
        # is the raw Chinese literal ("偏多"/"偏空"), not the English word
        # -- normalize it so the AI prompt states the direction in
        # English (the language this prompt itself is written in),
        # rather than embedding a stray Chinese character the model would
        # have to guess the meaning of.
        direction = _normalize_direction(s.get("confluence_direction"))
        facts.append(f"- {s.get('ticker', '?')}: direction={direction}, confidence={s.get('confluence_confidence_pct', 0)}%")
    facts_block = "\n".join(facts)

    prompt = (
        f"You are writing a SHORT spoken video-narration script in {lang_name} for a daily "
        f"AI financial market signal video. Output EXACTLY {expected_lines} lines, one "
        f"sentence per line, no numbering, no markdown, no quotation marks:\n"
        f"Line 1: a 1-sentence intro mentioning XFINLAB and today's AI market snapshot.\n"
        f"Lines 2 through {expected_lines - 1}: one natural sentence per signal below, in a "
        f"professional financial-news tone (not a robotic template), stating the ticker, its "
        f"direction, and its confidence percentage.\n"
        f"Line {expected_lines}: a 1-sentence closing disclaimer that this is technical "
        f"reference only, not investment advice, mentioning xfinlab.com.\n\n"
        f"Signals:\n{facts_block}\n\n"
        f"Output ONLY the {expected_lines} lines of narration text, nothing else."
    )

    try:
        response = get_ai_response(prompt, max_tokens=400, reasoning_effort="low")
    except Exception:
        return None

    if not response:
        return None

    lines = [ln.strip(" \t\"'") for ln in response.strip().split("\n") if ln.strip()]
    if len(lines) != expected_lines:
        return None
    return lines


def _build_ssml(sentence: str) -> str:
    """Wraps a plain narration sentence in simple SSML: a short pause
    after clause-ending punctuation, for more natural pacing than one
    flat monotone run-on. Applied uniformly regardless of whether the
    sentence came from _ai_rewrite_narration() or the _SCRIPT template
    fallback.

    2026-08-04 fix #1 (user-reported: EN/ES/JA/KO video generation
    failing with "TTS API error (400): Invalid SSML. Newer voices like
    Neural2 require valid SSML."): this used to also wrap percentage
    figures in <emphasis level="moderate">. That turned out NOT to be
    the actual bug (Google's docs confirm Neural2 does support
    <emphasis>) -- removing it was a red herring that didn't fix the
    error, kept here only as dead-end history since the real bug (below)
    was hiding underneath it.

    2026-08-04 fix #2 (the REAL bug, found after fix #1 didn't resolve
    the user's repeat report): the punctuation->pause regex used to be
    `re.sub(..., r"\1<break time=\"250ms\"/>", ...)`. In a Python raw
    string, `\"` does NOT become a plain `"` -- Python's raw-string rule
    keeps the backslash AND the quote as two literal characters (the
    backslash only exists to stop the quote from closing the string
    literal). So that regex was actually inserting the literal text
    `<break time=\"250ms\"/>` -- with a real backslash character sitting
    right where the attribute value's opening quote should be -- into
    every single narration line, in every language. That's malformed
    XML (confirmed here by parsing the old output with
    xml.etree.ElementTree: "not well-formed (invalid token)"), which is
    exactly what Google's error message is complaining about. This only
    surfaced as a user-visible failure on Neural2 voices (en/es/ja/ko)
    because Google's error message itself says newer voices *require*
    valid SSML -- Standard/Wavenet (zh-HK/zh-CN/zh-TW) apparently parse
    more leniently and tolerated the malformed tag, which is why this
    bug shipped unnoticed and fix #1 (which didn't touch this line)
    didn't fix anything. Fixed by using single quotes for the attribute
    value (`time='250ms'`) instead of escaped double quotes -- no
    escaping needed inside a double-quoted raw string, so there's no
    slot for this exact mistake to recur. Verified the new output
    parses cleanly with ElementTree for both English and Chinese
    sample sentences."""
    escaped = sentence.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    escaped = re.sub(r"([，,、。.！!？?])", r"\1<break time='250ms'/>", escaped)
    return f"<speak>{escaped}</speak>"


def _text_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, fallback_per_char: float) -> float:
    """textbbox is the modern Pillow way to measure text width for manual
    centering/right-alignment (textsize was removed in recent Pillow) --
    wrapped so every call site below degrades to a rough estimate instead
    of crashing if a given font/text combination ever raises."""
    try:
        bbox = draw.textbbox((0, 0), text, font=font)
        return bbox[2] - bbox[0]
    except Exception:
        return len(text) * fallback_per_char


_CJK_CHAR_RE = re.compile(
    r"[一-鿿㐀-䶿豈-﫿぀-ヿ゠-ヿ가-힯]"
)


def _tokenize_breakable(text: str) -> List[str]:
    """2026-08-13 (AJ, follow-up after screenshot showed "方法進行股票技術
    性分析，找出五大共" STILL cut off past the frame edge even after the
    first _wrap_text_pixel fix): the first fix's word-splitting branch
    (triggered whenever the text contained ANY space -- which admin-
    generated narration always does, since AI-written intros mix a bare
    Latin ticker/acronym like "AAL" with Chinese prose around it) treated
    an entire run of CJK characters between two spaces as ONE atomic
    "word". If that whole run was wider than the frame -- routine for a
    full Chinese sentence -- it still got force-placed on a line
    (unconditionally, to avoid looping forever on an empty line), so it
    overflowed exactly like before.

    Fixes this at the tokenizing level instead of patching around it:
    splits `text` into small pieces such that ''.join(pieces) == text
    exactly, and it is ALWAYS safe to break a line between any two
    adjacent pieces -- CJK/Japanese/Korean characters are split one per
    piece (safe to wrap after any of them), while runs of Latin/digit/
    punctuation characters and runs of whitespace are each kept as a
    single piece (so "XFINLAB" or "AAL" never gets torn mid-word, and a
    run of spaces collapses into one breakable gap). Both
    _wrap_text_pixel() and _split_lines() below build on this so neither
    can reproduce this bug."""
    if not text:
        return []
    pieces: List[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            j = i
            while j < n and text[j].isspace():
                j += 1
            pieces.append(text[i:j])
            i = j
        elif _CJK_CHAR_RE.match(ch):
            pieces.append(ch)
            i += 1
        else:
            j = i
            while j < n and not text[j].isspace() and not _CJK_CHAR_RE.match(text[j]):
                j += 1
            pieces.append(text[i:j])
            i = j
    return pieces


def _wrap_text_pixel(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_w: float) -> List[str]:
    """2026-08-13 (AJ: "圖中字幕顯示穿出屏幕右邊，要分2行顯示" -- caption text
    was poking off the right edge of the frame): the intro/outro/custom
    slides used to wrap with textwrap.wrap(text, width=N), which counts
    N as a CHARACTER count, not a pixel width -- badly wrong for CJK,
    whose glyphs render roughly 2x as wide per character as the divisor
    assumed, so a "safe" N-char line overflowed the frame. This measures
    each candidate line's REAL rendered width with the actual font in
    use and only breaks once it would actually overflow max_w. Walks
    _tokenize_breakable() pieces rather than raw text.split(" ") words
    so a long unbroken CJK run next to a bare Latin token (e.g. "AAL
    分析...") is still breakable character-by-character instead of being
    force-placed as one oversized unbreakable chunk (see that function's
    docstring for the exact bug this replaced)."""
    text = (text or "").strip()
    if not text:
        return []

    fallback = font.size * 0.6 if hasattr(font, "size") else 20
    pieces = _tokenize_breakable(text)

    lines: List[str] = []
    current = ""
    for piece in pieces:
        if not current and piece.strip() == "":
            continue  # never start a wrapped line with a leading space
        candidate = current + piece
        if not current.strip() or _text_width(draw, candidate, font, fallback) <= max_w:
            current = candidate
        else:
            lines.append(current.strip())
            current = piece
    if current.strip():
        lines.append(current.strip())
    return [l for l in lines if l]


def _render_slide(kind: str, signal: Optional[dict], lang: str, caption_text: str,
                   width: int, height: int, colors: dict,
                   bg_image: Optional[Image.Image] = None) -> Image.Image:
    if bg_image is not None:
        # 2026-09-06 addition: AI-generated background photo (see
        # _generate_video_background() below) instead of a flat theme
        # color, for "custom" text slides only. A fixed dark overlay is
        # composited on top regardless of the photo's own colors/contrast
        # so the existing white/light text drawn below stays readable no
        # matter what the generated image looks like -- this can't be
        # skipped or made conditional without risking illegible slides.
        img = bg_image.convert("RGB")
        overlay = Image.new("RGBA", (width, height), (0, 0, 0, 135))
        img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
    else:
        img = Image.new("RGB", (width, height), colors["bg"])
    draw = ImageDraw.Draw(img)
    script = _SCRIPT.get(lang, _SCRIPT["en"])

    pad_x = int(width * 0.055)
    header_font_size = int(height * 0.033)
    sub_font_size = int(height * 0.017)
    header_y = int(height * 0.036)

    # Branding header, every slide: logo mark + "XFINLAB" wordmark at
    # top-left (2026-08-04: user asked for the logo icon to appear before
    # the wordmark, not just plain text), "xfinlab.com" at top-right so
    # it's visible even on a muted/no-sound autoplay view.
    icon_size = header_font_size + sub_font_size + 6
    logo_icon = _get_logo(icon_size)
    text_x = pad_x
    if logo_icon:
        img.paste(logo_icon, (pad_x, header_y), logo_icon)
        text_x = pad_x + icon_size + int(width * 0.018)
    draw.text((text_x, header_y), "XFINLAB", font=_get_font(header_font_size, lang), fill=colors["accent"])
    draw.text((text_x, header_y + header_font_size + 6), "AI Market Signal",
              font=_get_font(sub_font_size, lang), fill=colors["muted"])
    header_bottom = header_y + header_font_size + sub_font_size + int(height * 0.03)

    url_font = _get_font(sub_font_size, lang)
    url_w = _text_width(draw, "xfinlab.com", url_font, sub_font_size * 0.6)
    draw.text((width - pad_x - url_w, header_y + 2), "xfinlab.com", font=url_font, fill=colors["muted"])

    if kind == "intro":
        intro_font = _get_font(int(height * 0.032), lang)
        for i, line in enumerate(_wrap_text_pixel(draw, caption_text, intro_font, width - 2 * pad_x)):
            draw.text((pad_x, int(height * 0.42) + i * int(height * 0.037)), line,
                      font=intro_font, fill=colors["fg"])
    elif kind == "outro":
        outro_font = _get_font(int(height * 0.027), lang)
        for i, line in enumerate(_wrap_text_pixel(draw, caption_text, outro_font, width - 2 * pad_x)):
            draw.text((pad_x, int(height * 0.34) + i * int(height * 0.033)), line,
                      font=outro_font, fill=colors["fg"])

        # Prominent end-screen callout (2026-08-04: user asked for the
        # closing slide to show a big logo + xfinlab.com, not just the
        # small persistent header mark) -- centered, below the spoken
        # outro line above.
        end_logo_size = int(height * 0.16)
        end_logo = _get_logo(end_logo_size)
        end_y = int(height * 0.62)
        if end_logo:
            img.paste(end_logo, (width // 2 - end_logo_size // 2, end_y), end_logo)
            end_y += end_logo_size + int(height * 0.02)
        wordmark_font = _get_font(int(height * 0.045), lang)
        wm_w = _text_width(draw, "XFINLAB", wordmark_font, height * 0.03)
        draw.text((width // 2 - wm_w / 2, end_y), "XFINLAB", font=wordmark_font, fill=colors["accent"])
        end_y += int(height * 0.055)
        end_url_font = _get_font(int(height * 0.024), lang)
        end_url_w = _text_width(draw, "xfinlab.com", end_url_font, height * 0.016)
        draw.text((width // 2 - end_url_w / 2, end_y), "xfinlab.com", font=end_url_font, fill=colors["muted"])
    elif kind == "custom":
        # 2026-08-09 (admin chat-to-video feature): body slides for an
        # arbitrary admin-supplied topic, with no ticker/OHLC data to
        # render -- same simple vertically-centered wrapped-text layout as
        # "intro" above, reused rather than duplicated since there's no
        # per-slide structured data to lay out beyond the caption itself.
        custom_font = _get_font(int(height * 0.032), lang)
        lines = _wrap_text_pixel(draw, caption_text, custom_font, width - 2 * pad_x)
        line_h = int(height * 0.037)
        start_y = int(height * 0.5) - (len(lines) * line_h) // 2
        for i, line in enumerate(lines):
            draw.text((pad_x, start_y + i * line_h), line,
                      font=custom_font, fill=colors["fg"])
    elif kind == "chart":
        # 2026-08-09 (admin chat-to-video feature): custom-topic slide that
        # DOES have a real ticker -- e.g. admin typed "make a video about
        # NVDA earnings". Reuses the same real-OHLC candlestick renderer as
        # the "signal" branch below (_draw_candles, fed by _fetch_candles's
        # genuine Alpaca/yfinance data), but without a direction/confidence
        # call, since a custom-topic video isn't making a confluence-engine
        # signal claim -- it's just grounding the AI narration in an actual
        # price chart instead of a blank slide, same anti-fabrication
        # discipline as the rest of this module.
        ticker = (signal or {}).get("ticker", "")
        y = header_bottom
        draw.text((pad_x, y), ticker, font=_get_font(int(height * 0.062), lang), fill=colors["fg"])
        y += int(height * 0.09)

        candles = (signal or {}).get("_candles") or []
        indicator = (signal or {}).get("_indicator")
        candle_bottom = height - int(height * 0.2)
        # 2026-09-07 (upgrade #3, RSI/MACD indicator charts): when a
        # ticker's chart also has indicator data, shrink the candlestick
        # area to make room for the indicator panel underneath instead of
        # overlapping it -- the candles stay the primary visual, the
        # indicator is a secondary strip below, same visual hierarchy a
        # real charting terminal uses.
        if indicator:
            panel_h = int(height * 0.155)
            panel_y0 = candle_bottom - panel_h
            candle_area_bottom = panel_y0 - int(height * 0.018)
        else:
            candle_area_bottom = candle_bottom
        if candles and candle_area_bottom > y:
            _draw_candles(draw, candles, x0=pad_x, y0=y, w=width - 2 * pad_x, h=candle_area_bottom - y,
                          colors=colors, lang=lang, sr=(signal or {}).get("_sr"))
        if indicator:
            _draw_indicator_panel(draw, indicator, x0=pad_x, y0=panel_y0, w=width - 2 * pad_x, h=panel_h,
                                   colors=colors, lang=lang)

        # 2026-08-13: the spoken-line caption used to be burned into this
        # PNG as static wrapped text right here. It's now a real scrolling
        # marquee instead (2 lines, right-to-left, per-language font
        # sizing) composited on top of the assembled video by ffmpeg's
        # drawtext filter -- see _marquee_filters() and
        # _render_video_pipeline() below. The blank space below the
        # candlestick chart (candle_bottom's 0.2*height reserve) is left
        # here on purpose so the overlay has room to sit in the same spot
        # the static caption used to occupy.
    else:  # "signal"
        direction = _normalize_direction(signal.get("confluence_direction"))
        color = colors["green"] if direction == "bullish" else (colors["red"] if direction == "bearish" else colors["muted"])
        direction_label = script["direction_label"].get(direction, direction)
        label = _translate_label(signal.get("label", ""), lang)

        y = header_bottom
        draw.text((pad_x, y), signal["ticker"], font=_get_font(int(height * 0.062), lang), fill=colors["fg"])
        y += int(height * 0.075)
        draw.text((pad_x, y), label, font=_get_font(int(height * 0.019), lang), fill=colors["muted"])
        y += int(height * 0.035)
        draw.text((pad_x, y), direction_label.upper(), font=_get_font(int(height * 0.029), lang), fill=color)
        y += int(height * 0.042)
        conf = signal.get("confluence_confidence_pct")
        if conf is not None:
            draw.text((pad_x, y), f"{conf}%", font=_get_font(int(height * 0.047), lang), fill=color)
        y += int(height * 0.09)

        # Real candlestick strip from real OHLC data -- honesty per this
        # module's docstring: a genuine (if simplified) chart, not a
        # decorative placeholder. Leaves room below for the caption band
        # + disclaimer footer.
        candles = signal.get("_candles") or []
        candle_bottom = height - int(height * 0.2)
        if candles and candle_bottom > y:
            _draw_candles(draw, candles, x0=pad_x, y0=y, w=width - 2 * pad_x, h=candle_bottom - y,
                          colors=colors, lang=lang, sr=signal.get("_sr"))

        # 2026-08-13: same change as the "chart" branch above -- the
        # spoken-line caption is now a scrolling ffmpeg-drawtext marquee
        # composited afterward, not burned into this PNG. See
        # _marquee_filters() / _render_video_pipeline() below. Lets
        # silent/muted autoplay viewers (the default on most social
        # feeds) still follow along without sound, same reason the old
        # static caption existed -- just animated now instead of static.

    # Disclaimer footer on every slide -- same standing site-wide rule
    # (see risk-warning.html / every AI-analysis page) that any signal-
    # like content carries a non-advice disclaimer. 2026-08-04 fix: this
    # used to be a hardcoded "技術面參考，並非投資建議 / Not investment
    # advice" literal regardless of narration language -- now pulled from
    # _SCRIPT[lang]["footer"], translated for real.
    footer_text = script.get("footer", "Technical reference only, not investment advice")
    draw.text((pad_x, height - int(height * 0.073)), footer_text,
              font=_get_font(int(height * 0.0125), lang), fill=colors["muted"])

    return img


def _draw_candles(draw: ImageDraw.ImageDraw, candles: List[dict], x0: int, y0: int, w: int, h: int,
                   colors: dict, lang: Optional[str] = None, sr: Optional[dict] = None):
    if not candles or h <= 0:
        return
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    top, bottom = max(highs), min(lows)
    span = (top - bottom) or 1.0

    n = len(candles)
    slot_w = w / n
    body_w = max(4, slot_w * 0.55)

    def y_for(price: float) -> float:
        return y0 + h - ((price - bottom) / span) * h

    for i, c in enumerate(candles):
        cx = x0 + slot_w * i + slot_w / 2
        color = colors["green"] if c["close"] >= c["open"] else colors["red"]
        # Wick.
        draw.line([(cx, y_for(c["high"])), (cx, y_for(c["low"]))], fill=color, width=2)
        # Body.
        top_y = y_for(max(c["open"], c["close"]))
        bot_y = y_for(min(c["open"], c["close"]))
        draw.rectangle([cx - body_w / 2, top_y, cx + body_w / 2, max(bot_y, top_y + 2)], fill=color)

    # 2026-08-13 (AJ: "K線圖可劃出說出的支持壓力位嗎？把技術分析圖示化"):
    # real support/resistance overlay, sourced from
    # _fetch_support_resistance() below -- which reuses
    # TechnicalAnalysisService._support_resistance()'s existing real
    # swing-point clustering, the SAME method chart-analysis.html's own
    # support/resistance display uses, not an AI guess narrated into
    # existence. `sr` is None whenever that fetch failed/had nothing
    # (see that function's docstring) or the caller never had a ticker to
    # look one up for -- in either case this silently draws nothing extra
    # and the chart is just the plain candlesticks, same anti-fabrication
    # discipline as the rest of this module: no line unless the number
    # is real. A level is also skipped if it falls outside this specific
    # chart's visible price range (drawing a line off-canvas or implying
    # a level "just above/below" the visible window would be misleading).
    if sr:
        dash_w, gap_w = 10, 6
        for key, color_key in (("support", "green"), ("resistance", "red")):
            level_data = sr.get(key)
            if not level_data:
                continue
            level = level_data.get("level")
            if level is None or not (bottom <= level <= top):
                continue
            ly = y_for(level)
            color = colors[color_key]
            x = x0
            while x < x0 + w:
                draw.line([(x, ly), (min(x + dash_w, x0 + w), ly)], fill=color, width=2)
                x += dash_w + gap_w
            label = f"{level:g}"
            label_font = _get_font(max(10, int(h * 0.045)), lang)
            label_w = _text_width(draw, label, label_font, h * 0.03)
            draw.text((x0 + w - label_w, ly - int(h * 0.05)), label, font=label_font, fill=color)


_INDICATOR_KEYWORDS = {
    "macd": ["macd", "moving average convergence", "平滑異同移動平均線", "麥克指標"],
    "rsi": ["rsi", "relative strength index", "relative strength", "相對強弱指數", "相對強弱指標", "相對強弱"],
}


def _detect_requested_indicator(topic: str) -> Optional[str]:
    """2026-09-07 addition for upgrade #3 (AJ: "1-4可以全做" -- technical
    indicator charts). Checked before any chart is fetched in
    generate_custom_video(): if the admin's topic explicitly names RSI
    or MACD (e.g. "explain NVDA's RSI", "講吓大市MACD走勢"), every chart
    slide in this video gets that indicator panel added below its
    candlesticks, using the SAME real math (TechnicalAnalysisService.
    _rsi()/_macd(), the exact methods chart-analysis.html's own
    indicator values come from) -- never an AI-narrated guess about
    what the indicator "probably" shows. Checks MACD first since "macd"
    never collides with an RSI keyword, whereas being liberal about
    matching order doesn't matter here (the two keyword lists don't
    overlap). Returns None (never raises) when neither is mentioned --
    the existing plain-candlestick chart is not a fallback state, it's
    simply what most requests still want, so this only ever ADDS a
    panel, never removes the chart itself."""
    lower = (topic or "").lower()
    for indicator, keywords in _INDICATOR_KEYWORDS.items():
        if any(kw in lower for kw in keywords):
            return indicator
    return None


def _compute_indicator_series(ticker: str, indicator: str, limit: int = 20) -> Optional[dict]:
    """Real RSI/MACD series for the last `limit` bars, aligned to the
    same window _fetch_candles() renders as candlesticks. Fetches its
    own longer OHLC history (6mo, vs. _fetch_candles' 2mo) because
    MACD's 26-period slow EMA needs real warmup data before its early
    values are meaningful -- computing it over only the visible 20 bars
    would silently understate the indicator, which this codebase's
    anti-fabrication rule can't allow even as a rendering shortcut.
    Reuses TechnicalAnalysisService._rsi()/_macd() directly (the same
    static methods get_technical_analysis() calls internally) rather
    than reimplementing the math, so this can never drift from the
    values chart-analysis.html shows for the same ticker.

    Returns None on ANY failure (fetch error, insufficient history,
    unrecognized indicator) -- caller must treat an indicator panel as
    a bonus, same "real chart is a bonus, never a requirement"
    convention as _fetch_support_resistance() above. Never raises."""
    try:
        from services.technical_analysis_service import fetch_ohlc_history, TechnicalAnalysisService

        hist = fetch_ohlc_history(ticker, period="6mo")
        if hist is None or hist.empty:
            return None
        closes = hist["Close"].dropna()
        if len(closes) < 30:
            return None

        if indicator == "rsi":
            rsi = TechnicalAnalysisService._rsi(closes)
            values = [round(float(v), 2) for v in rsi.tail(limit)]
            if not values:
                return None
            return {"kind": "rsi", "values": values}
        elif indicator == "macd":
            macd_line, signal_line, hist_series = TechnicalAnalysisService._macd(closes)
            m = [round(float(v), 4) for v in macd_line.tail(limit)]
            s = [round(float(v), 4) for v in signal_line.tail(limit)]
            h = [round(float(v), 4) for v in hist_series.tail(limit)]
            if not m:
                return None
            return {"kind": "macd", "macd_line": m, "signal_line": s, "histogram": h}
        return None
    except Exception:
        return None


def _draw_indicator_panel(draw: ImageDraw.ImageDraw, indicator: dict, x0: int, y0: int, w: int, h: int,
                           colors: dict, lang: Optional[str] = None):
    """Draws a real RSI or MACD panel below the candlestick chart, fed
    by _compute_indicator_series() above. Silently does nothing on
    missing/too-short data (h<=0, <2 points) -- same "no line unless the
    number is real" discipline _draw_candles()'s support/resistance
    overlay already follows just above."""
    if not indicator or h <= 0:
        return
    kind = indicator.get("kind")
    label_font = _get_font(max(10, int(h * 0.16)), lang)

    if kind == "rsi":
        values = indicator.get("values") or []
        if len(values) < 2:
            return
        draw.text((x0, y0), "RSI (14)", font=label_font, fill=colors["muted"])
        panel_y0 = y0 + int(h * 0.26)
        panel_h = h - int(h * 0.26)
        if panel_h <= 0:
            return
        n = len(values)
        slot_w = w / (n - 1) if n > 1 else w

        def y_for(v):
            v = max(0.0, min(100.0, v))
            return panel_y0 + panel_h - (v / 100.0) * panel_h

        for level, color_key in ((70, "red"), (30, "green")):
            ly = y_for(level)
            draw.line([(x0, ly), (x0 + w, ly)], fill=colors[color_key], width=1)
        points = [(x0 + i * slot_w, y_for(v)) for i, v in enumerate(values)]
        draw.line(points, fill=colors["accent"], width=3)

    elif kind == "macd":
        macd_line = indicator.get("macd_line") or []
        signal_line = indicator.get("signal_line") or []
        hist = indicator.get("histogram") or []
        if len(macd_line) < 2:
            return
        draw.text((x0, y0), "MACD (12,26,9)", font=label_font, fill=colors["muted"])
        panel_y0 = y0 + int(h * 0.26)
        panel_h = h - int(h * 0.26)
        if panel_h <= 0:
            return
        n = len(macd_line)
        all_vals = macd_line + signal_line + hist or [0.0]
        top, bottom = max(all_vals), min(all_vals)
        span = (top - bottom) or 1.0
        slot_w = w / n

        def y_for(v):
            return panel_y0 + panel_h - ((v - bottom) / span) * panel_h

        if bottom <= 0 <= top:
            zero_y = y_for(0)
            draw.line([(x0, zero_y), (x0 + w, zero_y)], fill=colors["muted"], width=1)

        bar_w = max(2, slot_w * 0.5)
        for i, v in enumerate(hist):
            cx = x0 + slot_w * i + slot_w / 2
            color = colors["green"] if v >= 0 else colors["red"]
            top_y = y_for(max(v, 0))
            bot_y = y_for(min(v, 0))
            draw.rectangle([cx - bar_w / 2, top_y, cx + bar_w / 2, max(bot_y, top_y + 2)], fill=color)

        pts_m = [(x0 + i * slot_w + slot_w / 2, y_for(v)) for i, v in enumerate(macd_line)]
        draw.line(pts_m, fill=colors["accent"], width=2)
        if len(signal_line) > 1:
            pts_s = [(x0 + i * slot_w + slot_w / 2, y_for(v)) for i, v in enumerate(signal_line)]
            draw.line(pts_s, fill=colors["fg"], width=2)


def _fetch_candles(ticker: str, limit: int = 20) -> List[dict]:
    try:
        from services.technical_analysis_service import fetch_ohlc_history

        hist = fetch_ohlc_history(ticker, period="2mo")
        if hist is None or hist.empty:
            return []
        tail = hist.tail(limit)
        return [
            {"open": float(r["Open"]), "high": float(r["High"]), "low": float(r["Low"]), "close": float(r["Close"])}
            for _, r in tail.iterrows()
            if all(v == v for v in (r["Open"], r["High"], r["Low"], r["Close"]))  # drop NaN rows
        ]
    except Exception:
        return []


def _fetch_support_resistance(ticker: str) -> Optional[dict]:
    """2026-08-13: real support/resistance levels for the video's K-line
    overlay -- calls technical_analysis_service.get_technical_analysis(),
    which internally runs TechnicalAnalysisService._support_resistance()'s
    real swing-point clustering over actual OHLC history (the same
    real-data method chart-analysis.html's own support/resistance display
    already uses, see task #524's descriptive-support/resistance rewrite).
    Deliberately a SEPARATE call from _fetch_candles() above rather than
    threading extra return values through it -- keeps each helper doing
    one clearly-named thing, and the extra fetch is a rounding error
    against this pipeline's already-1-2-minute TTS+ffmpeg render budget.
    Returns None on ANY failure (insufficient history, fetch error,
    neither level found) -- caller (_draw_candles) must then render the
    plain candlestick chart with no lines, never a guessed/AI-narrated
    level standing in for a real one."""
    try:
        from services.technical_analysis_service import get_technical_analysis

        tech = get_technical_analysis(ticker, period="6mo", interval="1d")
        if not tech or "error" in tech:
            return None
        support = tech.get("support")
        resistance = tech.get("resistance")
        if not support and not resistance:
            return None
        return {"support": support, "resistance": resistance}
    except Exception:
        return None


def _ffprobe_duration(path: str) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration", "-of", "csv=p=0", path],
        capture_output=True, text=True, timeout=15,
    )
    try:
        return float(out.stdout.strip())
    except (ValueError, TypeError):
        return 3.0  # safe fallback slide length if ffprobe's own output is ever unparseable


def generate_daily_video(lang: str = "zh-HK", max_signals: int = 3,
                          aspect_ratio: str = _DEFAULT_ASPECT, theme: str = _DEFAULT_THEME) -> dict:
    """Real end-to-end render. Returns {"available": False, "message":
    ...} immediately if TTS or ffmpeg aren't configured -- never
    attempts a partial render. On success returns {"available": True,
    "path": ..., "duration_sec": ..., "slides_count": ..., "lang": ...,
    "aspect_ratio": ..., "theme": ..., "used_ai_script": bool}."""
    if not is_available():
        msg = "Video Engine unavailable: " + (
            "GOOGLE_TTS_API_KEY not set" if not tts_service.is_available() else "ffmpeg/ffprobe not found on PATH"
        )
        _log_generation("unavailable", msg)
        return {"available": False, "message": msg}

    lang = lang if lang in _SCRIPT else "en"
    script = _SCRIPT[lang]
    width, height = _ASPECT_RATIOS.get(aspect_ratio, _ASPECT_RATIOS[_DEFAULT_ASPECT])
    aspect_ratio = aspect_ratio if aspect_ratio in _ASPECT_RATIOS else _DEFAULT_ASPECT
    colors = _THEMES.get(theme, _THEMES[_DEFAULT_THEME])
    theme = theme if theme in _THEMES else _DEFAULT_THEME

    try:
        from api.market_pulse import _compute_free_signals

        cache = _compute_free_signals()
        signals = (cache.get("signals") or [])[:max_signals]
    except Exception as e:
        _log_generation("error", f"Failed to fetch signals: {e}")
        return {"available": False, "message": f"Failed to fetch today's signals: {e}"}

    if not signals:
        _log_generation("error", "No signals available today")
        return {"available": False, "message": "No signals available today"}

    for s in signals:
        s["_candles"] = _fetch_candles(s["ticker"])
        s["_sr"] = _fetch_support_resistance(s["ticker"])

    slides = [("intro", None)] + [("signal", s) for s in signals] + [("outro", None)]

    # Template narration is built first as the honest, always-available
    # fallback; the AI rewrite (if it succeeds and returns the right
    # number of lines) replaces it wholesale, never partially -- a mix
    # of AI-written and template lines would be a worse, inconsistent
    # result than picking one source cleanly.
    template_texts = []
    for kind, s in slides:
        if kind == "intro":
            template_texts.append(script["intro"])
        elif kind == "outro":
            template_texts.append(script["outro"])
        else:
            # 2026-08-04 fix: same raw-Chinese-vs-English-key mismatch as
            # _render_slide()/_ai_rewrite_narration() above -- confluence_
            # direction is "偏多"/"偏空" from the source data, not
            # "bullish"/"bearish", so this lookup used to silently miss on
            # every non-Chinese language and speak the raw Chinese word.
            direction = _normalize_direction(s.get("confluence_direction"))
            template_texts.append(script["signal"].format(
                ticker=s["ticker"],
                direction=script["direction_label"].get(direction, direction),
                confidence=s.get("confluence_confidence_pct", 0),
            ))

    ai_texts = _ai_rewrite_narration(signals, lang)
    used_ai_script = ai_texts is not None
    narration_texts = ai_texts if used_ai_script else template_texts

    result = _render_video_pipeline(
        slides, narration_texts, lang, aspect_ratio, width, height, colors,
        log_note=f"Generated with {len(signals)} signals ({lang}, {aspect_ratio}, {theme} theme, "
                  f"AI script: {'yes' if used_ai_script else 'no (template fallback)'})",
    )
    if result.get("available"):
        result["theme"] = theme
        result["used_ai_script"] = used_ai_script
    return result


def _fit_marquee_fontsize(text: str, lang: str, height: int, width: int) -> int:
    """2026-08-13 ("不同語言自動調整" -- auto-adjust the marquee caption per
    language): a fixed pixel fontsize renders very differently wide
    depending on script (CJK/Arabic glyphs are typically wider per
    character than Latin at the same point size), which would make some
    languages' scroll pass take far longer than others at a shared speed.
    Measures this specific line's actual rendered width with the real
    script-aware font (_get_font already picks the right glyph set per
    lang) and scales the fontsize down -- never up, to avoid an
    already-short line ballooning to an oversized single word -- if it
    would render wider than the screen.

    2026-08-13 (later same day, AJ: "先滾出前段再滾出後，全顯示後，然後停下"
    -- each line now scrolls in from the right and then HOLDS at a fully
    on-screen resting position instead of looping forever): max_w was
    previously width*2.6 (fine for an infinite loop where the line only
    ever needed to *pass through* the frame). Now that a line must come
    to rest fully visible, it has to actually fit within the frame, so
    max_w is capped near the real screen width instead."""
    base = int(height * 0.026)
    floor = int(height * 0.016)
    font = _get_font(base, lang)
    scratch_draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    line_w = _text_width(scratch_draw, text, font, base * 0.6)
    max_w = width * 0.94
    if line_w > max_w > 0:
        return max(floor, int(base * (max_w / line_w)))
    return base


def _line_fits_at_floor(text: str, lang: str, width: int, height: int) -> bool:
    """Helper for the 2-line-vs-3-line decision below: even at the
    smallest allowed fontsize (the same floor _fit_marquee_fontsize()
    respects), does this line's real rendered width still fit on
    screen? If not, the line is simply too long for 2-line layout and
    the caller should re-split into 3 lines instead."""
    floor = int(height * 0.016)
    font = _get_font(floor, lang)
    scratch_draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    line_w = _text_width(scratch_draw, text, font, floor * 0.6)
    return line_w <= width * 0.94


def _split_lines(text: str, n: int) -> List[str]:
    """2026-08-13 (AJ: "分2行順滾...太長，再長自動分3行" -- split into 2
    lines, auto-escalating to 3 if a line is still too long): distributes
    `text` into `n` roughly equal-length segments.

    2026-08-13 (later same day, same root cause as _wrap_text_pixel()'s
    fix above): originally split on raw text.split(" ") "words" whenever
    the text contained any space -- but admin narration mixing a bare
    Latin ticker with Chinese prose (no internal spaces) meant a whole
    CJK sentence could be treated as one atomic, unsplittable "word",
    landing entirely on a single line regardless of `n`. Now walks
    _tokenize_breakable() pieces instead, so a CJK run is always
    breakable character-by-character even next to Latin tokens, while
    Latin words themselves still stay intact."""
    text = (text or "").strip()
    if n <= 1 or not text:
        return [text] if text else []

    pieces = _tokenize_breakable(text)
    target = len(text) / n
    lines: List[str] = []
    current = ""
    current_len = 0
    for piece in pieces:
        if current and current_len + len(piece) > target and len(lines) < n - 1:
            lines.append(current.strip())
            current = piece
            current_len = len(piece)
        else:
            current += piece
            current_len += len(piece)
    if current.strip():
        lines.append(current.strip())

    # very short input relative to n can under-produce lines -- pad by
    # repeatedly halving the longest remaining line at a piece boundary.
    while len(lines) < n:
        idx = max(range(len(lines)), key=lambda i: len(lines[i]))
        seg_pieces = _tokenize_breakable(lines[idx])
        if len(seg_pieces) < 2:
            break
        mid = len(seg_pieces) // 2
        left = "".join(seg_pieces[:mid]).strip()
        right = "".join(seg_pieces[mid:]).strip()
        if not left or not right:
            break
        lines[idx:idx + 1] = [left, right]

    return [l for l in lines if l]


def _marquee_filters(text: str, lang: str, width: int, height: int, colors: dict,
                      start: float, dur: float, workdir: str, idx: int) -> List[str]:
    """2026-08-13 (AJ: "滾快D出來，分2行順滾，即先滾出前段再滾出後，全顯示
    後，然後停下，要分2行 太長，再長自動分3行" -- scroll faster; split into
    2 lines that reveal in reading order, one after another; once fully
    shown, stop; auto-escalate to 3 lines if 2 is still too cramped):
    replaces the earlier single continuously-looping line. Builds one
    ffmpeg drawtext filter per line. Each line's `x` expression slides in
    from off-screen right and is clamped with max(...) to a centered
    resting position once it arrives -- it does NOT loop back off-screen,
    it just stops there and holds for the rest of the slide. Lines are
    staggered in time (line 2 doesn't start moving until line 1 has
    finished arriving) so they read top-to-bottom in the same order the
    narration says them, matching AJ's "先...再..." (first...then...)
    request.

    Text is written to a UTF-8 textfile= on disk per line rather than
    embedded inline via text= -- ffmpeg's filtergraph syntax treats
    `:`, `,`, `'`, `\\`, `[`, `]` as structural, and this module supports
    16 languages including CJK/Arabic/Devanagari/Bengali script text that
    can't be reliably escaped inline; textfile= sidesteps that whole
    class of bug.

    Returns an empty list (never raises) on blank text or a missing font,
    so a slide just gets no overlay instead of a broken filter -- same
    "never hard-fail the whole render over an optional visual" posture as
    the rest of this module."""
    text = (text or "").strip()
    if not text:
        return []

    font_path = _resolve_font_path(lang) or next((p for p in _FONT_CANDIDATES if os.path.exists(p)), None)
    if not font_path:
        return []  # no usable font on this host -- skip the overlay rather than crash ffmpeg

    lines_2 = _split_lines(text, 2)
    if lines_2 and all(_line_fits_at_floor(l, lang, width, height) for l in lines_2):
        lines = lines_2
    else:
        lines = _split_lines(text, 3)
    if not lines:
        return []

    fontsize = max(int(height * 0.016), min(_fit_marquee_fontsize(l, lang, height, width) for l in lines))
    font = _get_font(fontsize, lang)
    scratch_draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))

    fg_hex = "0x%02x%02x%02x" % colors["fg"]
    speed = max(220, int(width * 0.34))  # AJ: "滾快D" -- roughly 2x the old pace
    end = start + dur
    n = len(lines)
    line_spacing = int(height * 0.05)
    bottom_y = height - int(height * 0.10)

    def esc_path(p: str) -> str:
        return p.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")

    filters: List[str] = []
    local_offset = 0.0
    for i, line in enumerate(lines):
        line_w = _text_width(scratch_draw, line, font, fontsize * 0.6)
        # distance this line has to travel: from fully off-screen right
        # (x=w) to its centered resting spot (x=(w-text_w)/2).
        travel = (width + line_w) / 2.0
        arrival = travel / speed
        line_start = start + local_offset
        y = bottom_y - (n - 1 - i) * line_spacing

        text_path = os.path.join(workdir, f"marquee_{idx}_{i}.txt")
        with open(text_path, "w", encoding="utf-8") as f:
            f.write(line)

        filters.append(
            "drawtext="
            f"fontfile='{esc_path(font_path)}':"
            f"textfile='{esc_path(text_path)}':"
            f"fontcolor={fg_hex}:fontsize={fontsize}:"
            f"x='max((w-text_w)/2,w-(t-{line_start:.3f})*{speed})':y={y}:"
            f"enable='between(t,{line_start:.3f},{end:.3f})'"
        )
        # next line starts moving only once this one has arrived and
        # settled -- a short 0.12s gap keeps the cascade from feeling
        # instantaneous/robotic.
        local_offset += arrival + 0.12

    return filters


def _render_video_pipeline(slides: list, narration_texts: List[str], lang: str, aspect_ratio: str,
                            width: int, height: int, colors: dict, log_note: str) -> dict:
    """Shared TTS -> slide-render -> ffmpeg-assemble pipeline used by both
    generate_daily_video() (fixed today's-signals content) and
    generate_custom_video() (arbitrary admin-chat-requested content) --
    extracted 2026-08-09 so the two content sources don't duplicate this
    ~80-line ffmpeg/TTS assembly logic. `slides` is a list of (kind,
    payload) tuples matching len(narration_texts) 1:1, exactly as
    _render_slide() expects."""
    workdir = tempfile.mkdtemp(prefix="xfl_video_")
    try:
        audio_paths = []
        for i, text in enumerate(narration_texts):
            tts_result = tts_service.synthesize(_build_ssml(text), lang=lang, ssml=True)
            if not tts_result.get("available"):
                _log_generation("error", f"TTS failed on slide {i}: {tts_result.get('message')}")
                return {"available": False, "message": f"TTS failed: {tts_result.get('message')}"}
            audio_path = os.path.join(workdir, f"audio_{i}.mp3")
            with open(audio_path, "wb") as f:
                f.write(tts_result["audio_bytes"])
            audio_paths.append(audio_path)

        durations = [_ffprobe_duration(p) for p in audio_paths]

        image_list_path = os.path.join(workdir, "images.txt")
        with open(image_list_path, "w") as f:
            for i, ((kind, s), dur) in enumerate(zip(slides, durations)):
                bg_image = (s or {}).get("_bg_image")
                img = _render_slide(kind, s, lang, narration_texts[i], width, height, colors, bg_image=bg_image)
                img_path = os.path.join(workdir, f"slide_{i}.png")
                img.save(img_path)
                f.write(f"file '{img_path}'\nduration {dur}\n")
            # concat demuxer quirk: the last image's `duration` is ignored
            # unless the file is listed one more time afterward.
            f.write(f"file '{os.path.join(workdir, f'slide_{len(slides) - 1}.png')}'\n")

        silent_video_path = os.path.join(workdir, "silent.mp4")
        subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", image_list_path,
             "-vsync", "vfr", "-pix_fmt", "yuv420p", silent_video_path],
            capture_output=True, timeout=120, check=True,
        )

        # 2026-08-13 (AJ: faster sequential 2/3-line marquee caption that
        # reveals line-by-line then holds -- see _marquee_filters()
        # above): only the "signal"/"chart" slide kinds (the real
        # K-line/technical slides) get this overlay, timed to each
        # slide's own window on the FINAL assembled video's cumulative
        # timeline.
        marquee_filters = []
        cum_start = 0.0
        for i, ((kind, _s), dur) in enumerate(zip(slides, durations)):
            if kind in ("signal", "chart"):
                marquee_filters += _marquee_filters(
                    narration_texts[i], lang, width, height, colors, cum_start, dur, workdir, i
                )
            cum_start += dur

        narration_path = os.path.join(workdir, "narration.mp3")
        concat_inputs = []
        for p in audio_paths:
            concat_inputs += ["-i", p]
        n = len(audio_paths)
        filter_str = "".join(f"[{i}:a]" for i in range(n)) + f"concat=n={n}:v=0:a=1[out]"
        subprocess.run(
            ["ffmpeg", "-y", *concat_inputs, "-filter_complex", filter_str, "-map", "[out]", narration_path],
            capture_output=True, timeout=60, check=True,
        )

        os.makedirs(_OUTPUT_DIR, exist_ok=True)
        final_path = os.path.join(_OUTPUT_DIR, _OUTPUT_FILENAME)
        if marquee_filters:
            # Compositing text requires re-encoding the video stream --
            # can't use the "-c:v copy" fast path below once drawtext is
            # in play, hence the longer timeout than the no-marquee branch.
            #
            # "fps=25," prefix is load-bearing, not decorative: the concat
            # demuxer above was built with -vsync vfr, which for a still
            # image held across its whole slide duration collapses that
            # slide down to a SINGLE encoded frame (verified directly --
            # a 3-second still slide produces exactly 1 frame in the
            # container, not ~75). That's fine for a static PNG, but a
            # time-varying drawtext x-position filter needs the decoder to
            # actually emit frames spread across the timeline to have
            # anything to animate between -- without this, ffmpeg's
            # filtergraph sees one input frame and produces one output
            # frame total, and the whole marquee silently never appears
            # (confirmed by an empty test render before this fix: 0 bright
            # pixels across 6 sampled frames). fps=25 forces the still
            # frame to be resampled at 25fps through the filter chain so
            # `t` in _marquee_filters()'s x= expression actually advances.
            video_filter = "[0:v]fps=25," + ",".join(marquee_filters) + "[vout]"
            subprocess.run(
                ["ffmpeg", "-y", "-i", silent_video_path, "-i", narration_path,
                 "-filter_complex", video_filter, "-map", "[vout]", "-map", "1:a",
                 "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
                 "-shortest", final_path],
                capture_output=True, timeout=180, check=True,
            )
        else:
            # No signal/chart slides in this video (e.g. a custom-topic
            # video with no detected ticker) -- nothing to overlay, keep
            # the original fast stream-copy path unchanged.
            subprocess.run(
                ["ffmpeg", "-y", "-i", silent_video_path, "-i", narration_path,
                 "-c:v", "copy", "-c:a", "aac", "-b:a", "128k", "-shortest", final_path],
                capture_output=True, timeout=60, check=True,
            )

        total_duration = sum(durations)
        _log_generation("ok", log_note, total_duration, len(slides))
        return {
            "available": True,
            "path": final_path,
            "duration_sec": round(total_duration, 1),
            "slides_count": len(slides),
            "lang": lang,
            "aspect_ratio": aspect_ratio,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
    except subprocess.CalledProcessError as e:
        err = (e.stderr or b"").decode("utf-8", errors="ignore")[-500:]
        _log_generation("error", f"ffmpeg failed: {err}")
        return {"available": False, "message": f"Video rendering failed: {err}"}
    except Exception as e:
        _log_generation("error", str(e))
        return {"available": False, "message": f"Video generation failed: {e}"}
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


_VALID_LANGS = None  # set lazily below to avoid a forward-reference to _SCRIPT


def _fit_cover(img: Image.Image, width: int, height: int) -> Image.Image:
    """Resize+crop img to exactly (width, height), covering the whole
    frame (cropping any overflow) rather than letterboxing -- same
    "cover" behavior as CSS background-size:cover. A generated image
    that only partially filled a full-bleed slide would look like a
    rendering bug, not a design choice, so this always fills the frame."""
    src_ratio = img.width / img.height
    dst_ratio = width / height
    if src_ratio > dst_ratio:
        new_height = height
        new_width = max(1, round(height * src_ratio))
    else:
        new_width = width
        new_height = max(1, round(width / src_ratio))
    img = img.resize((new_width, new_height), Image.LANCZOS)
    left = (new_width - width) // 2
    top = (new_height - height) // 2
    return img.crop((left, top, left + width, top + height))


def _generate_video_background(topic: str, width: int, height: int) -> Optional[Image.Image]:
    """2026-09-06 addition (AJ asked "GEN出來可以有相關圖片做背景嗎" --
    can the generated video have a relevant background image): one
    AI-generated background image per video via DeepInfra's FLUX Schnell
    (ai_router.generate_background_image(), same DEEPINFRA_API_KEY the
    AI Debate feature already depends on), at a fraction of a cent per
    video -- not requested per-slide, to keep this a rounding error
    against the pipeline's existing TTS+ffmpeg cost, and because a
    background that changes every slide would look more like a slideshow
    of stock photos than one coherent video.

    The prompt deliberately asks for an abstract/editorial style image
    with NO text, numbers, charts, or logos baked in -- image models
    reliably render embedded text as garbled nonsense, and a chart-like
    image generated by an AI model could be mistaken for real data,
    which this codebase's anti-fabrication rule can't allow anywhere,
    including a decorative background.

    Best-effort like _fetch_candles()/_fetch_support_resistance() above:
    returns None on ANY failure (no DEEPINFRA_API_KEY configured, request
    error, bad image data) -- caller must treat a background image as a
    bonus, never a requirement, and fall back to the plain theme-color
    background exactly as before this feature existed."""
    try:
        from ai.ai_router import generate_background_image
        from io import BytesIO

        prompt = (
            f"A professional, abstract financial/business background image evoking: {topic}. "
            f"Clean modern editorial style, muted colors, soft lighting, subtle depth of field. "
            f"No text, no words, no numbers, no charts, no graphs, no logos, no readable signage, "
            f"no close-up faces."
        )
        image_bytes = generate_background_image(prompt, size="1024x1024")
        if not image_bytes:
            return None
        img = Image.open(BytesIO(image_bytes)).convert("RGB")
        return _fit_cover(img, width, height)
    except Exception:
        return None


def _ai_write_custom_script(topic: str, num_slides: int, lang: str) -> Optional[List[str]]:
    """2026-08-09 (admin chat-to-video feature): asks the site's AI router
    to write narration for an ADMIN-SUPPLIED arbitrary topic, not today's
    real signals -- so unlike _ai_rewrite_narration() above, there is no
    real-data fact block to ground the output in. Guardrails here matter
    more, not less: the prompt explicitly forbids inventing specific
    numbers (prices/percentages/returns) unless the admin's own topic text
    supplied them, same "never fabricate a figure" rule this codebase has
    enforced since the MasterPipeline/Stress-Lab fabricated-number cleanup
    (tasks #226/#230/#272). Returns None on any failure -- caller must
    treat that as "cannot generate this custom video", there is no
    template fallback for an arbitrary topic the way the daily video has."""
    try:
        from ai.ai_router import get_ai_response
    except Exception:
        return None

    lang_name = _AI_LANG_NAMES.get(lang, "English")
    body_lines = max(1, num_slides - 2)

    prompt = (
        f"You are writing a SHORT spoken video-narration script in {lang_name} for XFINLAB, a "
        f"financial data/research platform. Output EXACTLY {num_slides} lines, one sentence per "
        f"line, no numbering, no markdown, no quotation marks:\n"
        f"Line 1: a 1-sentence intro naming XFINLAB and the topic below.\n"
        f"Lines 2 through {num_slides - 1}: {body_lines} sentence(s) of general, factual "
        f"commentary on the topic, professional financial-news tone. Do NOT invent specific "
        f"prices, percentages, dates, or return figures that are not explicitly given in the "
        f"topic text -- describe concepts, context, and publicly known facts only.\n"
        f"Line {num_slides}: a 1-sentence closing disclaimer that this is general information "
        f"only, not investment advice, mentioning xfinlab.com.\n\n"
        f"IMPORTANT: the 'Topic' text below is an instruction describing what the video should "
        f"be about -- it is NOT a script to read aloud. Do not copy, quote, or lightly reword the "
        f"topic text back as narration. Write original spoken sentences ABOUT the topic, in your "
        f"own words, as a narrator explaining it to a viewer who has not seen the topic text.\n\n"
        f"Topic (as requested by the XFINLAB team):\n{topic}\n\n"
        f"Output ONLY the {num_slides} lines of narration text, nothing else."
    )

    try:
        # 2026-09-02 fix (AJ reported: typing a request into the admin chat
        # box produced narration that just read the typed text back almost
        # verbatim instead of writing new commentary about it). reasoning_
        # effort="low" (the original setting) gives Groq's model a small
        # completion-token floor (700, see _groq()'s docstring) and little
        # room to do anything but closely mirror short/directive input --
        # "high" (floor 1400) gives it room to actually reason about the
        # topic and produce original sentences, at the cost of a slightly
        # slower call. Paired with the explicit anti-echo instruction above.
        response = get_ai_response(prompt, max_tokens=500, reasoning_effort="high")
    except Exception:
        return None
    if not response:
        return None

    lines = [ln.strip(" \t\"'") for ln in response.strip().split("\n") if ln.strip()]
    if len(lines) != num_slides:
        return None
    return lines


def parse_video_chat_request(message: str) -> dict:
    """2026-08-09: turns one free-text admin chat message (e.g. "make a
    video about NVDA earnings, in Spanish, square format") into
    {"topic": ..., "lang": ..., "aspect_ratio": ..., "theme": ...}.
    Deliberately simple keyword matching, not another AI call -- this is
    an internal admin tool where a wrong guess just means the admin picks
    the right dropdown value instead, not worth a second LLM round-trip
    (and a second point of failure) for. `topic` is always the full
    original message verbatim, since the AI script-writer in
    _ai_write_custom_script() needs the complete request anyway, including
    whatever language/format words a keyword scan might strip out."""
    text = (message or "").strip()
    lower = text.lower()

    lang = "zh-HK"
    for code, name in _AI_LANG_NAMES.items():
        if code.lower() in lower or name.lower() in lower:
            lang = code
            break

    aspect_ratio = _DEFAULT_ASPECT
    if any(k in lower for k in ("square", "1:1", "instagram", "ig feed")):
        aspect_ratio = "1:1"
    elif any(k in lower for k in ("16:9", "landscape", "youtube", "widescreen")):
        aspect_ratio = "16:9"
    elif any(k in lower for k in ("9:16", "vertical", "shorts", "reels", "tiktok")):
        aspect_ratio = "9:16"

    theme = _DEFAULT_THEME
    if "light theme" in lower or "light mode" in lower or " light " in f" {lower} ":
        theme = "light"
    elif "dark theme" in lower or "dark mode" in lower:
        theme = "dark"

    return {"topic": text, "lang": lang, "aspect_ratio": aspect_ratio, "theme": theme}



# 2026-09-05 (admin asked "商品期貨外匯 點打" -- commodities/forex don't
# have a deterministic ticker that classify_ai() below can reliably
# invent from a plain-language name; that function is a generic LLM
# ticker-extraction prompt with no forex/commodity examples, so typing
# "gold"/"oil"/"EUR/USD" was silently falling through to a plain text
# slide -- no chart, no error, easy to miss why. This is a small,
# deterministic keyword -> ETF-ticker lookup checked FIRST, before the
# AI classifier -- scoped entirely to this file (does not touch
# services/intent_router_service.py, which other features share) so it
# cannot change ticker-detection behavior anywhere else on the site.
# Maps to liquid, plain-equity-format commodity/currency ETFs rather
# than yfinance's =F/=X futures/forex syntax specifically because
# _fetch_candles() -> fetch_ohlc_history() only special-cases Alpaca for
# ^[A-Z]{1,5}$-shaped symbols and falls back to yfinance for everything
# else -- an ETF ticker is the one format guaranteed to resolve either
# way, and it's also a symbol a human can type directly if this lookup
# ever misses a phrasing.
_COMMODITY_FOREX_KEYWORDS = {
    "gold": "GLD", "黃金": "GLD", "金價": "GLD",
    "silver": "SLV", "白銀": "SLV", "銀價": "SLV",
    "crude oil": "USO", "crude": "USO", "oil price": "USO", "原油": "USO", "石油": "USO",
    "natural gas": "UNG", "天然氣": "UNG",
    "copper": "CPER", "銅價": "CPER",
    "platinum": "PPLT", "鉑金": "PPLT", "白金": "PPLT",
    "wheat": "WEAT", "小麥": "WEAT",
    "corn": "CORN", "粟米": "CORN", "玉米": "CORN",
    "soybean": "SOYB", "大豆": "SOYB", "黃豆": "SOYB",
    "dollar index": "UUP", "dxy": "UUP", "美元指數": "UUP",
    "eur/usd": "FXE", "eurusd": "FXE", "歐元": "FXE",
    "japanese yen": "FXY", "usd/jpy": "FXY", "日圓": "FXY", "日元": "FXY",
    "british pound": "FXB", "gbp/usd": "FXB", "英鎊": "FXB",
    "vix": "VXX", "volatility index": "VXX", "波幅指數": "VXX",
}


def _detect_commodity_forex_ticker(topic: str) -> Optional[str]:
    """Checked before intent_router_service.classify_ai() in
    generate_custom_video() -- see _COMMODITY_FOREX_KEYWORDS' comment
    above for why. Longest keyword first so e.g. "crude oil" matches
    before a shorter, less specific substring would. Returns None
    (never raises) on no match -- same contract as classify_ai()'s
    ticker field, so the caller's existing fallback/error handling
    doesn't need to change."""
    lower = (topic or "").lower()
    for keyword in sorted(_COMMODITY_FOREX_KEYWORDS, key=len, reverse=True):
        if keyword in lower:
            return _COMMODITY_FOREX_KEYWORDS[keyword]
    return None


def _detect_all_commodity_forex_tickers(topic: str, max_count: int) -> List[str]:
    """2026-09-07 addition for multi-chart-slide support (AJ: "1-4可以全做"
    -- upgrade #2, multiple chart slides per video, e.g. "compare gold and
    oil" or "黃金同白銀邊隻強啲"). Unlike _detect_commodity_forex_ticker()
    above (which stops at the first/longest match, correct for the
    single-chart case), this scans for EVERY distinct commodity/forex
    keyword present so a topic naming several can get a chart slide each.
    Order-preserving by first appearance in the topic text; deduplicates
    by resulting ticker (e.g. "gold" and "金價" both map to GLD -- only
    counted once). Capped at max_count since a video only has so many
    body slides. Never raises; returns [] on no match, same
    best-effort contract as the rest of this ticker-detection code."""
    lower = (topic or "").lower()
    hits = []  # list of (first_index, ticker)
    for keyword, ticker in _COMMODITY_FOREX_KEYWORDS.items():
        idx = lower.find(keyword)
        if idx != -1:
            hits.append((idx, ticker))
    hits.sort(key=lambda pair: pair[0])
    result = []
    for _, ticker in hits:
        if ticker not in result:
            result.append(ticker)
        if len(result) >= max_count:
            break
    return result


def _ai_extract_extra_tickers(topic: str, exclude: List[str], max_count: int) -> List[str]:
    """AI-assisted extraction of stock/crypto tickers for multi-chart
    videos (e.g. "compare NVDA and AMD earnings", "邊隻股票強：TSLA定
    BYD"). Complements _detect_all_commodity_forex_tickers() above, which
    only knows a fixed commodity/forex keyword list -- equities and
    crypto need the same AI-assisted, conversational-text-tolerant
    matching intent_router_service.classify_ai() already uses for the
    single-ticker case, just extended to return more than one.

    Best-effort like every other ticker-detection helper in this file:
    on ANY failure (AI router down, bad/unparseable JSON) this returns
    [] rather than raising -- the caller already treats a chart as a
    bonus over plain text slides, never a requirement, and this must
    not change that."""
    if max_count <= 0:
        return []
    try:
        import json
        import re as _re
        from ai.ai_router import get_ai_response

        exclude_note = f" (not: {', '.join(exclude)})" if exclude else ""
        prompt = (
            f"List up to {max_count} distinct stock or crypto ticker symbols that are "
            f"explicitly named or clearly and specifically implied in this video topic"
            f"{exclude_note}. Only include a ticker if the topic is really about that "
            f"specific company/asset -- do not guess or pad the list.\n\n"
            f'Topic: "{topic}"\n\n'
            'Respond with ONLY a JSON array of ticker strings, no other text, e.g. '
            '["NVDA", "AMD"] or [] if none.'
        )
        raw = get_ai_response(prompt, max_tokens=100)
        match = _re.search(r"\[.*\]", raw or "", _re.S)
        if not match:
            return []
        parsed = json.loads(match.group(0))
        if not isinstance(parsed, list):
            return []
        seen = set(t.upper() for t in exclude)
        result = []
        for t in parsed:
            if not isinstance(t, str):
                continue
            t = t.strip().upper()
            if t and t not in seen and _re.match(r"^[A-Z][A-Z0-9.\-]{0,9}$", t):
                seen.add(t)
                result.append(t)
            if len(result) >= max_count:
                break
        return result
    except Exception:
        return []


def _detect_multiple_tickers(topic: str, max_count: int) -> List[str]:
    """Combines the deterministic commodity/forex keyword scan with the
    AI-assisted equity/crypto extractor to support multi-chart-slide
    videos. Commodity/forex hits come first (cheap, deterministic, zero
    extra API call), then the AI extractor fills any remaining slots
    with equity/crypto tickers, excluding what was already found so the
    two don't duplicate. Returns [] (never raises) if nothing is
    confidently detected -- caller falls back to plain "custom" text
    slides exactly as before this feature existed."""
    if max_count <= 0:
        return []
    tickers = _detect_all_commodity_forex_tickers(topic, max_count)
    if len(tickers) < max_count:
        tickers += _ai_extract_extra_tickers(topic, exclude=tickers, max_count=max_count - len(tickers))
    return tickers[:max_count]


def generate_custom_video(prompt_text: str, num_slides: int = 4, lang_override: str = None) -> dict:
    """2026-08-09 (admin chat-to-video feature, requested as "Video Engine
    可以加個CHAT更彈性做任何影片嗎"): admin-only, free-text-driven video
    generation, separate from generate_daily_video()'s fixed
    today's-signals format. Same availability/TTS/ffmpeg gating as
    generate_daily_video() -- reuses is_available() implicitly via
    _render_video_pipeline's TTS calls failing gracefully if unconfigured.
    num_slides is capped to a small range so one chat message can't
    request an unreasonably long (expensive) render.

    2026-08-13 (explicit language dropdown for the chat panel, matching
    the fixed Generate Now panel's videoLangSelect): lang_override, if
    given and a recognized code, takes priority over
    parse_video_chat_request()'s keyword-guessed language -- lets the
    admin just pick a dropdown instead of having to phrase the prompt so
    the guesser catches it (e.g. non-English topic text that doesn't
    literally name its own language)."""
    if not is_available():
        msg = "Video Engine unavailable: " + (
            "GOOGLE_TTS_API_KEY not set" if not tts_service.is_available() else "ffmpeg/ffprobe not found on PATH"
        )
        _log_generation("unavailable", msg)
        return {"available": False, "message": msg}

    if not (prompt_text or "").strip():
        return {"available": False, "message": "Empty request -- describe what the video should be about."}

    num_slides = max(3, min(8, num_slides))  # intro + at least 1 body + outro, capped at 8 total

    parsed = parse_video_chat_request(prompt_text)
    if lang_override and lang_override in _SCRIPT:
        lang = lang_override
    else:
        lang = parsed["lang"] if parsed["lang"] in _SCRIPT else "en"
    aspect_ratio = parsed["aspect_ratio"] if parsed["aspect_ratio"] in _ASPECT_RATIOS else _DEFAULT_ASPECT
    theme = parsed["theme"] if parsed["theme"] in _THEMES else _DEFAULT_THEME
    width, height = _ASPECT_RATIOS[aspect_ratio]
    colors = _THEMES[theme]

    narration_texts = _ai_write_custom_script(parsed["topic"], num_slides, lang)
    if narration_texts is None:
        _log_generation("error", f"AI script-writer failed for custom topic: {parsed['topic'][:80]!r}")
        return {"available": False, "message": "AI could not write a script for this request -- try rephrasing it."}

    # 2026-08-09, extended 2026-09-07 for multiple chart slides (AJ's
    # upgrade #2, "1-4可以全做"): if the admin's topic names one or more
    # real tickers (e.g. "compare NVDA and AMD", "gold vs oil this week"),
    # ground that many body slides in actual candlestick charts instead
    # of plain text -- same real-OHLC renderer ("chart" kind in
    # _render_slide) the daily-signals video already uses, now just
    # looped over however many distinct tickers were found (capped by
    # how many body slots the video actually has). Detection tries the
    # deterministic commodity/forex keyword list first (cheap, no AI
    # call), then falls back to an AI-assisted extractor for equities/
    # crypto and conversational phrasing (e.g. "講吓比特幣"). Best-effort
    # throughout: any failure (AI router down, no candle data for a
    # given ticker) just means fewer/zero chart slides and more "custom"
    # text slides -- a chart is always a bonus here, never a requirement.
    # 2026-09-07 (upgrade #3, "1-4可以全做"): if the topic explicitly names
    # RSI or MACD, every chart slide below also gets that indicator panel
    # -- detected once per video (not per ticker) since a topic naming an
    # indicator is asking about that indicator in general, not a
    # different one per ticker.
    requested_indicator = _detect_requested_indicator(parsed["topic"])

    n_body = num_slides - 2
    chart_slides_data = []  # list of (ticker, candles, sr, indicator)
    try:
        candidates = _detect_multiple_tickers(parsed["topic"], max(1, n_body))
        for candidate in candidates:
            candles = _fetch_candles(candidate, limit=20)
            if candles:
                sr = _fetch_support_resistance(candidate)
                indicator = (
                    _compute_indicator_series(candidate, requested_indicator, limit=20)
                    if requested_indicator else None
                )
                chart_slides_data.append((candidate, candles, sr, indicator))
            if len(chart_slides_data) >= n_body:
                break
    except Exception:
        chart_slides_data = []

    n_charts = min(len(chart_slides_data), n_body)
    chart_ticker = chart_slides_data[0][0] if chart_slides_data else None  # back-compat: first chart's ticker
    body_kinds = ["chart"] * n_charts + ["custom"] * (n_body - n_charts)

    # 2026-09-06 (AJ asked "GEN出來可以有相關圖片做背景嗎" -- can the
    # generated video have a relevant background image): ONE AI-generated
    # image per video (not per slide) applied to intro/outro/"custom" text
    # slides only -- "chart" slides keep their plain background since a
    # real candlestick chart is already the real-data visual for that
    # slide and doesn't need a decorative photo competing with it. Best-
    # effort: bg_image stays None on any failure, same as the chart path
    # just above -- a nicer background is a bonus, never a requirement.
    bg_image = _generate_video_background(parsed["topic"], width, height)
    bg_payload = {"_bg_image": bg_image} if bg_image is not None else None

    body_payloads = [
        {"ticker": t, "_candles": candles, "_sr": sr, "_indicator": indicator}
        for (t, candles, sr, indicator) in chart_slides_data[:n_charts]
    ] + [bg_payload] * (n_body - n_charts)
    slides = [("intro", bg_payload)] + list(zip(body_kinds, body_payloads)) + [("outro", bg_payload)]

    result = _render_video_pipeline(
        slides, narration_texts, lang, aspect_ratio, width, height, colors,
        log_note=f"Custom video: {parsed['topic'][:80]!r} ({lang}, {aspect_ratio}, {theme} theme)",
    )
    if result.get("available"):
        result["theme"] = theme
        result["topic"] = parsed["topic"]
        if chart_ticker:
            result["chart_ticker"] = chart_ticker  # back-compat: first chart's ticker
            result["chart_tickers"] = [t for (t, _, _, _) in chart_slides_data[:n_charts]]
        if requested_indicator and n_charts:
            result["indicator"] = requested_indicator
    return result
