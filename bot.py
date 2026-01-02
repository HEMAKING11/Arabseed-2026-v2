# arabseed_telegram_bot_final.py
import os
import re
import sys
import json
import time
import random
import logging
import traceback
from urllib.parse import urlparse, unquote, urlunparse, quote, parse_qs
from typing import Dict, List, Optional, Tuple, Any
from datetime import datetime

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
)

# ----------------- إعدادات التسجيل -----------------
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ----------------- إعدادات البوت -----------------
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "7064549403:AAHWQsrZPekW1M9kHacqB6N19aMj_xjspf4")

# ----------------- قائمة User-Agents -----------------
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Mobile/15E148 Safari/604.1",
]

# ----------------- إدارة الجلسات -----------------
class UserSession:
    def __init__(self):
        self.processing = False
        self.auto_mode = False
        self.current_episode = 0
        self.builder_func = None
        self.last_url = ""
        self.last_title = ""
        self.history = []
        
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

# ----------------- دوال المساعدة المحسنة -----------------
def extract_base_url(url: str) -> str:
    """استخراج الرابط الأساسي"""
    parsed_url = urlparse(url)
    return f"{parsed_url.scheme}://{parsed_url.netloc}"

def extract_title_from_url(url: str) -> str:
    """استخراج العنوان من الرابط"""
    try:
        parsed_url = urlparse(url)
        path = unquote(parsed_url.path)
        path_parts = path.strip('/').split('-')
        title = ' '.join(path_parts).replace('.html', '').replace('.php', '').title()
        
        if title.startswith("مسلسل"):
            words = title.split()
            new_title = []
            for word in words:
                new_title.append(word)
                if any(char.isdigit() for char in word):
                    break
            title = ' '.join(new_title)
        return title
    except:
        return "عنوان غير معروف"

def get_random_headers() -> Dict:
    """الحصول على هيدرات عشوائية"""
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "ar,en-US;q=0.7,en;q=0.3",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Cache-Control": "max-age=0",
        "TE": "Trailers",
    }

def make_request(url: str, max_retries: int = 3, session: Optional[requests.Session] = None) -> Optional[requests.Response]:
    """طلب محسن مع إعادة محاولة"""
    headers = get_random_headers()
    
    for attempt in range(max_retries):
        try:
            if session:
                response = session.get(
                    url,
                    headers=headers,
                    timeout=20,
                    allow_redirects=True,
                    verify=False  # إيقاف التحقق من SSL مؤقتاً
                )
            else:
                response = requests.get(
                    url,
                    headers=headers,
                    timeout=20,
                    allow_redirects=True,
                    verify=False
                )
            
            if response.status_code == 200:
                return response
            elif response.status_code == 403:
                logger.warning(f"403 Forbidden on attempt {attempt + 1}")
                time.sleep(2 ** attempt)  # زيادة وقت الانتظار تدريجياً
                headers = get_random_headers()  # تغيير الهيدرات
            else:
                logger.warning(f"Status {response.status_code} on attempt {attempt + 1}")
                time.sleep(1)
                
        except requests.exceptions.RequestException as e:
            logger.error(f"Request error on attempt {attempt + 1}: {e}")
            time.sleep(2 ** attempt)
    
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

# ----------------- دالة استخراج معلومات التحميل المحسنة -----------------
def get_download_info(server_href: str, referer: str) -> Optional[Dict]:
    """استخراج معلومات التحميل من رابط السيرفر"""
    try:
        session = requests.Session()
        headers = get_random_headers()
        headers["Referer"] = referer
        session.headers.update(headers)
        
        logger.info(f"🔍 جاري معالجة: {server_href}")
        
        # الخطوة 1: تتبع إعادة التوجيه
        try:
            response = session.get(server_href, timeout=15, allow_redirects=False)
            if response.status_code in [301, 302, 303, 307, 308] and 'location' in response.headers:
                redirected_url = response.headers['location']
                if not redirected_url.startswith('http'):
                    base = extract_base_url(server_href)
                    redirected_url = base + redirected_url
                logger.info(f"↪️ تم التوجيه إلى: {redirected_url}")
                server_href = redirected_url
        except:
            pass
        
        # الخطوة 2: الحصول على الصفحة الرئيسية
        response = make_request(server_href, session=session)
        if not response:
            return None
        
        html_content = response.text
        
        # البحث عن رابط ?r= أو downloadz
        r_link = None
        patterns = [
            r'(https?://[^"\'>\s]+/category/downloadz/\?r=\d+[^"\'>\s]*)',
            r'(https?://[^"\'>\s]+\?r=\d+[^"\'>\s]*)',
            r'href=["\']([^"\']+downloadz[^"\']*)["\']',
            r'window\.location\s*=\s*["\']([^"\']+)["\']',
        ]
        
        for pattern in patterns:
            matches = re.findall(pattern, html_content, re.IGNORECASE)
            if matches:
                r_link = matches[0]
                if r_link.startswith('//'):
                    r_link = 'https:' + r_link
                elif not r_link.startswith('http'):
                    r_link = extract_base_url(server_href) + r_link
                break
        
        if not r_link:
            # إذا لم نجد، استخدم الرابط الحالي
            r_link = response.url
        
        logger.info(f"✅ وجدت رابط التحميل: {r_link}")
        
        # الخطوة 3: جلب صفحة التحميل
        time.sleep(0.5)  # تأخير بسيط
        response = make_request(r_link, session=session)
        if not response:
            return None
        
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # البحث عن رابط التحميل النهائي
        final_link = None
        
        # البحث في جميع الروابط
        for a in soup.find_all('a', href=True):
            href = a['href']
            # البحث عن روابط MP4 أو direct
            if re.search(r'\.(mp4|m3u8|mkv|avi)$', href, re.IGNORECASE) or 'direct' in href.lower() or 'download' in href.lower():
                final_link = href
                if not final_link.startswith('http'):
                    final_link = extract_base_url(r_link) + final_link
                break
        
        # إذا لم نجد، نبحث في النصوص البرمجية
        if not final_link:
            script_tags = soup.find_all('script')
            for script in script_tags:
                if script.string:
                    # البحث في JavaScript
                    patterns = [
                        r'src=["\']([^"\']+\.mp4[^"\']*)["\']',
                        r'file["\']?\s*:\s*["\']([^"\']+\.mp4[^"\']*)["\']',
                        r'url["\']?\s*:\s*["\']([^"\']+\.mp4[^"\']*)["\']',
                        r'["\']?(?:file|url|src)["\']?\s*:\s*["\']([^"\']+)["\']',
                    ]
                    for pattern in patterns:
                        match = re.search(pattern, script.string, re.IGNORECASE)
                        if match:
                            final_link = match.group(1)
                            if not final_link.startswith('http'):
                                final_link = extract_base_url(r_link) + final_link
                            break
                if final_link:
                    break
        
        # إذا لم نجد بعد، نستخدم بعض الاستراتيجيات البديلة
        if not final_link:
            # محاولة استخراج من iframe
            iframe = soup.find('iframe', src=True)
            if iframe:
                final_link = iframe['src']
                if not final_link.startswith('http'):
                    final_link = extract_base_url(r_link) + final_link
        
        if not final_link:
            logger.error("❌ لم أتمكن من استخراج رابط التحميل")
            return None
        
        logger.info(f"🎯 رابط التحميل النهائي: {final_link}")
        
        # استخراج معلومات الملف
        file_name = None
        file_size = None
        
        # البحث عن العنوان والحجم
        title_elem = soup.find(['h1', 'h2', 'h3', 'title'])
        if title_elem:
            title_text = title_elem.get_text(strip=True)
            # استخراج حجم الملف من النص
            size_match = re.search(r'(\d+(?:\.\d+)?)\s*(MB|GB|KB)', title_text, re.IGNORECASE)
            if size_match:
                file_size = f"{size_match.group(1)} {size_match.group(2).upper()}"
            
            # استخدام العنوان كاسم ملف
            file_name = title_text[:50]  # تقليل الطول
        
        # إذا لم نجد اسماً، نستخدم اسم الملف من الرابط
        if not file_name:
            file_name = os.path.basename(final_link).split('?')[0] or "ملف_تحميل"
        
        # إذا لم نجد حجماً، نستخدم قيمة افتراضية
        if not file_size:
            file_size = "غير معروف"
        
        # تنظيف الرابط
        final_link = final_link.replace(" ", "%20")
        
        return {
            'direct_link': final_link,
            'file_name': file_name,
            'file_size': file_size
        }
        
    except Exception as e:
        logger.error(f"❌ خطأ في استخراج معلومات التحميل: {e}")
        return None

# ----------------- دالة المعالجة الرئيسية -----------------
def process_arabseed_url(url: str) -> Tuple[bool, str, List[List[Dict]]]:
    """معالجة رابط عرب سيد"""
    session = requests.Session()
    headers = get_random_headers()
    session.headers.update(headers)
    
    try:
        logger.info(f"🚀 بدء معالجة الرابط: {url}")
        
        # الخطوة 1: جلب صفحة الحلقة
        response = make_request(url, session=session)
        if not response:
            return False, "❌ تعذر الوصول إلى الرابط، تأكد من صحته", []
        
        # التحقق من أن الصفحة موجودة
        if response.status_code != 200:
            return False, f"❌ خطأ في جلب الصفحة (رمز: {response.status_code})", []
        
        html_content = response.text
        soup = BeautifulSoup(html_content, 'html.parser')
        
        # التحقق من وجود الحلقة
        error_indicators = [
            'لم يتم العثور',
            'صفحة غير موجودة',
            'not found',
            '404',
            'error',
            'عذراً'
        ]
        
        page_text = soup.get_text().lower()
        if any(indicator in page_text for indicator in error_indicators):
            return False, "❌ الحلقة غير موجودة أو الرابط غير صحيح", []
        
        # الخطوة 2: البحث عن روابط التحميل
        download_links = []
        
        # البحث في الروابط
        for a in soup.find_all('a', href=True):
            href = a['href'].lower()
            if any(keyword in href for keyword in ['download', 'تحميل', 'server', 'سيرفر', 'جودة', 'quality']):
                download_links.append(a['href'])
        
        # إذا لم نجد، نبحث في الأزرار
        if not download_links:
            buttons = soup.find_all(['button', 'a'], text=re.compile(r'تحميل|تنزيل|download', re.IGNORECASE))
            for btn in buttons:
                if btn.get('onclick'):
                    # استخراج الرابط من onclick
                    match = re.search(r"location\.href=['\"]([^'\"]+)['\"]", btn.get('onclick', ''))
                    if match:
                        download_links.append(match.group(1))
        
        # إذا لم نجد بعد، نستخدم بعض الروابط الشائعة
        if not download_links:
            # البحث عن أي رابط يحتوي على /download/
            for a in soup.find_all('a', href=re.compile(r'/download/', re.IGNORECASE)):
                download_links.append(a['href'])
        
        if not download_links:
            return False, "❌ لم أتمكن من العثور على روابط التحميل في الصفحة", []
        
        logger.info(f"🔗 وجدت {len(download_links)} روابط تحميل")
        
        # الخطوة 3: معالجة كل رابط
        buttons_data = []
        base_url = extract_base_url(url)
        
        for i, link in enumerate(download_links[:5]):  # نأخذ أول 5 روابط فقط
            if not link.startswith('http'):
                link = base_url + link
            
            logger.info(f"⚙️ معالجة الرابط {i+1}: {link}")
            
            # استخراج معلومات التحميل
            info = get_download_info(link, base_url + "/")
            
            if info and info.get('direct_link'):
                # تحديد الجودة
                quality = "جودة عالية"
                if '360' in link or '360' in info['file_name']:
                    quality = "360p"
                elif '480' in link or '480' in info['file_name']:
                    quality = "480p"
                elif '720' in link or '720' in info['file_name']:
                    quality = "720p"
                elif '1080' in link or '1080' in info['file_name']:
                    quality = "1080p"
                
                # إنشاء زر
                btn_text = f"📥 {quality} - {info['file_size']}"
                buttons_data.append([{"text": btn_text, "url": info['direct_link']}])
                
                logger.info(f"✅ تم إضافة {quality}")
            
            # تأخير بسيط بين الطلبات
            time.sleep(0.3)
        
        if not buttons_data:
            return False, "❌ لم أتمكن من استخراج روابط تحميل صالحة", []
        
        # استخراج العنوان
        title = extract_title_from_url(url)
        
        logger.info(f"🎉 تمت المعالجة بنجاح: {title}")
        return True, title, buttons_data
        
    except Exception as e:
        logger.error(f"❌ خطأ في معالجة الرابط: {e}")
        return False, f"❌ حدث خطأ غير متوقع: {str(e)}", []

# ----------------- دوال Telegram -----------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """أمر /start"""
    user = update.effective_user
    welcome_text = f"""
🎬 *مرحباً {user.first_name}!* 

🤖 *بوت تحميل عرب سيد المباشر*

🔗 *كيفية الاستخدام:*
1. أرسل رابط حلقة من موقع عرب سيد
2. انتظر قليلاً حتى تتم المعالجة
3. اختر جودة التحميل من الأزرار

📌 *مثال للرابط:*
`https://arabseed.top/مسلسل-العنكبوت-الحلقة-1`
أو
`https://arabseed.cam/مسلسل-العنكبوت-الحلقة-1.html`

🎯 *مميزات البوت:*
• تحميل مباشر بجودات متعددة
• دعم جميع روابط عرب سيد
• واجهة سهلة الاستخدام
• يعمل 24/7

⚡ *للبدء:* أرسل رابط الحلقة الآن!
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

1. 🔍 اذهب إلى موقع عرب سيد (arabseed.top أو arabseed.cam)
2. 📋 انسخ رابط الحلقة المطلوبة
3. 📩 أرسل الرابط هنا في البوت
4. ⏳ انتظر قليلاً (10-20 ثانية)
5. 📥 اختر جودة التحميل المناسبة

⚠️ *ملاحظات مهمة:*
• البوت لا يخزن أي ملفات
• الجودة تعتمد على المصدر الأصلي
• قد لا تعمل بعض الحلقات القديمة

🔄 *في حالة وجود مشكلة:*
1. تأكد من صحة الرابط
2. حاول مرة أخرى بعد قليل
3. تأكد أن الحلقة موجودة على الموقع
4. تواصل مع الدعم @arabseed_support

🎬 *مواقع مدعومة:*
• arabseed.top
• arabseed.cam
• arabseed.ink
• وأي موقع عرب سيد آخر
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
        # التحقق من أن الرابط صالح
        if not url.startswith(('http://', 'https://')):
            await update.message.reply_text("❌ هذا ليس رابطاً صالحاً! يرجى إرسال رابط يبدأ بـ http:// أو https://")
            session.processing = False
            return
        
        # التحقق من أن الرابط يحتوي على arabseed
        if 'arabseed' not in url.lower():
            await update.message.reply_text("⚠️ يبدو أن هذا الرابط ليس من موقع عرب سيد. تأكد من الرابط وحاول مرة أخرى.")
            session.processing = False
            return
        
        # إرسال رسالة الانتظار
        wait_msg = await update.message.reply_text("⏳ جاري معالجة الرابط، يرجى الانتظار 10-20 ثانية...")
        
        # معالجة الرابط
        success, title_or_msg, buttons_data = process_arabseed_url(url)
        
        if success:
            # حذف رسالة الانتظار
            await wait_msg.delete()
            
            # تحويل البيانات إلى أزرار Telegram
            keyboard = []
            for button_row in buttons_data:
                row = []
                for button in button_row:
                    # تنظيف نص الزر
                    clean_text = button["text"].replace("[", "").replace("]", "").strip()
                    row.append(InlineKeyboardButton(clean_text, url=button["url"]))
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
            
            # حفظ في التاريخ
            session.history.append({
                'url': url,
                'title': title_or_msg,
                'time': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            })
            
        else:
            await wait_msg.delete()
            
            # رسالة خطأ أكثر وصفية
            error_text = f"""
{title_or_msg}

🔍 *نصائح لحل المشكلة:*
1. تأكد من أن الرابط يعمل في متصفحك
2. تحقق من أن الحلقة موجودة على الموقع
3. حاول استخدام رابط مختلف لنفس الحلقة
4. قد يكون الموقع معطل مؤقتاً

🔄 جرب رابطاً آخر من موقع عرب سيد
            """
            
            keyboard = [
                [InlineKeyboardButton("🔄 محاولة برابط آخر", callback_data="new_link")],
                [InlineKeyboardButton("📢 قناة الدعم", url="https://t.me/arabseed_support")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(error_text, reply_markup=reply_markup, parse_mode='Markdown')
            
    except Exception as e:
        logger.error(f"❌ خطأ في handle_message: {e}\n{traceback.format_exc()}")
        await update.message.reply_text("❌ حدث خطأ غير متوقع، يرجى المحاولة مرة أخرى.")
        
    finally:
        session.processing = False

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة ضغطات الأزرار"""
    query = update.callback_query
    await query.answer()
    
    if query.data == "new_link":
        await query.edit_message_text("🔄 *أرسل رابط الحلقة الجديدة...*\n\nتأكد من أن الرابط من موقع عرب سيد ويبدأ بـ https://", parse_mode='Markdown')

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """فحص حالة البوت"""
    status_text = """
✅ *البوت يعمل بشكل طبيعي*

🤖 *معلومات البوت:*
• الحالة: 🟢 نشط
• المستخدمين: {}
• يعمل منذ: {}

⚡ *آخر تحديث:* {}
    """.format(
        len(user_sessions),
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        datetime.now().strftime("%H:%M:%S")
    )
    
    await update.message.reply_text(status_text, parse_mode='Markdown')

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالج الأخطاء"""
    logger.error(f"❌ خطأ: {context.error}")
    
    try:
        if update and update.effective_message:
            await update.effective_message.reply_text("❌ حدث خطأ، جاري إعادة المحاولة...")
    except:
        pass

# ----------------- التشغيل الرئيسي -----------------
def main():
    """الدالة الرئيسية"""
    print("=" * 50)
    print("🎬 بدء تشغيل بوت عرب سيد Telegram")
    print("=" * 50)
    
    try:
        # إنشاء التطبيق
        application = Application.builder().token(TOKEN).build()
        
        # إضافة المعالجات
        application.add_handler(CommandHandler("start", start_command))
        application.add_handler(CommandHandler("help", help_command))
        application.add_handler(CommandHandler("status", status_command))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        application.add_handler(CallbackQueryHandler(handle_callback))
        
        # إضافة معالج الأخطاء
        application.add_error_handler(error_handler)
        
        # بدء البوت
        print("✅ البوت جاهز للعمل!")
        print("📱 افتح Telegram وابحث عن البوت")
        print("⚡ أرسل /start للبدء")
        
        application.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
            close_loop=False
        )
        
    except Exception as e:
        print(f"❌ فشل تشغيل البوت: {e}")
        logger.error(f"فشل تشغيل البوت: {e}\n{traceback.format_exc()}")

if __name__ == "__main__":
    main()
