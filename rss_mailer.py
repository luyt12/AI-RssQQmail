import os
import ssl
import smtplib
import feedparser
import xml.etree.ElementTree as ET
import json
import time
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
KIMI_TIMEOUT_SECONDS = 30


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


def zh_en_pair(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return ""
    zh = translate_en_to_zh(s)
    if not zh or zh.strip() == s.strip():
        return _escape(s)
    return f"{_escape(zh)}（{_escape(s)}）"


def _escape(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ── Kimi API 摘要 ───────────────────────────────────────────────
_kimi_cache = {}


def _call_kimi(messages: list[dict], timeout: int = KIMI_TIMEOUT_SECONDS) -> str:
    api_key = os.environ.get("KIMI_API_KEY")
    api_url = os.environ.get("KIMI_API_URL", "https://integrate.api.nvidia.com/v1/chat/completions")
    model = os.environ.get("KIMI_MODEL", "moonshotai/kimi-k2.5")
    if not api_key:
        return ""
    cache_key = json.dumps(messages, sort_keys=True)
    if cache_key in _kimi_cache:
        return _kimi_cache[cache_key]
    payload = json.dumps({
        "model": model,
        "messages": messages,
        "temperature": 0.3,
        "max_tokens": 300,
    }).encode()
    req = Request(api_url,
                   data=payload,
                   headers={"Authorization": f"Bearer {api_key}",
                            "Content-Type": "application/json"},
                   method="POST")
    try:
        with urlopen(req, timeout=timeout) as r:
            resp = json.loads(r.read())
        content = resp["choices"][0]["message"]["content"].strip()
        _kimi_cache[cache_key] = content
        return content
    except Exception as e:
        print(f"[KIMI WARN] {e}")
        return ""


def summarize_title(title: str, link: str = "") -> str:
    """
    用 Kimi 为单条 RSS 条目生成中文摘要（约 50 字）。
    如果 KIMI_API_KEY 未配置则返回空字符串。
    """
    api_key = os.environ.get("KIMI_API_KEY")
    if not api_key:
        return ""
    prompt = (
        f"标题：{title}\n"
        + (f"链接：{link}\n" if link else "")
        + "\n请用中文写一段 30~60 字的摘要，概括这篇文章的核心内容。不要翻译标题。不要超过 60 字。"
    )
    return _call_kimi([
        {"role": "system",
         "content": "你是一个内容摘要助手，用简洁的中文（30~60字）概括文章的核心内容。直接输出摘要，不要加前缀。"},
        {"role": "user", "content": prompt}
    ])


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


def fetch_feed_bytes(url: str, timeout: int) -> bytes:
    req = Request(url, headers={"User-Agent": "rss-mailer/1.0"})
    with urlopen(req, timeout=timeout) as r:
        return r.read()


def safe_parse_feed(url: str, timeout: int):
    try:
        data = fetch_feed_bytes(url, timeout=timeout)
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
            # ← BUG FIX: 如果没有时间字段，跳过（避免旧条目无限重发）
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

        parts.append(f"<p>每日 RSS 摘要（过去 {LOOKBACK_HOURS} 小时，共 {len(items)} 条）</p>")
        for feed, lst in by_feed.items():
            parts.append(f"<h3>{zh_en_pair(feed)}</h3><ul>")
            for it in lst:
                title_html = zh_en_pair(it["title"])
                link = it["link"]
                time_s = _escape(it["time"][:10])  # 只显示日期
                # 尝试 AI 摘要（带缓存）
                summary = summarize_title(it["title"], it["link"])
                summary_html = f'<br/><small>{_escape(summary)}</small>' if summary else ""
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
                f"<small>{zh_en_pair(reason)}</small></li>"
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

    context = ssl.create_default_context()
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
    feeds = load_feeds_from_opml_file(OPML_PATH)
    if not feeds:
        raise RuntimeError("feeds.opml 里没有任何 xmlUrl")
    print(f"[INFO] 共加载 {len(feeds)} 个 RSS 源")

    since = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    items, failures = fetch_recent_items(feeds, since_utc=since, per_feed_limit=PER_FEED_LIMIT)
    print(f"[INFO] 获取到 {len(items)} 条有效条目，{len(failures)} 个失败")

    if items:
        print("[INFO] 正在生成 AI 摘要…（KIMI_API_KEY 配置后启用）")
    html = build_html(items, failures)
    send_email(html)
    print("[INFO] 邮件已发送")


if __name__ == "__main__":
    main()
