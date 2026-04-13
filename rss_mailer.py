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
ARTICLE_TIMEOUT_SECONDS = 20
PER_FEED_LIMIT = 10
LOOKBACK_HOURS = 24
TRANSLATE_SUMMARY_LEN = 300   # 摘要截断字数（中文字符）


# ── Argos 翻译（离线缓存） ──────────────────────────────────────
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


# ── 文章全文抓取 + 纯文本提取 ───────────────────────────────────
def _strip_html_tags(text: str) -> str:
    """去掉 HTML 标签并解码实体字符。"""
    text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = html.unescape(text)
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n\s*\n', '\n\n', text).strip()
    return text


def fetch_article_text(url: str, timeout: int = ARTICLE_TIMEOUT_SECONDS) -> str:
    """
    抓取网页，提取纯文本。失败返回空字符串（不阻断流程）。
    """
    try:
        req = Request(url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; rss-mailer/1.0)"
        })
        with urlopen(req, timeout=timeout) as r:
            raw = r.read()
        # 检测编码
        text = raw.decode("utf-8", errors="replace")
        return _strip_html_tags(text)[:5000]  # 最多取前 5000 字符防过大
    except Exception as e:
        print(f"[WARN] fetch_article_text failed: {url} -> {e}")
        return ""


def summarize_by_translate(title: str, link: str) -> str:
    """
    抓取文章全文，翻译成中文，截取前 TRANSLATE_SUMMARY_LEN 字作为摘要。
    标题保留原文不翻译。
    """
    article = fetch_article_text(link)
    if not article:
        return ""
    zh = translate_en_to_zh(article)
    # 按中文字符截断（避免切断单词）
    if len(zh) <= TRANSLATE_SUMMARY_LEN:
        return zh
    # 找最后一个完整的句子断点
    cut = zh[:TRANSLATE_SUMMARY_LEN]
    last_punct = max(cut.rfind('。'), cut.rfind('！'), cut.rfind('？'),
                     cut.rfind('；'), cut.rfind('，'))
    if last_punct > TRANSLATE_SUMMARY_LEN * 0.6:
        return cut[:last_punct + 1]
    return cut + "…"


# ── RSS 解析 ────────────────────────────────────────────────────
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
            # ← BUG FIX: 没有时间字段的条目跳过，避免旧条目无限重发
            if not t:
                continue
            if t < since_utc:
                continue

            items.append({
                "feed": str(feed_title),
                "title": e.get("title", "无标题"),
                "link": e.get("link", ""),
                "time": t.isoformat(),
            })

    return items, failures


# ── HTML 构建 ───────────────────────────────────────────────────
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
                title_html = _escape(it["title"])  # 标题保留原文
                link = it["link"]
                time_s = _escape(it["time"][:10])   # 只显示日期
                # 全文翻译摘要（标题不翻译）
                summary = summarize_by_translate(it["title"], it["link"])
                summary_html = (
                    f'<br/><small style="color:#555">{_escape(summary)}</small>'
                    if summary else ""
                )
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


# ── 邮件发送 ─────────────────────────────────────────────────────
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
    
    # 加载 feeds
    print(f"[INFO] Loading feeds from {OPML_PATH}", flush=True)
    feed_urls = load_feeds_from_opml_file(OPML_PATH)
    print(f"[INFO] Loaded {len(feed_urls)} feeds", flush=True)
    
    # 计算时间窗口
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    print(f"[INFO] Looking for articles since {cutoff.isoformat()}", flush=True)
    
    # 获取文章
    items, failures = fetch_recent_items(feed_urls, cutoff, PER_FEED_LIMIT)
    print(f"[INFO] Found {len(items)} new items, {len(failures)} failures", flush=True)
    
    # 构建并发送邮件
    html = build_html(items, failures)
    send_email(html)
    print("[INFO] Email sent successfully", flush=True)


if __name__ == "__main__":
    main()
