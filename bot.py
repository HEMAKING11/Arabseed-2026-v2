# arabseed_telegram_bot.py
import os
import re
import sys
import json
import time
import logging
import traceback
from urllib.parse import urlparse, unquote, urlunparse, quote
from typing import Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
    CallbackContext
)

# ----------------- إعدادات التسجيل -----------------
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ----------------- إعدادات البوت -----------------
# استخدم متغير بيئة أو ضع التوكن هنا مباشرة
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "7064549403:AAHWQsrZPekW1M9kHacqB6N19aMj_xjspf4")

# ----------------- الألوان (للطباعة فقط) -----------------
class Colors:
    RED = '\033[91m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    CYAN = '\033[96m'
    WHITE = '\033[97m'
    RESET = '\033[0m'

# ----------------- إدارة الجلسات -----------------
class UserSession:
    def __init__(self):
        self.processing = False
        self.auto_mode = False
        self.current_episode = 0
        self.builder_func = None
        self.last_url = ""
        self.last_title = ""
        
    def reset(self):
        self.processing = False
        self.auto_mode = False
        self.current_episode = 0
        self.builder_func = None

user_sessions = {}

def get_user_session(user_id: int) -> UserSession:
    if user_id not in user_sessions:
        user_sessions[user_id] = UserSession()
    return user_sessions[user_id]

# ----------------- دوال المساعدة (من الكود الأصلي) -----------------
def extract_base_url(url: str) -> str:
    """استخراج الرابط الأساسي"""
    parsed_url = urlparse(url)
    return f"{parsed_url.scheme}://{parsed_url.netloc}"

def extract_title_from_url(url: str) -> str:
    """استخراج العنوان من الرابط"""
    parsed_url = urlparse(url)
    path = unquote(parsed_url.path)
    path_parts = path.strip('/').split('-')
    title = ' '.join(path_parts).replace('.html', '').title()
    if title.startswith("مسلسل"):
        words = title.split()
        new_title = []
        for word in words:
            new_title.append(word)
            if any(char.isdigit() for char in word):
                break
        title = ' '.join(new_title)
    return title

def follow_redirect(url: str, session: Optional[requests.Session] = None, headers: Optional[Dict] = None, timeout: int = 10) -> Optional[str]:
    """تتبع إعادة التوجيه"""
    if session is None:
        session = requests.Session()
    if headers is None:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

    try:
        r = session.get(url, headers=headers, allow_redirects=False, timeout=timeout)
        if 'location' in r.headers:
            loc = r.headers['location']
            logger.info(f"Found location header: {loc}")
            return loc
        r2 = session.get(url, headers=headers, allow_redirects=True, timeout=timeout)
        final = r2.url
        logger.info(f"Final URL after redirects: {final}")
        return final
    except Exception as e:
        logger.error(f"Error following redirect: {e}")
        return None

def find_last_numeric_segment_in_path(path_unquoted: str) -> Tuple[Optional[int], Optional[str]]:
    """إيجاد الجزء الرقمي الأخير في المسار"""
    parts = path_unquoted.strip('/').split('-')
    for i in range(len(parts)-1, -1, -1):
        if re.fullmatch(r'\d+', parts[i]):
            return i, parts[i]
    return None, None

def build_episode_url_from_any(url: str, episode_number: int) -> Optional[str]:
    """بناء رابط الحلقة"""
    p = urlparse(url)
    path_unquoted = unquote(p.path)
    idx, num = find_last_numeric_segment_in_path(path_unquoted)
    if idx is None:
        return None
    parts = path_unquoted.strip('/').split('-')[:idx+1]
    parts[-1] = str(episode_number)
    new_path = '/' + '-'.join(parts)
    quoted_path = quote(new_path, safe="/%")
    new_parsed = (p.scheme, p.netloc, quoted_path, '', '', '')
    return urlunparse(new_parsed)

def extract_episode_and_base(url: str) -> Tuple[Optional[int], Optional[callable]]:
    """استخراج رقم الحلقة ودالة البناء"""
    p = urlparse(url)
    path_unquoted = unquote(p.path)
    idx, num = find_last_numeric_segment_in_path(path_unquoted)
    if idx is None or num is None:
        return None, None
    return int(num), lambda ep: build_episode_url_from_any(url, ep)

# ----------------- دالة استخراج معلومات التحميل (مطابقة للكود الأصلي) -----------------
def get_download_info(server_href: str, referer: str) -> Optional[Dict]:
    """استخراج معلومات التحميل من رابط السيرفر"""
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Referer": referer
    })

    try:
        logger.info(f"Processing server link: {server_href}")
        
        # تتبع إعادة التوجيه
        redirected = follow_redirect(server_href, session=session)
        if not redirected:
            logger.error(f"Couldn't obtain redirected r-link for {server_href}")
            return None

        # البحث عن رابط ?r=
        r_link = None
        if '?r=' in redirected:
            r_link = redirected
        else:
            tmp = session.get(redirected, timeout=12)
            m = re.search(r'(https?://[^"\'>\s]+/category/downloadz/\?r=\d+[^"\'>\s]*)', tmp.text)
            if m:
                r_link = m.group(1)
            elif '?r=' in tmp.url:
                r_link = tmp.url
            else:
                if 'location' in tmp.headers and '?r=' in tmp.headers['location']:
                    r_link = tmp.headers['location']
        
        if not r_link:
            logger.error(f"Could not find ?r= link for {server_href}")
            return None

        logger.info(f"Found r_link: {r_link}")

        # تحليل صفحة التحميل
        rpage = session.get(r_link, timeout=12)
        rsoup = BeautifulSoup(rpage.text, 'html.parser')

        # البحث عن زر التحميل
        btn_tag = rsoup.find('a', id='btn') or rsoup.select_one('a.downloadbtn') or rsoup.find('a', class_='downloadbtn')
        final_asd_url = None

        if btn_tag and btn_tag.get('href'):
            candidate = btn_tag.get('href')
            if candidate.startswith('/'):
                candidate = extract_base_url(r_link) + candidate
            final_asd_url = candidate
            logger.info(f"Found btn href: {final_asd_url}")
        else:
            # محاولة إنشاء الرابط ديناميكياً
            dynamic_param_pattern = r'([?&][a-zA-Z0-9_]+\d*=[^"&\']+)'
            qs_matches = re.findall(dynamic_param_pattern, rpage.text)
            params = []
            for q in qs_matches:
                normalized_param = q.lstrip('?&')
                if normalized_param.lower().startswith('r='):
                    continue
                param_name = normalized_param.split('=', 1)[0]
                if not any(p.startswith(param_name + '=') for p in params):
                    params.append(normalized_param)
            
            if params:
                sep = '&' if '?' in r_link else '?'
                final_asd_url = r_link + sep + '&'.join(params)
                logger.info(f"Constructed dynamic url: {final_asd_url}")

        if not final_asd_url:
            logger.warning("Falling back to r_link only")
            final_asd_url = r_link

        # الحصول على الرابط النهائي
        final_resp = session.get(final_asd_url, timeout=15)
        if final_resp.status_code != 200:
            logger.error(f"Failed loading final url (status {final_resp.status_code})")
            return None
            
        fsoup = BeautifulSoup(final_resp.text, 'html.parser')

        # البحث عن رابط MP4 النهائي
        final_tag = fsoup.find('a', id='btn') or fsoup.find('a', class_='downloadbtn') or fsoup.find('a', href=re.compile(r'\.mp4'))
        if not final_tag:
            logger.error("Couldn't locate final .mp4 link")
            return None

        file_link = final_tag.get('href')
        if file_link and file_link.startswith('/'):
            file_link = extract_base_url(final_asd_url) + file_link

        # استخراج معلومات الملف
        file_name = None
        file_size = None
        
        try:
            name_span = fsoup.select_one('.TitleCenteral h3 span')
            if name_span:
                file_name = name_span.get_text(strip=True)
            
            size_span = fsoup.select_one('.TitleCenteral h3:nth-of-type(2) span')
            if size_span:
                file_size = size_span.get_text(strip=True)
        except Exception:
            pass

        if not file_size:
            h3 = fsoup.find('h3')
            if h3:
                msize = re.search(r'الحجم[:\s\-–]*([\d\.,]+\s*(?:MB|GB))', h3.get_text())
                if msize:
                    file_size = msize.group(1)

        if not file_name:
            file_name = os.path.basename(file_link) if file_link else "unknown"

        return {
            'direct_link': file_link.replace(" ", ".") if file_link else None,
            'file_name': file_name,
            'file_size': file_size or "Unknown"
        }

    except Exception as e:
        logger.error(f"Error extracting download info: {e}")
        return None

# ----------------- دالة معالجة الحلقة (مطابقة للكود الأصلي) -----------------
def process_single_episode(arabseed_url: str, session: requests.Session) -> Tuple[bool, Optional[str], Optional[List[List[Dict]]]]:
    """
    معالجة حلقة واحدة
    ترجع: (نجاح/فشل, رسالة/عنوان, قائمة الأزرار)
    """
    try:
        # تتبع الروابط المختصرة
        if '/l/' in arabseed_url or 'reviewrate.net' in arabseed_url:
            arabseed_url = follow_redirect(arabseed_url, session=session) or arabseed_url

        # جلب صفحة الحلقة
        try:
            resp = session.get(arabseed_url, timeout=12)
        except Exception as e:
            logger.error(f"Connection error: {e}")
            return False, "❌ خطأ في الاتصال", None

        # التحقق من وجود الحلقة
        if resp.status_code == 404:
            return False, "❌ الحلقة غير موجودة (404)", None
            
        if resp.status_code != 200:
            logger.warning(f"Status {resp.status_code} — retrying...")
            time.sleep(1.2)
            try:
                resp = session.get(arabseed_url, timeout=12)
            except Exception as e:
                logger.error(f"Retry connection error: {e}")
                return False, "❌ خطأ في الاتصال بعد إعادة المحاولة", None
                
            if resp.status_code != 200:
                return False, f"❌ خطأ في جلب الحلقة (رمز: {resp.status_code})", None

        # التحقق من محتوى الصفحة
        text_lower = resp.text.lower()
        if any(phrase in text_lower for phrase in ['لم يتم العثور', 'page not found', 'صفحة غير موجودة', 'not found']):
            return False, "❌ الحلقة غير موجودة", None

        # البحث عن رابط صفحة التحميل
        soup = BeautifulSoup(resp.text, 'html.parser')
        download_anchor = soup.find('a', href=re.compile(r'/download/')) or soup.find('a', class_=re.compile(r'download__btn|downloadBTn'))
        
        if not download_anchor:
            return False, "❌ لم يتم العثور على رابط التحميل", None

        # جلب صفحة الجودات
        quality_page_url = download_anchor.get('href')
        if quality_page_url.startswith('/'):
            quality_page_url = extract_base_url(arabseed_url) + quality_page_url
        
        try:
            qresp = session.get(quality_page_url, headers={'Referer': extract_base_url(arabseed_url)}, timeout=12)
            if qresp.status_code != 200:
                return False, "❌ خطأ في جلب صفحة الجودات", None
        except Exception as e:
            logger.error(f"Error loading quality page: {e}")
            return False, "❌ خطأ في الاتصال بصفحة الجودات", None

        # استخراج روابط السيرفرات
        qsoup = BeautifulSoup(qresp.text, 'html.parser')
        server_links = qsoup.find_all('a', href=re.compile(r'/l/'))
        if not server_links:
            server_links = qsoup.select('ul.downloads__links__list a') or qsoup.find_all('a', class_=re.compile(r'download__item|arabseed'))

        if not server_links:
            return False, "❌ لا توجد روابط تحميل متاحة", None

        # معالجة كل سيرفر
        buttons_data = []
        referer = extract_base_url(quality_page_url) + "/"
        seen_qualities = set()

        for a in server_links:
            href = a.get('href')
            if not href:
                continue
                
            # تخطي الروابط غير المباشرة
            if 'arabseed' not in href and 'عرب سيد' not in a.get_text(" ", strip=True):
                continue

            # تحديد الجودة
            quality = "Unknown"
            parent_with_quality = a.find_parent(attrs={"data-quality": True})
            if parent_with_quality:
                quality = parent_with_quality.get('data-quality')
            else:
                ptxt = a.get_text(" ", strip=True)
                qmatch = re.search(r'(\d{3,4}p)', ptxt)
                if qmatch:
                    quality = qmatch.group(1)
                else:
                    sq = a.find_previous('div', class_=re.compile(r'txt|text'))
                    if sq:
                        qmatch = re.search(r'(\d{3,4}p)', sq.get_text())
                        if qmatch:
                            quality = qmatch.group(1)

            if quality in seen_qualities:
                continue
            seen_qualities.add(quality)

            # استخراج معلومات التحميل
            logger.info(f"Processing server link ({quality}): {href}")
            info = get_download_info(href, referer)
            
            if info and info.get('direct_link'):
                btn_text = f"[ {info.get('file_size','?')} ]  •  {quality}"
                buttons_data.append([{"text": btn_text, "url": info['direct_link']}])
                logger.info(f"Added Quality: {quality} ({info.get('file_size')})")

        if not buttons_data:
            return False, "❌ لم أتمكن من استخراج روابط التحميل", None

        # استخراج العنوان
        media_title = extract_title_from_url(arabseed_url)
        return True, media_title, buttons_data

    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        return False, f"❌ حدث خطأ غير متوقع: {str(e)}", None

# ----------------- دوال Telegram -----------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """أمر /start"""
    user = update.effective_user
    welcome_text = f"""
🎬 *مرحباً {user.first_name}!*

🤖 *بوت تحميل عرب سيد المباشر*

🔗 *كيفية الاستخدام:*
1. أرسل رابط حلقة من موقع عرب سيد
2. انتظر حتى تتم معالجة الرابط
3. اختر جودة التحميل من الأزرار

📌 *مثال للرابط:*
`https://arabseed.cam/مسلسل-العنكبوت-الحلقة-1.html`

🎯 *مميزات البوت:*
• تحميل مباشر بجودات متعددة
• دعم الروابط المختصرة
• واجهة سهلة الاستخدام

⚡ *يعمل 24/7 دون توقف*
    """
    
    keyboard = [
        [InlineKeyboardButton("🎬 إرسال رابط", switch_inline_query_current_chat="")],
        [InlineKeyboardButton("📢 قناة الدعم", url="https://t.me/arabseed_support")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(welcome_text, reply_markup=reply_markup, parse_mode='Markdown')

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """أمر /help"""
    help_text = """
📖 *تعليمات استخدام البوت:*

1. 🔍 اذهب إلى موقع عرب سيد
2. 📋 انسخ رابط الحلقة المطلوبة
3. 📩 أرسل الرابط هنا في البوت
4. ⏳ انتظر قليلاً حتى تتم المعالجة
5. 📥 اختر جودة التحميل المناسبة

⚠️ *ملاحظات مهمة:*
• البوت لا يخزن أي ملفات على سيرفراته
• الجودة تعتمد على المصدر الأصلي
• بعض الحلقات القديمة قد لا تعمل

🔄 *في حالة وجود مشكلة:*
• تأكد من صحة الرابط
• حاول مرة أخرى بعد قليل
• تواصل مع الدعم عبر @arabseed_support
    """
    
    await update.message.reply_text(help_text, parse_mode='Markdown')

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة الرسائل"""
    user_id = update.effective_user.id
    session = get_user_session(user_id)
    
    if session.processing:
        await update.message.reply_text("⏳ جاري معالجة طلبك السابق، انتظر قليلاً...")
        return
    
    session.processing = True
    url = update.message.text.strip()
    
    try:
        # إرسال رسالة الانتظار
        wait_msg = await update.message.reply_text("⏳ جاري معالجة الرابط، يرجى الانتظار...")
        
        # إنشاء جلسة طلبات
        req_session = requests.Session()
        req_session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        
        # معالجة الرابط
        success, title_or_msg, buttons_data = process_single_episode(url, req_session)
        
        if success:
            # حذف رسالة الانتظار
            await wait_msg.delete()
            
            # تحويل البيانات إلى أزرار Telegram
            keyboard = []
            for button_row in buttons_data:
                row = []
                for button in button_row:
                    row.append(InlineKeyboardButton(button["text"], url=button["url"]))
                keyboard.append(row)
            
            # إضافة أزرار إضافية
            keyboard.append([
                InlineKeyboardButton("🔄 معالجة رابط آخر", callback_data="new_link"),
                InlineKeyboardButton("📢 قناة الدعم", url="https://t.me/arabseed_support")
            ])
            
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            # إرسال الرسالة مع الأزرار
            message_text = f"""
🎬 *{title_or_msg}*

📥 *روابط التحميل المتاحة:*
اختر الجودة المناسبة من الأزرار أدناه.

🔔 *ملاحظة:* الروابط مباشرة من سيرفرات عرب سيد
            """
            
            await update.message.reply_text(
                message_text,
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
            
            # حفظ بيانات الجلسة
            session.last_url = url
            session.last_title = title_or_msg
            
            # التحقق مما إذا كان رابط مسلسل
            if 'مسلسل' in unquote(urlparse(url).path) or 'الحلقة' in unquote(urlparse(url).path):
                current_num, builder = extract_episode_and_base(url)
                if current_num is not None and builder is not None:
                    session.current_episode = current_num + 1  # الحلقة التالية
                    session.builder_func = builder
                    
                    # سؤال المستخدم عن وضع التلقائي
                    auto_keyboard = [
                        [
                            InlineKeyboardButton("✅ نعم", callback_data="auto_yes"),
                            InlineKeyboardButton("❌ لا", callback_data="auto_no")
                        ]
                    ]
                    auto_markup = InlineKeyboardMarkup(auto_keyboard)
                    
                    await update.message.reply_text(
                        f"🎯 *تم اكتشاف مسلسل*\n\nهل تريد تفعيل الوضع التلقائي لتحميل الحلقات التالية تلقائياً؟",
                        reply_markup=auto_markup,
                        parse_mode='Markdown'
                    )
        
        else:
            await wait_msg.delete()
            await update.message.reply_text(f"{title_or_msg}\n\n⚠️ تأكد من صحة الرابط وحاول مرة أخرى.")
            
    except Exception as e:
        logger.error(f"Error in handle_message: {e}\n{traceback.format_exc()}")
        await update.message.reply_text("❌ حدث خطأ غير متوقع، يرجى المحاولة مرة أخرى.")
        
    finally:
        session.processing = False

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة ضغطات الأزرار"""
    query = update.callback_query
    await query.answer()
    
    user_id = update.effective_user.id
    session = get_user_session(user_id)
    
    if query.data == "new_link":
        await query.edit_message_text("🔄 أرسل رابط الحلقة الجديدة...")
        
    elif query.data == "auto_yes":
        session.auto_mode = True
        await query.edit_message_text("✅ *تم تفعيل الوضع التلقائي*\n\nجاري تحميل الحلقات التالية تلقائياً...", parse_mode='Markdown')
        
        # بدء التحميل التلقائي
        await auto_process_episodes(update, context, user_id)
        
    elif query.data == "auto_no":
        session.auto_mode = False
        await query.edit_message_text("❌ *تم إلغاء الوضع التلقائي*\n\nيمكنك إرسال رابط جديد عندما تريد.", parse_mode='Markdown')

async def auto_process_episodes(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    """معالجة الحلقات تلقائياً"""
    session = get_user_session(user_id)
    
    if not session.auto_mode:
        return
    
    max_episodes = 10  # أقصى عدد حلقات لتجنب التحميل الزائد
    
    for i in range(max_episodes):
        if not session.auto_mode:
            break
            
        if session.builder_func is None:
            break
            
        episode_url = session.builder_func(session.current_episode)
        if not episode_url:
            break
        
        try:
            # إرسال رسالة تتبع
            status_msg = await context.bot.send_message(
                chat_id=user_id,
                text=f"⏳ جاري معالجة الحلقة {session.current_episode}..."
            )
            
            # معالجة الحلقة
            req_session = requests.Session()
            req_session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
            
            success, title_or_msg, buttons_data = process_single_episode(episode_url, req_session)
            
            if success:
                # تحويل البيانات إلى أزرار
                keyboard = []
                for button_row in buttons_data:
                    row = []
                    for button in button_row:
                        row.append(InlineKeyboardButton(button["text"], url=button["url"]))
                    keyboard.append(row)
                
                keyboard.append([
                    InlineKeyboardButton("⏹ إيقاف التلقائي", callback_data="stop_auto")
                ])
                
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                # إرسال النتيجة
                await context.bot.send_message(
                    chat_id=user_id,
                    text=f"🎬 *الحلقة {session.current_episode} - {title_or_msg}*\n\n📥 روابط التحميل:",
                    reply_markup=reply_markup,
                    parse_mode='Markdown'
                )
                
                # الانتقال للحلقة التالية
                session.current_episode += 1
                
                # تأخير قصير بين الحلقات
                await asyncio.sleep(2)
                
            else:
                # إذا فشلت الحلقة، توقف التلقائي
                await context.bot.send_message(
                    chat_id=user_id,
                    text=f"⚠️ *تم إيقاف الوضع التلقائي*\n\n{title_or_msg}",
                    parse_mode='Markdown'
                )
                session.auto_mode = False
                break
                
            # حذف رسالة التتبع
            await status_msg.delete()
            
        except Exception as e:
            logger.error(f"Error in auto processing: {e}")
            await context.bot.send_message(
                chat_id=user_id,
                text=f"❌ حدث خطأ في معالجة الحلقة {session.current_episode}"
            )
            session.auto_mode = False
            break

async def stop_auto_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """إيقاف الوضع التلقائي"""
    user_id = update.effective_user.id
    session = get_user_session(user_id)
    session.auto_mode = False
    await update.message.reply_text("⏹ *تم إيقاف الوضع التلقائي*", parse_mode='Markdown')

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالج الأخطاء"""
    logger.error(f"Update {update} caused error {context.error}")
    
    if update and update.effective_message:
        try:
            await update.effective_message.reply_text("❌ حدث خطأ غير متوقع، يرجى المحاولة مرة أخرى.")
        except:
            pass

# ----------------- التشغيل الرئيسي -----------------
def main():
    """الدالة الرئيسية"""
    print(f"{Colors.GREEN}🎬 بدء تشغيل بوت عرب سيد Telegram...{Colors.RESET}")
    print(f"{Colors.CYAN}Token: {TOKEN[:10]}...{Colors.RESET}")
    
    try:
        # إنشاء التطبيق
        application = Application.builder().token(TOKEN).build()
        
        # إضافة المعالجات
        application.add_handler(CommandHandler("start", start_command))
        application.add_handler(CommandHandler("help", help_command))
        application.add_handler(CommandHandler("stop", stop_auto_command))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        application.add_handler(CallbackQueryHandler(handle_callback))
        
        # إضافة معالج الأخطاء
        application.add_error_handler(error_handler)
        
        # بدء البوت
        print(f"{Colors.GREEN}🤖 البوت يعمل الآن!{Colors.RESET}")
        print(f"{Colors.YELLOW}اضغط Ctrl+C لإيقاف البوت{Colors.RESET}")
        
        application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
        
    except Exception as e:
        logger.error(f"Failed to start bot: {e}")
        print(f"{Colors.RED}❌ فشل تشغيل البوت: {e}{Colors.RESET}")

if __name__ == "__main__":
    # إضافة asyncio للتشغيل التلقائي
    import asyncio
    main()