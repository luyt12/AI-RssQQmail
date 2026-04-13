import os
import ssl
import smtplib
import feedparser
import xml.etree.ElementTree as ET
import re
import html
import ssl as ssl_module
from datetime import datetime, timezone, timedelta
from dateutil import parser as dtparser
from urllib.request import urlopen, Request
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.header import Header

import argostranslate.package
import argostranslate.translate


OPML_PATH = "feeds.opml"

FEED_TIMEOUT_SECONDS = 15
PER_FEED_LIMIT = 10
LOOKBACK_HOURS = 24

# 翻译缓存
_translate_cache = {}


def ensure_argos_en_zh_installed():
    try:
        argostranslate.translate.get_translation_from_codes("en", "zh")
        return
    except Exception:
        pass
    print("[INFO] 安装 Argos 离线翻译模型（en→zh），首次运行会下载，请稍等…")
    argostranslate.package.update_package_index()
    available = argostranslate.package.get_available_packages()
    pkg = next((p for p in available if p.from_code == "en" and p.to_code == "zh"), None)
    if not pkg:
        raise RuntimeError("未找到 Argos en→zh 翻译模型")
    argostranslate.package.install_from_path(pkg.download())
    print("[INFO] Argos 翻译模型安装完成")


def translate_en_to_zh(text: str) -> str:
    """英译中，带缓存。"""
    text = (text or "").strip()
    if not text:
        return text
    if text in _translate_cache:
        return _translate_cache[text]
    try:
        zh = argostranslate.translate.get_translation_from_codes("en", "zh").translate(text)
    except Exception:
        zh = text
    _translate_cache[text] = zh
    return zh


def _escape(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def strip_html(text: str) -> str:
    """去除HTML标签，保留纯文本"""
    text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = html.unescape(text)
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n\s*\n', '\n\n', text).strip()
    return text


def truncate_text(text: str, max_len: int = 500) -> str:
    """截断文本到指定长度，在句子边界处截断"""
    if len(text) <= max_len:
        return text
    cut = text[:max_len]
    last_punct = max(cut.rfind('。'), cut.rfind('！'), cut.rfind('？'),
                     cut.rfind('；'), cut.rfind('，'), cut.rfind('.'))
    if last_punct > max_len * 0.6:
        return cut[:last_punct + 1]
    return cut + "…"


def get_entry_summary(entry) -> str:
    """
    从 RSS entry 获取摘要/描述，不抓取全文。
    优先使用 summary，其次是 description，都没有则返回空。
    """
    for field in ['summary', 'description', 'content', 'value']:
        text = entry.get(field)
        if text:
            if isinstance(text, list) and len(text) > 0:
                text = text[0].get('value', '')
            if isinstance(text, dict):
                text = text.get('value', '')
            text = strip_html(text)
            if len(text) > 50:
                return truncate_text(text, 800)
    return ""


def load_feeds_from_opml_file(opml_path: str) -> list[str]:
    with open(opml_path, "r", encoding="utf-8") as f:
        content = f.read().strip()
    root = ET.fromstring(content)
    seen = set()
    out = []
    for node in root.findall(".//outline"):
        xml_url = node.attrib.get("xmlUrl")
        if xml_url and xml_url.strip() not in seen:
            seen.add(xml_url.strip())
            out.append(xml_url.strip())
    return out


def safe_parse_feed(url: str, timeout: int):
    try:
        req = Request(url, headers={"User-Agent": "rss-mailer/1.0"})
        with urlopen(req, timeout=timeout) as r:
            data = r.read()
        parsed = feedparser.parse(data)
        if getattr(parsed, "bozo", 0):
            ex = getattr(parsed, "bozo_exception", None)
            if ex:
                return parsed, f"bozo_exception: {type(ex).__name__}: {ex}"
        return parsed, None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def entry_time_utc(entry) -> datetime | None:
    for k in ("published", "updated"):
        v = entry.get(k)
        if not v:
            continue
        try:
            dt = dtparser.parse(v)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            pass
    return None


def fetch_recent_items(feed_urls: list[str], since_utc: datetime, per_feed_limit: int):
    items = []
    failures = []

    for url in feed_urls:
        parsed, err = safe_parse_feed(url, timeout=FEED_TIMEOUT_SECONDS)
        if parsed is None:
            print(f"[SKIP] {url} -> {err}")
            failures.append((url, err))
            continue
        if err:
            print(f"[WARN] {url} -> {err}")

        feed_title = getattr(parsed.feed, "title", url) if hasattr(parsed, "feed") else url
        entries = getattr(parsed, "entries", [])[:per_feed_limit]

        for e in entries:
            t = entry_time_utc(e)
            if not t:
                continue
            if t < since_utc:
                continue

            # 获取 RSS 自带的摘要并翻译
            summary_en = get_entry_summary(e)
            summary_zh = translate_en_to_zh(summary_en) if summary_en else ""

            items.append({
                "feed": str(feed_title),
                "title": e.get("title", "无标题"),
                "link": e.get("link", ""),
                "time": t.isoformat(),
                "summary_en": summary_en,
                "summary_zh": summary_zh,
            })

    return items, failures


def build_html(items, failures):
    ensure_argos_en_zh_installed()

    parts = []
    if not items:
        parts.append(f"<p>过去 {LOOKBACK_HOURS} 小时没有抓到新的 RSS 条目。</p>")
    else:
        by_feed = {}
        for it in items:
            by_feed.setdefault(it["feed"], []).append(it)

        parts.append(
            f"<p>每日 RSS 摘要（过去 {LOOKBACK_HOURS} 小时，共 {len(items)} 条）</p>"
        )
        for feed, lst in by_feed.items():
            parts.append(f"<h3>{_escape(str(feed))}</h3><ul>")
            for it in lst:
                title_html = _escape(it["title"])
                link = it["link"]
                time_s = _escape(it["time"][:10])
                
                # 显示英文原文 + 中文翻译
                summary_html = ""
                if it.get("summary_en"):
                    en = _escape(it["summary_en"])
                    zh = _escape(it.get("summary_zh", ""))
                    summary_html = f'<br/><small style="color:#333">{zh}</small>'
                    if zh != en:
                        summary_html += f'<br/><small style="color:#888;font-style:italic">[{en}]</small>'
                
                parts.append(
                    f'<li><a href="{_escape(link)}">{title_html}</a> '
                    f'<small>({time_s})</small>{summary_html}</li>'
                )
            parts.append("</ul>")

    if failures:
        parts.append(f"<hr/><p>抓取失败（已跳过）: {len(failures)} 个</p><ul>")
        for url, reason in failures[:30]:
            parts.append(
                f"<li><code>{_escape(url)}</code><br/>"
                f"<small>{_escape(str(reason))}</small></li>"
            )
        if len(failures) > 30:
            parts.append(f"<li>……省略 {len(failures) - 30} 个</li>")
        parts.append("</ul>")

    return "\n".join(parts)


def send_email(html_body: str):
    smtp_host = os.environ.get("SMTP_HOST")
    smtp_port = int(os.environ.get("SMTP_PORT", "465"))
    email_user = os.environ["EMAIL_USER"]
    email_pass = os.environ["EMAIL_PASS"]
    email_to = os.environ["EMAIL_TO"]
    subject = os.environ.get("EMAIL_SUBJECT", "每日 RSS 摘要")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = email_user
    msg["To"] = email_to
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    context = ssl_module.create_default_context()
    if smtp_port == 465:
        with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=60, context=context) as server:
            server.login(email_user, email_pass)
            server.sendmail(email_user, [email_to], msg.as_string())
    else:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=60) as server:
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
            server.login(email_user, email_pass)
            server.sendmail(email_user, [email_to], msg.as_string())


def main():
    print("[INFO] RSS Mailer started", flush=True)
    
    feed_urls = load_feeds_from_opml_file(OPML_PATH)
    print(f"[INFO] Loaded {len(feed_urls)} feeds", flush=True)
    
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    print(f"[INFO] Looking for articles since {cutoff.isoformat()}", flush=True)
    
    items, failures = fetch_recent_items(feed_urls, cutoff, PER_FEED_LIMIT)
    print(f"[INFO] Found {len(items)} new items, {len(failures)} failures", flush=True)
    
    html = build_html(items, failures)
    send_email(html)
    print("[INFO] Email sent successfully", flush=True)


if __name__ == "__main__":
    main()
